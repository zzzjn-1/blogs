# -*- coding: utf-8 -*-
"""逐段合成阶段计时插桩（默认关，仅性能诊断用）。

背景（R21）：D10 压测发现合成阶段 **56% 的时间落在「段与段之间」**——从 CosyVoice
打印 `yield speech len` 到下一条 `synthesis text`（下一段开工），中位 5.60 s、
P90 5.62 s，414 段里 401 段挤在 5.50~6.00 s。分布如此紧，说明是**固定耗时被逐段
累加**，而不是算力或锁争用。而 D3/D5 时代同一条路径只隔 ~0.05 s，所以这是**回归**。

**结论（已定位）**：那 5.6 s 是 `TaskRunner._on_synth_progress` 的 SQLite 忙等 ——
`_stage_synthesize` 把写事务一路带进了 `synthesize_lines`，每句完成时的进度回写
要另开 session 写 `task.progress`，撞上写锁后忙等到 `PRAGMA busy_timeout`（5000ms）
再抛 `database is locked`，又被回调里的 `except` 静默吞掉。实测逐段
`l2_named_copy → l3_progress_cb` = 5521/5522/5534 ms，独立进程同口径仅 23.8 ms；
修复标记 `[FIX-SYNTH-DBLOCK-01]`，最小复现
`outputs/eval/20260918_0920/probes/probe_sqlite_busy.py`（5.52s ／ 0.00s）。

本模块只做一件事：把「一段 = 一次 synthesize() 调用 + 循环体收尾」拆成带时间戳的
阶段序列，落成 jsonl。它**不猜测原因**，只负责把时间摊开——先看清 5.6 s 长在哪个
阶段，再去改代码。

三条纪律：

1. **默认关闭**（`SYNTH_TRACE=1` 才开）。开启会多出逐段文件 IO，绝不允许污染
   生产批次的耗时口径；压测报告里的数字必须来自关闭状态。
2. **绝不影响主流程**。任何内部异常都被吞掉并降级为「不记录」，合成照常完成。
   插桩把成片搞挂，比不插桩严重得多。
3. **记绝对单调时钟**，不只是阶段内耗时。段与段之间的残余开销（循环体、回调）
   只有靠「上一段结束 → 下一段开始」的绝对时间差才能看见。

输出格式（每行一个 JSON 对象）：

```json
{"seg": 12, "speaker": "A", "chars": 17, "text": "…", "cached": false,
 "t0": 12345.678, "t1": 12351.234, "wall": "2026-09-18T09:14:02",
 "llm_end": 12349.001, "audio_ms": 5500,
 "marks": [["l0_enter", 0.0], ["l1_cache_key", 0.4], …]}
```

`marks` 是 `[阶段名, 距 t0 的毫秒数]` 的有序列表 —— 相邻两项之差才是该阶段耗时。
`llm_end` 单独记绝对时刻（来自 cosyvoice 的 `yield speech len` 日志），用它可把
「一次 infer」在**首个 yield** 处切成两段。**注意它切不出 LLM 与声码器**：
`stream=False` 路径上 `token2wav()`(flow+hift) 在 `yield` 之前就完成了，声码器属于
前段（见 `scripts/analyze_synth_trace.py::split_at_yield`）。字段名沿用 `llm_end`
是历史原因，别照名字理解。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: 一次 infer 首个 yield 的日志前缀（CosyVoice cli/cosyvoice.py 打印）。
#: `stream=False` 下每段只 yield 一次；该日志在声码器**之后**打出。
YIELD_LEN_PREFIX = "yield speech len"


class _YieldLenHandler(logging.Handler):
    """抓 `yield speech len` 的绝对时刻，用于切开「至 yield」与「yield 之后」。

    注意**不是**切开 LLM 与声码器：`stream=False` 时 `token2wav()` 在 yield 之前
    已完成，声码器被算进了前半段。

    挂在 root logger 上（cosyvoice 用 `logging.info` 直接打）。必须足够廉价：
    每条日志都会经过它，所以只做一次 `startswith` 判断。
    """

    def __init__(self, sink: list) -> None:
        super().__init__(level=logging.INFO)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        if msg.startswith(YIELD_LEN_PREFIX):
            self._sink.append((time.perf_counter(), msg))


class SynthTracer:
    """逐段阶段时间线记录器。`enabled=False` 时所有方法都是空操作。"""

    def __init__(self, enabled: bool, path: Path | None) -> None:
        self.enabled = bool(enabled) and path is not None
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._marks: list[tuple[str, float]] = []
        self._t0 = 0.0
        self._ctx: dict = {}
        self._yield_events: list[tuple[float, str]] = []
        self._handler: _YieldLenHandler | None = None
        self._n_written = 0
        self._n_failed = 0
        self._active = False
        if self.enabled:
            self._install_hook()

    # ---------------- 日志钩子 ----------------
    def _install_hook(self) -> None:
        try:
            root = logging.getLogger()
            h = _YieldLenHandler(self._yield_events)
            root.addHandler(h)
            self._handler = h
            # 隐性前提：cosyvoice 用 `logging.info` 打 `yield speech len`，而
            # `logging.getLogger().info()` 在 root 级别高于 INFO 时**根本不产生记录**，
            # 连 handler 都不会被调用。生产（uvicorn --log-level info）满足，
            # 但脚本/测试环境常常不满足 —— 那会让「至 yield / yield 之后」的切分
            # 静默失效，所以必须显式告警，不能让人以为拿到了数据。
            if not root.isEnabledFor(logging.INFO):
                log.warning(
                    "[SYNTH-TRACE] root logger 级别为 %s（高于 INFO），抓不到 "
                    "`yield speech len`：infer 的 yield 前后切分不可用，"
                    "阶段打点不受影响。需要该切分时请把日志级别调到 INFO。",
                    logging.getLevelName(root.level))
        except Exception as exc:  # noqa: BLE001
            log.warning("[SYNTH-TRACE] 安装日志钩子失败，将只记录阶段（%s）", exc)

    def close(self) -> None:
        if self._handler is not None:
            try:
                logging.getLogger().removeHandler(self._handler)
            except Exception:  # noqa: BLE001
                pass
            self._handler = None

    # ---------------- 记录 ----------------
    def begin(self, **ctx: object) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._marks = []
            self._t0 = time.perf_counter()
            self._ctx = dict(ctx)
            self._active = True

    def mark(self, name: str) -> None:
        if not (self.enabled and self._active):
            return
        with self._lock:
            self._marks.append((name, time.perf_counter()))

    def end(self, **extra: object) -> None:
        """收尾并落盘。任何异常都只告警——插桩绝不能拖垮合成。"""
        if not (self.enabled and self._active):
            return
        try:
            with self._lock:
                self._active = False
                t1 = time.perf_counter()
                t0 = self._t0
                marks = list(self._marks)
                ctx = dict(self._ctx)
                self._marks = []
                self._ctx = {}
                llm_end = self._pop_yield_since(t0)
            # 空打点也照写：一段「什么都没记到」本身就是信息（说明打点位置漏了），
            # 静默跳过会让人以为那段没跑。
            rec = {
                **ctx,
                "t0": round(t0, 6),
                "t1": round(t1, 6),
                "dur_ms": round((t1 - t0) * 1000, 2),
                "wall": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "llm_end": round(llm_end, 6) if llm_end else None,
                "marks": [[n, round((t - t0) * 1000, 2)] for n, t in marks],
                **extra,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            with self.path.open("a", encoding="utf-8") as fh:  # type: ignore[union-attr]
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._n_written += 1
        except Exception as exc:  # noqa: BLE001
            self._n_failed += 1
            if self._n_failed <= 3:
                log.warning("[SYNTH-TRACE] 写入失败（%s）", exc)

    def _pop_yield_since(self, t0: float) -> float | None:
        """取本次段开始之后最近的一条 `yield speech len` 时刻（调用方已持锁）。

        **必须原地更新列表**（`self._yield_events[:] = ...`），不能写
        `self._yield_events = ...` 重新赋值 —— `_YieldLenHandler` 装钩子时拿的是
        **同一个 list 对象**，重新赋值会让它从此往**孤儿列表**里写，于是下一次
        `end()` 就再也收不到事件。

        这个坑真实发生过且很隐蔽：只要**任意一次** `end()` 发生在没有 yield 事件的段上
        （最典型的就是**缓存命中段**——不合成，自然没有 yield），列表就被换掉，
        此后所有段的 `llm_end` 恒为 null，看起来像「日志级别没开」。
        实测证据：`outputs/synth_trace/probe_llm_end2.py`（seg=1 命中缓存 → seg=2 起全丢）。
        """
        keep: list[tuple[float, str]] = []
        hit: float | None = None
        for t, msg in self._yield_events:
            if t >= t0:
                hit = t  # 留最后一条
            else:
                keep.append((t, msg))
        # 列表通常只有 0~1 条；仍按时间序保留历史，避免无界增长
        self._yield_events[:] = keep[-8:] + ([(hit, "")] if hit else [])
        return hit

    # ---------------- 诊断 ----------------
    @property
    def stats(self) -> dict:
        return {"enabled": self.enabled, "written": self._n_written,
                "failed": self._n_failed,
                "path": str(self.path) if self.path else None}


def make_tracer(settings: object, path: Path | None = None) -> SynthTracer:
    """按配置构造 tracer。`path` 显式传入时覆盖配置（测试用）。

    注意 `Settings.synth_trace_file` 是 **property 不是方法**：直接取属性值即可，
    不要写 `getter() if callable(getter) else None` —— property 访问的结果是 `Path`，
    `callable(Path)` 为假，那样会静默退化成「没路径 → 插桩关闭」，且不报任何错。
    """
    enabled = bool(getattr(settings, "synth_trace", False))
    if path is None:
        path = getattr(settings, "synth_trace_file", None)
        if callable(path):        # 兼容将来改成方法的情况
            path = path()
    return SynthTracer(enabled, path)
