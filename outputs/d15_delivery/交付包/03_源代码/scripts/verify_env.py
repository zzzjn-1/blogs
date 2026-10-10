#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
D1 环境自检：一键核对全部前置资源与依赖（对应计划书 1.4 节 P-1~P-9）

用法：
    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts\\verify_env.py

退出码：0 = 全部通过；1 = 存在 FAIL 项
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys

ROOT = r"D:\podcast-ai"
COSY = os.path.join(ROOT, "CosyVoice")
MODELS = {
    "CosyVoice2-0.5B": os.path.join(ROOT, "pretrained_models", "CosyVoice2-0.5B"),
    "Fun-CosyVoice3-0.5B": os.path.join(ROOT, "pretrained_models", "Fun-CosyVoice3-0.5B"),
}
# 推理必需文件（已按 cosyvoice/cli/cosyvoice.py 的加载逻辑裁剪）
REQUIRED_FILES = ["campplus.onnx", "llm.pt", "flow.pt", "hift.pt", "configuration.json"]
INFER_MODULES = [
    "torch", "torchaudio", "numpy", "onnxruntime", "whisper", "librosa", "soundfile",
    "inflect", "omegaconf", "hyperpyyaml", "transformers", "x_transformers",
    "conformer", "diffusers", "einops", "modelscope", "tqdm", "wetext",
]
APP_MODULES = ["fastapi", "uvicorn", "pydantic", "sqlalchemy", "jwt", "passlib", "openai", "tenacity", "httpx", "pydub", "feedgen"]

RESULTS: list[tuple[str, str, str]] = []


def rec(level: str, name: str, detail: str) -> None:
    RESULTS.append((level, name, detail))
    icon = {"OK": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
    print(f"{icon} {name:<34} {detail}")


def check_torch() -> None:
    try:
        import torch
        import torchaudio
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "torch / torchaudio", f"导入失败: {type(e).__name__}: {e}")
        return
    if not torch.cuda.is_available():
        rec("FAIL", "CUDA 可用性", "torch.cuda.is_available() = False")
        return
    free, total = torch.cuda.mem_get_info()
    rec("OK", "torch", f"{torch.__version__} | torchaudio {torchaudio.__version__} | cuda {torch.version.cuda}")
    rec("OK", "GPU", f"{torch.cuda.get_device_name(0)}")
    used = total - free
    level = "OK" if free / 1048576 >= 3800 else "WARN"
    rec(level, "显存", f"总 {total/1048576:.0f} MB | 已占 {used/1048576:.0f} MB | "
                       f"**净可用 {free/1048576:.0f} MB**（门槛 ≥3800 MB）")


def check_onnx() -> None:
    try:
        import onnxruntime as ort
    except Exception as e:  # noqa: BLE001
        rec("FAIL", "onnxruntime", f"导入失败: {e}")
        return
    provs = ort.get_available_providers()
    ok = provs == ["CPUExecutionProvider"] or "CUDAExecutionProvider" not in provs
    rec("OK" if ok else "WARN", "onnxruntime", f"{ort.__version__} | providers={provs}")
    if not ok:
        rec("WARN", "ONNX 显存策略", "装了 GPU 版 onnxruntime，需确认 COSYVOICE_ONNX_DEVICE=cpu 且补丁生效")

    # 补丁是否生效
    fp = os.path.join(COSY, "cosyvoice", "cli", "frontend.py")
    if os.path.isfile(fp):
        t = open(fp, encoding="utf-8", errors="replace").read()
        if "PATCH-VRAM-01" in t:
            rec("OK", "ONNX CPU 补丁", "PATCH-VRAM-01 已应用（frontend.py）")
        else:
            rec("FAIL", "ONNX CPU 补丁", "未应用 —— CPU 版 onnxruntime 会导致模型加载 ValueError")
    else:
        rec("FAIL", "frontend.py", "文件不存在")


def check_modules(mods: list[str], group: str) -> None:
    missing = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{m}({type(e).__name__})")
    if missing:
        rec("FAIL", f"{group} 依赖", f"缺失 {len(missing)}: {', '.join(missing)}")
    else:
        rec("OK", f"{group} 依赖", f"{len(mods)} 个全部可导入")


def check_matcha() -> None:
    if "COSY_ROOT" not in globals():
        pass
    p = os.path.join(COSY, "third_party", "Matcha-TTS")
    if not os.path.isdir(os.path.join(p, "matcha")):
        rec("FAIL", "Matcha-TTS 源码", "third_party/Matcha-TTS/matcha 不存在")
        return
    sys.path.insert(0, p)
    # 只验证 CosyVoice 实际用到的 4 个模块（不需要 pip install，也不需要 Cython 编译）
    mods = ["matcha.models.components.decoder", "matcha.models.components.transformer",
            "matcha.models.components.flow_matching", "matcha.hifigan.models"]
    bad = []
    for m in mods:
        try:
            importlib.import_module(m)
        except Exception as e:  # noqa: BLE001
            bad.append(f"{m}({type(e).__name__}: {e})")
    if bad:
        rec("FAIL", "matcha 模块", "; ".join(bad)[:200])
    else:
        rec("OK", "matcha 模块", "4/4 可导入（sys.path 注入，无需 pip install / Cython）")


def check_models() -> None:
    for name, d in MODELS.items():
        if not os.path.isdir(d):
            rec("WARN", f"模型 {name}", "目录不存在")
            continue
        total = 0
        for r, _, fs in os.walk(d):
            for x in fs:
                try:
                    total += os.path.getsize(os.path.join(r, x))
                except OSError:
                    pass
        missing = [f for f in REQUIRED_FILES if not os.path.isfile(os.path.join(d, f))]
        yaml_ok = any(os.path.isfile(os.path.join(d, y)) for y in ("cosyvoice2.yaml", "cosyvoice3.yaml"))
        tok = any(os.path.isfile(os.path.join(d, t)) for t in ("speech_tokenizer_v2.onnx", "speech_tokenizer_v3.onnx"))
        enc = os.path.isdir(os.path.join(d, "CosyVoice-BlankEN"))
        if missing or not yaml_ok or not tok or not enc:
            rec("FAIL", f"模型 {name}", f"缺文件 {missing} yaml={yaml_ok} tokenizer={tok} BlankEN={enc}")
        else:
            rec("OK", f"模型 {name}", f"{total/1e9:.2f} GB，必需文件齐备")
        # spk2info.pt 是可选
        if not os.path.isfile(os.path.join(d, "spk2info.pt")):
            rec("OK", f"模型 {name} spk2info", "无 spk2info.pt（可选，空字典正常走零样本）")


def check_tools() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        p = shutil.which(exe)
        if not p:
            # 兜底：检查 winget 解压目录
            base = os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages")
            found = None
            if os.path.isdir(base):
                for r, _, fs in os.walk(base):
                    if exe + ".exe" in fs:
                        found = os.path.join(r, exe + ".exe")
                        break
            if found:
                rec("WARN", exe, f"未在 PATH 中，但存在于 {found}")
            else:
                rec("FAIL", exe, "未找到（后期处理必需）")
            continue
        p2 = subprocess.run([p, "-version"], capture_output=True)
        out = ((p2.stdout or b"") + (p2.stderr or b"")).decode("utf-8", "replace")
        rec("OK", exe, (out.splitlines() or ["?"])[0][:70])
    # 关键编码器与滤镜
    p = shutil.which("ffmpeg")
    if p:
        enc = subprocess.run([p, "-hide_banner", "-encoders"], capture_output=True)
        filt = subprocess.run([p, "-hide_banner", "-filters"], capture_output=True)
        e = ((enc.stdout or b"") + (enc.stderr or b"")).decode("utf-8", "replace")
        f = ((filt.stdout or b"") + (filt.stderr or b"")).decode("utf-8", "replace")
        miss_e = [x for x in ("libmp3lame", "pcm_s16le", "aac") if x not in e]
        miss_f = [x for x in ("loudnorm", "acrossfade", "afade", "aresample") if x not in f]
        if miss_e or miss_f:
            rec("FAIL", "FFmpeg 能力", f"缺编码器 {miss_e} 滤镜 {miss_f}")
        else:
            rec("OK", "FFmpeg 能力", "libmp3lame / pcm_s16le / aac / loudnorm / acrossfade / afade / aresample 齐备")


def check_config() -> None:
    env = os.path.join(ROOT, ".env")
    if not os.path.isfile(env):
        rec("WARN", ".env", "不存在（可从 .env.example 复制）")
    else:
        keys = {}
        for line in open(env, encoding="utf-8").read().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()
        key = keys.get("LLM_API_KEY", "")
        if not key or key in ("[redacted]", "sk_your_deepseek_key_here", ""):
            rec("FAIL", ".env LLM_API_KEY", "未设置或为占位符")
        else:
            rec("OK", ".env LLM_API_KEY", f"已设置（{key[:3]}****{key[-2:]}，长度 {len(key)}）")
        rec("OK", ".env LLM_BASE_URL", keys.get("LLM_BASE_URL", "(未设置)"))
        rec("OK", ".env LLM_MODEL", keys.get("LLM_MODEL", "(未设置)"))
    # 长路径
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\FileSystem", 0, winreg.KEY_READ)
        v = winreg.QueryValueEx(k, "LongPathsEnabled")[0]
        rec("OK" if v == 1 else "WARN", "LongPathsEnabled", str(v))
    except Exception as e:  # noqa: BLE001
        rec("WARN", "LongPathsEnabled", f"读取失败: {e}")


def check_layout() -> None:
    for p in (ROOT, COSY):
        rec("OK" if os.path.isdir(p) else "FAIL", "目录", p)
    if any(ord(c) > 127 for c in ROOT):
        rec("FAIL", "项目路径", "含非 ASCII 字符，有 MAX_PATH 风险")
    else:
        rec("OK", "项目路径", f"{ROOT}（纯 ASCII）")
    # 孤儿备份残留
    trash = os.path.join(ROOT, "_orphan_trash")
    if os.path.isdir(trash):
        n = sum(len(fs) for _, _, fs in os.walk(trash))
        rec("WARN", "孤儿备份残留", f"{trash}（{n} 个文件，可手动删除）")


def main() -> int:
    print("=" * 78)
    print("D1 环境自检  |  项目:", ROOT)
    print("=" * 78)
    print("\n--- 1. 解释器与算力 ---")
    print("python:", sys.version.split()[0], "|", sys.executable)
    check_torch()
    print("\n--- 2. ONNX 与显存策略 ---")
    check_onnx()
    print("\n--- 3. 依赖 ---")
    check_modules(INFER_MODULES, "推理")
    check_modules(APP_MODULES, "应用层")
    check_matcha()
    print("\n--- 4. 模型权重 ---")
    check_models()
    print("\n--- 5. 外部工具 ---")
    check_tools()
    print("\n--- 6. 配置与环境 ---")
    check_config()
    check_layout()

    fails = [n for lv, n, _ in RESULTS if lv == "FAIL"]
    warns = [n for lv, n, _ in RESULTS if lv == "WARN"]
    print("\n" + "=" * 78)
    print(f"汇总：OK {len(RESULTS)-len(fails)-len(warns)} | WARN {len(warns)} | FAIL {len(fails)}")
    if fails:
        print("阻塞项: " + ", ".join(fails))
    if warns:
        print("提示项: " + ", ".join(warns))
    print("=" * 78)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
