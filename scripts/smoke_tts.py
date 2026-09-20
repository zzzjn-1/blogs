#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D1 冒烟测试：CosyVoice 加载 / 合成 / 显存与性能基线采集

本机硬约束（实测）：
    RTX 4050 Laptop，总显存 6141 MiB，桌面已占约 1.5 GB，**净可用约 4.3 GB**
    计划书 V1.3.0 硬门槛：峰值显存 ≤ 3.8 GB

采集三类数据（回填计划书 2.3 / 2.5 表）：
  A. 模型加载耗时 + 加载后常驻显存
  B. **朴素路径**：每句都重算 prompt 特征（speech_token ONNX + fbank + mel）
  C. **生产路径**：先用 add_zero_shot_spk 注册音色档案，之后每句只算文本 token
     -> B 与 C 的差值就是「音色档案」省下的开销，是 4.32 GB 下的关键杠杆
  D. RTF 与峰值显存

用法：
    python scripts/smoke_tts.py --model cosyvoice2
    python scripts/smoke_tts.py --model both          # A/B 对比选型
    python scripts/smoke_tts.py --model cosyvoice2 --no-profile   # 只测朴素路径
"""
from __future__ import annotations

import argparse
import os
import sys
import time

# ---- 必须在 import torch 之前生效 ----
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# 本项目默认：ONNX 组件全部走 CPU（省 0.5~1.0 GB 显存）；见 patches/apply_onnx_cpu_patch.py
os.environ.setdefault("COSYVOICE_ONNX_DEVICE", "cpu")

ROOT = r"D:\podcast-ai"
COSY_ROOT = os.path.join(ROOT, "CosyVoice")
OUT_DIR = os.path.join(ROOT, "outputs", "smoke")
sys.path.insert(0, os.path.join(COSY_ROOT, "third_party", "Matcha-TTS"))
sys.path.insert(0, COSY_ROOT)

MODELS = {
    "cosyvoice2": os.path.join(ROOT, "pretrained_models", "CosyVoice2-0.5B"),
    "cosyvoice3": os.path.join(ROOT, "pretrained_models", "Fun-CosyVoice3-0.5B"),
}

PROMPT_WAV = os.path.join(COSY_ROOT, "asset", "zero_shot_prompt.wav")
PROMPT_TRANSCRIPT = "希望你以后能够做的比我还好呦。"
# CosyVoice3 与 CosyVoice2 的 prompt_text 语义不同：
#   CV2 传「prompt 音频的纯转写文本」
#   CV3 必须形如 'You are a helpful assistant.<|endofprompt|>' + 转写文本
#   （见 cosyvoice/llm/llm.py:479 断言 与 example.py:76），否则 AssertionError：
#    "assert 151646 in text, '<|endofprompt|> not detected in CosyVoice3 ...'"
CV3_PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"
PROMPT_TEXT = PROMPT_TRANSCRIPT  # 由 run_one 按模型改写

# 三段测试文本，逼近真实播客单句（含数字/英文/标点，走规范化路径）
SENTENCES = [
    "欢迎收听本期节目，今天我们来聊一个有趣的话题。",
    "根据 2026 年的统计，国内播客听众规模已经突破 3 亿。",
    "其中大约 45% 的人每周至少听 2 小时，这个数字还在增长。",
]

VRAM_BUDGET_ALLOC_MB = 3600      # 主口径（计划书 V1.4.0）
VRAM_BUDGET_RESERVED_MB = 4200   # 辅口径
VRAM_LIMIT_MB = VRAM_BUDGET_ALLOC_MB  # 兼容旧引用


def mb(n: float) -> float:
    return round(n / 1048576, 1)


def vram(tag: str) -> None:
    import torch
    if not torch.cuda.is_available():
        print(f"  [显存] {tag}: CUDA 不可用")
        return
    free, total = torch.cuda.mem_get_info()
    print(f"  [显存] {tag}: allocated={mb(torch.cuda.memory_allocated())} MB | "
          f"reserved={mb(torch.cuda.memory_reserved())} MB | "
          f"peak_alloc={mb(torch.cuda.max_memory_allocated())} MB | "
          f"系统级已占={mb(total - free)}/{mb(total)} MB")


def synth(model, text, prompt_wav, spk_id="") -> tuple[str | None, float]:
    """合成一句，返回 (wav路径, 耗时秒)。

    文件名用 sha1 前 12 位：Python 的 str hash 每进程随机化（PYTHONHASHSEED），
    不可复现；且 A/B 两模型必须落在不同子目录，否则同文本会互相覆盖。
    """
    import hashlib
    import torchaudio
    t0 = time.time()
    out_path = None
    key = hashlib.sha1(f"{text}|{spk_id or 'naive'}".encode("utf-8")).hexdigest()[:12]
    for i, out in enumerate(model.inference_zero_shot(
            text, PROMPT_TEXT, prompt_wav, zero_shot_spk_id=spk_id,
            stream=False, speed=1.0, text_frontend=True)):
        out_path = os.path.join(OUT_DIR, f"seg_{key}_{i}.wav")
        torchaudio.save(out_path, out["tts_speech"], model.sample_rate)
    return out_path, time.time() - t0


def audio_seconds(paths: list[str]) -> float:
    import torchaudio
    total = 0.0
    for p in paths:
        if not p:
            continue
        w, sr = torchaudio.load(p)
        total += w.shape[1] / sr
    return total


def run_one(key: str, args) -> dict:
    import torch
    from cosyvoice.cli.cosyvoice import AutoModel

    global OUT_DIR, PROMPT_TEXT
    OUT_DIR = os.path.join(ROOT, "outputs", "smoke", key)  # A/B 分目录，避免互相覆盖
    PROMPT_TEXT = (CV3_PROMPT_PREFIX + PROMPT_TRANSCRIPT
                   if key == "cosyvoice3" else PROMPT_TRANSCRIPT)

    model_dir = MODELS[key]
    res: dict = {"model": key, "ok": False}
    print("\n" + "=" * 74)
    print(f"模型 {key}  ->  {model_dir}")
    print(f"prompt_text : {PROMPT_TEXT}")
    print("=" * 74)
    if not os.path.isdir(model_dir):
        print("  [SKIP] 模型目录不存在")
        return {**res, "err": "model dir missing"}

    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()

    # ---------- A. 加载 ----------
    print("\n--- A) 加载模型 (fp16) ---")
    t0 = time.time()
    model = AutoModel(model_dir=model_dir, fp16=args.fp16)
    load_s = time.time() - t0
    print(f"  加载耗时 {load_s:.1f} s | sample_rate={model.sample_rate}")
    vram("加载后（常驻）")
    resid_mb = mb(torch.cuda.memory_allocated()) if torch.cuda.is_available() else 0.0
    res.update(load_s=round(load_s, 1), resident_mb=resid_mb, sample_rate=model.sample_rate)

    # ⚠️ prompt_wav 必须是「路径字符串」，不能传 load_wav() 出来的张量：
    #   frontend._extract_speech_feat(prompt_wav) 内部会执行 load_wav(prompt_wav, 24000)，
    #   而 load_wav 走 torchaudio.load(uri) -> 传张量直接 TypeError: Invalid file。
    #   官方 example.py 传的也是 './asset/zero_shot_prompt.wav' 路径。
    prompt = PROMPT_WAV
    os.makedirs(OUT_DIR, exist_ok=True)

    # ---------- 预热 ----------
    print("\n--- 预热（CUDA 首次调用含图/内核编译，必须单独计掉）---")
    t0 = time.time()
    synth(model, "这是一次用于预热的合成测试。", prompt)
    print(f"  预热耗时 {time.time() - t0:.2f} s")
    torch.cuda.reset_peak_memory_stats()

    # ---------- B. 朴素路径 ----------
    if not args.only_profile:
        print(f"\n--- B) 朴素路径：每句重算 prompt 特征（{len(SENTENCES)} 句）---")
        torch.cuda.empty_cache()
        t0 = time.time()
        naive_wavs = []
        per = []
        for s in SENTENCES:
            p, dt = synth(model, s, prompt)
            naive_wavs.append(p)
            per.append(round(dt, 2))
            print(f"    {dt:6.2f}s  {s[:24]}...")
        naive_s = time.time() - t0
        naive_dur = audio_seconds(naive_wavs)
        # 单句平均（含 prompt 重算开销）
        print(f"  合计 {naive_s:.2f} s / 音频 {naive_dur:.2f} s -> RTF {naive_s/naive_dur:.3f}")
        print(f"  单句耗时 {per}")
        res.update(naive_s=round(naive_s, 2), naive_rtf=round(naive_s / naive_dur, 3), naive_per=per)

    # ---------- C. 生产路径（音色档案）----------
    if not args.only_naive:
        print("\n--- C) 生产路径：注册音色档案后逐句合成 ---")
        t0 = time.time()
        model.add_zero_shot_spk(PROMPT_TEXT, PROMPT_WAV, "smoke_spk")
        reg_s = time.time() - t0
        print(f"  注册音色档案耗时 {reg_s:.2f} s（一次性）")
        print(f"  已注册音色: {model.list_available_spks()}")

        torch.cuda.empty_cache()
        torch.cuda.reset_peak_memory_stats()
        t0 = time.time()
        prof_wavs = []
        per2 = []
        for s in SENTENCES:
            p, dt = synth(model, s, prompt, spk_id="smoke_spk")
            prof_wavs.append(p)
            per2.append(round(dt, 2))
            print(f"    {dt:6.2f}s  {s[:24]}...")
        prof_s = time.time() - t0
        prof_dur = audio_seconds(prof_wavs)
        print(f"  合计 {prof_s:.2f} s / 音频 {prof_dur:.2f} s -> RTF {prof_s/prof_dur:.3f}")
        print(f"  单句耗时 {per2}")
        res.update(reg_s=round(reg_s, 2), profile_s=round(prof_s, 2),
                   profile_rtf=round(prof_s / prof_dur, 3), profile_per=per2,
                   avg_sentence_s=round(sum(per2) / len(per2), 2))
        if "naive_per" in res:
            saved = sum(res["naive_per"]) - sum(per2)
            pct = saved / sum(res["naive_per"]) * 100 if sum(res["naive_per"]) else 0
            print(f"\n  >> 音色档案节省：{saved:.2f} s / {len(SENTENCES)} 句（{pct:.1f}%）")
            res["profile_saved_s"] = round(saved, 2)
            res["profile_saved_pct"] = round(pct, 1)

    # ---------- D. 显存与推算 ----------
    peak = torch.cuda.max_memory_allocated() / 1048576 if torch.cuda.is_available() else 0
    print(f"\n--- D) 显存峰值 ---")
    vram("全程峰值")
    peak_res = torch.cuda.max_memory_reserved() / 1048576 if torch.cuda.is_available() else 0
    ok_alloc = peak <= VRAM_BUDGET_ALLOC_MB
    ok_res = peak_res <= VRAM_BUDGET_RESERVED_MB
    print(f"  **peak_allocated = {peak:.0f} MB（门槛 {VRAM_BUDGET_ALLOC_MB} -> "
          f"{'达标' if ok_alloc else '超限'}） | "
          f"peak_reserved = {peak_res:.0f} MB（门槛 {VRAM_BUDGET_RESERVED_MB} -> "
          f"{'达标' if ok_res else '超限'}）**")

    rtf = res.get("profile_rtf") or res.get("naive_rtf") or 0
    if rtf:
        # 1000 字 ≈ 3.8 分钟音频
        est = 3.8 * 60 * rtf
        print(f"  推算：1000 字（约 3.8 min 音频）合成 = {est:.0f} s ≈ {est/60:.1f} min"
              f"  -> {'满足' if est <= 240 else '压缩后期耗时后仍可满足' if est <= 420 else '**需启动云端兜底**'}")
    res.update(peak_mb=round(peak), peak_reserved_mb=round(peak_res), ok=True)

    del model
    torch.cuda.empty_cache()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="cosyvoice2",
                    choices=["cosyvoice2", "cosyvoice3", "both"])
    ap.add_argument("--fp16", action="store_true", default=True)
    ap.add_argument("--no-fp16", dest="fp16", action="store_false")
    ap.add_argument("--only-naive", action="store_true")
    ap.add_argument("--only-profile", action="store_true")
    args = ap.parse_args()

    import torch
    print("=" * 74)
    print("环境自检")
    print("=" * 74)
    print("python          :", sys.version.split()[0])
    print("torch           :", torch.__version__)
    print("cuda available  :", torch.cuda.is_available(), "| cuda:", torch.version.cuda)
    if torch.cuda.is_available():
        print("device          :", torch.cuda.get_device_name(0))
        free, total = torch.cuda.mem_get_info()
        print(f"显存            : 总 {mb(total)} MB | 空闲 {mb(free)} MB | 已占 {mb(total-free)} MB")
    print("ALLOC_CONF      :", os.environ.get("PYTORCH_CUDA_ALLOC_CONF"))
    print("ONNX_DEVICE     :", os.environ.get("COSYVOICE_ONNX_DEVICE"))
    try:
        import onnxruntime as ort
        print("onnxruntime     :", ort.__version__, ort.get_available_providers())
    except Exception as e:  # noqa: BLE001
        print("onnxruntime     : 导入失败 -", e)

    keys = ["cosyvoice2", "cosyvoice3"] if args.model == "both" else [args.model]
    results = []
    for k in keys:
        try:
            results.append(run_one(k, args))
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            results.append({"model": k, "ok": False, "err": f"{type(e).__name__}: {e}"})

    print("\n" + "=" * 74)
    print("汇总（回填计划书 2.3 / 2.5）")
    print("=" * 74)
    hdr = f"{'模型':<12}{'加载s':>7}{'常驻MB':>9}{'RTF':>8}{'峰值MB':>9}{'单句s':>8}"
    print(hdr)
    for r in results:
        if r.get("ok"):
            print(f"{r['model']:<12}{r.get('load_s',0):>7}{r.get('resident_mb',0):>9}"
                  f"{r.get('profile_rtf') or r.get('naive_rtf',0):>8}"
                  f"{r.get('peak_reserved_mb',0):>9}{r.get('avg_sentence_s',0):>8}")
        else:
            print(f"{r['model']:<12}  失败: {r.get('err')}")
    print("\n输出音频:", OUT_DIR)

    # 落盘 JSON，供回填计划书 2.3 / 2.5 表
    import json
    out = os.path.join(ROOT, "outputs", "smoke_result.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"args": vars(args), "results": results}, fh, ensure_ascii=False, indent=2)
    print("结果 JSON:", out)
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
