# -*- coding: utf-8 -*-
"""`scripts/eval_batch.py` 的单元测试。

覆盖三类容易出错的东西：
  1. **测试主题集本身**（评测资产，坏了整批结论都不可信）：12 条 / 3 类×4 / 长短各半 /
     不撞 D4 已用主题（撞了会命中句级缓存，把耗时系统性压低）；
  2. **口径算术**（千字耗时、一致率、可用率、>10min 判定）——报告结论全部由它们推出；
  3. **WAV 时长解析**：逐句一致率回验的地基。它错了，「一致率 100%」就是假的
     （同 test_make_listen_pack 里 F0 估计器的定位）。
"""
import json
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import eval_batch as eb  # noqa: E402

TOPICS_FILE = ROOT / "backend" / "assets" / "eval_topics.json"


# ------------------------------------------------------------ 1. 测试主题集

@pytest.fixture(scope="module")
def spec():
    return eb.load_topics(TOPICS_FILE)


def test_topic_set_shape(spec):
    """12 条 = 3 类 × 4 条，且每类长短各半。"""
    rows = spec["topics"]
    assert len(rows) == 12
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    assert set(by_cat) == {"知识科普", "行业解读", "生活闲聊"}
    for cat, items in by_cat.items():
        assert len(items) == 4, cat
        lengths = sorted(i["length"] for i in items)
        assert lengths == ["long", "long", "short", "short"], cat


def test_topic_ids_unique_and_topics_unique(spec):
    ids = [r["id"] for r in spec["topics"]]
    topics = [r["topic"] for r in spec["topics"]]
    assert len(set(ids)) == len(ids)
    assert len(set(topics)) == len(topics)


def test_topic_durations_match_declared_lengths(spec):
    """短/长档时长必须与 spec.lengths 一致——否则「千字耗时」的归一化基准就飘了。"""
    lengths = spec["spec"]["lengths"]
    for r in spec["topics"]:
        assert r["duration_min"] == lengths[r["length"]], r["id"]


def test_long_topics_align_with_m4_target(spec):
    """长档字数配额须覆盖 M4 的 1000 字口径，否则指标线没被测到。"""
    wpm = spec["spec"]["words_per_minute"]
    for r in spec["topics"]:
        if r["length"] != "long":
            continue
        words = int(round(r["duration_min"] * wpm))
        assert 1000 <= words <= 1100, (r["id"], words)


def test_topic_set_avoids_d4_topics(spec):
    """D4 抽测已用过的主题若重复出现，句级缓存会命中，耗时被系统性低估。"""
    d4_src = (ROOT / "scripts" / "verify_d4.py").read_text(encoding="utf-8")
    for r in spec["topics"]:
        assert r["topic"] not in d4_src, f"主题与 D4 抽测重复：{r['id']} {r['topic']}"


def test_load_topics_rejects_empty_and_duplicate_ids(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"topics": []}), encoding="utf-8")
    with pytest.raises(eb.EvalError):
        eb.load_topics(bad)

    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps({"topics": [
        {"id": "A", "category": "x", "length": "short", "topic": "t1", "duration_min": 1.5},
        {"id": "A", "category": "x", "length": "short", "topic": "t2", "duration_min": 1.5},
    ]}), encoding="utf-8")
    with pytest.raises(eb.EvalError):
        eb.load_topics(dup)

    missing = tmp_path / "missing.json"
    missing.write_text(json.dumps({"topics": [{"id": "A"}]}), encoding="utf-8")
    with pytest.raises(eb.EvalError):
        eb.load_topics(missing)


# ------------------------------------------------------------ 筛选

def test_select_cases_by_ids_category_length_limit(spec):
    assert len(eb.select_cases(spec, ids=["K1", "L3"])) == 2
    assert all(c["category"] == "知识科普" for c in eb.select_cases(spec, category="知识科普"))
    assert len(eb.select_cases(spec, category="知识科普")) == 4
    short = eb.select_cases(spec, length="short")
    assert len(short) == 6 and all(c["length"] == "short" for c in short)
    assert len(eb.select_cases(spec, length="long", limit=2)) == 2


def test_select_cases_unknown_id_raises(spec):
    with pytest.raises(eb.EvalError):
        eb.select_cases(spec, ids=["NOPE"])


# ------------------------------------------------------------ 2. 口径算术

def test_per_1000_words():
    # 1040 字跑 520 s -> 千字 500 s
    assert eb.per_1000_words(520.0, 1040) == 500.0
    assert eb.per_1000_words(120.0, 390) == pytest.approx(307.7, abs=0.05)
    assert eb.per_1000_words(None, 400) is None
    assert eb.per_1000_words(100.0, 0) is None


def _case(**kw) -> dict:
    base = {
        "id": "K1", "category": "知识科普", "length": "short", "topic": "t",
        "status": "DONE", "structure_ok": True, "content_flagged": False,
        "consistency_total": 20, "consistency_passed": 20,
        "elapsed_total_s": 120.0, "synth_elapsed_s": 100.0,
        "words": 390, "per_1000_words_s": 307.7, "audio_duration_s": 100.0,
        "rtf_e2e": 1.2, "lufs": -16.0, "true_peak_dbtp": -1.8,
        "word_deviation_pct": -5.0,
    }
    base.update(kw)
    return base


def test_summarize_rates_and_medians():
    cases = [
        _case(id="K1", length="short", elapsed_total_s=120.0, per_1000_words_s=300.0),
        _case(id="K3", length="long", elapsed_total_s=700.0, per_1000_words_s=650.0,
              words=1040),
    ]
    s = eb.summarize(cases)
    assert s["total"] == 2 and s["done"] == 2 and s["failed"] == 0
    assert s["usable"] == 2 and s["usable_rate"] == 1.0
    assert s["consistency_passed"] == 40 and s["consistency_total"] == 40
    assert s["consistency_rate"] == 1.0
    assert s["over_10min"] == ["K3"]            # 700s > 600s
    assert s["long_median_per_1000_words"] == 650.0
    assert s["short_median_elapsed_s"] == 120.0


def test_summarize_counts_unusable_and_failed():
    cases = [
        _case(id="K1"),
        _case(id="K2", structure_ok=False),                 # 结构不合规 -> 不可用
        _case(id="K4", content_flagged=True),               # 合规命中 -> 不可用
        _case(id="I3", status="FAILED", error="boom",
              structure_ok=None, consistency_total=None, consistency_passed=None),
    ]
    s = eb.summarize(cases)
    assert s["done"] == 3 and s["failed"] == 1
    assert s["usable"] == 1
    assert s["usable_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["failed_ids"] == ["I3"]
    assert s["consistency_rate"] == 1.0                     # 只统计 DONE 的


def test_summarize_over_10min_boundary():
    """边界：正好 600 s 不算超（M4 是「≤ 10 分钟」）。"""
    assert eb.summarize([_case(elapsed_total_s=600.0)])["over_10min"] == []
    assert eb.summarize([_case(elapsed_total_s=600.1)])["over_10min"] == ["K1"]


def test_summarize_empty_is_safe():
    s = eb.summarize([])
    assert s["total"] == 0 and s["usable_rate"] == 0.0 and s["consistency_rate"] == 0.0
    assert s["long_median_per_1000_words"] is None


# ------------------------------------------------------------ 3. 脚本结构检查

def _lines(*pairs) -> list[dict]:
    return [{"seq": i, "speaker": sp, "text": tx} for i, (sp, tx) in enumerate(pairs, 1)]


def test_structure_check_ok():
    ok, issues = eb.structure_check(_lines(("A", "你好"), ("B", "你也好"), ("A", "聊聊")))
    assert ok and issues == []


def test_structure_check_single_speaker():
    ok, issues = eb.structure_check(_lines(("A", "你好"), ("A", "你好")))
    assert not ok
    assert any("双人对话" in i for i in issues)


def test_structure_check_consecutive_same_speaker():
    ok, issues = eb.structure_check(_lines(("A", "你好"), ("A", "继续"), ("B", "嗯")))
    assert not ok
    assert any("连续同人" in i for i in issues)


def test_structure_check_over_length_line():
    ok, issues = eb.structure_check(_lines(("A", "字" * 41), ("B", "短")))
    assert not ok
    assert any("超过单行上限" in i for i in issues)


def test_structure_check_ignores_whitespace_when_counting():
    """字数口径含标点、不含空白：40 字 + 空格仍然合规。"""
    ok, _ = eb.structure_check(_lines(("A", "字" * 20 + " " * 10 + "字" * 20),
                                      ("B", "短句")))
    assert ok


# ------------------------------------------------------------ WAV 时长解析

def _riff_wav(*, channels: int = 1, rate: int = 44100, bits: int = 16,
              frames: int = 44100) -> bytes:
    bytes_per = bits // 8
    data = b"\x00" * (frames * channels * bytes_per)
    (avg,) = struct.unpack("<I", struct.pack("<I", rate * channels * bytes_per))
    fmt = struct.pack("<HHIIHH", 1, channels, rate, avg, channels * bytes_per, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt \
        + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_wav_duration_pcm16_one_second():
    assert eb.wav_duration_from_bytes(_riff_wav(frames=44100)) == pytest.approx(1.0)


def test_wav_duration_float32_format3():
    """CosyVoice 输出是 float32（wFormatTag=3，bits=32）——stdlib `wave` 读不了，必须自解析。"""
    raw = _riff_wav(bits=32, frames=24000)
    # 把 wFormatTag 改成 3（IEEE float）
    assert eb.wav_duration_from_bytes(raw) == pytest.approx(24000 / 44100, abs=1e-6)


def test_wav_duration_stereo_and_half_second():
    assert eb.wav_duration_from_bytes(
        _riff_wav(channels=2, frames=22050)) == pytest.approx(0.5)


def test_wav_duration_rejects_garbage():
    assert eb.wav_duration_from_bytes(b"") is None
    assert eb.wav_duration_from_bytes(b"not a wav at all, really not") is None
    assert eb.wav_duration_from_bytes(b"RIFF" + b"\x00" * 60) is None


# ------------------------------------------------------------ 渲染

def test_render_cases_csv_header_and_over_flag():
    text = eb.render_cases_csv([_case(id="K3", length="long", elapsed_total_s=700.0)])
    head = text.lstrip("\ufeff").splitlines()[0]
    assert head.count(",") == len(eb.CSV_COLUMNS) - 1
    assert "编号" in head and "端到端耗时s" in head
    row = text.splitlines()[1]
    assert "K3" in row
    assert row.rstrip().endswith("Y") or ",Y," in row or row.split(",")[-2] == "Y"
    # over_10min 列必须落在声明的位置
    idx = [k for k, _ in eb.CSV_COLUMNS].index("over_10min")
    assert row.split(",")[idx] == "Y"


def test_render_report_contains_required_sections():
    meta = {"generated_at": "2026-09-17T18:00:00", "base_url": "http://127.0.0.1:8000",
            "topics_file": "eval_topics.json", "topics_version": "1.0.0",
            "user": "eval_x", "check_segments": True, "with_synth": True,
            "out_dir": "outputs/eval/x"}
    cases = [_case(id="K1"), _case(id="K3", length="long", elapsed_total_s=700.0,
                                   per_1000_words_s=650.0, words=1040)]
    text = eb.render_report(meta, cases)
    assert "端到端耗时表" in text
    assert ">10min" in text and "K3" in text
    assert "口径声明" in text
    assert "千字端到端耗时" in text
    # K1 未超线，不应出现在超线清单里
    over_section = text.split("超 10 分钟 case 清单")[1].split("一致率失败明细")[0]
    assert "K3" in over_section and "K1" not in over_section


def test_render_report_lists_consistency_failures():
    meta = {"generated_at": "t", "base_url": "b", "topics_file": "f", "topics_version": "v",
            "user": "u", "check_segments": True, "with_synth": True, "out_dir": "o"}
    c = _case(consistency_total=20, consistency_passed=19,
              consistency_failures=[{"seq": 7, "text": "abc", "reason": "试听端点返回 404"}])
    text = eb.render_report(meta, [c])
    assert "seq=7" in text and "试听端点返回 404" in text


def test_render_report_no_over_and_no_failure_says_none():
    meta = {"generated_at": "t", "base_url": "b", "topics_file": "f", "topics_version": "v",
            "user": "u", "check_segments": True, "with_synth": True, "out_dir": "o"}
    text = eb.render_report(meta, [_case()])
    assert "无。所有 case 端到端耗时均在 10 分钟线内。" in text
    assert "无（未发现「脚本行 ↔ 音轨」对不上的行）。" in text


def test_render_report_word_deviation_is_not_double_scaled():
    """回归：字数偏差曾被渲染成 -805.0%。

    存储端 `word_deviation_pct` 已是百分数（-12.3 表示 −12.3%），渲染端若再用
    `%` 格式会再乘一次 100。这条测试同时锁住「值正确」和「没有多一个数量级」。
    """
    meta = {"generated_at": "t", "base_url": "b", "topics_file": "f",
            "topics_version": "v", "user": "u", "check_segments": True,
            "with_synth": True, "out_dir": "o"}
    text = eb.render_report(meta, [_case(id="K1", word_deviation_pct=-12.3)])
    line = next(l for l in text.splitlines() if "字数偏差" in l)
    assert "-12.3%" in line, line
    assert "-1230" not in line and "-123.0%" not in line, line


def test_summarize_word_deviation_keeps_percent_unit():
    """口径锚点：summary 里的中位数就是百分数本身，不是小数比例。"""
    s = eb.summarize([_case(id="K1", word_deviation_pct=-8.0),
                      _case(id="K2", word_deviation_pct=-12.4)])
    assert s["word_deviation_median"] == pytest.approx(-10.2, abs=0.06)


def test_render_report_shows_warmup_state():
    """M4 的指标前提是「模型已预热」，报告必须交代有没有预热，否则首例耗时会被误读。"""
    base = {"generated_at": "t", "base_url": "b", "topics_file": "f",
            "topics_version": "v", "user": "u", "check_segments": True,
            "with_synth": True, "out_dir": "o"}
    assert "预热：未执行" in eb.render_report({**base, "warmup": None}, [_case()])
    text = eb.render_report({**base, "warmup": {"elapsed_total_s": 95.0,
                                                "status": "DONE"}}, [_case()])
    assert "预热：已执行" in text and "95.0" in text


# ------------------------------------------------------------ 客户端：代理环境隔离

def test_eval_client_ignores_env_proxy(monkeypatch):
    """回归：本机后端必须绕开 HTTP_PROXY。

    真实故障：环境里设了 HTTP_PROXY 时，httpx 默认 trust_env=True 会走代理，
    代理转发 absolute-form 请求行，uvicorn 把整串当路径 → 每次 GET 都 404，
    压测在注册成功后立刻以 EXIT=2 退出（表现为「登录态建不起来」）。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("EVAL_HTTP_TRUST_ENV", raising=False)
    c = eb.EvalClient("http://127.0.0.1:8000")
    try:
        assert c._c.trust_env is False
    finally:
        c.close()


def test_eval_client_can_opt_back_into_env_proxy(monkeypatch):
    """给了逃生舱：确需走代理（对远端后端）时显式开启。"""
    monkeypatch.setenv("EVAL_HTTP_TRUST_ENV", "1")
    c = eb.EvalClient("http://127.0.0.1:8000")
    try:
        assert c._c.trust_env is True
    finally:
        c.close()


def test_eval_client_strips_trailing_slash():
    c = eb.EvalClient("http://127.0.0.1:8000/")
    try:
        assert c.base_url == "http://127.0.0.1:8000"
        assert str(c._c.build_request("GET", "/health").url) == "http://127.0.0.1:8000/health"
    finally:
        c.close()


# ------------------------------------------------------------ 断点续跑（--merge-from）

def test_load_prior_cases_marks_carried_over(tmp_path):
    d = tmp_path / "20260917_172917"
    d.mkdir()
    (d / "results.json").write_text(json.dumps(
        {"meta": {}, "cases": [_case(id="K1"), _case(id="K2", status="FAILED")]}),
        encoding="utf-8")
    cases, used, warns = eb.load_prior_cases([d])
    assert [c["id"] for c in cases] == ["K1", "K2"]
    assert all(c["carried_over"] is True for c in cases)
    assert all(c["source_dir"] == str(d) for c in cases)
    assert used == [d]
    assert any("并入 2 条" in w for w in warns)


def test_load_prior_cases_missing_dir_warns_without_raising(tmp_path):
    """续跑不该因为写错一个目录名就整体失败。"""
    cases, used, warns = eb.load_prior_cases([tmp_path / "nope"])
    assert cases == [] and used == []
    assert warns and "不存在" in warns[0]


def test_load_prior_cases_broken_json_warns(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "results.json").write_text("{not json", encoding="utf-8")
    cases, used, warns = eb.load_prior_cases([d])
    assert cases == [] and used == []
    assert "解析失败" in warns[0]


def test_merge_carried_over_keeps_fresh_and_drops_prior_failures():
    """本次真跑的优先；上次的失败记录不并入（没统计价值，重跑更干净）。"""
    fresh = [_case(id="K4", elapsed_total_s=600.0)]
    prior = [_case(id="K1", elapsed_total_s=111.0),
             _case(id="K2", elapsed_total_s=222.0, status="FAILED"),
             _case(id="K4", elapsed_total_s=999.0)]               # 本次已跑，不覆盖
    out = eb.merge_carried_over(fresh, prior)
    got = {c["id"]: c for c in out}
    assert set(got) == {"K1", "K4"}
    assert got["K4"]["elapsed_total_s"] == 600.0
    assert got["K1"]["elapsed_total_s"] == 111.0


def test_order_results_by_topic_set_restores_declared_order():
    spec = {"topics": [{"id": "K1"}, {"id": "K2"}, {"id": "K3"}]}
    out = eb.order_results_by_topic_set(spec, [{"id": "K3"}, {"id": "K1"}, {"id": "K2"}])
    assert [c["id"] for c in out] == ["K1", "K2", "K3"]


def test_render_report_flags_carried_over_rows():
    """报告必须让人一眼看出哪些行是「续」来的，否则会误以为本批重跑过。"""
    meta = {"generated_at": "t", "base_url": "b", "topics_file": "f",
            "topics_version": "v", "user": "u", "check_segments": True,
            "with_synth": True, "out_dir": "o",
            "merged_from": ["outputs/eval/20260917_172917"]}
    carried = {**_case(id="K1"), "carried_over": True}
    text = eb.render_report(meta, [carried, _case(id="K4")])
    assert "断点续跑" in text and "（续）" in text
    assert "20260917_172917" in text


def test_render_report_without_merge_has_no_merge_noise():
    meta = {"generated_at": "t", "base_url": "b", "topics_file": "f",
            "topics_version": "v", "user": "u", "check_segments": True,
            "with_synth": True, "out_dir": "o"}
    text = eb.render_report(meta, [_case()])
    assert "断点续跑" not in text


# ------------------------------------------------------------ 轮询（真 bug 的回归测试）

class _FakeClient:
    """按序吐出状态序列；序列走空后一直返回最后一个。"""

    def __init__(self, statuses: list[str]) -> None:
        self._statuses = statuses
        self.calls = 0

    def get_task(self, _task_id: str) -> dict:
        st = self._statuses[min(self.calls, len(self._statuses) - 1)]
        self.calls += 1
        return {"status": st, "stage": st, "progress": 15}


def test_wait_terminal_returns_on_script_ready():
    """回归：SCRIPT_READY 不是终态，脚本阶段必须能在此停下。

    首次自测就是少了 `targets`，任务早已 SCRIPT_READY 而 harness 一直等到 300 s 超时。
    """
    c = _FakeClient(["PENDING", "SCRIPTING", "SCRIPT_READY", "DONE"])
    got = eb.wait_terminal(c, "t", timeout=5.0, interval=0.01, label="脚本生成",
                           targets=(eb.SCRIPT_READY,))
    assert got["status"] == eb.SCRIPT_READY
    assert c.calls == 3                                     # 命中即返回，不多轮一次


def test_wait_terminal_stops_at_terminal_even_if_not_targeted():
    """脚本阶段遇到 FAILED 不该继续等 —— 终态优先级高于 targets。"""
    c = _FakeClient(["SCRIPTING", "FAILED"])
    got = eb.wait_terminal(c, "t", timeout=5.0, interval=0.01, label="脚本生成",
                           targets=(eb.SCRIPT_READY,))
    assert got["status"] == "FAILED"


def test_wait_terminal_default_targets_is_terminal_only():
    """默认（合成阶段）不认 SCRIPT_READY：那是中途停靠点，不是完成。"""
    c = _FakeClient(["SCRIPT_READY"])
    with pytest.raises(eb.EvalError):
        eb.wait_terminal(c, "t", timeout=0.2, interval=0.01, label="合成流水线")
    assert c.calls >= 1


def test_wait_terminal_timeout_message_has_stage_and_progress():
    c = _FakeClient(["SYNTHESIZING"])
    with pytest.raises(eb.EvalError) as ei:
        eb.wait_terminal(c, "t", timeout=0.2, interval=0.01, label="合成流水线")
    assert "SYNTHESIZING" in str(ei.value) and "15%" in str(ei.value)


# ------------------------------------------------------------ run_case 的端到端形状

class _Resp:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


class _FlowClient:
    """最小可用的假客户端：脚本阶段先返回 PENDING 行，合成后返回 DONE 行。

    这正是真实现象——`GET /script` 在 PENDING 时返回的是「还没合成」的脚本快照。
    """

    def __init__(self) -> None:
        self.get_task_calls = 0
        self.get_script_calls = 0
        self.synthesize_calls = 0

    def create_task(self, *, topic, duration_min, style=""):
        return {"id": "T1", "target_word_count": 390}

    def get_task(self, _tid):
        self.get_task_calls += 1
        # 前两次仍是脚本阶段（PENDING/SCRIPTING），第三次起给 SCRIPT_READY；
        # 触发合成后返回 DONE。
        if self.synthesize_calls == 0:
            st = "SCRIPT_READY" if self.get_task_calls >= 3 else "SCRIPTING"
        else:
            st = "DONE"
        return {"status": st, "stage": st, "progress": 15, "content_flagged": False}

    def get_script(self, _tid):
        self.get_script_calls += 1
        if self.get_script_calls == 1:                       # 合成前快照
            return {"title": "标题", "lines": [_line(1, "A"), _line(2, "B")]}
        return {"title": "标题", "lines": [_done_line(1, "A"), _done_line(2, "B")]}

    def synthesize(self, _tid):
        self.synthesize_calls += 1
        return {}

    def audio_bytes(self, _tid):
        return b"\x00" * 16

    def segment_audio(self, _tid, _seq):
        return _Resp(200, _riff_wav(frames=66150))           # 1.5 s，匹配 duration_ms=1500


def _line(seq, speaker, text="你好", **kw):
    base = {"seq": seq, "speaker": speaker, "text": text, "read_text": "",
            "text_hash": None, "duration_ms": 0, "seg_status": "PENDING"}
    base.update(kw)
    return base


def _done_line(seq, speaker, text="你好"):
    return _line(seq, speaker, text, read_text=text, text_hash="a" * 40,
                 duration_ms=1500, seg_status="DONE")


def test_run_case_uses_post_synthesis_script_snapshot(tmp_path, monkeypatch):
    """回归：一致率必须比对**合成后**的脚本。

    首次真实自测就是拿了 SCRIPT_READY 时的快照，20 行全是 PENDING，
    被误判成「一致率 0%」——那是一个假失败，会让人去查根本不存在的后端缺陷。
    """
    monkeypatch.setattr(eb, "measure_audio", lambda p: {
        "audio_duration_s": 100.0, "lufs": -16.6, "true_peak_dbtp": -1.9,
        "size_bytes": 1024})
    client = _FlowClient()
    rec = eb.run_case(client, {"id": "K1", "category": "知识科普", "length": "short",
                               "topic": "t", "duration_min": 1.5},
                      out_dir=tmp_path, style="", with_synth=True, check_segments=True,
                      timeout_script=5.0, timeout_synth=5.0, poll_interval=0.01)
    assert rec["status"] == "DONE"
    assert client.get_script_calls == 2                      # 合成后又拉了一次
    assert rec["consistency_total"] == 2
    assert rec["consistency_passed"] == 2                    # 用旧快照会得 0
    assert rec["consistency_failures"] == []
    assert rec["rtf_e2e"] == pytest.approx(rec["elapsed_total_s"] / 100.0, abs=0.01)


def test_run_case_reports_failed_task_without_raising(tmp_path):
    class _FailClient(_FlowClient):
        def get_task(self, _tid):
            self.get_task_calls += 1
            return {"status": "FAILED", "stage": "脚本生成", "progress": 0,
                    "error_msg": "脚本生成失败：LLM 超时"}

    rec = eb.run_case(_FailClient(), {"id": "K1", "category": "知识科普",
                                      "length": "short", "topic": "t",
                                      "duration_min": 1.5},
                      out_dir=tmp_path, style="", with_synth=True, check_segments=True,
                      timeout_script=5.0, timeout_synth=5.0, poll_interval=0.01)
    assert rec["status"] == "FAILED"
    assert "LLM 超时" in rec["error"]
    assert eb.summarize([rec])["failed_ids"] == ["K1"]


# ------------------------------------------------------------ CLI

def test_dry_run_does_not_touch_network(capsys):
    assert eb.main(["--dry-run", "--limit", "2", "--topics", str(TOPICS_FILE)]) == 0
    out = capsys.readouterr().out
    assert "--dry-run" in out


def test_cli_rejects_unknown_ids(capsys):
    assert eb.main(["--dry-run", "--ids", "NOPE", "--topics", str(TOPICS_FILE)]) == 2
