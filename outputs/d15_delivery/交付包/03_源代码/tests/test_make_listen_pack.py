# -*- coding: utf-8 -*-
"""`scripts/make_listen_pack.py` 的单元测试。

覆盖 2026-09-16 试听包「很炸」故障修复时引入的三条不变量：
  1. 提示音组结构正确（响几声 = 第几项，且组内首音偏移可被正确记账）；
  2. 目标响度必须收在「峰值天花板 − 最大峰均比」的可达上界内（否则响度永远对不齐）；
  3. F0 估计器可信 —— 它是「无音高偏移」这条自检的地基，地基错了自检就是摆设。
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import make_listen_pack as mk  # noqa: E402


# ------------------------------------------------------------ 1. 提示音组

@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_beep_group_burst_count_and_offset(count):
    arr, lead = mk.make_beep_group(count)

    # 组总长不小于配置值；项数多到装不下时自适应加长（不得抛错）
    assert len(arr) >= mk.GAP_AFTER_BEEPS * mk.SR - 2

    # 记账的偏移处确实就是第一个提示音（偏移前不应有能量）
    assert lead > 0
    assert np.abs(arr[:lead]).max() == pytest.approx(0.0, abs=1e-12)
    assert np.abs(arr[lead:lead + int(0.02 * mk.SR)]).max() > 0

    # 提示音个数 = 项号
    assert mk.count_bursts(arr) == count


def test_beep_peak_respects_configured_level():
    arr, _ = mk.make_beep_group(1)
    peak_db = 20 * np.log10(np.abs(arr).max())
    assert peak_db == pytest.approx(mk.BEEP_PEAK_DBFS, abs=0.5)


def test_beep_group_edges_are_faded():
    """首尾必须有淡化，否则会「咔」一声（提示音本身也属于交付音频）。"""
    arr, lead = mk.make_beep_group(1)
    n = int(mk.BEEP_DUR * mk.SR)
    tone = arr[lead:lead + n]
    assert abs(tone[0]) < abs(tone).max() * 0.2
    assert abs(tone[-1]) < abs(tone).max() * 0.2


# ------------------------------------------------------------ 2. 可达目标响度

def test_target_is_capped_when_aim_is_too_loud():
    """期望 −18 LUFS，但峰均比 21 dB + 天花板 −3 dBFS 根本做不到。"""
    got = mk.feasible_target(-18.0, -3.0, 21.01)
    assert got == pytest.approx(-24.51, abs=0.01)
    assert got < -18.0


def test_target_keeps_aim_when_reachable():
    got = mk.feasible_target(-24.0, -3.0, 16.0)
    assert got == pytest.approx(-24.0, abs=1e-9)


def test_feasible_target_never_exceeds_ceiling_minus_crest():
    """不变式：任何目标下方能保证「所有项都不撞峰值天花板」。"""
    for crest in (10.0, 16.29, 21.01, 30.0):
        t = mk.feasible_target(-18.0, -3.0, crest)
        assert t <= -3.0 - crest


# ------------------------------------------------------------ 3. F0 估计器

def _sine(freq: float, dur: float = 1.0, sr: int = 16000) -> np.ndarray:
    t = np.arange(int(dur * sr)) / sr
    return 0.5 * np.sin(2 * np.pi * freq * t)


@pytest.mark.parametrize("freq", [120.0, 180.0, 240.0])
def test_f0_median_is_accurate_on_known_tones(freq):
    got = mk.f0_median(_sine(freq), sr=16000)
    assert got == pytest.approx(freq, rel=0.02)


def test_f0_median_detects_deliberate_pitch_drop():
    """模拟「24 kHz 素材按 22050 Hz 播放」：音高降 8.1%，必须被 F0 回测抓到。"""
    orig = mk.f0_median(_sine(200.0), sr=16000)
    dropped = mk.f0_median(_sine(200.0 * 22050 / 24000), sr=16000)
    assert dropped < orig
    assert abs(dropped - orig) / orig > mk.F0_TOL


def test_f0_median_returns_nan_on_silence():
    assert np.isnan(mk.f0_median(np.zeros(16000)))
