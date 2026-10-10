#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建 A/B 试听包（正确的增益分场 + 硬自检）。

背景：2026-09-16 首次构建「试听_女声换档_同句A-B.mp3」时，用一次性脚本做了三件错事，
导致成品峰值 **+3.91 dBFS**（削波、听感「很炸」）、三项响度相差 31 LU、且 24 kHz 素材被
按 22050 Hz 播放造成整体降调 8.8%。本脚本把这些坑固化成规则与断言，避免重犯。

三条规则（硬编码，不可绕过）
  1. 上采样一律经 ffmpeg（`-ar 44100`），禁止「按采样数重写采样率」——那会同时改时长与音高。
     构建后**必须**用 F0 回测比对源素材（相对偏差 ≤ 2%），否则判失败。
  2. 只允许**静态增益**（一项一个 dB 值），禁用 loudnorm / dynaudnorm / speechnorm 等动态处理
     —— 动态处理会改动包络与信噪比，毁掉 A/B 的可比性。
  3. 增益取「响度目标」与「峰值天花板」两约束的**较小值**，且天花板按**最终 MP3 解码后的
     实测峰值**判定（有损编码会过冲），不是按编码前的 wav。

同时输出一份「电平台账」（与 mp3 同名的 .md）：逐项记录源文件、SHA1、原始响度/峰值、
施加的静态增益、最终响度/峰值 —— 增益处理会毁掉绝对电平对比，台账是唯一补救。

用法（中文标签走 JSON 规格文件，不经 shell 传参，避免乱码）：
    python scripts/make_listen_pack.py --spec pack_spec.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

SR = 44100
BEEP_FREQ = 1000.0
BEEP_DUR = 0.15
BEEP_GAP = 0.15
BEEP_PEAK_DBFS = -20.0
BEEP_FADE = 0.01
GAP_AFTER_BEEPS = 1.00
GAP_AFTER_ITEM = 0.90
LEAD_SILENCE = 0.10
TAIL_SILENCE = 0.60
F0_TOL = 0.02          # F0 相对偏差上限
LOUDNESS_TOL = 1.0     # 最终响度与目标的最大偏差（LU）

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


# ---------------------------------------------------------------- 基础工具

def run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def run_bin(args: list[str], data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=data, capture_output=True)


def decode(path: Path, sr: int = SR, ss: float | None = None,
           dur: float | None = None) -> np.ndarray:
    """经 ffmpeg 解码为单声道 float，**正确重采样**到 sr。"""
    args = [FFMPEG, "-v", "error", "-i", str(path)]
    if ss is not None:
        args += ["-ss", f"{ss:.4f}"]
    if dur is not None:
        args += ["-t", f"{dur:.4f}"]
    args += ["-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    p = run_bin(args)
    if p.returncode != 0:
        raise RuntimeError(f"解码失败 {path}："
                           f"{p.stderr.decode('utf-8', 'replace')[:400]}")
    return np.frombuffer(p.stdout, dtype="<i2").astype(np.float64) / 32768.0


def write_wav(path: Path, y: np.ndarray, sr: int = SR) -> None:
    """写 16 bit 单声道 PCM（s16le 由 ffmpeg 封装，避免手写 RIFF）。"""
    raw = (np.clip(y, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    p = run_bin([FFMPEG, "-y", "-v", "error", "-f", "s16le", "-ar", str(sr),
                 "-ac", "1", "-i", "pipe:0", "-c:a", "pcm_s16le", str(path)], data=raw)
    if p.returncode != 0:
        raise RuntimeError("写 wav 失败：" + p.stderr.decode("utf-8", "replace")[:400])


def measure(path: Path) -> dict:
    """ebur128 测综合响度/真峰；astats 测采样峰值。"""
    p = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
             "-af", "ebur128=peak=true", "-f", "null", "-"])
    txt = p.stderr
    out: dict = {}
    m = re.search(r"Integrated loudness:\s*\n\s*I:\s*(-?[\d.]+|inf)\s*LUFS", txt)
    out["I"] = float(m.group(1)) if (m and m.group(1) != "inf") else float("-inf")
    m = re.search(r"Loudness range:\s*\n\s*LRA:\s*(-?[\d.]+)\s*LU", txt)
    out["LRA"] = float(m.group(1)) if m else float("nan")
    m = re.search(r"True peak:\s*\n\s*Peak:\s*(-?[\d.]+)\s*dBFS", txt)
    out["TP"] = float(m.group(1)) if m else float("nan")

    p = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(path),
             "-af", "astats=metadata=1:reset=0", "-f", "null", "-"])
    # 注意：astats 的 "Max level" 在整数 PCM 下报的是**样本值**（如 32767），
    # 在浮点输入下报的才是归一化幅度（如 1.47）。不要用 log10(Max level) 换算 dBFS，
    # 直接用已经归一化好的 "Peak level dB"（两种格式下都是 dBFS 口径）。
    pk = re.findall(r"Peak level dB:\s*(-?[\d.]+|inf)", p.stderr)
    out["sample_peak_db"] = float(pk[-1]) if pk and pk[-1] != "inf" else float("-inf")
    mx = re.findall(r"Max level:\s*(-?[\d.]+)", p.stderr)
    out["max_level_raw"] = float(mx[-1]) if mx else float("nan")
    return out


def f0_median(y: np.ndarray, sr: int = 16000, fmin: float = 70.0,
              fmax: float = 400.0, frame_ms: int = 40, hop_ms: int = 10) -> float:
    """统一自相关法 F0 中位（全帧、中位）——引用 F0 必须带这个口径。"""
    n = int(frame_ms * sr / 1000)
    hop = int(hop_ms * sr / 1000)
    lo, hi = int(sr / fmax), int(sr / fmin)
    vals = []
    for i in range(0, len(y) - n, hop):
        fr = y[i:i + n]
        if np.sqrt((fr ** 2).mean()) < 0.01:
            continue
        fr = fr - fr.mean()
        ac = np.correlate(fr, fr, mode="full")[n - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if len(seg) == 0:
            continue
        k = int(np.argmax(seg)) + lo
        if ac[k] / ac[0] > 0.3:
            vals.append(sr / k)
    return float(np.median(vals)) if vals else float("nan")


def make_beep_group(count: int) -> tuple[np.ndarray, int]:
    """返回 (提示音组, 组内第一个提示音的样本偏移)。

    组结构 = 前置静音 + (beep + gap)×count（去掉末尾 gap） + 后置静音，总长固定为
    GAP_AFTER_BEEPS —— 这样「响几声 = 第几项」在时间轴上是稳定的。
    """
    n = int(BEEP_DUR * SR)
    nf = int(BEEP_FADE * SR)
    tone = np.sin(2 * np.pi * BEEP_FREQ * np.arange(n) / SR) * (10 ** (BEEP_PEAK_DBFS / 20.0))
    win = np.ones(n)
    win[:nf] = np.linspace(0.0, 1.0, nf)
    win[-nf:] = np.linspace(1.0, 0.0, nf)
    tone *= win
    gap = np.zeros(int(BEEP_GAP * SR))
    block = np.concatenate([np.concatenate([tone, gap]) for _ in range(count)])
    block = block[:-(len(gap))]
    # 组总长取「配置值」与「装得下 block 再留 0.3 s 静音」的较大者。
    # 否则项数一多（提示音占的时长超过 GAP_AFTER_BEEPS）就会直接抛错，包没法用。
    total = max(int(GAP_AFTER_BEEPS * SR), len(block) + int(0.30 * SR))
    lead = (total - len(block)) // 2
    return np.concatenate([np.zeros(lead), block, np.zeros(total - len(block) - lead)]), lead


def count_bursts(y: np.ndarray, rel: float = 0.3, min_frames: int = 5) -> int:
    """数提示音个数：10 ms 帧短时能量的连通区。"""
    hop = int(0.010 * SR)
    n = int(0.020 * SR)
    if len(y) < n:
        return 0
    frames = np.lib.stride_tricks.sliding_window_view(y, n)[::hop]
    env = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    if env.max() <= 0:
        return 0
    bursts, run = 0, 0
    for v in env > env.max() * rel:
        if v:
            run += 1
        else:
            if run >= min_frames:
                bursts += 1
            run = 0
    if run >= min_frames:
        bursts += 1
    return bursts


def sha1_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def feasible_target(target_aim: float, ceiling: float, crest_max: float,
                    margin: float = 0.5) -> float:
    """静态增益下「可达的最响响度」上界。

    想对齐到某个响度目标 T，需要给每项加 T − I_i 的增益；但任何一项的峰值都不能越过
    天花板 C。设某项峰均比（peak − I）为 c，则它的增益上限是 C − (I + c)，对应响度
    C − c。于是所有项都能对齐的条件是 T ≤ C − max(c) − 余量。
    首次构建翻车就是因为没做这个约束：最吵的那项先撞顶，响度永远对不齐。
    """
    return min(target_aim, ceiling - crest_max - margin)


# ---------------------------------------------------------------- 主流程

def build(spec: dict) -> int:
    if not FFMPEG or not FFPROBE:
        print("!! 找不到 ffmpeg / ffprobe", file=sys.stderr)
        return 2

    out_path = Path(spec["out"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    target_aim = float(spec.get("target_lufs", -18.0))
    ceiling = float(spec.get("peak_ceiling_dbfs", -3.0))
    spec_items = spec["items"]

    tmpdir = Path(tempfile.mkdtemp(prefix="listenpack_"))
    ledger: list[dict] = []
    timeline: list[np.ndarray] = []
    marks: list[dict] = []          # 逐项的时间轴记账（样本偏移）
    cursor = 0

    # ---- 第一遍：全部解码并量源，推导「可达目标响度」 ----
    items_data: list[dict] = []
    print("=" * 82)
    print("源素材")
    print("=" * 82)
    for i, it in enumerate(spec_items, start=1):
        src = Path(it["path"])
        if not src.is_file():
            print(f"!! 源文件不存在：{src}", file=sys.stderr)
            return 2
        y = decode(src)
        probe = tmpdir / f"item{i}_raw.wav"
        write_wav(probe, y)
        m0 = measure(probe)
        crest = m0["sample_peak_db"] - m0["I"]
        items_data.append({"it": it, "src": src, "y": y, "m0": m0, "crest": crest})
        print(f"  [{i}] {it.get('label', f'item{i}')}")
        print(f"      I={m0['I']:.2f} LUFS / 采样峰值 {m0['sample_peak_db']:.2f} dBFS"
              f" / 峰均比 {crest:.2f} dB / 时长 {len(y) / SR:.3f} s")

    # 静态增益下，响度不可能超过「峰值天花板 − 最大峰均比」；目标必须收在这个上界之内，
    # 否则最吵的那一项会先撞天花板，三项响度就永远对不齐（首次构建翻车的原因之一）。
    crest_max = max(d["crest"] for d in items_data)
    target_feasible = feasible_target(target_aim, ceiling, crest_max)
    target = target_feasible
    print("\n" + "=" * 82)
    print(f"目标响度：期望 {target_aim} LUFS；可达上界 {target_feasible:.2f} LUFS"
          f"（= 天花板 {ceiling} − 最大峰均比 {crest_max:.2f} − 0.5 余量）")
    print(f"→ 实际采用 **{target:.2f} LUFS**；峰值天花板 {ceiling} dBFS"
          f"（按 MP3 解码后实测判定）")
    if target < target_aim - 1e-9:
        print("  注：期望目标不可达，已自动回落到可达上界 —— 这是纯静态增益的物理限制，")
        print("      不是缺陷；要更响只能引入动态处理，那会毁掉 A/B 的可比性。")
    print("=" * 82)

    lead = np.zeros(int(LEAD_SILENCE * SR))
    timeline.append(lead)
    cursor += len(lead)

    for i, d in enumerate(items_data, start=1):
        it, src, y, m0 = d["it"], d["src"], d["y"], d["m0"]

        # 规则 3：响度目标与峰值天花板取较小值
        gain_ld = target - m0["I"]
        gain_pk = ceiling - m0["sample_peak_db"]
        gain = min(gain_ld, gain_pk)
        limited_by = "峰值" if gain_pk < gain_ld else "响度"

        y2 = y * (10 ** (gain / 20.0))
        p2 = tmpdir / f"item{i}_gain.wav"
        write_wav(p2, y2)
        m1 = measure(p2)

        beeps, beep_lead = make_beep_group(i)
        beep_start = cursor + beep_lead
        timeline.append(beeps)
        cursor += len(beeps)

        item_start = cursor
        timeline.append(y2)
        cursor += len(y2)

        gap = np.zeros(int(GAP_AFTER_ITEM * SR))
        timeline.append(gap)
        cursor += len(gap)
        marks.append({"beep_start": beep_start, "item_start": item_start,
                      "dur": len(y2), "i": i})

        ledger.append({
            "序号": i, "标签": it.get("label", f"item{i}"), "源文件": src.name,
            "源SHA1": sha1_of(src)[:16], "源时长s": round(len(y) / SR, 3),
            "源响度LUFS": round(m0["I"], 2), "源采样峰值dBFS": round(m0["sample_peak_db"], 2),
            "施加静态增益dB": round(gain, 2), "受限项": limited_by,
            "最终响度LUFS": round(m1["I"], 2), "最终采样峰值dBFS": round(m1["sample_peak_db"], 2),
            "备注": it.get("note", ""),
        })
        print(f"  [{i}] {ledger[-1]['标签']}：源 I={m0['I']:.2f} / 峰值 {m0['sample_peak_db']:.2f}"
              f"  → 增益 {gain:+.2f} dB（{limited_by}约束）"
              f"  → I={m1['I']:.2f} / 峰值 {m1['sample_peak_db']:.2f}")

    tail = np.zeros(int(TAIL_SILENCE * SR))
    timeline.append(tail)
    y_all = np.concatenate(timeline)
    mixed = tmpdir / "mixed.wav"
    write_wav(mixed, y_all)

    p = run_bin([FFMPEG, "-y", "-v", "error", "-i", str(mixed), "-c:a", "libmp3lame",
                 "-b:a", "192k", "-ar", str(SR), "-ac", "1", str(out_path)])
    if p.returncode != 0:
        print("!! 编码失败：" + p.stderr.decode("utf-8", "replace")[:400], file=sys.stderr)
        return 2

    # ---------------- 自检 ----------------
    print("\n" + "=" * 82)
    print("自检")
    print("=" * 82)
    m_all = measure(out_path)
    ok = True

    c = m_all["sample_peak_db"] <= ceiling + 0.5 and m_all["TP"] <= ceiling + 0.5
    print(f"  [{'PASS' if c else 'FAIL'}] 编码后无削波：采样峰值 {m_all['sample_peak_db']:.2f} dBFS"
          f"、真峰 {m_all['TP']:.2f} dBTP（均须 ≤ {ceiling + 0.5:.1f}，"
          f"即天花板 {ceiling} 留 0.5 dB 余量给有损编码过冲）")
    ok &= c

    full = decode(out_path)

    finals: list[float] = []
    for mk in marks:
        i, start, dur = mk["i"], mk["item_start"], mk["dur"]
        ms = measure(_tmp_slice(tmpdir, f"chk{i}", full, start, dur))
        finals.append(ms["I"])
        d = abs(ms["I"] - target)
        c = d <= LOUDNESS_TOL
        print(f"  [{'PASS' if c else 'FAIL'}] item{i} 响度落点 {ms['I']:.2f} LUFS"
              f"（目标 {target}，偏差 {d:.2f} LU ≤ {LOUDNESS_TOL}）")
        ok &= c

    for mk in marks:
        i, start, dur = mk["i"], mk["item_start"], mk["dur"]
        f_src = f0_median(decode(Path(spec_items[i - 1]["path"]), sr=16000))
        f_out = f0_median(decode(out_path, sr=16000, ss=start / SR, dur=dur / SR))
        rel = abs(f_out - f_src) / f_src if f_src > 0 else 9.9
        c = rel <= F0_TOL
        print(f"  [{'PASS' if c else 'FAIL'}] item{i} 无音高偏移：源 F0 {f_src:.2f} Hz → "
              f"成包 F0 {f_out:.2f} Hz（相对偏差 {rel * 100:.2f}% ≤ {F0_TOL * 100:.0f}%）")
        ok &= c

    spread = max(finals) - min(finals)
    c = spread <= LOUDNESS_TOL
    print(f"  [{'PASS' if c else 'FAIL'}] 项间响度差 {spread:.2f} LU ≤ {LOUDNESS_TOL}"
          f"（min {min(finals):.2f} / max {max(finals):.2f} LUFS）"
          f" —— 对听而言「三项互相可比」比「落到某个绝对目标」更重要")
    ok &= c

    for mk in marks:
        i, bs = mk["i"], mk["beep_start"]
        span = i * (BEEP_DUR + BEEP_GAP) + 0.05
        seg = full[bs:bs + int(span * SR)]
        n = count_bursts(seg)
        c = n == i
        print(f"  [{'PASS' if c else 'FAIL'}] item{i} 提示音个数 = {n}（期望 {i}）")
        ok &= c

    # ---------------- 台账 ----------------
    lines = [f"# 试听包电平台账：{out_path.name}", "",
             "> 由 `scripts/make_listen_pack.py` 自动生成。",
             "> 试听包各项**做过响度对齐**，因此**不能**用它比较绝对电平或信噪比 ——",
             "> 绝对量请查下表「源响度 / 源采样峰值」两列，或查 D5 报告中的实测数字。", "",
             f"- 目标响度 **{target:.2f} LUFS**（期望 {target_aim}；可达上界 "
             f"{target_feasible:.2f} = 天花板 {ceiling} − 最大峰均比 {crest_max:.2f} − 0.5 余量）；"
             f"峰值天花板 **{ceiling} dBFS**（按 MP3 解码后实测判定）",
             f"- 成品真峰 **{m_all['TP']:.2f} dBTP**；采样峰值 **{m_all['sample_peak_db']:.2f} dBFS**；"
             f"项间响度差 **{min(finals):.2f} ~ {max(finals):.2f} LUFS**",
             f"- 提示音 {int(BEEP_FREQ)} Hz / {BEEP_DUR} s / {BEEP_PEAK_DBFS} dBFS 峰值，"
             f"组后静音 {GAP_AFTER_BEEPS} s，项间静音 {GAP_AFTER_ITEM} s", "",
             "| " + " | ".join(ledger[0].keys()) + " |",
             "| " + " | ".join("---" for _ in ledger[0]) + " |"]
    for row in ledger:
        lines.append("| " + " | ".join(str(v) for v in row.values()) + " |")
    lines += ["", "## 构建时固化的规则", "",
              "1. 上采样一律经 ffmpeg（`-ar 44100`），并在成包上**用 F0 回测源素材**（相对偏差 ≤ 2%），",
              "   杜绝「按采样数重写采样率」造成的整体降调与时长拉长。",
              "2. 只允许**静态增益**；禁用 loudnorm / dynaudnorm / speechnorm 等动态处理。",
              "3. 增益取「响度目标」与「峰值天花板」两约束的较小值，且天花板按最终 MP3",
              "   解码后的实测峰值判定（有损编码会过冲）。"]
    (out_path.with_suffix(".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "=" * 82)
    print(f"成品：{out_path}（{out_path.stat().st_size} B，真峰 {m_all['TP']:.2f} dBTP）")
    print(f"台账：{out_path.with_suffix('.md')}")
    print("判定：" + ("PASS" if ok else "FAIL"))
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if ok else 1


def _tmp_slice(tmpdir: Path, name: str, full: np.ndarray, start: int, dur: int) -> Path:
    p = tmpdir / f"{name}.wav"
    write_wav(p, full[start:start + dur])
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="构建 A/B 试听包（含硬自检）")
    ap.add_argument("--spec", required=True, help="JSON 规格文件路径")
    args = ap.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    return build(spec)


if __name__ == "__main__":
    raise SystemExit(main())
