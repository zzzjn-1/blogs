# -*- coding: utf-8 -*-
"""说话人解析（resolve_speaker）测试。

覆盖 [FIX-SPK-ID-01]：传**已注册音色 id**（如 "voice_b"）必须直接寻址。

修复前 `resolve_speaker` 只对输入取 `raw.upper()[:1]`：
    "voice_b" → "V" → 既非 A 也非 B → `_resolve_speaker` 回退到 `speaker_a_voice`
结果：传音色 id 的调用方（脚本 `--speaker voice_b`、外部 API）会**静默拿到女声**，
而且因为 `raw in self.registry` 判断成立，连「未知说话人」告警都不会触发 ——
属于最难排查的一类缺陷：不报错、结果错、日志静默。

本文件全部用例都不加载 GPU 模型（TTSEngine 构造不触发 load）。
"""
from __future__ import annotations

import json

import pytest

from api.config import get_settings
from api.services.tts import TTSEngine, VoiceRegistry


def _make_voices(tmp_path):
    """在临时目录造两个音色档案（wav 内容任意，load 只校验存在性）。"""
    vdir = tmp_path / "voices"
    vdir.mkdir()
    for vid in ("voice_a", "voice_b"):
        (vdir / f"{vid}.wav").write_bytes(b"RIFF" + b"\x00" * 64)
        (vdir / f"{vid}.json").write_text(
            json.dumps({"id": vid, "name": vid, "wav": f"{vid}.wav",
                        "prompt_text": "测试文本。", "gender": "unknown"},
                       ensure_ascii=False),
            encoding="utf-8")
    return vdir


@pytest.fixture()
def engine(tmp_path):
    s = get_settings()
    eng = TTSEngine(s)
    eng.registry = VoiceRegistry(_make_voices(tmp_path))
    eng.registry.load()
    eng._spk_cache.clear()
    return eng


def test_script_speaker_a_b(engine):
    """脚本说话人 A/B 正常映射到各自音色。"""
    assert engine.resolve_speaker("A") == "voice_a"
    assert engine.resolve_speaker("B") == "voice_b"


def test_registered_voice_id_direct(engine):
    """[FIX-SPK-ID-01] 已注册音色 id 直接寻址，不再被截断成首字符。"""
    assert engine.resolve_speaker("voice_a") == "voice_a"
    assert engine.resolve_speaker("voice_b") == "voice_b"


def test_voice_b_is_not_silently_downgraded(engine):
    """回归核心：修复前 'voice_b' 会被解析成 voice_a（女声），且无任何告警。"""
    assert engine.resolve_speaker("voice_b") != "voice_a"


def test_case_insensitive_ab(engine):
    assert engine.resolve_speaker("a") == "voice_a"
    assert engine.resolve_speaker("b") == "voice_b"


def test_unknown_falls_back_to_a(engine, caplog):
    """未知标识回退到 A，且必须显式告警（不能静默）。"""
    with caplog.at_level("WARNING"):
        got = engine.resolve_speaker("ZZZ")
    assert got == "voice_a"
    assert "ZZZ" in caplog.text


def test_empty_falls_back_to_a(engine):
    assert engine.resolve_speaker("") == "voice_a"
    assert engine.resolve_speaker(None) == "voice_a"
