"""D5 验收脚本：音频后期与 mp3 导出（里程碑 M2 收口）。

    python scripts/verify_d5.py              # 全量（含真实 TTS，约 3 分钟）
    python scripts/verify_d5.py --no-tts     # 只跑纯 FFmpeg 部分（秒级，无需 GPU）

分四段取证：
  P1 FFmpeg 能力与片头片尾素材是否就位（含 loudnorm / libmp3lame）
  P2 R12 混合格式链路（16k/24k/44.1k + 立体声）→ 成片，校验时长守恒与规格
  P3 端到端（真实 TTS + 后期，脚本固定 10 行，不经 LLM）
  P4 复核 CLI 的「主题 → mp3」全链路摘要（若 outputs/d5_e2e/run_summary.json 存在）

结果同时打印并写入 outputs/d5_verify_result.json。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("COSYVOICE_ONNX_DEVICE", "cpu")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

WORK = ROOT / "data" / "work" / "d5_verify"
REPORT = ROOT / "outputs" / "d5_verify_result.json"
E2E_SUMMARY = ROOT / "outputs" / "d5_e2e" / "run_summary.json"
FIXTURE = ROOT / "tests" / "fixtures" / "d5_turns_demo.json"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print("  [%s] %-44s %s" % ("PASS" if ok else "FAIL", name, detail))
    return bool(ok)


def tone(path: Path, *, sr: int = 44100, ch: int = 1, dur: float = 0.5,
         freq: int = 440, peak_db: float = -6.0, ffmpeg: str) -> Path:
    """合成指定峰值电平的正弦音。lavfi sine 固定 1/8 满幅（实测 -18.06 dBFS）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            "sine=frequency=%d:sample_rate=%d:duration=%.4f" % (freq, sr, dur),
            "-af", "volume=%.2fdB" % (peak_db + 18.06), "-ac", str(ch)]
    if path.suffix.lower() == ".mp3":
        args += ["-ar", str(sr), "-c:a", "libmp3lame", "-b:a", "128k"]
    else:
        args += ["-c:a", "pcm_s16le"]
    from api.services import postprocess as pp

    pp.run_ffmpeg(args + [str(path)])
    return path


# --------------------------------------------------------------------------- #
# P1 FFmpeg 能力与素材
# --------------------------------------------------------------------------- #

def p1_environment(settings) -> dict:
    from api.services import postprocess as pp

    print("\nP1 FFmpeg 能力与片头片尾素材")
    ffmpeg, ffprobe = pp.find_ffmpeg(settings)
    print("  ffmpeg  = %s" % ffmpeg)

    run = pp.run_ffmpeg([ffmpeg, "-hide_banner", "-version"])
    ver_line = run.stdout.decode("utf-8", "replace").splitlines()[0] if run.stdout else ""
    check("ffmpeg 可执行", bool(ver_line), ver_line[:70])

    run = pp.run_ffmpeg([ffmpeg, "-hide_banner", "-filters"])
    filters = run.stdout.decode("utf-8", "replace")
    for f in ("loudnorm", "concat", "afade", "amix", "volumedetect"):
        check("滤镜 %s 可用" % f, bool(re.search(r"\b%s\b" % f, filters)))

    run = pp.run_ffmpeg([ffmpeg, "-hide_banner", "-encoders"])
    check("编码器 libmp3lame 可用",
          "libmp3lame" in run.stdout.decode("utf-8", "replace"))

    assets = {}
    for kind in ("intro", "outro"):
        p = getattr(settings, "%s_file" % kind)
        if p and Path(p).is_file():
            info = pp.probe(p, settings=settings)
            assets[kind] = str(p)
            check("片头/片尾 %s.mp3 规格" % kind,
                  (info.codec, info.sample_rate, info.channels) == ("mp3", 44100, 1)
                  and info.duration_s > 0.5,
                  "%s  %.2f s / %s / %d Hz / %d ch"
                  % (Path(p).name, info.duration_s, info.codec,
                     info.sample_rate, info.channels))
        else:
            assets[kind] = None
            print("  [warn] %s 未配置或不存在（成片将不含该段）" % kind)
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "version": ver_line,
            "assets": assets}


# --------------------------------------------------------------------------- #
# P2 R12 混合格式链路
# --------------------------------------------------------------------------- #

def p2_mixed_formats(settings) -> dict:
    from api.services import postprocess as pp

    print("\nP2 R12 混合采样率/声道 → 成片（纯 FFmpeg，无模型）")
    ffmpeg, _ = pp.find_ffmpeg(settings)
    work = WORK / "mixed"
    raw = [
        tone(work / "r0_16k.wav", sr=16000, ch=1, dur=0.6, freq=300, ffmpeg=ffmpeg),
        tone(work / "r1_24k.wav", sr=24000, ch=1, dur=0.6, freq=350, ffmpeg=ffmpeg),
        tone(work / "r2_44k_stereo.wav", sr=44100, ch=2, dur=0.6, freq=400, ffmpeg=ffmpeg),
    ]
    clips = [pp.Clip(wav=p, speaker="A" if i < 2 else "B", line_seq=i + 1)
             for i, p in enumerate(raw)]
    t0 = time.time()
    res = pp.postprocess(clips, out_dir=work / "out", name="d5_mixed",
                         settings=settings, intro=False, outro=False)
    spec = pp.probe(res.mp3, settings=settings)

    expect = 0.6 * 3 + settings.audio_pause_sentence_ms / 1000.0 \
        + settings.audio_pause_turn_ms / 1000.0
    check("时长守恒（R12）", abs(res.duration_s - expect) <= 0.08,
          "实测 %.2f s / 预期 %.2f s（3 段 + 句间 250 + 话轮 500）"
          % (res.duration_s, expect))
    check("mp3 规格 44.1k / 单声道", (spec.sample_rate, spec.channels) == (44100, 1),
          "%s / %d Hz / %d ch" % (spec.codec, spec.sample_rate, spec.channels))
    kbps = spec.size_bytes * 8 / max(spec.duration_s, 0.001) / 1000.0
    check("码率接近 128k", 100 < kbps < 150, "%.1f kbps" % kbps)
    check("无削波", not res.clipped, "峰值 %.2f dB" % (res.max_volume_db or 0.0))
    landed = res.loudness_after.get("input_i")
    check("响度落到 -16 LUFS ±1", landed is not None
          and abs(float(landed) - settings.audio_loudness_i) <= 1.0,
          "实测 %s LUFS（目标 %s）" % (landed, settings.audio_loudness_i))
    check("中间产物齐全", (res.build_dir / "merged.wav").is_file()
          and (res.build_dir / "concat_list.txt").is_file()
          and (res.build_dir / "normalized.wav").is_file(),
          str(res.build_dir))
    return {"elapsed_s": round(time.time() - t0, 2), "result": res.to_dict(),
            "probe": spec.to_dict(), "expect_duration_s": round(expect, 3)}


# --------------------------------------------------------------------------- #
# P3 端到端（真实 TTS）
# --------------------------------------------------------------------------- #

def p3_end_to_end(settings) -> dict:
    print("\nP3 端到端：10 行固定脚本 → 真实 TTS → mp3（不经 LLM）")
    out = WORK / "e2e"
    cmd = [sys.executable, str(ROOT / "scripts" / "cli.py"),
           "--turns-json", str(FIXTURE), "--out", str(out),
           "--name", "d5_verify_e2e", "--quiet"]
    t0 = time.time()
    run = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=3600)
    elapsed = time.time() - t0
    summary_path = out / "run_summary.json"
    if run.returncode != 0 or not summary_path.is_file():
        check("CLI 端到端返回 0", False,
              "rc=%d；stderr=%s" % (run.returncode, (run.stderr or "")[-400:]))
        return {"elapsed_s": round(elapsed, 2), "returncode": run.returncode,
                "stderr": (run.stderr or "")[-2000:]}

    check("CLI 端到端返回 0", True, "%.1f s" % elapsed)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    pp_res = summary.get("stages", {}).get("postprocess", {})
    tts = summary.get("stages", {}).get("tts", {})

    mp3 = Path(pp_res.get("mp3", ""))
    check("成片存在", mp3.is_file(), str(mp3))
    if mp3.is_file():
        from api.services import postprocess as pp

        spec = pp.probe(mp3, settings=settings)
        check("成片规格 mp3 / 44.1k / 单声道",
              (spec.codec, spec.sample_rate, spec.channels) == ("mp3", 44100, 1),
              "%s / %d Hz / %d ch" % (spec.codec, spec.sample_rate, spec.channels))

        # 时长应约等于「语音总长 + 停顿 + 片头片尾」
        audio_s = float(tts.get("audio_s") or 0.0)
        assets_s = 0.0
        for kind, used in (("intro", pp_res.get("intro_used")),
                           ("outro", pp_res.get("outro_used"))):
            if used:
                p = getattr(settings, "%s_file" % kind)
                if p and Path(p).is_file():
                    assets_s += pp.probe(p, settings=settings).duration_s
        # 停顿数已知，但换人=500ms、同人=250ms 无法从摘要区分，故给出上下界
        pause_n = float(pp_res.get("pause_count") or 0)
        pause_lo = pause_n * settings.audio_pause_sentence_ms / 1000.0
        pause_hi = pause_n * settings.audio_pause_turn_ms / 1000.0
        check("成片时长 ≈ 语音 + 停顿 + 片头尾",
              audio_s + assets_s + pause_lo - 0.8
              <= spec.duration_s
              <= audio_s + assets_s + pause_hi + 0.8,
              "成片 %.2f s；语音 %.2f s + 片头尾 %.2f s + %d 处停顿 [%.2f, %.2f] s"
              % (spec.duration_s, audio_s, assets_s, int(pause_n), pause_lo, pause_hi))
        check("无削波", not pp_res.get("clipped", False),
              "峰值 %s dB" % pp_res.get("max_volume_db"))
        landed = (pp_res.get("loudness_after") or {}).get("input_i")
        check("响度落到 -16 LUFS ±1", landed is not None
              and abs(float(landed) - settings.audio_loudness_i) <= 1.0,
              "实测 %s LUFS" % landed)
        check("片头片尾均已用上",
              bool(pp_res.get("intro_used")) and bool(pp_res.get("outro_used")),
              "intro=%s outro=%s" % (pp_res.get("intro_used"), pp_res.get("outro_used")))

    check("分句数 ≥ 10", int(tts.get("segments") or 0) >= 10,
          "%s 段（缓存命中 %s）" % (tts.get("segments"), tts.get("cached")))
    return {"elapsed_s": round(elapsed, 2), "summary": summary}


# --------------------------------------------------------------------------- #
# P4 复核 CLI 全链路（LLM）摘要
# --------------------------------------------------------------------------- #

def p4_llm_summary() -> dict:
    print("\nP4 复核 CLI「主题 → mp3」全链路摘要（LLM）")
    if not E2E_SUMMARY.is_file():
        print("  [skip] 未找到 %s —— 请先跑：python scripts/cli.py --topic ..." % E2E_SUMMARY)
        return {"skipped": True, "path": str(E2E_SUMMARY)}
    s = json.loads(E2E_SUMMARY.read_text(encoding="utf-8"))
    sc = s.get("stages", {}).get("script", {})
    pp_res = s.get("stages", {}).get("postprocess", {})
    tts = s.get("stages", {}).get("tts", {})
    check("全链路成功", bool(s.get("ok")), "总耗时 %s s" % s.get("elapsed_s"))
    check("脚本可用", bool(sc.get("usable")),
          "%d 行 / 偏差 %.1f%% / 模型 %s"
          % (sc.get("lines", 0), sc.get("word_deviation_pct", 0.0),
             sc.get("model_returned")))
    check("成片无削波且响度达标", not pp_res.get("clipped", True)
          and abs(float((pp_res.get("loudness_after") or {}).get("input_i") or 0.0)
                  - -16.0) <= 1.0,
          "峰值 %s dB / %s LUFS" % (pp_res.get("max_volume_db"),
                                    (pp_res.get("loudness_after") or {}).get("input_i")))
    check("端到端时长合理", float(pp_res.get("duration_s") or 0) >= 30.0,
          "%.1f s（%s 段语音 %.1f s）"
          % (pp_res.get("duration_s") or 0.0, tts.get("segments"), tts.get("audio_s") or 0.0))
    return {"summary": s}


def main() -> int:
    ap = argparse.ArgumentParser(description="D5 验收：后期与导出")
    ap.add_argument("--no-tts", action="store_true", help="跳过 P3 的真实合成")
    ap.add_argument("--no-llm", action="store_true", help="跳过 P4 的 LLM 全链路复核")
    args = ap.parse_args()

    from api.config import Settings

    settings = Settings()
    print("D5 验收：音频后期与 mp3 导出")
    print("=" * 78)
    report: dict = {"ok": False, "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    t0 = time.time()

    report["env"] = p1_environment(settings)
    report["mixed"] = p2_mixed_formats(settings)
    report["e2e"] = {"skipped": True} if args.no_tts else p3_end_to_end(settings)
    report["llm_e2e"] = {"skipped": True} if args.no_llm else p4_llm_summary()

    report["elapsed_s"] = round(time.time() - t0, 2)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    report["ok"] = not failed
    report["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS]
    report["passed"] = passed
    report["failed"] = failed

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print("结果：%d 项通过 / %d 项失败；总耗时 %.1f s"
          % (passed, len(failed), report["elapsed_s"]))
    if failed:
        for n in failed:
            print("  - %s" % n)
    print("报告：%s" % REPORT)
    print("判定：%s" % ("PASS" if report["ok"] else "FAIL"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
