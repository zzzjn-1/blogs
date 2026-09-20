# -*- coding: utf-8 -*-
"""D5 CLI 入口的离线单元测试。

只覆盖**不依赖 GPU / 网络**的部分：文件名规范化、外部脚本读取、参数与失败退出码。
真实端到端（主题 → mp3）由 `scripts/cli.py` 自身跑，见 D5 报告的验收章节。
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_cli():
    """scripts/ 不是包，按文件加载；模块级只做 sys.path 与 env 默认值设置。"""
    spec = importlib.util.spec_from_file_location(
        "podcast_cli_under_test", ROOT / "scripts" / "cli.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


# --------------------------------------------------------------------------- #
# 文件名规范化
# --------------------------------------------------------------------------- #

def test_slugify_keeps_cjk_and_strips_illegal_chars():
    assert cli.slugify("为什么天空是蓝色的") == "为什么天空是蓝色的"
    assert cli.slugify('AI/播客: "入门"') == "AI_播客_入门"


def test_slugify_normalizes_whitespace_and_punctuation_edges():
    assert cli.slugify("  a   b  ") == "a_b"
    assert cli.slugify("...topic...") == "topic"
    assert cli.slugify("a___b") == "a_b"


def test_slugify_limits_length_and_has_fallback():
    assert len(cli.slugify("字" * 200)) == 40
    assert cli.slugify("") == "podcast"
    assert cli.slugify("///") == "podcast", "全非法字符也要有兜底名"
    # 截断后不能留下尾随下划线（否则文件名很难看）
    assert not cli.slugify("字" * 39 + "_" + "字").endswith("_")


# --------------------------------------------------------------------------- #
# 外部脚本读取
# --------------------------------------------------------------------------- #

def _write(dirpath: Path, payload) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / "turns.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_turns_accepts_three_shapes(tmp_path):
    lines = [{"seq": 1, "speaker": "A", "text": "你好"},
             {"seq": 2, "speaker": "B", "text": "你也好"}]

    # ① 完整 {script:{lines:[]}}（D4 落盘的形态）
    turns, title = cli.load_turns_from_json(
        str(_write(tmp_path / "a", {"script": {"title": "标题甲", "lines": lines}})))
    assert [t["text"] for t in turns] == ["你好", "你也好"] and title == "标题甲"

    # ② 扁平 {lines:[]}
    turns, _ = cli.load_turns_from_json(str(_write(tmp_path / "b", {"lines": lines})))
    assert len(turns) == 2

    # ③ 直接数组
    turns, title = cli.load_turns_from_json(str(_write(tmp_path / "c", lines)))
    assert len(turns) == 2 and title == ""


def test_load_turns_normalizes_speaker_and_skips_blanks(tmp_path):
    p = _write(tmp_path / "d", [
        {"speaker": "a", "text": "小写会被转大写"},
        {"speaker": "  ", "text": "缺 speaker 时按序交替"},
        {"speaker": "B", "text": "   "},
        {"speaker": "B", "text": "空白行被跳过，序号因此顺延"},
    ])
    turns, _ = cli.load_turns_from_json(str(p))
    assert [t["speaker"] for t in turns] == ["A", "B", "B"], "空文本必须被过滤"
    assert turns[1]["text"] == "缺 speaker 时按序交替"


@pytest.mark.parametrize("payload,needle", [
    ({"lines": "not-a-list"}, "lines 应为数组"),
    ({"lines": ["just-a-string"]}, "不是对象"),
    ({"lines": []}, "没有任何有效对白"),
    (42, "结构无法识别"),
])
def test_load_turns_reports_bad_structures(tmp_path, payload, needle):
    p = _write(tmp_path / "bad", payload)
    with pytest.raises(SystemExit) as ei:
        cli.load_turns_from_json(str(p))
    assert needle in str(ei.value)


def test_load_turns_rejects_missing_file_and_bad_json(tmp_path):
    with pytest.raises(SystemExit) as ei:
        cli.load_turns_from_json(str(tmp_path / "nope.json"))
    assert "不存在" in str(ei.value)

    bad = tmp_path / "broken.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        cli.load_turns_from_json(str(bad))
    assert "不是合法 JSON" in str(ei.value)


# --------------------------------------------------------------------------- #
# 参数与退出码
# --------------------------------------------------------------------------- #

def test_no_tts_stops_after_script_stage(tmp_path):
    fixture = ROOT / "tests" / "fixtures" / "d5_turns_demo.json"
    rc = cli.main(["--turns-json", str(fixture), "--no-tts",
                   "--out", str(tmp_path), "--quiet"])
    assert rc == 0
    summary = json.loads((tmp_path / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["ok"] is True
    assert summary["stages"]["script"]["lines"] == 10
    assert "tts" not in summary["stages"], "--no-tts 不应进入合成阶段"


def test_missing_source_is_a_usage_error(tmp_path):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--out", str(tmp_path)])
    assert ei.value.code == 2, "argparse 的用法错误退出码应为 2"


def test_default_name_falls_back_to_script_title(tmp_path):
    """不给 --topic/--name 时，用脚本里的标题做文件名，而不是干巴巴的 episode。"""
    fixture = ROOT / "tests" / "fixtures" / "d5_turns_demo.json"
    cli.main(["--turns-json", str(fixture), "--no-tts", "--out", str(tmp_path), "--quiet"])
    summary = json.loads((tmp_path / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["stages"]["script"]["title"] == "三分钟看懂播客后期"


def test_llm_path_persists_turns_so_script_json_round_trips(tmp_path, monkeypatch):
    """LLM 路径写出的 script.json 必须**含正文**。

    否则一轮端到端跑完后，产物无法用 `--turns-json outputs/…/script.json` 复现
    （只能重调 LLM，既慢又受网络波动影响）——这是 2026-09-16 修掉的真实缺陷。
    """
    from api.services import script_gen as sg

    class _Line:
        def __init__(self, spk: str, text: str) -> None:
            self.speaker, self.text = spk, text

    class _Script:
        title = "测试标题"
        total_chars = 5
        lines = [_Line("A", "你好呀"), _Line("B", "你也好")]

    class _Res:
        script = _Script()
        target_words = 5
        word_deviation = 0.0
        word_deviation_pct = 0.0
        usable = True
        first_pass = True
        errors: list = []
        model_returned = "deepseek-flash"
        model_mapped = False
        calls = 1
        correction_rounds = 0
        degraded = False
        flagged = False
        elapsed_s = 0.1

        def to_dict(self):
            return {"topic": "t", "line_count": 2}

    monkeypatch.setattr(sg, "ScriptGenerator",
                        lambda settings: types.SimpleNamespace(generate=lambda **kw: _Res()))

    rc = cli.main(["--topic", "随便什么主题", "--no-tts", "--out", str(tmp_path), "--quiet"])
    assert rc == 0
    written = json.loads((tmp_path / "script.json").read_text(encoding="utf-8"))
    assert written["line_count"] == 2, "生成报告字段应保留"
    assert written["script"]["title"] == "测试标题"

    # 关键断言：写出的文件必须能被 cli 自己读回，且对白逐条一致
    turns, title = cli.load_turns_from_json(str(tmp_path / "script.json"))
    assert [t["text"] for t in turns] == ["你好呀", "你也好"]
    assert [t["speaker"] for t in turns] == ["A", "B"]
    assert title == "测试标题"
