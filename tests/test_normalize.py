# -*- coding: utf-8 -*-
"""文本规范化与切句单测（对应计划书 4.4 S2/S3）。"""
from __future__ import annotations

import json

import pytest

from api.services.normalize import (NormalizeError, build_script_lines, build_segments,
                                    int_to_cn, load_polyphone, normalize_text,
                                    split_sentences)


# --------------------------------------------------------------------------- #
# 数字读法（计划书 4.4 表格里逐条给出的示例）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw, expect", [
    ("2026年", "二零二六年"),        # 四位年份 → 逐位读
    ("3.5", "三点五"),                # 小数
    ("45%", "百分之四十五"),          # 百分比
    ("3亿", "三亿"),                  # 大数单位保留
    ("2小时", "两小时"),              # 2 + 量词 → 两
    ("0.5", "零点五"),
    ("128", "一百二十八"),
    ("10000", "一万"),
    ("10001", "一万零一"),
    ("2000", "两千"),
    ("15", "十五"),                   # 十~十九 不带「一」
    ("110", "一百一十"),              # 非独立 10~19，保留「一十」
])
def test_number_reading(raw: str, expect: str) -> None:
    assert normalize_text(raw).read_text == expect


@pytest.mark.parametrize("n, expect", [
    (0, "零"), (2, "二"), (10, "十"), (19, "十九"), (20, "二十"),
    (110, "一百一十"), (2026, "两千零二十六"), (100000000, "一亿"),
])
def test_int_to_cn(n: int, expect: str) -> None:
    assert int_to_cn(n) == expect


# --------------------------------------------------------------------------- #
# 标点、标记与噪声
# --------------------------------------------------------------------------- #

def test_markdown_stripped() -> None:
    assert normalize_text("**重点**内容").read_text == "重点内容"
    assert normalize_text("# 标题\n正文").read_text == "标题正文"
    assert normalize_text("[链接](https://a.com)文字").read_text == "链接文字"


def test_emoji_and_zero_width_stripped() -> None:
    assert normalize_text("今天天气不错😀🌤️").read_text == "今天天气不错"
    assert normalize_text("前\u200b后").read_text == "前后"


def test_aside_stripped_but_normal_paren_kept() -> None:
    # 命中旁白关键词 → 剥离
    assert normalize_text("这个嘛（笑）我同意。").read_text == "这个嘛我同意。"
    # 普通括号内容（非旁白）保留，括号本身也保留
    out = normalize_text("碳14（一种同位素）测年法。").read_text
    assert "一种同位素" in out and "（" in out and "）" in out
    assert "碳十四" in out, "括号内的数字不应影响括号外的数字读法"


def test_cjk_latin_spacing() -> None:
    assert normalize_text("RSS订阅").read_text == "RSS 订阅"
    assert normalize_text("用GPU跑推理").read_text == "用 GPU 跑推理"


def test_fullwidth_digits_converted() -> None:
    assert normalize_text("２０２６年").read_text == "二零二六年"


def test_inline_cosyvoice_tag_kept() -> None:
    # [laughter] 是 CosyVoice 认识的行内标签，不能当噪声删掉
    assert "[laughter]" in normalize_text("他说完停顿了一下[laughter]。").read_text


def test_idempotent() -> None:
    """规范化结果再次规范化应保持不变（保证重试/续跑不漂移）。"""
    once = normalize_text("根据 2026 年的统计，45% 的人每周听 2 小时。").read_text
    twice = normalize_text(once).read_text
    assert once == twice


def test_empty_and_none() -> None:
    assert normalize_text("").read_text == ""
    assert normalize_text("   ").read_text == ""
    with pytest.raises(NormalizeError):
        normalize_text(None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 原文 → 读文映射（计划书 4.4 可逆性约束 / 逐句一致率 100% 的前提）
# --------------------------------------------------------------------------- #

def test_offsets_point_into_original() -> None:
    src = "根据 2026 年的统计"
    res = normalize_text(src)
    assert len(res.offsets) == len(res.read_text)
    idx = [o for o in res.offsets if o is not None]
    assert idx, "至少要有指向原文的下标"
    assert all(0 <= o < len(src) for o in idx), "所有下标必须落在原文范围内"
    assert idx == sorted(idx), "下标必须单调不减，否则映射不可用"


def test_edits_record_original_and_read() -> None:
    """Edit.orig 记录的是该步流水线中间态，真正的原文用原串切片取。"""
    src = "45% 的人"
    res = normalize_text(src)
    hit = [e for e in res.edits if e.rule == "number_to_cn"]
    assert hit, "百分比替换必须被记录"
    assert hit[0].read == "百分之四十五"
    assert src[hit[0].orig_start:hit[0].orig_end] == "45%", "原串切片必须精确覆盖被改写的部分"
    assert res.changed and res.reversible


def test_to_dict_serializable() -> None:
    d = normalize_text("**2026年**").to_dict()
    assert json.dumps(d, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 切句（单句 ≤ 40 字）
# --------------------------------------------------------------------------- #

def test_split_by_sentence_end() -> None:
    assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]


def test_split_respects_max_chars() -> None:
    long_line = "这是一个很长的句子，" * 12 + "结束。"
    out = split_sentences(long_line, max_chars=40)
    assert out, "必须切出内容"
    assert all(len(s) <= 40 for s in out), [len(s) for s in out]
    assert "".join(out).replace(" ", "") == long_line.replace(" ", ""), "切分不得丢字"


def test_split_hard_case_without_punctuation() -> None:
    out = split_sentences("字" * 95, max_chars=40)
    assert [len(s) for s in out] == [40, 40, 15]


def test_split_empty() -> None:
    assert split_sentences("") == []
    assert split_sentences("   ") == []
    with pytest.raises(ValueError):
        split_sentences("abc", max_chars=0)


# --------------------------------------------------------------------------- #
# 脚本行 / 分段
# --------------------------------------------------------------------------- #

def test_build_lines_and_segments() -> None:
    turns = [
        {"speaker": "A", "text": "欢迎收听。今天聊聊 2026 年的播客。"},
        {"speaker": "b", "text": "好的，我来补充。"},
    ]
    lines = build_script_lines(turns)
    assert [l.speaker for l in lines] == ["A", "B"], "speaker 需规整为大写单字母"
    assert lines[0].read_text.startswith("欢迎收听。今天聊聊二零二六年")

    segs = build_segments(lines)
    assert segs, "必须切出分段"
    assert all(s.char_count <= 40 for s in segs)
    assert [s.seq for s in segs] == list(range(1, len(segs) + 1)), "seq 必须连续"
    assert segs[0].line_seq == 1 and segs[-1].line_seq == 2


def test_build_script_lines_keeps_original_text() -> None:
    """read_text 供 TTS 用，text 必须保留原文（4.6 script_lines 两个字段的区别）。"""
    lines = build_script_lines([{"speaker": "A", "text": "**2026年**的统计"}])
    assert lines[0].text == "**2026年**的统计"
    assert lines[0].read_text == "二零二六年的统计"


# --------------------------------------------------------------------------- #
# 多音字词典
# --------------------------------------------------------------------------- #

def test_polyphone_dict_applies_and_warns() -> None:
    res = normalize_text("重庆的天气", polyphone={"重庆": "崇庆"})
    assert res.read_text == "崇庆的天气"
    assert not res.reversible, "词典替换属不可逆改写"
    assert any("不可逆" in w for w in res.warnings)


def test_load_polyphone_skips_readme(tmp_path) -> None:
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"_readme": ["说明"], "重庆": "崇庆"}), encoding="utf-8")
    assert load_polyphone(str(p)) == {"重庆": "崇庆"}
    assert load_polyphone(str(tmp_path / "missing.json")) == {}


def test_shipped_polyphone_dict_is_empty_by_default() -> None:
    """内置词典默认必须为空：wetext 已做上下文消歧，预填大词表会主动劣化音质。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert load_polyphone(str(root / "backend" / "dicts" / "polyphone.json")) == {}
