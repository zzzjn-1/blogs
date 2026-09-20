# -*- coding: utf-8 -*-
"""synth_trace 分析器测试。

这些函数把「逐段阶段时间线」折算成人能读的结论（谁是大头、infer 在首个 yield
处怎么切、段间残余多少）。**算错一个分母，后面所有优化方向都会跟着错**，所以逐条钉死。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import analyze_synth_trace as at  # noqa: E402


def _rec(**kw):
    base = {"seg": 1, "t0": 100.0, "t1": 110.0, "dur_ms": 10000.0,
            "cached": False,
            "marks": [["s4_empty_cache_pre", 0.0], ["s5_infer", 5000.0]]}
    base.update(kw)
    return base


# ------------------------------------------------------------ 读取
def test_load_records_skips_bad_lines_and_empty_marks(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        json.dumps(_rec(seg=1)),
        "{ 坏 json",
        "",
        json.dumps({"seg": 2, "marks": []}),        # 无打点 → 丢
        json.dumps({"not": "a record"}),            # 无 marks → 丢
        json.dumps(_rec(seg=3)),
    ]), encoding="utf-8")
    recs = at.load_records(p)
    assert [r["seg"] for r in recs] == [1, 3]


def test_load_records_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        at.load_records(tmp_path / "nope.jsonl")


# ------------------------------------------------------------ 阶段折算
def test_stage_durations_uses_consecutive_deltas_not_absolute():
    """回归：打点是**绝对**单调时钟。若误当累计值用，阶段耗时会被算成天文数字。"""
    rec = _rec(marks=[["a", 0.0], ["b", 1000.0], ["c", 4000.0]])
    assert at.stage_durations(rec) == [("b", 1000.0), ("c", 3000.0), ("_tail", 6000.0)]


def test_interval_is_attributed_to_the_mark_that_ends_it():
    """回归：区间必须归给**结束**它的打点，不是起始打点。

    这是真踩过的坑：错位一格后 `empty_cache(pre) → infer` 那 4.9 s 被记到
    `empty_cache` 名下，报告显示「empty_cache 占 93%」—— 照它去关 empty_cache
    是白干，真正的解释是「infer 本体本来就占 93%」。
    """
    rec = _rec(marks=[["s3_tmpdir", 0.0], ["s4_empty_cache_pre", 30.0],
                      ["s5_infer", 4930.0]], dur_ms=4930.0)
    got = dict(at.stage_durations(rec))
    assert got["s4_empty_cache_pre"] == pytest.approx(30.0), "empty_cache 只该拿 30ms"
    assert got["s5_infer"] == pytest.approx(4900.0), "infer 必须拿到 4900ms"
    assert "s3_tmpdir" not in got, "起始打点不该得到任何区间"


def test_stage_durations_head_before_first_mark():
    """首打点之前到 t0 的部分必须显式记成 `_entry`，否则时间会凭空消失。"""
    rec = _rec(marks=[["a", 250.0], ["b", 1250.0]])
    assert at.stage_durations(rec) == [("_entry", 250.0), ("b", 1000.0),
                                       ("_tail", 8750.0)]


def test_stage_durations_total_equals_dur_ms():
    """守恒：折算出的各阶段之和必须等于该段总时长。对不上就说明折算错了。"""
    rec = _rec(marks=[["a", 0.0], ["b", 1000.0], ["c", 4000.0]])
    assert sum(ms for _, ms in at.stage_durations(rec)) == pytest.approx(rec["dur_ms"])


def test_stage_durations_single_mark_is_empty():
    """只有一个打点时无法构成区间（其余全归尾部），返回空而不是瞎猜。"""
    assert at.stage_durations(_rec(marks=[["a", 0.0]])) == []


def test_stage_durations_negative_delta_clamped():
    """时钟回退（理论不该有）不得产生负耗时把统计拉低。"""
    rec = _rec(marks=[["a", 5000.0], ["b", 4000.0]])
    assert all(ms >= 0 for _, ms in at.stage_durations(rec))


# ------------------------------------------------------------ 聚合
def test_aggregate_sums_and_shares():
    """rec1: b=2000 c=2000 _tail=6000；rec2: b=4000 c=4000 _tail=2000。"""
    recs = [_rec(marks=[["a", 0.0], ["b", 2000.0], ["c", 4000.0]]),
            _rec(marks=[["a", 0.0], ["b", 4000.0], ["c", 8000.0]])]
    rows = {r["stage"]: r for r in at.aggregate(recs)}
    assert rows["b"]["total_ms"] == pytest.approx(6000.0)   # 2000 + 4000
    assert rows["c"]["total_ms"] == pytest.approx(6000.0)   # 2000 + 4000
    assert rows["_tail"]["total_ms"] == pytest.approx(8000.0)  # 6000 + 2000
    assert sum(r["share_pct"] for r in at.aggregate(recs)) == pytest.approx(100.0, abs=0.5)


def test_aggregate_sorted_by_total_desc():
    """c 吃掉 10000ms，必须排第一。"""
    rec = _rec(marks=[["a", 0.0], ["b", 1000.0], ["c", 11000.0]], dur_ms=12000.0)
    rows = at.aggregate([rec])
    assert rows[0]["stage"] == "c"
    assert rows[0]["total_ms"] == pytest.approx(10000.0)
    assert rows[0]["share_pct"] == pytest.approx(83.3, abs=0.2)


def test_aggregate_empty_input():
    assert at.aggregate([]) == []


def test_aggregate_labels_known_stages():
    rows = at.aggregate([_rec()])
    assert rows[0]["label"] != rows[0]["stage"], "已知阶段应显示中文说明"
    assert any(r["stage"] == "s5_infer" for r in rows)
    assert any(r["label"] == "★ infer 本体（前端+LLM+flow+hift）" for r in rows)


# ------------------------------------------------------------ infer 在 yield 处切分
def test_split_at_yield_cuts_at_the_marker():
    """infer 区间 = s4 打点 → s5 打点；`llm_end`（= `yield speech len` 时刻）一刀两断。"""
    rec = _rec(marks=[["s4_empty_cache_pre", 0.0], ["s5_infer", 5000.0]],
               t0=100.0, llm_end=104.0)
    assert at.split_at_yield(rec) == (4000.0, 1000.0)


def test_split_at_yield_is_not_llm_vs_vocoder():
    """**回归：这个切分不能当成 LLM / 声码器占比。**

    `stream=False` 下 `CosyVoice2Model.tts` 是
    `p.join()` → `token2wav()`(flow+hift) → `yield`，而 `yield speech len` 在 yield
    **之后**才打 —— 声码器属于前段。曾经把这儿标成「LLM 出 token / 声码器 flow+hift」，
    照着它去优化声码器就是白干。标签里必须明确喊出「不区分」。
    """
    src = Path(at.__file__).read_text(encoding="utf-8")
    assert "不把 LLM 与声码器分开" in src or "不区分" in src
    text = at.render_report([_rec(llm_end=104.0)])
    assert "≠ LLM / 声码器占比" in text


def test_split_at_yield_none_without_llm_end():
    """回归：抓不到 `yield speech len` 时必须返回 None。

    用 0 冒充会让报告显示「前段不耗时」，把结论引到反方向。
    """
    assert at.split_at_yield(_rec(llm_end=None)) is None


def test_split_at_yield_none_when_marks_missing():
    rec = _rec(marks=[["a", 0.0], ["b", 1000.0]], llm_end=104.0)
    assert at.split_at_yield(rec) is None


def test_split_at_yield_none_on_inconsistent_clock():
    """llm_end 落在区间外（时钟异常）时不硬算。"""
    rec = _rec(marks=[["s4_empty_cache_pre", 0.0], ["s5_infer", 5000.0]],
               t0=100.0, llm_end=99.0)
    assert at.split_at_yield(rec) is None


# ------------------------------------------------------------ 段间残余
def test_inter_segment_gaps_basic():
    a = _rec(seg=1, t0=100.0, t1=110.0)
    b = _rec(seg=2, t0=110.5, t1=120.0)
    assert at.inter_segment_gaps([a, b]) == [500.0]


def test_inter_segment_gaps_drops_negative():
    a = _rec(t0=100.0, t1=110.0)
    b = _rec(t0=109.0, t1=120.0)
    assert at.inter_segment_gaps([a, b]) == []


def test_inter_segment_gaps_single_record():
    assert at.inter_segment_gaps([_rec()]) == []


# ------------------------------------------------------------ 渲染
def test_render_report_separates_cached(tmp_path):
    """缓存命中的段耗时口径完全不同（近零），必须排除在阶段统计外。"""
    text = at.render_report([_rec(seg=1, cached=False), _rec(seg=2, cached=True)])
    assert "缓存未命中 1" in text


def test_render_report_warns_when_no_llm_end():
    text = at.render_report([_rec(llm_end=None)])
    assert "yield speech len" in text and "INFO" in text


def test_render_report_includes_infer_split_when_available():
    text = at.render_report([_rec(llm_end=104.0)])
    assert "至首个 yield" in text and "yield 之后" in text


def test_render_report_empty_is_safe():
    text = at.render_report([])
    assert "段数：0" in text


def test_cli_without_args_returns_2(capsys):
    old = sys.argv
    sys.argv = ["analyze_synth_trace.py"]
    try:
        assert at.main() == 2
    finally:
        sys.argv = old
    assert "用法" in capsys.readouterr().out


def test_cli_missing_file_returns_1(tmp_path, capsys):
    old = sys.argv
    sys.argv = ["analyze_synth_trace.py", str(tmp_path / "nope.jsonl")]
    try:
        assert at.main() == 1
    finally:
        sys.argv = old
    assert "不存在" in capsys.readouterr().out
