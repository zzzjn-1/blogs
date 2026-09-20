# -*- coding: utf-8 -*-
"""`scripts/verify_consistency.py` 的单元测试。

为什么值得单独钉死：一致率是 M3 的验收指标。判据错一点，结论就会从「100% 达标」
翻转，或者反过来 —— **判据放宽到永远为真，「100%」就成了摆设**。本文件钉三件事：

  1. **判据本身**：四层行级 + 两条期级，每层都有「必须拦住」的用例；
  2. **`wav_duration_ms` 这个地基**：它错了，所有时长比对都失去意义；
  3. **初版踩过的坑**：多句行的 `text_hash` 只指首段，其时长**本就短于**
     `duration_ms`，必须判**通过** —— 这条若回退，会把 14 行正常数据误报成失败。
"""

from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_consistency as vc  # noqa: E402


# --------------------------------------------------------------------------- #
# 构造真实 RIFF 文件（不依赖 torchaudio）
# --------------------------------------------------------------------------- #

def _write_wav(path: Path, *, tag: int = 3, channels: int = 1, rate: int = 24000,
               bits: int = 32, frames: int = 2400, with_data: bool = True,
               riff_magic: bytes = b"RIFF") -> Path:
    bps = channels * (bits // 8)
    data = b"\x00" * (bps * frames)
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * bps, bps, bits)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    if with_data:
        chunks += b"data" + struct.pack("<I", len(data)) + data
    else:
        # 必须补一个无关 chunk 把文件撑到 44 字节以上。
        # 否则会被 `len(raw) < 44` 这条**更早**的规则拦掉，测不到
        # 「有 fmt、长度也够，但没有 data chunk」这条分支
        # —— 变异 M6 正是靠这条分支才发现的（见 mutation_check_d11.py）。
        chunks += b"LIST" + struct.pack("<I", 4) + b"\x00" * 4
    path.write_bytes(riff_magic + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
    return path


def _write_wav_extensible(path: Path, *, subtag: int = 3, channels: int = 1,
                          rate: int = 24000, bits: int = 32, frames: int = 2400) -> Path:
    bps = channels * (bits // 8)
    data = b"\x00" * (bps * frames)
    fmt = struct.pack("<HHIIHH", 0xFFFE, channels, rate, rate * bps, bps, bits)
    fmt += struct.pack("<HHI", 22, bits, 0)            # cbSize / validBits / channelMask
    fmt += struct.pack("<H", subtag) + b"\x00" * 14    # SubFormat GUID（前 2 字节即真实 tag）
    assert len(fmt) == 40, len(fmt)
    chunks = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(data)) + data
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
    return path


# --------------------------------------------------------------------------- #
# 1. wav 时长解析
# --------------------------------------------------------------------------- #

def test_wav_duration_float32(tmp_path: Path) -> None:
    """本项目缓存 wav 的真实格式：24 kHz 单声道 float32（`wave` 模块读不了）。

    24000 帧 @ 24 kHz = 1000 ms。
    """
    p = _write_wav(tmp_path / "a.wav", tag=3, bits=32, frames=24000)
    assert vc.wav_duration_ms(p) == 1000


def test_wav_duration_pcm16(tmp_path: Path) -> None:
    p = _write_wav(tmp_path / "b.wav", tag=1, bits=16, frames=2400)
    assert vc.wav_duration_ms(p) == 100


def test_wav_duration_extensible_uses_subformat_tag(tmp_path: Path) -> None:
    """EXTENSIBLE（0xFFFE）必须读 SubFormat 里的真实 tag，否则会被误判为未知格式。"""
    p = _write_wav_extensible(tmp_path / "c.wav", subtag=3, frames=1200)
    assert vc.wav_duration_ms(p) == 50


def test_wav_duration_stereo_counts_frames_not_bytes(tmp_path: Path) -> None:
    """立体声时若按字节数当帧数，时长会算成 2 倍 —— 必须除以声道数。"""
    p = _write_wav(tmp_path / "d.wav", tag=1, bits=16, channels=2, frames=2400)
    assert vc.wav_duration_ms(p) == 100


@pytest.mark.parametrize("case", ["missing", "not_riff", "no_data_chunk", "GIF89a"])
def test_wav_duration_returns_none_on_bad_input(tmp_path: Path, case: str) -> None:
    """解析失败一律返回 None，由调用方判失败；**不许猜一个数出来**。"""
    p = tmp_path / "bad.wav"
    if case == "missing":
        assert vc.wav_duration_ms(p) is None
        return
    if case == "GIF89a":
        p.write_bytes(b"GIF89a" + b"\x00" * 64)
    elif case == "not_riff":
        _write_wav(p, riff_magic=b"RIFX")
    else:
        _write_wav(p, with_data=False)
        # 前提断言：本用例必须真的落在「长度够、但没有 data chunk」这条路径上。
        # 文件短于 44 字节就会被更早的 `len(raw) < 44` 拦掉，用例会静默空转
        # —— 变异 M6 首次跑绿就是因为踩了这个（见 mutation_check_d11.py）。
        assert p.stat().st_size >= 44, "构造文件太短，本用例测的不是目标分支"
    assert vc.wav_duration_ms(p) is None


# --------------------------------------------------------------------------- #
# 2. 行级判据
# --------------------------------------------------------------------------- #

def _line(**kw) -> dict:
    base = {"read_text": "甲。", "text_hash": "a" * 40, "duration_ms": 5000,
            "seg_status": "DONE", "text": "甲。", "speaker": "A", "seq": 1}
    base.update(kw)
    return base


def test_line_all_good_passes() -> None:
    assert vc.evaluate_line(_line(), {"wav_path": "x.wav"}, 5000).ok


def test_line_multi_segment_first_seg_shorter_must_pass() -> None:
    """**核心回归**：多句行的 `text_hash` 只指首段，其时长本就短于整行合计。

    `task_runner` 落库时 `duration_ms = sum(segs)`、`text_hash = segs[0]`。
    初版判据写成等式，把 14 行（全部是「一行多句」的行）误报成失败。
    """
    v = vc.evaluate_line(
        _line(read_text="甲。乙。丙。", text="甲。乙。丙。", duration_ms=10000),
        {"wav_path": "x.wav"}, 4200)
    assert v.ok, v.reason


def test_line_first_seg_longer_than_line_fails_L4b() -> None:
    """反向：首段不可能长于整行。若变红，说明两个字段指向了不同粒度的音频。"""
    v = vc.evaluate_line(_line(duration_ms=1000), {"wav_path": "x.wav"}, 5000)
    assert not v.ok
    assert v.layer == "L4b"
    assert "粒度" in v.reason


def test_line_missing_read_text_fails_L1() -> None:
    v = vc.evaluate_line(_line(read_text="   "), {"wav_path": "x.wav"}, 5000)
    assert (not v.ok) and v.layer == "L1"


def test_line_missing_text_hash_fails_L2() -> None:
    v = vc.evaluate_line(_line(text_hash=None), None, None)
    assert (not v.ok) and v.layer == "L2"


@pytest.mark.parametrize("status,dur", [
    ("PENDING", 5000), ("FAILED", 5000), ("DONE", 0), ("DONE", None),
])
def test_line_not_ready_fails_L3(status: str, dur: object) -> None:
    v = vc.evaluate_line(_line(seg_status=status, duration_ms=dur), None, None)
    assert (not v.ok) and v.layer == "L3"


def test_line_unknown_fingerprint_fails_L4() -> None:
    v = vc.evaluate_line(_line(), None, None)
    assert (not v.ok) and v.layer == "L4"
    assert "audio_cache" in v.reason


def test_line_unreadable_wav_fails_L4() -> None:
    """有台账但文件不在盘（被回收/改名）→ 音轨无法定位，同样是失败。"""
    v = vc.evaluate_line(_line(), {"wav_path": "gone.wav"}, None)
    assert (not v.ok) and v.layer == "L4"
    assert "gone.wav" in v.reason


def test_line_short_circuit_reports_first_layer_only() -> None:
    """按层短路：只报首个未过的层，避免级联噪音掩盖根因。"""
    v = vc.evaluate_line(_line(read_text="", text_hash=None, seg_status="PENDING"),
                         None, None)
    assert v.layer == "L1"


# --------------------------------------------------------------------------- #
# 3. 期级判据
# --------------------------------------------------------------------------- #

def test_episode_ok() -> None:
    assert vc.evaluate_episode(100000, 120000).ok


def test_episode_missing_audio_fails_E1() -> None:
    v = vc.evaluate_episode(100000, None)
    assert (not v.ok) and v.layer == "E1"


def test_episode_sum_exceeds_final_fails_E2() -> None:
    """成片 = 片头 + 语音 + 停顿 + 片尾，必然长于语音总和；反了就是数据错了。"""
    v = vc.evaluate_episode(130000, 120000)
    assert (not v.ok) and v.layer == "E2"


# --------------------------------------------------------------------------- #
# 4. 端到端：分母口径（只有已成片任务的行进入一致率）
# --------------------------------------------------------------------------- #

def _mkdb(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        create table episodes(task_id text, title text, mp3_path text, duration_sec integer);
        create table audio_cache(text_hash text, speaker text, wav_path text, duration_ms integer);
        create table script_lines(task_id text, seq integer, speaker text, text text,
                                  read_text text, text_hash text, duration_ms integer,
                                  seg_status text);
    """)
    return conn


def _seed(conn: sqlite3.Connection, tmp_path: Path) -> None:
    mp3 = tmp_path / "final.mp3"
    mp3.write_bytes(b"\x00" * 32)
    conn.execute("insert into episodes values (?,?,?,?)",
                 ("t_done", "全对的一期", str(mp3), 120))
    conn.execute("insert into episodes values (?,?,?,?)",
                 ("t_bad", "有一行坏的一期", str(mp3), 120))

    wav = _write_wav(tmp_path / "seg.wav", tag=3, bits=32, frames=24000)   # 1000 ms
    for h in ("h_a", "h_b"):
        conn.execute("insert into audio_cache values (?,?,?,?)",
                     (h, "A", str(wav), 1000))
    conn.execute("insert into audio_cache values (?,?,?,?)",
                 ("h_bad", "A", str(tmp_path / "missing.wav"), 1000))

    rows = [
        ("t_done", 1, "A", "甲。", "甲。", "h_a", 1000, "DONE"),
        # 多句行：首段 1000ms，整行 3000ms —— 必须通过
        ("t_done", 2, "B", "乙。丙。丁。", "乙。丙。丁。", "h_b", 3000, "DONE"),
        # 首段（h_bad 文件缺失）→ L4
        ("t_bad", 1, "A", "戊。", "戊。", "h_bad", 1000, "DONE"),
        # 干净行
        ("t_bad", 2, "B", "己。", "己。", "h_a", 1000, "DONE"),
        # 未成片任务：全 PENDING，**不得进入分母**
        ("t_idle", 1, "A", "庚。", "庚。", None, 0, "PENDING"),
        ("t_idle", 2, "B", "辛。", "辛。", None, 0, "PENDING"),
    ]
    conn.executemany("insert into script_lines values (?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def test_run_excludes_tasks_without_episode(tmp_path: Path) -> None:
    """分母只含已成片任务的行。放弃的任务若被计入，会把「没做完」误报成「不一致」。"""
    db = tmp_path / "t.db"
    conn = _mkdb(db)
    _seed(conn, tmp_path)
    conn.close()

    res = vc.run(db, use_ffprobe=False)
    t = res["totals"]
    assert t["episodes"] == 2
    assert t["lines"] == 4              # t_done 2 行 + t_bad 2 行；t_idle 的 2 行不计
    assert t["idle_tasks"] == 1
    assert t["idle_lines"] == 2
    assert t["failures"] == 1
    assert t["lines_passed"] == 3
    assert t["consistency_rate"] == pytest.approx(0.75)

    layers = [f["layer"] for f in res["failures"]]
    assert layers == ["L4"]


def test_run_all_good_is_100_percent(tmp_path: Path) -> None:
    db = tmp_path / "t2.db"
    conn = _mkdb(db)
    _seed(conn, tmp_path)
    conn.execute("delete from script_lines where task_id='t_bad'")
    conn.execute("delete from episodes where task_id='t_bad'")
    conn.commit()
    conn.close()

    res = vc.run(db, use_ffprobe=False)
    assert res["totals"]["consistency_rate"] == 1.0
    assert res["totals"]["failures"] == 0


def test_run_granularity_stats_are_measured(tmp_path: Path) -> None:
    """粒度统计必须来自实测：相等 vs 首段更短，用来证明 duration_ms 是行级合计。"""
    db = tmp_path / "t3.db"
    conn = _mkdb(db)
    _seed(conn, tmp_path)
    conn.close()

    res = vc.run(db, use_ffprobe=False)
    g = res["granularity"]
    # 通过的行共 3 条：t_done seq1(相等) + t_done seq2(首段更短) + t_bad seq2(相等)
    assert g["exact"] == 2
    assert g["first_seg_shorter"] == 1
    assert g["max_gap_ms"] == 2000


def test_run_episode_invariant_detected(tmp_path: Path) -> None:
    """Σ行时长超过成片时长 → 期级 E2 必须报出来。"""
    db = tmp_path / "t4.db"
    conn = _mkdb(db)
    mp3 = tmp_path / "final.mp3"
    mp3.write_bytes(b"\x00" * 32)
    # duration_sec=1 → 成片 1000ms，而行合计 2000ms
    conn.execute("insert into episodes values (?,?,?,?)", ("t_x", "X", str(mp3), 1))
    wav = _write_wav(tmp_path / "s.wav", tag=3, bits=32, frames=24000)
    conn.execute("insert into audio_cache values (?,?,?,?)", ("h1", "A", str(wav), 1000))
    conn.execute("insert into script_lines values (?,?,?,?,?,?,?,?)",
                 ("t_x", 1, "A", "甲。", "甲。", "h1", 1000, "DONE"))
    conn.execute("insert into script_lines values (?,?,?,?,?,?,?,?)",
                 ("t_x", 2, "B", "乙。", "乙。", "h1", 1000, "DONE"))
    conn.commit()
    conn.close()

    res = vc.run(db, use_ffprobe=False)
    assert res["totals"]["failures"] == 1
    assert res["failures"][0]["layer"] == "E2"


# --------------------------------------------------------------------------- #
# 5. 报告契约
# --------------------------------------------------------------------------- #

def test_render_report_states_no_failures(tmp_path: Path) -> None:
    db = tmp_path / "t5.db"
    conn = _mkdb(db)
    _seed(conn, tmp_path)
    conn.execute("delete from script_lines where task_id in ('t_bad','t_idle')")
    conn.execute("delete from episodes where task_id='t_bad'")
    conn.commit()
    conn.close()

    md = vc.render_report(vc.run(db, use_ffprobe=False))
    assert "**无失败项。**" in md
    assert "= 100.00%" in md
    assert "粒度实测" in md


def test_render_report_lists_failure_rows(tmp_path: Path) -> None:
    db = tmp_path / "t6.db"
    conn = _mkdb(db)
    _seed(conn, tmp_path)
    conn.close()

    md = vc.render_report(vc.run(db, use_ffprobe=False))
    assert "**不达标**" in md
    assert "audio_cache 查不到指纹" in md or "不可解析" in md
