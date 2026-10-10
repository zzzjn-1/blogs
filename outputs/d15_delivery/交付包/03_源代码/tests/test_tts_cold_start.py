# -*- coding: utf-8 -*-
"""冷启动回归：`synthesize_lines()` / `synthesize_turns()` 必须先 load() 再算缓存键。

## 这个测试守的是什么

`synthesize_lines()` 的循环里，**先**调 `cache_key()`，**再**调 `synthesize()`。
而 `load()` 只藏在 `synthesize()` 内部 —— 于是冷引擎上调批量入口时：

    cache_key() → resolve_speaker() → registry.get()  →  VoiceNotFound("…已加载 []")

表现为「线程/音色都配好了，却在第一句就报『音色不存在；已加载 []』」，而且日志里
**没有任何模型加载记录**（因为 load() 根本没跑），非常容易被误判成路径或配置问题。

`verify_d3.py` 恰好自己先显式 `load()` 过一次，把这个问题盖住了 —— 所以这个用例
必须**跳过任何手动 load**，直接调批量入口，才能守住它。

全程不加载模型、不占显存：`load()` 与 `synthesize()` 都被替换成桩。
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from api.config import Settings
from api.services import tts as tts_mod
from api.services.tts import Segment, SynthResult, TTSEngine


def _write_wav(path: Path, seconds: float = 0.3, rate: int = 16000) -> Path:
    """stdlib 写一个合法的最小 wav（指数字节即可，不需要真实人声）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _voices_dir(root: Path) -> Path:
    d = root / "voices"
    _write_wav(d / "voice_a.wav")
    (d / "voice_a.json").write_text(
        '{"id": "voice_a", "name": "甲", "wav": "voice_a.wav", '
        '"prompt_text": "这是一段用于测试的参考文本。"}',
        encoding="utf-8")
    return d


@pytest.fixture
def cold_engine(tmp_path, monkeypatch):
    """返回 (engine, order)：engine 未 load；order 记录 load / cache_key 的调用顺序。"""
    voices = _voices_dir(tmp_path)
    settings = Settings(
        voices_dir=str(voices),
        cache_dir=str(tmp_path / "cache"),
        speaker_a_voice="voice_a",
        speaker_voice_map={"A": "voice_a", "B": "voice_a"},
    )
    TTSEngine.reset()               # 确保不是上一条用例留下的已加载单例
    eng = TTSEngine(settings)
    assert eng.registry.list() == [], "前提：构造后注册表应为空（load 尚未发生）"

    order: list[str] = []

    def fake_load(self):
        order.append("load")
        self.model = object()        # 让 load() 的短路判断成立
        self.sample_rate = 24000
        self.text_frontend = "wetext"
        self.registry.load()         # 真正加载音色档案（走被测的 VoiceRegistry）
        return self

    def fake_synthesize(self, text, speaker="voice_a", *, speed=1.0, tone="",
                        text_hash=None):
        order.append("synthesize")
        wav = tmp_path / ("stub_%d.wav" % len(order))
        _write_wav(wav, 0.1, 24000)
        seg = Segment(seq=0, line_seq=0, speaker=speaker, text=text,
                      read_text=text, char_count=len(text))
        return SynthResult(segment=seg, wav_path=wav, text_hash=text_hash or "k",
                           duration_ms=100, cached=False, elapsed_s=0.01)

    real_cache_key = TTSEngine.cache_key

    def spy_cache_key(self, *a, **kw):
        order.append("cache_key")
        return real_cache_key(self, *a, **kw)

    monkeypatch.setattr(TTSEngine, "load", fake_load)
    monkeypatch.setattr(TTSEngine, "synthesize", fake_synthesize)
    monkeypatch.setattr(TTSEngine, "cache_key", spy_cache_key)
    yield eng, order
    TTSEngine.reset()


def test_synthesize_turns_loads_engine_before_hashing_cache_key(cold_engine):
    """核心断言：load 必须早于第一次 cache_key —— 否则就是线上那个 VoiceNotFound。"""
    eng, order = cold_engine

    results = eng.synthesize_turns(
        [{"speaker": "A", "text": "第一句。"}, {"speaker": "B", "text": "第二句。"}],
        out_dir=Path(eng.settings.cache_dir).parent / "segs",
    )

    assert len(results) == 2
    assert "load" in order, "批量入口没有触发 load()"
    assert "cache_key" in order
    assert order.index("load") < order.index("cache_key"), (
        "load() 必须在 cache_key() 之前发生，实际顺序：%s" % order)


def test_synthesize_turns_registers_voices_so_speaker_mapping_resolves(cold_engine):
    """load 之后注册表非空，A/B 都能解析到音色，而不是抛「已加载 []」。"""
    eng, _ = cold_engine
    eng.synthesize_turns([{"speaker": "A", "text": "只有一句。"}],
                         out_dir=Path(eng.settings.cache_dir).parent / "segs2")
    assert [v.id for v in eng.registry.list()] == ["voice_a"]
    assert eng.resolve_speaker("A") == "voice_a"


def test_cold_batch_entry_would_fail_without_the_fix(tmp_path, monkeypatch):
    """反向用例：把 load() 还原成「不加载注册表」的行为，批量入口必须报错。

    这样即使将来有人把 `self.load()` 从 synthesize_lines 里删掉，
    也会被这条用例挡住，而不是等到跑真机才炸。
    """
    voices = _voices_dir(tmp_path)
    settings = Settings(voices_dir=str(voices), cache_dir=str(tmp_path / "c2"),
                        speaker_a_voice="voice_a",
                        speaker_voice_map={"A": "voice_a", "B": "voice_a"})
    TTSEngine.reset()
    eng = TTSEngine(settings)

    def load_without_registry(self):
        self.model = object()          # 假装模型已就绪，但**不**加载音色档案
        return self

    monkeypatch.setattr(TTSEngine, "load", load_without_registry)
    monkeypatch.setattr(TTSEngine, "synthesize",
                        lambda self, text, speaker="voice_a", **kw: None)
    with pytest.raises(tts_mod.VoiceNotFound) as ei:
        eng.synthesize_turns([{"speaker": "A", "text": "任意一句。"}],
                             out_dir=tmp_path / "segs3")
    assert "已加载 []" in str(ei.value) or "不存在" in str(ei.value)
    TTSEngine.reset()
