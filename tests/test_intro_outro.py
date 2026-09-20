# -*- coding: utf-8 -*-
"""片头/片尾文案渲染与素材解析的单测。

覆盖重点是**用户自由文本**带来的边界：占位符写法不一、主题超长截断、
以及渲染绝不能因文案含 `{}` 而抛异常（故用 replace 而非 format）。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from api.services.intro_outro import (
    DATE_PLACEHOLDERS,
    DEFAULT_INTRO_TEMPLATE,
    TOPIC_PLACEHOLDERS,
    has_placeholder,
    render_template,
    resolve_intro_file,
    resolve_outro_file,
    today_cn,
)


def test_today_cn_format():
    n = time.struct_time((2026, 9, 16, 10, 0, 0, 2, 259, 0))
    assert today_cn(n) == "2026年9月16日"


def test_today_cn_no_zero_padding():
    """Windows 的 strftime 不支持 %-m，必须手写拼接；这里锁定月/日不补零。"""
    n = time.struct_time((2026, 1, 5, 10, 0, 0, 0, 5, 0))
    assert today_cn(n) == "2026年1月5日"


@pytest.mark.parametrize("ph_date", DATE_PLACEHOLDERS)
@pytest.mark.parametrize("ph_topic", TOPIC_PLACEHOLDERS)
def test_render_all_placeholder_styles(ph_date, ph_topic):
    tpl = "大家好，今天是%s，今天的主题是%s。" % (ph_date, ph_topic)
    out = render_template(tpl, "2026年9月16日", "人工智能")
    assert out == "大家好，今天是2026年9月16日，今天的主题是人工智能。"


def test_default_template_renders():
    out = render_template(DEFAULT_INTRO_TEMPLATE, "2026年9月16日", "播客自动化")
    assert out == "大家好，今天是2026年9月16日，今天的主题是播客自动化。"


def test_render_with_max_chars_truncates_topic_not_skeleton():
    """超长时截断**主题**、保住模板骨架，并加省略号。"""
    long_topic = "人工智能" * 30
    out = render_template(DEFAULT_INTRO_TEMPLATE, "2026年9月16日", long_topic,
                          max_chars=40)
    assert len(out) <= 40
    assert out.startswith("大家好，今天是2026年9月16日，今天的主题是")
    # 省略号后仍保留模板骨架的句号
    assert out.endswith("…。")


def test_render_respects_max_chars_with_short_input():
    out = render_template(DEFAULT_INTRO_TEMPLATE, "2026年9月16日", "AI",
                          max_chars=40)
    assert out == "大家好，今天是2026年9月16日，今天的主题是AI。"


def test_render_does_not_raise_on_braces():
    """文案含 `{}` 时不能走 str.format（会 KeyError），必须原样保留。"""
    tpl = "欢迎 {date} 的听众，主题 {topic}，附注 {未知变量}"
    out = render_template(tpl, "2026年9月16日", "测试")
    assert out == "欢迎 2026年9月16日 的听众，主题 测试，附注 {未知变量}"


def test_has_placeholder():
    assert has_placeholder(DEFAULT_INTRO_TEMPLATE) is True
    assert has_placeholder("大家好，欢迎收听本期播客。") is False
    assert has_placeholder("") is False


class _FakeSettings:
    """模拟真实 Settings：intro_file / outro_file 是 **property**（不是方法）。

    这一条很关键——早先假对象把它们定义成方法，掩盖了真实链路的
    `TypeError: 'WindowsPath' object is not callable`。
    """

    def __init__(self, base: Path):
        self._base = base

    @property
    def intro_file(self) -> Path:
        return self._base / "intro.mp3"

    @property
    def outro_file(self) -> Path:
        return self._base / "outro.mp3"


def test_resolve_accepts_property_and_method(tmp_path):
    """两种形态都要支持（见 _as_path 的说明）。"""
    (tmp_path / "intro.mp3").write_bytes(b"x")

    class _AsMethod:
        def intro_file(self):
            return tmp_path / "intro.mp3"

        def outro_file(self):
            return tmp_path / "missing.mp3"

    assert resolve_intro_file(_FakeSettings(tmp_path)) == tmp_path / "intro.mp3"
    assert resolve_intro_file(_AsMethod()) == tmp_path / "intro.mp3"
    assert resolve_outro_file(_AsMethod()) is None


def test_resolve_files_missing_returns_none(tmp_path):
    s = _FakeSettings(tmp_path)
    assert resolve_intro_file(s) is None
    assert resolve_outro_file(s) is None


def test_resolve_files_existing(tmp_path):
    (tmp_path / "intro.mp3").write_bytes(b"x")
    (tmp_path / "outro.mp3").write_bytes(b"x")
    s = _FakeSettings(tmp_path)
    assert resolve_intro_file(s) == tmp_path / "intro.mp3"
    assert resolve_outro_file(s) == tmp_path / "outro.mp3"
