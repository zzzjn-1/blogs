"""生成片头 / 片尾占位素材（计划书 P-5 前置项）。

正式素材应由用户提供；本脚本产出**明确标注为占位**的最短可用素材，
规格与计划书 P-5 一致：44.1 kHz / 单声道 / 128 kbps mp3，
供 D5 端到端验收与后续替换前的流水线自测使用。

    python scripts/make_assets.py [--force]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# (音名, 频率 Hz) —— 上行做片头、下行做片尾，听感上「起—收」
_PATTERNS = {
    "intro": [("C5", 523.25, 1.10), ("E5", 659.25, 1.30)],
    "outro": [("E5", 659.25, 1.00), ("C5", 523.25, 1.20)],
}


def _note_wav(freq: float, dur: float, dst: Path, *, ffmpeg: str, sr: int, ch: int) -> Path:
    """单音：基频 + 弱八度泛音，两端加淡入淡出，避免物理上的爆音与突兀。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fade_out_st = max(0.0, dur - 0.40)
    fc = (
        "sine=frequency=%.2f:sample_rate=%d:duration=%.4f[a];"
        "sine=frequency=%.2f:sample_rate=%d:duration=%.4f[b];"
        "[a]volume=-15dB[a1];[b]volume=-30dB[b1];"
        "[a1][b1]amix=inputs=2:duration=shortest:normalize=0,"
        "highpass=f=180,"
        "afade=t=in:st=0:d=0.06,afade=t=out:st=%.3f:d=0.40,"
        "aformat=sample_fmts=s16:channel_layouts=mono[out]"
        % (freq, sr, dur, freq * 2, sr, dur, fade_out_st)
    )
    from api.services import postprocess as pp

    pp.run_ffmpeg([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-filter_complex", fc, "-map", "[out]",
        "-ar", str(sr), "-ac", str(ch), "-c:a", "pcm_s16le", str(dst),
    ])
    return dst


def main() -> int:
    ap = argparse.ArgumentParser(description="生成片头/片尾占位素材")
    ap.add_argument("--force", action="store_true", help="已存在时覆盖")
    args = ap.parse_args()

    from api.config import Settings
    from api.services import postprocess as pp

    s = Settings()
    ffmpeg, ffprobe = pp.find_ffmpeg(s)
    print("ffmpeg : %s" % ffmpeg)
    print("ffprobe: %s" % ffprobe)

    assets = ROOT / "backend" / "assets"
    work = ROOT / "data" / "work" / "assets_build"
    work.mkdir(parents=True, exist_ok=True)

    made: list[tuple[Path, float]] = []
    for kind, parts in _PATTERNS.items():
        target = assets / ("%s.mp3" % kind)
        if target.is_file() and not args.force:
            print("[skip] %s 已存在（--force 可覆盖）" % target)
            made.append((target, pp.duration_s(target, settings=s)))
            continue

        wavs = [
            _note_wav(freq, dur, work / ("%s_%d.wav" % (kind, i)), ffmpeg=ffmpeg,
                      sr=s.audio_sample_rate, ch=s.audio_channels)
            for i, (_, freq, dur) in enumerate(parts)
        ]
        merged = pp.concat(wavs, work / ("%s_merged.wav" % kind),
                           list_path=work / ("%s_concat.txt" % kind), settings=s)
        out = pp.export_mp3(merged, target, sample_rate=s.audio_sample_rate,
                            channels=s.audio_channels, bitrate=s.audio_mp3_bitrate,
                            settings=s)
        info = pp.probe(out, settings=s)
        made.append((out, info.duration_s))
        print("[ok]   %s  %.2f s / %d B / %s / %d Hz / %d ch"
              % (out, info.duration_s, info.size_bytes, info.codec,
                 info.sample_rate, info.channels))

    print("\n共 %d 个素材：" % len(made))
    for p, d in made:
        print("  %-46s %.2f s" % (p, d))
    print("\n⚠️ 这些是**占位素材**（合成音，非人声/音乐）。正式上线前请替换为真实片头片尾，"
          "规格保持 44.1 kHz / 单声道 / 128 kbps mp3 即可直接沿用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
