# -*- coding: utf-8 -*-
"""`scripts/make_mos_pack.py` 的单元测试。

MOS 包是交给**外部评测人**的交付物：主题与序号一旦错位，回收来的分数就没有意义，
而且无法事后补救（评测人已经听完）。所以这里重点守三件事：
  1. `plan_clip` 的片段规划（短成片不能裁出空片段——那会产出静音，被误读成「系统没声音」）；
  2. `pick_cases` 的筛选契约（只能收录 DONE 且有产物的 case，编号筛不到要当场报错）；
  3. 评分表与说明的**预填列不能错行**（行序 = 包内提示音序号）。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import make_listen_pack as lp      # noqa: E402
import make_mos_pack as mm         # noqa: E402


# ------------------------------------------------------------ 提示音自检判据
# MOS 自检靠「数提示音个数 = 序号」来验证包内项序，判据一旦失真，
# 要么误报（把好包判 FAIL），要么漏报（项序错了也看不出来）。

def _timeline(n_beeps: int, clip_peak: float, clip_len_s: float = 0.05):
    """复刻造包时间轴的一小段：前置静音 + n 声提示音 + 正文片段。"""
    beeps, lead = lp.make_beep_group(n_beeps)
    rng = np.random.default_rng(0)
    clip = rng.standard_normal(int(clip_len_s * mm.SR)) * clip_peak
    return np.concatenate([beeps, clip]), lead


def test_count_beeps_selfref_counts_beeps():
    for n in (1, 2, 4, 7, 12):
        y, lead = _timeline(n, clip_peak=0.0)
        span = n * lp.BEEP_DUR + max(0, n - 1) * lp.BEEP_GAP + 0.03
        w = y[lead:lead + int(span * mm.SR)]
        assert mm._count_beeps_selfref(w) == n, f"n={n}"


def test_count_beeps_selfref_ignores_loud_clip_onset():
    """回归：尾部混进正文时不能多数一声（实测 item4 数出 5、item6 数出 7）。

    真因两条叠加：
      1. 旧窗口 `i*(BEEP_DUR+BEEP_GAP)+0.05` 恰好比提示音组**多出 0.05 s**，
         而片段从第 20 s 切入、常常切在词中间 —— 于是窗口尾部探进了一个响亮的音节；
      2. 旧判据用「窗口局部最大值×0.3」做阈值，而提示音比正文**轻**
         （正文峰值 ~-2 dBFS），基准被语音顶高后判据失真。
    本测试同时锁住「窗口不再外溢」与「阈值改用第一声提示音自身电平」。
    """
    n = 4
    y, lead = _timeline(n, clip_peak=0.6)          # 正文峰值远高于提示音
    beeps_len = int((n * lp.BEEP_DUR + max(0, n - 1) * lp.BEEP_GAP) * mm.SR)
    assert lead + beeps_len <= len(y)

    old_span = int((n * (lp.BEEP_DUR + lp.BEEP_GAP) + 0.05) * mm.SR)
    assert lead + old_span > lead + beeps_len, "旧窗口应确实外溢进正文（本回归的前提）"

    span = n * lp.BEEP_DUR + max(0, n - 1) * lp.BEEP_GAP + 0.03
    w = y[lead:lead + int(span * mm.SR)]
    assert mm._count_beeps_selfref(w) == n

    # 旧判据（相对窗口最大值）在这种输入上确实会多数一声 —— 说明这条回归有意义
    assert lp.count_bursts(y[lead:lead + old_span]) != n


# ------------------------------------------------------------ 片段规划

def test_plan_clip_normal_case():
    assert mm.plan_clip(120.0, offset=20.0, seg=45.0) == (20.0, 45.0)


def test_plan_clip_seg_zero_means_whole_tail():
    assert mm.plan_clip(120.0, offset=20.0, seg=0.0) == (20.0, None)
    assert mm.plan_clip(120.0, offset=0.0, seg=0.0) == (0.0, None)


def test_plan_clip_clamps_start_for_short_audio():
    """成片只有 50 s：offset=20 + seg=45 放不下，起点要回退到 5 s，长度恰好 45 s。"""
    start, length = mm.plan_clip(50.0, offset=20.0, seg=45.0)
    assert start == pytest.approx(5.0)
    assert length == pytest.approx(45.0)
    assert start + length <= 50.0 + 1e-9


def test_plan_clip_audio_shorter_than_segment():
    start, length = mm.plan_clip(8.0, offset=20.0, seg=45.0)
    assert start == 0.0
    assert length == pytest.approx(8.0)


def test_plan_clip_unknown_duration_passes_offset_through():
    """时长未知（results.json 缺字段）时不做夹取，交给 ffmpeg 读到结尾。"""
    assert mm.plan_clip(None, offset=20.0, seg=45.0) == (20.0, 45.0)
    assert mm.plan_clip(None, offset=20.0, seg=0.0) == (0.0, None)


def test_plan_clip_offset_beyond_end_does_not_go_negative():
    assert mm.plan_clip(30.0, offset=90.0, seg=0.0) == (30.0, None)


# ------------------------------------------------------------ 选例

def _results(cases) -> dict:
    return {"meta": {}, "summary": {}, "cases": cases}


def test_pick_cases_filters_done_and_missing_audio():
    rows = [
        {"id": "K1", "status": "DONE", "audio_path": "a.mp3"},
        {"id": "K2", "status": "FAILED", "audio_path": "b.mp3"},
        {"id": "K3", "status": "DONE", "audio_path": ""},
        {"id": "K4", "status": "DONE"},
    ]
    got = mm.pick_cases(_results(rows))
    assert [c["id"] for c in got] == ["K1"]


def test_pick_cases_ids_and_limit():
    rows = [{"id": i, "status": "DONE", "audio_path": f"{i}.mp3"}
            for i in ("K1", "K2", "K3", "K4")]
    assert [c["id"] for c in mm.pick_cases(_results(rows), ids=["K3", "K1"])] \
        == ["K1", "K3"]                                   # 保持原顺序，不按输入顺序
    assert [c["id"] for c in mm.pick_cases(_results(rows), limit=2)] == ["K1", "K2"]


def test_pick_cases_unknown_id_exits():
    rows = [{"id": "K1", "status": "DONE", "audio_path": "a.mp3"}]
    with pytest.raises(SystemExit):
        mm.pick_cases(_results(rows), ids=["K9"])


def test_pick_cases_empty_exits():
    with pytest.raises(SystemExit):
        mm.pick_cases(_results([]))


# ------------------------------------------------------------ 评分表

def _rows(n: int = 3) -> list[dict]:
    return [{"id": f"K{i}", "category": "知识科普", "topic": f"主题{i}",
             "clip": f"20–65s（45.0s）"} for i in range(1, n + 1)]


def test_scoresheet_header_and_row_count():
    text = mm.render_scoresheet(_rows(3))
    lines = text.lstrip("\ufeff").splitlines()
    assert len(lines) == 4                                     # 表头 + 3 行
    assert lines[0].count(",") == len(mm.SCORESHEET_COLUMNS) - 1
    assert "自然度(1-5)" in lines[0] and "多音字问题(有/无)" in lines[0]


def test_scoresheet_prefills_identity_and_leaves_scores_blank():
    lines = mm.render_scoresheet(_rows(2)).lstrip("\ufeff").splitlines()
    first = lines[1].split(",")
    assert first[0] == "1" and first[1] == "K1" and first[3] == "主题1"
    # 评分列留空（空单元格在 Excel 里可见；填 0 会被误当成「已评 0 分」）
    assert first[5] == "" and first[6] == "" and first[7] == ""
    assert lines[2].split(",")[0] == "2"


# ------------------------------------------------------------ 说明

def test_readme_contains_scale_flow_and_item_table():
    meta = {"pack_name": "mos_pack.mp3", "generated_at": "2026-09-17T18:00:00",
            "eval_dir": "outputs/eval/x", "clip_policy": "offset=20s, seg=45s",
            "target_lufs": -16.0, "ceiling_dbfs": -1.5, "seg_desc": "45 秒"}
    text = mm.render_readme(meta, _rows(2))
    assert "mos_pack.mp3" in text
    for score, _ in mm.SCALE_ROWS:
        assert f"| {score} |" in text
    assert "≥ 3.5" in text                                     # 验收线必须写明
    assert "| 1 | K1 | 知识科普 | 主题1 |" in text
    assert "| 2 | K2 | 知识科普 | 主题2 |" in text
    # 电平可能低于成片（静态增益受天花板约束）——必须提前说明，否则会被误当成音质问题
    assert "静态增益受峰值天花板约束" in text


def test_readme_marks_untrimmed_policy():
    meta = {"pack_name": "p.mp3", "generated_at": "t", "eval_dir": "e",
            "clip_policy": "offset=0s, seg=0s", "target_lufs": -16.0,
            "ceiling_dbfs": -1.5, "seg_desc": "整条正片"}
    assert "整条正片" in mm.render_readme(meta, _rows(1))


# ------------------------------------------------------------ 与 eval_batch 的衔接

def test_reuses_listen_pack_helpers():
    """复用而非重写：提示音结构与可达目标推算必须是同一份实现（否则两套数字对不上）。"""
    import make_listen_pack as lp
    assert mm.lp is lp
    assert mm.SR == lp.SR
    assert mm.plan_clip.__module__ == "make_mos_pack"    # 只有片段规划是本脚本自己的


def test_defaults_match_delivery_loudness():
    """MOS 包的目标必须等于成片交付电平，否则评测人听到的不是交付物。"""
    from api.config import Settings
    s = Settings()
    assert mm.DEFAULT_TARGET_LUFS == s.audio_loudness_i
    assert mm.DEFAULT_CEILING_DBFS == s.audio_true_peak


def test_manifest_fields_are_json_serialisable(tmp_path):
    """台账会被写进 results 目录供复核，必须是纯 JSON 可序列化结构。"""
    manifest = {"generated_at": "t", "items": [
        {"序号": 1, "编号": "K1", "施加静态增益dB": -0.25, "受限项": "响度"}]}
    p = tmp_path / "m.json"
    p.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    assert json.loads(p.read_text(encoding="utf-8"))["items"][0]["编号"] == "K1"
