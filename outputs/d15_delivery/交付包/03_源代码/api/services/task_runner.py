# -*- coding: utf-8 -*-
"""单并发任务队列 + 状态机 + 进度上报（计划书 4.5 / 4.3 / 4.8）。

## 状态机（与 4.5 mermaid 逐边对齐；retry 回边来自 4.5 正文「断点续跑」）

    PENDING --投递脚本作业--> SCRIPTING
    SCRIPTING --成功--> SCRIPT_READY   --失败--> FAILED
    SCRIPT_READY --确认脚本(用户)--> SYNTHESIZING
    SYNTHESIZING --全部句级音频就绪--> POSTPROCESSING   --显存/推理异常--> FAILED
    POSTPROCESSING --mp3 导出成功--> PACKAGING          --FFmpeg 失败--> FAILED
    PACKAGING --RSS 生成完成--> DONE                    --写文件失败--> FAILED
    PENDING/SCRIPT_READY --用户取消--> CANCELED
    FAILED --retry 断点续跑--> SYNTHESIZING              (4.5 正文明确，mermaid 未画)

## 生存条件（计划书 2.5 / 风险 12 / 风险 3）

净可用 4.32 GB 仅够一个模型实例，**并发推理必 OOM**。因此：
- `ThreadPoolExecutor(max_workers=1)` 单并发队列；
- 叠加 `threading.Lock` 显存互斥锁双保险（GPU 推理段加锁）；
- TTS 引擎进程内单例（`TTSEngine.instance`）。
三者共同保证「同一时刻只有一条流水线在碰 GPU」。

## 断点续跑（计划书 4.5 正文）

合成阶段按 `text_hash` 查 `audio_cache` 与磁盘缓存：已命中的句子由 `tts.synthesize_lines`
直接返回缓存 wav（不重复 GPU 推理），仅未命中的句子投推理。失败时只重跑未命中句。
`audio_cache` 表与 `script_lines` 在合成阶段落库，终态清理**只删 `data/work/{task_id}/`**，
**不动 `data/cache/`**（跨任务复用资产，见 10.2）。

## 可测性（依赖注入）

`make_engine` / `make_generator` / `make_postprocess` 三个工厂可在构造时替换，
单测用假引擎 + 假生成器即可跑完整状态机而不碰 GPU / 网络。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.db import session_scope
from api.services.intro_outro import (
    has_placeholder,
    render_template,
    resolve_intro_file as _resolve_intro_file,
    resolve_outro_file as _resolve_outro_file,
    today_cn,
)
from api.models import (
    AudioCache,
    Episode,
    ScriptLine,
    SegStatus,
    Task,
    TaskStatus,
    User,
    utcnow,
)

log = logging.getLogger(__name__)


def _ffprobe() -> str | None:
    return shutil.which("ffprobe")


def _probe_audio(path: Path) -> tuple[float, int]:
    """返回 (时长秒, 字节数)。ffprobe 缺失时时长回退 0（字节数仍可靠）。"""
    size = path.stat().st_size if path.is_file() else 0
    dur = 0.0
    exe = _ffprobe()
    if exe and path.is_file():
        try:
            r = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace")
            v = (r.stdout or "").strip()
            if v:
                dur = float(v)
        except (ValueError, OSError):
            dur = 0.0
    return dur, size


# --------------------------------------------------------------------------- #
# 状态迁移表（唯一权威定义，models.TaskStatus 只存词汇）
# --------------------------------------------------------------------------- #

TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.PENDING: {TaskStatus.SCRIPTING, TaskStatus.CANCELED},
    TaskStatus.SCRIPTING: {TaskStatus.SCRIPT_READY, TaskStatus.FAILED, TaskStatus.CANCELED},
    TaskStatus.SCRIPT_READY: {TaskStatus.SYNTHESIZING, TaskStatus.CANCELED},
    TaskStatus.SYNTHESIZING: {TaskStatus.POSTPROCESSING, TaskStatus.FAILED},
    TaskStatus.POSTPROCESSING: {TaskStatus.PACKAGING, TaskStatus.FAILED},
    TaskStatus.PACKAGING: {TaskStatus.DONE, TaskStatus.FAILED},
    # FAILED -> SYNTHESIZING 是 retry 断点续跑的回边（4.5 正文，mermaid 未画）
    #
    # FAILED -> SCRIPTING 是 **D12 崩溃恢复** 的回边（4.5 与 mermaid 均未画）：
    # 进程在脚本阶段被杀之后，任务停在 SCRIPTING —— 而 `_run_script` 的第
    # 一个动作就是 `_transition(..., SCRIPTING)`，状态机不许自环，所以必须先
    # 落 FAILED 再回到 SCRIPTING。回放是幂等的：`_run_script` 会先删掉本任务
    # 的全部 script_lines 再按新脚本整份重建（见其实现），不会叠加半截脚本。
    TaskStatus.FAILED: {TaskStatus.SYNTHESIZING, TaskStatus.SCRIPTING},
    TaskStatus.DONE: set(),
    TaskStatus.CANCELED: set(),
}

#: 非终态集合（用于崩溃恢复扫描）：这些状态属于「进程还在跑」的中间态，
#: 进程重启后若库里仍是它们，就一定是上次被杀留下的。
NON_TERMINAL: frozenset[str] = frozenset({
    TaskStatus.PENDING, TaskStatus.SCRIPTING, TaskStatus.SCRIPT_READY,
    TaskStatus.SYNTHESIZING, TaskStatus.POSTPROCESSING, TaskStatus.PACKAGING,
})

#: 崩溃恢复：这些状态能被**自动续跑**（合成阶段幂等，靠 audio_cache 重放）。
#: SCRIPT_READY 不在其中 —— 它在等用户确认，不是被中断的状态。
AUTO_RESUMABLE: frozenset[str] = frozenset({
    TaskStatus.SYNTHESIZING, TaskStatus.POSTPROCESSING, TaskStatus.PACKAGING,
})

#: 崩溃恢复时写给 `error_msg` 的原因。刻意写清楚「不是内容/模型出错」，
#: 免得用户把它当成生成质量事故；后续若被自动续跑成功，这条会被历程覆盖。
def _dir_size_mb(path: Path) -> float:
    """目录占用的 MB 数（只用于告警文案，取不到就报 0.0）。"""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / 1048576.0


_INTERRUPTED_MSG = "服务进程重启，该任务在上一次运行中被中断（已自动恢复）"

# 各阶段占用的进度区间（0~100），便于前端单一进度条展示
_STAGE_PROGRESS = {
    TaskStatus.SCRIPTING: (0, 15),
    TaskStatus.SCRIPT_READY: (15, 15),
    TaskStatus.SYNTHESIZING: (20, 60),
    TaskStatus.POSTPROCESSING: (65, 82),
    TaskStatus.PACKAGING: (85, 95),
    TaskStatus.DONE: (100, 100),
}


class IllegalTransition(Exception):
    """非法状态迁移。"""


class TaskRunnerError(Exception):
    """流水线内部错误。"""


def _transition(db: Session, task: Task, new_status: str, *,
                stage: str | None = None, error_msg: str | None = None,
                progress: int | None = None) -> None:
    """带校验的状态迁移。终态写入 finished_at（触发器：10.2 清理）。"""
    if task.status not in TRANSITIONS or new_status not in TRANSITIONS.get(task.status, set()):
        raise IllegalTransition(
            f"非法状态迁移：{task.status} -> {new_status}（允许："
            f"{sorted(TRANSITIONS.get(task.status, set()))}）")
    task.status = new_status
    if stage is not None:
        task.stage = stage
    if error_msg is not None:
        task.error_msg = error_msg[:4000]
    if progress is not None:
        task.progress = max(0, min(100, int(progress)))
    else:
        lo, hi = _STAGE_PROGRESS.get(new_status, (task.progress or 0, task.progress or 0))
        if new_status in (TaskStatus.SCRIPT_READY, TaskStatus.DONE):
            task.progress = hi
    if new_status in TaskStatus.TERMINAL:
        task.finished_at = utcnow()


class TaskRunner:
    """单并发任务调度器。"""

    def __init__(self, settings: Settings | None = None, *,
                 make_engine: Callable[[], object] | None = None,
                 make_generator: Callable[[], object] | None = None,
                 make_postprocess: Callable[..., object] | None = None) -> None:
        self.s = settings or get_settings()
        self._make_engine = make_engine or self._default_engine
        self._make_generator = make_generator or (lambda: self._default_generator())
        self._make_postprocess = make_postprocess or self._default_postprocess
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="task")
        self._gpu_lock = threading.Lock()
        self._cancel_flags: dict[str, bool] = {}
        self._futures: dict[str, Future] = {}
        #: 当前占用唯一工作线程的 task_id（单并发 ⇒ 至多一个）。仅供队列位置展示，
        #: 与正确性无关，所以不加锁。
        self._running_task_id: str | None = None
        #: 进度回写失败计数（用于把握不住静默失败的额度限流）
        self._progress_failures = 0

    # ---------------- 默认工厂（可被单测替换）----------------
    @staticmethod
    def _default_engine() -> object:
        from api.services.tts import TTSEngine
        return TTSEngine.instance(get_settings())

    def _default_generator(self) -> object:
        from api.services.script_gen import ScriptGenerator
        return ScriptGenerator(self.s)

    @staticmethod
    def _default_postprocess() -> "Callable[..., object]":
        """返回真实的后期函数（工厂）。调用点 `_make_postprocess()(clips, ...)` 再传参。

        ⚠️ 早期实现写成 `return postprocess(*args, **kwargs)`，等于构造期就空参调用
        `postprocess()`，后台流水线在 POSTPROCESSING 阶段抛
        `TypeError: missing 1 required positional argument: 'clips'`，整条失败。
        D6 验证脚本首次暴露，已修正（见 D6 报告 [FIX-PP-01]）。
        """
        from api.services.postprocess import postprocess
        return postprocess

    # ---------------- 提交 ----------------
    def submit_script(self, task_id: str) -> Future:
        fut = self._executor.submit(self._run_script, task_id)
        self._futures[task_id] = fut
        return fut

    def submit_synthesize(self, task_id: str) -> Future:
        fut = self._executor.submit(self._run_pipeline, task_id)
        self._futures[task_id] = fut
        return fut

    def request_cancel(self, task_id: str) -> None:
        """置取消标记。仅 PENDING/SCRIPT_READY 阶段真正生效（状态机不支持合成中取消）。"""
        self._cancel_flags[task_id] = True

    def is_canceled(self, task_id: str) -> bool:
        return bool(self._cancel_flags.get(task_id, False))

    # ---------------- 队列可观测（D12「队列排队提示」）----------------
    def queue_snapshot(self) -> dict:
        """队列现状：正在跑的 task_id + 还在排队的 task_id 列表。

        `ThreadPoolExecutor(max_workers=1)` 不暴露队列内容，但 `_futures` 里
        「尚未完成」的 future **按键的插入顺序**排列，就是投递顺序，取其键即排队序列。
        """
        not_done = [tid for tid, fut in self._futures.items() if not fut.done()]
        running = self._running_task_id
        return {"running": running, "waiting": [t for t in not_done if t != running]}

    def queue_position(self, task_id: str) -> int | None:
        """该任务的排队位置：**0 = 正在跑**，1 = 下一个，2 = 再下一个……不在队列返回 None。

        用 0 而不是 1 表示「正在跑」，是为了让前端能直接把 0 渲染成「进行中」、
        >0 渲染成「前面还有 N 个任务」，不必再单独判 `status`。
        """
        snap = self.queue_snapshot()
        if snap["running"] == task_id:
            return 0
        waiting = snap["waiting"]
        if task_id in waiting:
            return waiting.index(task_id) + 1
        return None

    # ---------------- 崩溃恢复（D12「杀进程后重启可续跑」）----------------
    def recover_interrupted(self, *, auto_resume: bool | None = None) -> dict:
        """启动时修掉「上次进程被杀」留下的非终态任务。

        为什么必须有这个：被杀之后库里会停在 SYNTHESIZING/POSTPROCESSING/PACKAGING
        这类中间态，而**没有任何东西会再推动它们** —— 前端进度条永远卡住，
        单并发队列也永远等不到结束。这类僵尸状态只能靠启动时扫一遍清掉。

        处置规则：

        | 残留状态 | 处置 | 理由 |
        | --- | --- | --- |
        | `PENDING` | 重新投递**脚本**作业 | 作业还没跑过（PENDING → SCRIPTING 合法） |
        | `SCRIPTING` | → `FAILED` → 重新投递脚本作业 | 脚本可能只写了一半；`_run_script` 整份替换 script_lines，回放幂等 |
        | `SCRIPT_READY` | **不动** | 它在等用户确认，本来就不是「被中断」的状态 |
        | `SYNTHESIZING` / `POSTPROCESSING` / `PACKAGING` | → `FAILED`（附可读原因）；`auto_resume` 为真时立即续跑合成 | 合成按 `text_hash` 命中 `audio_cache`，重放只补未命中段，**幂等且便宜** |

        **自动续跑不会无限循环**：恢复只认非终态状态，续跑后任务要么 DONE，
        要么因**真实原因**落到 FAILED —— 而 FAILED 不在恢复扫描范围内，
        所以同一次中断最多自动续跑一次。

        `startup_recover` 置 false 时整体 no-op（逃生舱，便于排障时手动接管）。
        """
        out: dict = {"scanned": 0, "script_requeued": [], "resumed": [],
                     "failed": [], "skipped": []}
        if not getattr(self.s, "startup_recover", True):
            log.info("[RECOVER] STARTUP_RECOVER=false，跳过崩溃恢复扫描")
            return out
        if auto_resume is None:
            auto_resume = bool(getattr(self.s, "startup_auto_resume", True))

        plan: list[tuple[str, str]] = []   # (task_id, "script" | "synth")
        with session_scope(self.s) as db:
            stale = (db.execute(select(Task)
                                .where(Task.status.in_(tuple(NON_TERMINAL)))
                                .order_by(Task.created_at))
                     .scalars().all())
            out["scanned"] = len(stale)
            for t in stale:
                st = t.status
                if st == TaskStatus.SCRIPT_READY:
                    out["skipped"].append(t.id)
                    continue
                if st == TaskStatus.PENDING:
                    out["script_requeued"].append(t.id)
                    plan.append((t.id, "script"))
                    continue
                if st == TaskStatus.SCRIPTING:
                    # 状态机不许自环，先落 FAILED 再回 SCRIPTING（见 TRANSITIONS 注释）
                    _transition(db, t, TaskStatus.FAILED, stage="脚本生成",
                                error_msg=_INTERRUPTED_MSG)
                    out["script_requeued"].append(t.id)
                    plan.append((t.id, "script"))
                    continue
                # 合成 / 后期 / 封装：幂等，可续跑
                _transition(db, t, TaskStatus.FAILED,
                            stage=t.stage or "合成", error_msg=_INTERRUPTED_MSG)
                if auto_resume:
                    out["resumed"].append(t.id)
                    plan.append((t.id, "synth"))
                else:
                    out["failed"].append(t.id)
        # 事务已提交，状态是 FAILED 了，此刻才能安全投递
        for tid, action in plan:
            if action == "script":
                self.submit_script(tid)
            else:
                self.submit_synthesize(tid)

        if out["scanned"]:
            log.warning(
                "[RECOVER] 扫描到 %d 个非终态任务：脚本重投 %d、续跑 %d、"
                "仅标记失败 %d、跳过（等待用户确认）%d",
                out["scanned"], len(out["script_requeued"]), len(out["resumed"]),
                len(out["failed"]), len(out["skipped"]))
        else:
            log.info("[RECOVER] 无残留非终态任务")
        return out

    # ---------------- 脚本阶段 ----------------
    def _run_script(self, task_id: str) -> None:
        self._running_task_id = task_id
        try:
            self._run_script_inner(task_id)
        finally:
            self._running_task_id = None

    def _run_script_inner(self, task_id: str) -> None:
        with session_scope(self.s) as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if self.is_canceled(task_id):
                _transition(db, task, TaskStatus.CANCELED, stage="已取消")
                self._cancel_flags.pop(task_id, None)
                return
            _transition(db, task, TaskStatus.SCRIPTING, stage="脚本生成")
            try:
                gen = self._make_generator()
                result = gen.generate(
                    topic=task.topic,
                    duration_min=max(0.5, task.target_duration_sec / 60.0),
                    style=task.style or "")
                script = result.script
                # 替换既有行（新任务通常无行；retry 不重跑脚本阶段）
                for old in list(task.script_lines):
                    db.delete(old)
                db.flush()
                for ln in script.lines:
                    db.add(ScriptLine(
                        task_id=task.id, seq=ln.seq, speaker=str(ln.speaker or "A")[:1],
                        text=ln.text, read_text="", seg_status=SegStatus.PENDING))
                task.script_title = script.title or ""
                task.script_summary = script.summary or ""
                task.target_word_count = int(result.target_words or 0)
                _transition(db, task, TaskStatus.SCRIPT_READY, stage="待确认脚本")
            except Exception as exc:  # noqa: BLE001
                log.exception("脚本生成失败 task=%s", task_id)
                _transition(db, task, TaskStatus.FAILED, stage="脚本生成",
                             error_msg=f"脚本生成失败：{exc}")

    # ---------------- 合成 → 后期 → 封装 流水线 ----------------
    def _run_pipeline(self, task_id: str) -> None:
        self._running_task_id = task_id
        try:
            with self._gpu_lock:
                try:
                    self._stage_synthesize(task_id)
                    self._stage_postprocess(task_id)
                    self._stage_package(task_id)
                except Exception as exc:  # noqa: BLE001
                    log.exception("流水线失败 task=%s", task_id)
                    with session_scope(self.s) as db:
                        task = db.get(Task, task_id)
                        if task is not None and task.status not in TaskStatus.TERMINAL:
                            _transition(db, task, TaskStatus.FAILED,
                                        error_msg=f"流水线失败：{exc}")
                finally:
                    # 终态清理中间产物（10.2）：仅删 work，不动 cache
                    self._cleanup_work(task_id)
        finally:
            self._running_task_id = None

    def _stage_synthesize(self, task_id: str) -> None:
        with session_scope(self.s) as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if task.status not in (TaskStatus.SCRIPT_READY, TaskStatus.FAILED):
                raise IllegalTransition(
                    f"合成阶段期望 SCRIPT_READY/FAILED，实际 {task.status}")
            _transition(db, task, TaskStatus.SYNTHESIZING, stage="合成")
            orm_lines = list(task.script_lines)
            if not orm_lines:
                raise TaskRunnerError("脚本为空，无法合成")

        engine = self._make_engine()
        from api.services.normalize import build_script_lines, load_polyphone
        poly = load_polyphone(str(self.s.path(self.s.polyphone_dict)))

        work_seg = self.s.work_path / task_id
        work_seg.mkdir(parents=True, exist_ok=True)

        # 规范化读文并回写 ORM。
        #
        # [FIX-SYNTH-DBLOCK-01] 这一步必须**独立成短事务、并在进合成之前提交**。
        # 曾经的写法是把 session 一路带进 `engine.synthesize_lines(...)`，于是：
        #   `db.flush()` 的 UPDATE 占住 SQLite 写锁（RESERVED）不释放，
        #   而每句完成时触发的进度回调 `_on_synth_progress` 要**另开一个 session**
        #   去写 `task.progress` —— 后者只能忙等到 `PRAGMA busy_timeout`
        #   （api/db.py 设 5000ms）才抛 `database is locked`。
        #   两个可观测后果：
        #     ① 每句固定多花 ≈5.5s（实测 5521/5522/5534ms；D10 的 401/414 段落在
        #        5.5~6.0s 正是它），合成阶段 56% 的时间耗在这里；
        #     ② `task.progress` 在合成全程停在区间下沿（=15），前端进度条假死。
        #   最小复现：outputs/eval/20260918_0920/probes/probe_sqlite_busy.py
        #   （长事务未提交 5.52s + database is locked ／ 已提交 0.00s 成功）。
        with session_scope(self.s) as db:
            task = db.get(Task, task_id)
            orm_lines = list(task.script_lines)
            norm_lines = build_script_lines(
                [{"speaker": ln.speaker, "text": ln.text} for ln in orm_lines],
                max_chars=self.s.max_chars_per_seg, polyphone=poly)
            warned = 0
            for ln, nl in zip(orm_lines, norm_lines):
                ln.read_text = nl.read_text
                # 不可逆改写必须留痕（词典命中、首尾标点裁剪等）。
                # `script_lines` 没有 warnings 列，加列要迁移、旧库不兼容，故落日志：
                # 一致率核对要求「按原文→读文映射比对」，没有这行日志就无从知道
                # 某行读文是被词典改的还是原文本来如此。
                if nl.warnings:
                    warned += 1
            if warned:
                log.warning("[POLYPHONE] %s：%d 行的读文含不可逆改写"
                            "（多音字词典命中 / 首尾标点裁剪等）；一致性核对请按"
                            "原文→读文映射比对。词典=%s",
                            task_id, warned, self.s.polyphone_dict)
            speed = float(task.speed or 1.0)
            tone = task.tone or ""
        # ← 事务在此提交，写锁释放；下面整段合成期间回调可自由落库

        results = engine.synthesize_lines(
            norm_lines, out_dir=work_seg, speed=speed, tone=tone,
            on_progress=lambda cur, total, r: self._on_synth_progress(
                task_id, cur, total))

        # 句级缓存 + 行状态回写（新事务，一行代码改一次库，不再横跨合成）
        hits = sum(1 for r in results if getattr(r, "cached", False))
        log.info("[CACHE] task=%s 合成段数=%d 命中缓存=%d（命中率 %.1f%%）",
                 task_id, len(results), hits,
                 (100.0 * hits / len(results)) if results else 0.0)
        with session_scope(self.s) as db:
            task = db.get(Task, task_id)
            orm_lines = list(task.script_lines)
            # 缓存命中读数：**整体重算**而非累加 —— 续跑会重跑整个合成阶段，
            # 累加会把同一批段算两遍，命中率虚高。见 models.Task 该列注释。
            task.cache_seg_count = len(results)
            task.cache_hit_count = hits
            # 按 line_seq 聚合分段结果
            by_line: dict[int, list] = {}
            for r in results:
                by_line.setdefault(int(r.segment.line_seq), []).append(r)
            for ln in orm_lines:
                segs = by_line.get(int(ln.seq), [])
                if not segs:
                    continue
                # 先落句级缓存（AudioCache 是 script_lines.text_hash 的父表），
                # 再回写 text_hash，避免 autoflush 时父行缺失触发外键冲突。
                for r in segs:
                    self._upsert_cache(db, r)
                ln.duration_ms = sum(int(r.duration_ms) for r in segs)
                ln.text_hash = segs[0].text_hash
                ln.seg_status = SegStatus.DONE
            db.flush()
        # 合成完成：进度推进到 POSTPROCESSING 区间下沿
        with session_scope(self.s) as db:
            task = db.get(Task, task_id)
            if task is not None:
                task.progress = _STAGE_PROGRESS[TaskStatus.POSTPROCESSING][0]

    def _on_synth_progress(self, task_id: str, cur: int, total: int) -> None:
        """每句完成即推进进度。**不允许静默失败**。

        历史上这里写的是 `except Exception: pass`，把 `database is locked` 藏了
        整整一个压测批次：合成阶段每句多花 5.5s、前端进度条假死，日志里却一个字
        都没有（见 [FIX-SYNTH-DBLOCK-01]）。进度只是观测量，仍然不能因为写不进去
        就中断合成，但**前几次失败必须留下 WARNING**，否则同一类问题会再次隐形。
        """
        if not total:
            return
        lo, hi = _STAGE_PROGRESS[TaskStatus.SYNTHESIZING]
        pct = lo + int((cur / total) * (hi - lo))
        try:
            with session_scope(self.s) as db:
                task = db.get(Task, task_id)
                if task is not None and task.status == TaskStatus.SYNTHESIZING:
                    task.progress = pct
        except Exception as exc:  # noqa: BLE001
            self._progress_failures += 1
            if self._progress_failures <= 3:
                log.warning(
                    "[FIX-SYNTH-DBLOCK-01] 进度回写失败（第 %d 次，忽略）："
                    "task=%s cur=%s/%s err=%s",
                    self._progress_failures, task_id, cur, total, exc)

    @staticmethod
    def _upsert_cache(db: Session, r: object) -> None:
        """把单句合成结果写入 audio_cache（幂等）。text_hash 即全局主键。"""
        key = str(getattr(r, "text_hash", "") or "")
        if not key:
            return
        wav = str(getattr(r, "wav_path", "") or "")
        if not wav:
            return
        existing = db.get(AudioCache, key)
        if existing is None:
            db.add(AudioCache(
                text_hash=key,
                speaker=str(getattr(r.segment, "speaker", "A") or "A")[:1],
                wav_path=wav,
                duration_ms=int(getattr(r, "duration_ms", 0) or 0)))
        else:
            existing.wav_path = wav
            existing.duration_ms = int(getattr(r, "duration_ms", 0) or 0)

    def _build_dynamic_intro(self, task: Task, *, engine,
                             work_dir: Path) -> Path | None:
        """按当期日期 / 主题合成动态片头，返回已归一到正片口径的 wav 路径。

        返回 None 的情形（均由调用方回退到固定素材 / 无片头）：
        - `intro_template` 为空，或**不含占位符**（与当期无关，用固定 mp3 更省时）；
        - 合成或归一失败 —— **片头不是成片必需项，失败只告警、不中断流水线**。

        ⚠️ 后期 `postprocess()` 对 intro 只做 `unify` + `fade`、**不做 loudnorm**，
        所以必须在这里归一，否则片头与正片响度脱节。
        """
        tpl = (self.s.intro_template or "").strip()
        if not tpl or not has_placeholder(tpl):
            return None

        topic = (task.script_title or task.topic or "本期话题").strip()
        text = render_template(tpl, today_cn(), topic,
                               max_chars=int(self.s.max_chars_per_seg))
        if not text:
            return None

        try:
            # 【已纠正：根因在 CosyVoice2 的 instruct2，而非 zero_shot】
            # 实测（outputs/_probe_dyn.py，双路径 ASR 隔离验证）：
            #   - inference_instruct2 在 zero_shot_spk_id 非空时，会把 prompt_wav
            #     （voice_a.wav，内容正是「生活就像海洋…」）作为声学前缀回显到输出开头
            #     → 成片开头先朗读一遍参考音频内容（爱情期片头 0–5s 即此句）。
            #   - inference_zero_shot 在 zero_shot_spk_id 非空时改用预计算的 spk2info，
            #     只输出目标文本、不回显参考音频，音色仍由零样本克隆（voice.id）决定。
            # 故片头必须走 zero_shot（tone 强制空），绝不能走 instruct2。
            # 注：intro_tone 配置项在此被有意忽略，避免再次误引入 instruct2 回显 bug。
            res = engine.synthesize(text, speaker=self.s.intro_speaker,
                                    speed=float(self.s.intro_speed),
                                    tone="")
        except Exception as exc:  # noqa: BLE001
            log.warning("动态片头合成失败，本期跳过片头：%s", exc)
            return None

        src = Path(res.wav_path)
        if not src.is_file():
            return None

        try:
            from api.services import postprocess as pp

            m = pp.measure_loudness(src, settings=self.s)
            ok, reason = pp.loudness_usable(m)
            if not ok:
                log.warning("动态片头响度测量不可用（%s）→ 不归一", reason)
                return src
            normed, _ = pp.normalize_loudness(
                src, work_dir / "intro_norm.wav", measured=m, settings=self.s)
            log.info("动态片头已生成：%s（%s 字）", normed.name, len(text))
            return normed
        except Exception as exc:  # noqa: BLE001
            log.warning("动态片头响度归一失败，改用原始音频：%s", exc)
            return src

    def _stage_postprocess(self, task_id: str) -> None:
        with session_scope(self.s) as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if task.status != TaskStatus.SYNTHESIZING:
                raise IllegalTransition(f"后期阶段期望 SYNTHESIZING，实际 {task.status}")
            _transition(db, task, TaskStatus.POSTPROCESSING, stage="后期拼接")

            engine = self._make_engine()
            from api.services.normalize import build_script_lines, build_segments
            from api.services.postprocess import Clip

            orm_lines = list(task.script_lines)
            clips = []
            for ln in orm_lines:
                single = build_script_lines(
                    [{"speaker": ln.speaker, "text": ln.text}],
                    max_chars=self.s.max_chars_per_seg)
                segs = build_segments(single, max_chars=self.s.max_chars_per_seg)
                for seg in segs:
                    key = engine.cache_key(seg.read_text, seg.speaker,
                                           float(task.speed or 1.0), task.tone or "")
                    ac = db.get(AudioCache, key)
                    if ac is None or not Path(ac.wav_path).is_file():
                        raise TaskRunnerError(f"句级缓存缺失（text_hash={key}），无法拼接")
                    clips.append(Clip(wav=Path(ac.wav_path), speaker=seg.speaker,
                                      line_seq=int(ln.seq)))

            work_dir = self.s.work_path / task_id
            work_dir.mkdir(parents=True, exist_ok=True)

            # 片头：优先动态合成（模板含当期日期/主题）；否则回退固定素材
            intro_path = self._build_dynamic_intro(
                task, engine=engine, work_dir=work_dir)
            if intro_path is None:
                intro_path = _resolve_intro_file(self.s)
            outro_path = _resolve_outro_file(self.s)
            log.info("后期片头尾：intro=%s outro=%s",
                     (intro_path.name if intro_path else "（无）"),
                     (outro_path.name if outro_path else "（无）"))

            res = self._make_postprocess()(
                clips, out_dir=work_dir, name=task_id, settings=self.s,
                intro=intro_path, outro=outro_path)
            # 成片落 data/audio/{task_id}/final.mp3（对外访问点）
            audio_dir = self.s.audio_path / task_id
            audio_dir.mkdir(parents=True, exist_ok=True)
            final_mp3 = audio_dir / "final.mp3"
            shutil.copy2(Path(res.mp3), final_mp3)
            task.progress = _STAGE_PROGRESS[TaskStatus.PACKAGING][0]

    def _stage_package(self, task_id: str) -> None:
        with session_scope(self.s) as db:
            task = db.get(Task, task_id)
            if task is None:
                return
            if task.status != TaskStatus.POSTPROCESSING:
                raise IllegalTransition(f"封装阶段期望 POSTPROCESSING，实际 {task.status}")
            _transition(db, task, TaskStatus.PACKAGING, stage="封装 RSS")

            final_mp3 = self.s.audio_path / task_id / "final.mp3"
            if not final_mp3.is_file():
                raise TaskRunnerError("成片缺失，无法封装")
            dur, size = _probe_audio(final_mp3)

            user = db.get(User, task.user_id)
            if user is None:
                raise TaskRunnerError("任务归属用户不存在")
            from api.services.podcast_rss import ensure_feed, write_feed_xml
            ensure_feed(db, user, self.s)

            # [D12 幂等封装] `episodes.task_id` 是 unique —— 若上一次进程在
            # PACKAGING 里被杀（Episode 行已提交、状态还没推到 DONE），续跑时
            # 直接 `db.add(...)` 会撞唯一约束，整条任务从「差一步」变成 FAILED。
            # 因此这里按 task_id 做 upsert：有则**就地更新**（时长/大小/路径可能
            # 因重跑而变），无则新建。guid 也一并沿用旧值，否则老订阅端会看到
            # 同一期两个不同 enclosure。
            ep = db.scalar(select(Episode).where(Episode.task_id == task.id))
            if ep is None:
                ep = Episode(id=str(uuid.uuid4()), task_id=task.id,
                             feed_guid=str(uuid.uuid4()))
                db.add(ep)
            ep.title = task.script_title or "未命名单集"
            ep.mp3_path = str(final_mp3)
            ep.duration_sec = int(round(dur))
            ep.file_size = int(size)
            ep.pub_date = ep.pub_date or utcnow()
            db.flush()
            try:
                # [FIX-FEED-LATEST-01] 必须显式带上本期：此刻本任务还在 PACKAGING，
                # 而 feed 的过滤条件是「Task 已 DONE」，不传这一句的话刚做好的这期
                # 会被挡在订阅源之外 —— 表现为 feed 恒久落后一期（14/14 复现）。
                write_feed_xml(db, user=user, settings=self.s,
                               include_task_id=task.id)
            except Exception as exc:  # noqa: BLE001
                raise TaskRunnerError(f"RSS 生成失败：{exc}")
            _transition(db, task, TaskStatus.DONE, stage="完成")

    # ---------------- 清理（10.2）----------------
    def _cleanup_work(self, task_id: str) -> None:
        """删除任务工作目录；**删不掉时必须留下证据**。

        这里刻意不用 `rmtree(..., ignore_errors=True)` 就算完 —— 那是本项目反复
        踩到的同一族缺陷（把失败伪装成「什么都没发生」）。实测在沙箱里 rmtree 会被
        bulk-delete 守卫拦下，`data/work/<task_id>/` 于是原地累积到 GB 级，
        而日志里**一个字都没有**，直到有人手工 `du` 才发现。

        所以：删完**回查一次**，仍在就 WARNING（附残留体积与回收命令）。
        **绝不因此把任务判失败** —— 清理失败是运维问题，不该让一期成片变成 FAILED。
        """
        work = self.s.work_path / task_id
        if work.is_dir():
            shutil.rmtree(work, ignore_errors=True)
            if work.exists():
                log.warning(
                    "[WORK] 工作目录未能删除（不影响成片）：%s 残留 %.1f MB；"
                    "回收：scripts/cleanup_workspace.py --apply --work-only",
                    work, _dir_size_mb(work),
                )
        self._cancel_flags.pop(task_id, None)
        self._futures.pop(task_id, None)


_runner: "TaskRunner | None" = None


def get_runner(settings: Settings | None = None,
               **inject) -> TaskRunner:
    """进程内单例（与 TTS 引擎单例对齐：模型只加载一次）。"""
    global _runner
    if _runner is None:
        _runner = TaskRunner(settings or get_settings(), **inject)
    return _runner


def reset_runner() -> None:
    """测试用：释放单例。"""
    global _runner
    _runner = None
