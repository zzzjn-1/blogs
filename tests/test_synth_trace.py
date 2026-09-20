# -*- coding: utf-8 -*-
"""逐段合成计时插桩（synth_trace）测试。

背景：R21 —— 合成阶段 56% 的时间落在「段与段之间」的固定开销（5.60 s/段，
414 段中 401 段挤在 5.50~6.00 s）。要定位它就必须先把一段的时间摊开，
本模块是那把尺子。**尺子本身不准，后面所有结论都是错的**，所以这里逐条锁死。

全部用例不加载 GPU 模型。
"""
from __future__ import annotations

import json
import logging

import pytest

from api.config import Settings
from api.services import synth_trace as st
from api.services.synth_trace import SynthTracer, make_tracer
from api.services.tts import TTSEngine

YIELD_MSG = "yield speech len 5.36, rtf 1.0961601983255413"


@pytest.fixture()
def root_info():
    """把 root logger 调到 INFO。

    cosyvoice 用 `logging.info` 打 `yield speech len`，而 `Logger.info()` 在 root
    级别高于 INFO 时**不产生任何记录**（handler 根本不会被调用）。生产由
    `uvicorn --log-level info` 满足这个前提，测试环境默认 WARNING，必须显式打开。
    """
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.INFO)
    try:
        yield root
    finally:
        root.setLevel(old)


def _records(path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _emit_yield(msg: str = YIELD_MSG) -> None:
    """模拟 CosyVoice 在**首个 yield 之后**打的日志（走 root logger）。

    注意语义：`stream=False` 下 `token2wav()`(flow+hift) 在 yield 之前已完成，
    所以这条日志**不代表 LLM 结束**，不能拿它划分 LLM / 声码器。
    """
    logging.getLogger().info(msg)


# ------------------------------------------------------------ 开关语义
def test_disabled_tracer_writes_nothing(tmp_path):
    """默认关闭：不建文件、不写记录。生产耗时口径必须干净。"""
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(False, p)
    t.begin(seg=1)
    t.mark("whatever")
    t.end()
    assert not p.exists()
    assert t.stats["written"] == 0


def test_disabled_tracer_does_not_raise_on_end(tmp_path):
    SynthTracer(False, None).end()          # 无 path 也不应炸


# ------------------------------------------------------------ 基本记录
def test_enabled_writes_one_record_with_ordered_marks(tmp_path):
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    t.begin(seg=3, speaker="B", chars=17, text="测试。")
    t.mark("s0_enter")
    t.mark("s5_infer")
    t.mark("l3_progress_cb")
    t.end(ok=True, cached=False, audio_ms=5500)

    recs = _records(p)
    assert len(recs) == 1
    r = recs[0]
    assert r["seg"] == 3 and r["speaker"] == "B" and r["chars"] == 17
    assert r["ok"] is True and r["cached"] is False and r["audio_ms"] == 5500
    names = [m[0] for m in r["marks"]]
    assert names == ["s0_enter", "s5_infer", "l3_progress_cb"]
    deltas = [m[1] for m in r["marks"]]
    assert deltas == sorted(deltas), "阶段偏移必须单调不减"
    assert all(d >= 0 for d in deltas)
    assert r["dur_ms"] >= deltas[-1] - 1e-6, "总时长不得小于最后一个打点偏移"


def test_each_segment_gets_its_own_record(tmp_path):
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    for i in (1, 2):
        t.begin(seg=i)
        t.mark(f"m{i}")
        t.end()
    recs = _records(p)
    assert [r["seg"] for r in recs] == [1, 2]
    assert [r["marks"][0][0] for r in recs] == ["m1", "m2"]


# ------------------------------------------------------------ 生命周期（真 bug 的回归）
def test_mark_before_begin_is_noop(tmp_path):
    """回归：未 begin 就打点会把 `_t0=0` 混进记录，偏移变成天文数字。

    裸调 `synthesize()`（不经 `synthesize_lines`）时正是这种情形。
    """
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    t.mark("stray")
    t.end()
    assert not p.exists()
    # 之后正常的 begin/end 不受污染
    t.begin(seg=1)
    t.mark("s0_enter")
    t.end()
    assert [m[0] for m in _records(p)[0]["marks"]] == ["s0_enter"]
    assert _records(p)[0]["marks"][0][1] >= 0


def test_end_without_begin_is_noop(tmp_path):
    p = tmp_path / "trace.jsonl"
    SynthTracer(True, p).end()
    assert not p.exists()


def test_second_begin_resets_marks(tmp_path):
    """回归：marks 若不重置，第二段会带上一段的全部打点，阶段分解全错。"""
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    t.begin(seg=1)
    t.mark("a")
    t.mark("b")
    t.end()
    t.begin(seg=2)
    t.mark("c")
    t.end()
    recs = _records(p)
    assert [m[0] for m in recs[0]["marks"]] == ["a", "b"]
    assert [m[0] for m in recs[1]["marks"]] == ["c"], "第二段不得继承第一段打点"


# ------------------------------------------------------------ 日志钩子：抓首个 yield 时刻
def test_yield_len_is_captured_as_llm_end(tmp_path, root_info):
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    try:
        t.begin(seg=1)
        t.mark("s4_empty_cache_pre")
        _emit_yield()
        t.mark("s5_infer")
        t.end()
    finally:
        t.close()
    r = _records(p)[0]
    assert r["llm_end"] is not None, "必须抓到 yield speech len 的绝对时刻"
    assert r["t0"] <= r["llm_end"] <= r["t1"], "llm_end 必须落在本段区间内"


def test_yield_hook_warns_when_root_level_hides_info(tmp_path, caplog):
    """回归：root 级别高于 INFO 时日志根本不产生，钩子静默失效。

    不告警的话，分析者会以为「infer 前段耗时 0」而去查模型 —— 方向直接错掉。
    """
    root = logging.getLogger()
    old = root.level
    root.setLevel(logging.WARNING)
    try:
        with caplog.at_level(logging.WARNING, logger="api.services.synth_trace"):
            t = SynthTracer(True, tmp_path / "trace.jsonl")
            t.close()
        assert any("抓不到" in r.getMessage() or "root logger 级别" in r.getMessage()
                   for r in caplog.records), \
            [r.getMessage() for r in caplog.records]
    finally:
        root.setLevel(old)


def test_stale_yield_event_is_not_attributed_to_next_segment(tmp_path, root_info):
    """回归：上一段的 `yield speech len` 不得被算进下一段。

    否则「至 yield / yield 之后」的切分会整段错位，得出相反的结论。
    """
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    try:
        t.begin(seg=1)
        _emit_yield()
        t.end()
        t.begin(seg=2)          # 本段不产生 yield 事件
        t.mark("s5_infer")
        t.end()
    finally:
        t.close()
    r1, r2 = _records(p)
    assert r1["llm_end"] is not None
    assert r2["llm_end"] is None, "陈旧事件被错误归属到下一段"


def test_yield_events_list_identity_survives_end(tmp_path, root_info):
    """回归：`end()` 必须**原地更新** `_yield_events`，不能重新赋值列表。

    `_YieldLenHandler` 装钩子时持有的是**同一个 list 对象**；一旦 `end()` 写
    `self._yield_events = ...`，handler 就改往孤儿列表里写，此后所有段的
    `llm_end` 恒为 null —— 看起来像「日志级别没开」，实际是插桩自己的 bug。

    触发条件很常见：**任意一段没有 yield 事件**即可（最典型的就是缓存命中段，
    不合成自然没有 yield）。实测正是这么踩到的：一段命中缓存 → 之后全丢。
    """
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    sink_before = t._yield_events
    try:
        t.begin(seg=1)
        _emit_yield("synthesis text 命中缓存，无 yield")
        t.end()                      # 这段没有 yield → 曾经在这里把列表换掉
        assert t._yield_events is sink_before, "列表被重新赋值，handler 将写进孤儿列表"

        t.begin(seg=2)
        _emit_yield()                # 这段有 yield，必须还能被收到
        t.end()
    finally:
        t.close()
    r1, r2 = _records(p)
    assert r1["llm_end"] is None
    assert r2["llm_end"] is not None, \
        "第二次 end() 之后收不到 yield 事件：handler 写进了孤儿列表"


def test_close_removes_log_hook(tmp_path, root_info):
    """回归：钩子不摘会让长期运行的进程里 handler 越积越多，且日志被反复解析。"""
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    t.close()
    t.begin(seg=1)
    _emit_yield()
    t.end()
    assert _records(p)[0]["llm_end"] is None


def test_non_matching_log_record_is_ignored(tmp_path, root_info):
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    try:
        t.begin(seg=1)
        _emit_yield("synthesis text 你好。")
        _emit_yield("yield speech len 3.0, rtf 1.0")
        t.end()
    finally:
        t.close()
    assert _records(p)[0]["llm_end"] is not None


def test_tqdm_progress_line_does_not_break_hook(tmp_path, root_info):
    """tqdm 往 stderr 写 `\\r  0%|…`，与日志混在一起；只要记录本身不含前缀就不该被采。"""
    p = tmp_path / "trace.jsonl"
    t = SynthTracer(True, p)
    try:
        t.begin(seg=1)
        _emit_yield("\r  0%|          | 0/1 [00:00<?, ?it/s]synthesis text 你好。")
        t.end()
    finally:
        t.close()
    assert _records(p)[0]["llm_end"] is None


# ------------------------------------------------------------ 失败降级
def test_write_failure_degrades_without_raising(tmp_path):
    """插桩把成片搞挂，比不插桩严重得多：写不进去只能告警，绝不外抛。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    t = SynthTracer(True, blocker / "trace.jsonl")
    t.begin(seg=1)
    t.mark("s0_enter")
    t.end()                                   # 不得抛
    assert t.stats["failed"] == 1
    assert t.stats["written"] == 0


# ------------------------------------------------------------ 配置接线
def test_make_tracer_reads_settings_flag_and_path(tmp_path):
    off = make_tracer(Settings(synth_trace=False), path=tmp_path / "a.jsonl")
    assert off.enabled is False

    on = make_tracer(Settings(synth_trace=True), path=tmp_path / "a.jsonl")
    assert on.enabled is True and on.path == tmp_path / "a.jsonl"


def test_make_tracer_falls_back_to_settings_path():
    t = make_tracer(Settings(synth_trace=True))
    assert t.enabled is True
    assert t.path is not None and t.path.name == "synth_trace.jsonl"


def test_make_tracer_explicit_path_overrides_settings(tmp_path):
    """显式传 path 时以它为准（测试与临时诊断用），不受 .env 影响。"""
    p = tmp_path / "override.jsonl"
    t = make_tracer(Settings(synth_trace=True, synth_trace_path="outputs/_ignored.jsonl"),
                    path=p)
    assert t.enabled is True and t.path == p


def test_settings_default_is_off():
    """默认必须关。开启会带来逐段文件 IO，污染压测耗时口径。"""
    assert Settings().synth_trace is False


def test_engine_wires_tracer(tmp_path):
    """TTSEngine 必须持有 tracer；默认关闭（不加载模型）。"""
    eng = TTSEngine(Settings(synth_trace=False))
    assert eng.tracer.enabled is False

    eng2 = TTSEngine(Settings(synth_trace=True, synth_trace_path="outputs/_t.jsonl"))
    assert eng2.tracer.enabled is True


def test_tracer_module_exposes_yield_prefix():
    assert st.YIELD_LEN_PREFIX == "yield speech len"
