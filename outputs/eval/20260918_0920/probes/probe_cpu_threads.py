# -*- coding: utf-8 -*-
"""查 CPU/线程/ONNX 配置——用于判断「段间固定 ~5.6s」是否来自 CPU 侧声码器与 HTTP 争用。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(r"D:\podcast-ai")
sys.path.insert(0, str(ROOT))

from api.config import get_settings  # noqa: E402


def main() -> int:
    import torch
    s = get_settings()
    print("cpu 逻辑核数:", os.cpu_count())
    print("torch.get_num_threads():", torch.get_num_threads())
    print("torch.get_num_interop_threads():", torch.get_num_interop_threads())
    for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "TOKENIZERS_PARALLELISM",
              "COSYVOICE_ONNX_DEVICE", "CUDA_VISIBLE_DEVICES"):
        print(f"  env {k} = {os.environ.get(k)}")
    print("cuda:", torch.cuda.is_available(),
          torch.cuda.get_device_name(0) if torch.cuda.is_available() else "")
    # 与合成相关的设置
    for name in ("tts_empty_cache_each_seg", "tts_guard_retry", "tts_guard_threshold",
                 "tts_convergence_guard", "max_chars_per_seg", "model_version",
                 "tts_warmup_text", "cache_path", "audio_cache_path", "sample_rate"):
        if hasattr(s, name):
            print(f"  settings.{name} = {getattr(s, name)!r}")
    try:
        import onnxruntime as ort
        print("onnxruntime providers:", ort.get_available_providers())
        so = ort.SessionOptions()
        print("  default intra_op_num_threads:", so.intra_op_num_threads,
              " inter_op:", so.inter_op_num_threads)
    except Exception as exc:                     # noqa: BLE001
        print("onnxruntime 探测失败:", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
