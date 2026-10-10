#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""音色档案建档工具（D2 起草，D5 音质专项扩展）

零样本合成需要「一段干净的人声」+「这段人声的转写文本」两个输入。
本脚本把任意音频变成可用的音色档案：**格式统一 → 噪声体检 → 裁首尾静音 →
峰值归一 → 备份旧档 → 写 voice_*.json**。

用法：
    # 已知转写文本（推荐，最可靠）
    python scripts/make_voice_profile.py --id voice_b --name "男声·沉稳" \
        --wav D:/my_voice.m4a --text "大家好，我是本期节目的另一位主播。" --gender male

    # 没有转写文本时用 ASR 自动识别（需 whisper 权重，首次会下载）
    python scripts/make_voice_profile.py --id voice_b --wav D:/my_voice.wav --asr

    # 只做体检（格式 + 噪声），不写档案
    python scripts/make_voice_profile.py --wav D:/my_voice.wav --check

    # 换参考后旧缓存必然失效，可顺带可回滚地移出
    python scripts/make_voice_profile.py --id voice_b --wav D:/new.m4a --text "..." --purge-cache

录音要求（计划书 4.4 S6）：
    5~10 秒 / 单人 / 无背景音乐 / 无混响 / 无爆音 / 情绪平稳；
    一男一女、沉稳 vs 活泼 差异最大化，便于听众区分。

──────────────────────────────────────────────────────────────────────────
关于降噪（**重要实测结论，勿凭直觉推翻**）
──────────────────────────────────────────────────────────────────────────
默认**不做**频谱降噪。D5 音质专项实测（2026-09-16，同一模型同一句、只换参考）：

    参考录音                                  合成输出间隙底噪      输出 SNR
    现役 voice_b（SNR 差、无静音段）           −41.2 / −45.5 dBFS    28.0 dB
    现役 voice_b + afftdn 降噪                −43.9 / −41.2 dBFS    26.7 dB  ← 更差
    新素材（干净录音，SNR 40 dB）              −57.6 / −58.4 dBFS    41.1 dB  ← 最佳
    新素材 + afftdn 降噪                      −46.7 / −56.9 dBFS    31.4 dB  ← 更差

结论两条：
  ① **换一段更干净的录音 >>> 事后降噪**（底噪差 16 dB，SNR 差 14 dB）；
  ② 参考录音加上 afftdn **一致地让结果变差**。原因：afftdn 需要「纯噪声段」估计噪声画像，
     参考录音里没有可用静音段时它拿不到画像，反而把语音弱帧当噪声减掉，产生 musical noise，
     零样本克隆于是把损伤一起学走。确需时显式加 --denoise，并自行听感复核。

──────────────────────────────────────────────────────────────────────────
关于裁首尾静音（**同样反直觉，实测两条探针一致**）
──────────────────────────────────────────────────────────────────────────
默认会裁（`trim_edges`），但**裁掉不等于更好**。D5 音质专项第二轮实测
（2026-09-16，同一模型同句同种子，只换参考；探针句两句独立复核）：

    参考版本                                     合成间隙底噪(p1/p2)   输出 SNR
    life.m4a 原样 5.46s（**--no-trim**）          −54.9 / −59.5 dBFS    41.8 dB  ← 最优
    life.m4a 裁静音 4.06s（默认）                 −48.8 / −49.2 dBFS    34.9 dB

平均差 **8.2 dB 底噪 / 6.9 dB SNR**，两句方向一致 → 非小样本假象。
两者**语音电平只差约 1 dB**，故不是峰值归一造成的量纲假象。
机制未确认（推测与 prompt 语音 token 数、起音段完整性有关），但效果量足够大：
**重要档案建议 `--no-trim` 与默认各建一版，交替试听后再定**。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
VOICES = ROOT / "backend" / "voices"
TARGET_SR = 16000
MIN_SEC, MAX_SEC = 5.0, 10.0      # 计划书 4.4 S6 建议区间
HARD_MIN_SEC = 3.0                # 低于此值克隆极不稳定，仅告警不作硬拦截
GOOD_SNR_DB = 35.0                # 低于此值零样本克隆会把底噪一起学走


# --------------------------------------------------------------------------- #
# 音频读写（统一走 ffmpeg —— D5 起已是项目硬依赖，见 1.4 P-8）
# --------------------------------------------------------------------------- #
def find_ffmpeg(explicit: str = "") -> str:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        raise SystemExit(f"[ERR] 指定的 ffmpeg 不存在：{p}")
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit("[ERR] 找不到 ffmpeg（D5 起为硬依赖）："
                         "请加入 PATH，或用 --ffmpeg 指定")
    return found


def decode(ff: str, src: Path, sr: int = TARGET_SR) -> np.ndarray:
    r = subprocess.run([ff, "-v", "error", "-i", str(src), "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"[ERR] 解码失败：{src}\n"
                         f"{r.stderr.decode('utf-8', 'ignore')[:500]}")
    return np.frombuffer(r.stdout, dtype="<f4").astype(np.float64)


def encode(ff: str, y: np.ndarray, dst: Path, sr: int = TARGET_SR) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    raw = np.clip(y, -1.0, 1.0).astype("<f4").tobytes()
    r = subprocess.run([ff, "-y", "-v", "error", "-f", "f32le", "-ar", str(sr), "-ac", "1",
                        "-i", "pipe:0", "-c:a", "pcm_s16le", str(dst)],
                       input=raw, capture_output=True)
    if r.returncode != 0:
        raise SystemExit(f"[ERR] 写出失败：{dst}\n"
                         f"{r.stderr.decode('utf-8', 'ignore')[:500]}")


def probe(ff: str, path: Path) -> dict:
    """用 ffprobe 读原始流参数（采样率/声道/时长）。"""
    fp = Path(ff).with_name("ffprobe.exe" if Path(ff).suffix.lower() == ".exe" else "ffprobe")
    fp = str(fp) if fp.is_file() else (shutil.which("ffprobe") or "")
    if not fp:
        return {"path": str(path), "sample_rate": "?", "channels": "?", "seconds": None}
    r = subprocess.run([fp, "-v", "error", "-select_streams", "a:0", "-show_streams",
                        "-of", "json", str(path)], capture_output=True, text=True)
    try:
        st = json.loads(r.stdout)["streams"][0]
        sec = st.get("duration")
        if sec is None and st.get("nb_frames") and st.get("sample_rate"):
            sec = None
        return {"path": str(path), "sample_rate": st.get("sample_rate"),
                "channels": st.get("channels"),
                "seconds": round(float(sec), 3) if sec else None}
    except Exception:  # noqa: BLE001
        return {"path": str(path), "sample_rate": "?", "channels": "?", "seconds": None}


# --------------------------------------------------------------------------- #
# 信号指标
# --------------------------------------------------------------------------- #
def noise_stats(y: np.ndarray, sr: int = TARGET_SR) -> dict:
    """底噪 / 语音电平 / SNR / 可用静音时长。

    「可用静音」= 连续 ≥0.2 s 且低于「语音 −25 dB」的帧。
    该口径同时暴露两件事：底噪有多低、以及**录音里到底有没有静音段**
    （没有静音段 = 全程都有可听能量，正是零样本克隆把噪声一起学走的成因）。
    """
    n = max(1, int(0.02 * sr))
    m = len(y) // n
    if m < 2:
        return {"floor_dbfs": None, "speech_dbfs": None, "snr_db": None,
                "silence_s": 0.0, "speech_p90_dbfs": None}
    fr = y[: m * n].reshape(m, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-24) + 1e-12)
    loud = fr[db >= np.percentile(db, 60)]
    sp = float(20 * np.log10(np.sqrt((loud ** 2).mean()) + 1e-12))
    thr = sp - 25
    runs, cur = [], []
    for i, v in enumerate(db):
        if v < thr:
            cur.append(i)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    good = [r for r in runs if len(r) * n >= 0.20 * sr]
    base = {"speech_dbfs": round(sp, 2), "speech_p90_dbfs": round(float(np.percentile(db, 90)), 2)}
    if not good:
        return {"floor_dbfs": None, "snr_db": None, "silence_s": 0.0, **base}
    idx = [i for r in good for i in r]
    floor = float(20 * np.log10(np.sqrt((fr[idx] ** 2).mean()) + 1e-12))
    return {"floor_dbfs": round(floor, 2), "snr_db": round(sp - floor, 2),
            "silence_s": round(len(idx) * n / sr, 2), **base}


def fmt_stats(s: dict) -> str:
    if s["floor_dbfs"] is None:
        return (f"底噪 无可测静音段（录音内无 ≥0.2s 停顿） | "
                f"语音 {s['speech_dbfs']} dBFS | 语音 P90 {s['speech_p90_dbfs']} dBFS")
    return (f"底噪 {s['floor_dbfs']} dBFS | 语音 {s['speech_dbfs']} dBFS | "
            f"SNR {s['snr_db']} dB | 可用静音 {s['silence_s']}s")


def trim_edges(y: np.ndarray, sr: int = TARGET_SR, keep_ms: int = 60,
               drop_db: float = 30.0):
    """裁首尾静音，**保留内部停顿**（prompt_text 必须与音频严格对齐）。"""
    n = max(1, int(0.02 * sr))
    m = len(y) // n
    if m < 2:
        return y, 0.0, 0.0
    fr = y[: m * n].reshape(m, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-24) + 1e-12)
    thr = float(np.percentile(db, 90)) - drop_db
    idx = np.where(db >= thr)[0]
    if len(idx) == 0:
        return y, 0.0, 0.0
    keep = int(keep_ms / 1000 * sr)
    a = max(0, int(idx[0]) * n - keep)
    b = min(len(y), (int(idx[-1]) + 1) * n + keep)
    return y[a:b], a / sr, (len(y) - b) / sr


def fingerprint(wav: Path, text: str) -> str:
    """与 tts._voice_fingerprint 同口径：参考音频字节 + 转写文本。"""
    return hashlib.sha1(wav.read_bytes() + b"|" + text.encode("utf-8")).hexdigest()[:12]


def transcribe(path: Path, language: str = "zh") -> str:
    """用 whisper 转写。首次调用会下载权重（base 约 140 MB）。

    ⚠ 实测：whisper(base/small) 在中文场景不可靠（对同一段 8.92s 录音一致漏掉尾段 1.9s），
    自动转写只宜作初稿，必须人工校正后再建档。
    """
    import whisper
    model = whisper.load_model("base")
    r = model.transcribe(str(path), language=language, fp16=False)
    return str(r.get("text", "")).strip()


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="参考录音 → 零样本音色档案")
    ap.add_argument("--id", help="音色 id，如 voice_b（决定输出文件名，--check 时可省）")
    ap.add_argument("--name", default="", help="展示名，如「男声·沉稳」")
    ap.add_argument("--wav", "--input", dest="wav", required=True, help="源音频路径")
    ap.add_argument("--text", default="", help="该音频的**准确**转写文本（与 --asr 二选一）")
    ap.add_argument("--asr", action="store_true", help="用 whisper 自动转写（需人工校正）")
    ap.add_argument("--gender", default="unknown", choices=["male", "female", "unknown"])
    ap.add_argument("--style", default="neutral")
    ap.add_argument("--description", default="")
    ap.add_argument("--check", action="store_true", help="只做体检（格式 + 噪声），不写档案")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的档案")
    ap.add_argument("--dry-run", action="store_true", help="试算但不写任何文件")
    ap.add_argument("--no-trim", action="store_true", help="不裁首尾静音")
    ap.add_argument("--peak-db", type=float, default=-3.0, help="峰值归一目标（默认 −3 dBFS）")
    ap.add_argument("--denoise", action="store_true",
                    help="启用 afftdn 降噪（默认关；实测会让克隆结果变差，见文件头）")
    ap.add_argument("--voices-dir", default=str(VOICES))
    ap.add_argument("--purge-cache", action="store_true",
                    help="顺带把 data/cache 可回滚地移出（换参考后旧缓存必失效）")
    ap.add_argument("--ffmpeg", default="")
    args = ap.parse_args(argv)

    ff = find_ffmpeg(args.ffmpeg)
    src = Path(args.wav).expanduser()
    if not src.is_file():
        print(f"[ERR] 源文件不存在：{src}")
        return 2
    voices_dir = Path(args.voices_dir)

    # ---- 1) 体检 ----
    info = probe(ff, src)
    print(f"[探测] 采样率 {info['sample_rate']} Hz | 声道 {info['channels']} | "
          f"时长 {info['seconds']} s")
    y = decode(ff, src)
    sec = len(y) / TARGET_SR
    print(f"[体检] {fmt_stats(noise_stats(y))}")

    problems = []
    if sec < HARD_MIN_SEC:
        problems.append(f"时长 {sec:.2f}s 过短（<{HARD_MIN_SEC}s），克隆音色会明显不稳定")
    elif sec < MIN_SEC:
        problems.append(f"时长 {sec:.2f}s 短于建议下限 {MIN_SEC}s，"
                        f"prompt 特征偏少，音色相似度与韵律稳定性会下降")
    elif sec > MAX_SEC:
        problems.append(f"时长 {sec:.2f}s 超过建议上限 {MAX_SEC}s，"
                        f"prompt 特征计算变慢（音色档案只省约 5%，不值得加长）")
    s0 = noise_stats(y)
    if s0["snr_db"] is not None and s0["snr_db"] < GOOD_SNR_DB:
        problems.append(f"SNR 仅 {s0['snr_db']} dB（<{GOOD_SNR_DB:.0f} dB）：零样本克隆会把这个底噪"
                        f"一起学走。实测同类参考合成输出底噪只能到 −42 dBFS，而 40 dB SNR 的参考能到 "
                        f"−58 dBFS → **首选重录**（安静房间、离麦 15~20 cm、避开风扇与空调）")
    elif s0["snr_db"] is None:
        problems.append("录音内没有可测的静音段（无 ≥0.2s 停顿）：全程都有可听能量，"
                        "克隆会把这段噪声一起学走。若听感发闷/有嘶声，建议重录而非事后降噪")
    for p in problems:
        print(f"[WARN] {p}")
    if args.check:
        print("[OK] 仅体检，未写入档案")
        return 0
    if not args.id:
        print("[ERR] 写入档案时必须提供 --id")
        return 2

    wav_dst = voices_dir / f"{args.id}.wav"
    meta_path = voices_dir / f"{args.id}.json"
    if meta_path.is_file() and not args.force and not args.dry_run:
        print(f"[ERR] 档案已存在：{meta_path}（加 --force 覆盖）")
        return 2

    # ---- 2) 转写文本 ----
    text = args.text.strip()
    if not text and args.asr:
        print("[ASR] 识别中（首次会下载 whisper 权重）…")
        text = transcribe(src)
        print(f"[ASR] {text}（⚠ 自动转写仅宜作初稿，必须人工校正）")
    if not text:
        print("[ERR] 缺少转写文本：请用 --text 提供，或加 --asr 自动识别。\n"
              "      零样本合成必须提供 prompt 音频的转写，否则韵律会明显劣化。")
        return 2

    # ---- 3) 可选降噪 ----
    if args.denoise:
        print("[WARN] --denoise 已启用：实测该步骤会让零样本克隆的底噪**变差**"
              "（见文件头对照表），仅在你亲自听感确认后才使用")
        tmp = voices_dir / f".{args.id}.pre.wav"
        encode(ff, y, tmp)
        noisy = voices_dir / f".{args.id}.dn.wav"
        r = subprocess.run([ff, "-y", "-v", "error", "-i", str(tmp),
                            "-af", "highpass=f=60,afftdn=nr=24:nf=-45:tn=1",
                            "-ac", "1", "-ar", str(TARGET_SR), "-c:a", "pcm_s16le", str(noisy)],
                           capture_output=True)
        if r.returncode != 0:
            raise SystemExit(f"[ERR] 降噪失败：{r.stderr.decode('utf-8', 'ignore')[:400]}")
        y = decode(ff, noisy)
        for p in (tmp, noisy):
            p.unlink(missing_ok=True)
        print(f"[降噪] 处理链 highpass=60 + afftdn(nr=24,nf=-45) → {fmt_stats(noise_stats(y))}")

    # ---- 4) 裁首尾静音 + 峰值归一 ----
    y2, head, tail = (y, 0.0, 0.0) if args.no_trim else trim_edges(y)
    if args.no_trim:
        print(f"[裁剪] --no-trim：保留首尾静音 → {len(y2)/TARGET_SR:.3f}s"
              f"（实测对间隙底噪更友好，见文件头对照表）")
    else:
        print(f"[裁剪] 去头 {head:.3f}s / 去尾 {tail:.3f}s → {len(y2)/TARGET_SR:.3f}s"
              f"（内部停顿保留，保证与 prompt_text 对齐）")
    final_sec = len(y2) / TARGET_SR
    if final_sec < HARD_MIN_SEC:
        print(f"[WARN] 裁剪后仅 {final_sec:.2f}s（<{HARD_MIN_SEC}s），克隆会不稳定")
    elif final_sec < MIN_SEC:
        print(f"[WARN] 裁剪后 {final_sec:.2f}s 短于建议下限 {MIN_SEC}s。"
              f"这段是**纯语音净时长**（静音已去），4~5s 仍属可用区间；"
              f"若音色相似度不达预期，可加 --no-trim 连同停顿一起作为 prompt 再比一次")

    pk = float(np.abs(y2).max())
    gain = 10 ** (args.peak_db / 20) / (pk + 1e-12)
    y3 = y2 * gain
    print(f"[归一] 峰值 {20*np.log10(pk+1e-12):+.2f} dBFS → {args.peak_db:+.2f} dBFS"
          f"（增益 {20*np.log10(gain):+.2f} dB）")
    print(f"[结果] {fmt_stats(noise_stats(y3))}")

    if args.dry_run:
        print("[DRY-RUN] 不写文件。目标：")
        print(f"          {wav_dst}")
        print(f"          {meta_path}")
        return 0

    # ---- 5) 先写临时文件，内容**真正变了**才备份并替换 ----
    #  不能「先备份再直接覆盖」：一旦脚本重跑（例如被沙箱拦截后重试），
    #  旧档早已被上一轮替换，于是又复制一份同样的新档进 _archive，
    #  留下冗余目录，并在报告里打出「指纹未变」这种看似异常、实为误导的行。
    tmp_wav = voices_dir / f".{args.id}.new.wav"
    encode(ff, y3, tmp_wav)
    new_bytes = tmp_wav.read_bytes()
    same_audio = wav_dst.is_file() and wav_dst.read_bytes() == new_bytes

    old_fp = ""
    if same_audio:
        print("[备份] 音频与现档逐字节一致，跳过备份（避免留下冗余归档目录）")
    elif wav_dst.is_file() or meta_path.is_file():
        bak = voices_dir / "_archive" / f"{time.strftime('%Y%m%d_%H%M%S')}_{args.id}"
        bak.mkdir(parents=True, exist_ok=True)
        for p in (wav_dst, meta_path):
            if p.is_file():
                shutil.copy2(p, bak / p.name)
        old_fp = fingerprint(wav_dst, text) if wav_dst.is_file() else ""
        print(f"[备份] 旧档案 → {bak}")

    shutil.move(str(tmp_wav), str(wav_dst))
    _steps = "16k单声道+" + ("不裁静音" if args.no_trim else "裁首尾静音") + \
             f"+峰值{args.peak_db:g}dBFS" + ("，含afftdn降噪" if args.denoise else "，未降噪")
    meta = {
        "id": args.id, "name": args.name or args.id, "wav": f"{args.id}.wav",
        "prompt_text": text, "gender": args.gender, "style": args.style,
        "description": args.description or (
            f"由 scripts/make_voice_profile.py 生成；源={src.name}；处理={_steps}"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    new_fp = fingerprint(wav_dst, text)
    print(f"[OK] 档案已写入：{wav_dst}（{wav_dst.stat().st_size:,} B）+ {meta_path.name}")

    print(f"\n[指纹] 旧 {old_fp or '(无旧音频变更)'} → 新 {new_fp}"
          "（进句级缓存键，见 [PATCH-CACHE-01]）")
    if same_audio:
        print("       音频逐字节未变：音频这一维的缓存键不变，同句仍可命中缓存。")
    elif old_fp and old_fp != new_fp:
        print("       指纹已变：既有句级缓存自然失效，旧音频不会被误复用。")

    if args.purge_cache:
        cache = ROOT / "data" / "cache"
        if cache.is_dir() and any(cache.rglob("*.wav")):
            dst = ROOT / "data" / "work" / "_cache_cleanup" / time.strftime("%Y%m%d_%H%M%S")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(cache), str(dst / "cache"))
            cache.mkdir(parents=True, exist_ok=True)
            print(f"[缓存] 已可回滚地移出 → {dst / 'cache'}（清空后首轮会全量重合成）")
        else:
            print("[缓存] data/cache 为空，无需清理")
    else:
        print("       提示：旧缓存键已成孤儿行（不再命中但占空间）。"
              "需要时加 --purge-cache，或自行清理 data/cache 与 audio_cache 表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
