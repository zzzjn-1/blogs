#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PATCH-01: 让 CosyVoice 的 ONNX Runtime provider 可配置（默认走 CPU）

背景
----
`cosyvoice/cli/frontend.py` 里 speech_tokenizer 的 provider 是**硬编码**的：

    providers=["CUDAExecutionProvider" if torch.cuda.is_available() else "CPUExecutionProvider"]

本项目运行在 **RTX 4050 Laptop，净可用显存仅约 4.3 GB** 的机器上。
该 ONNX 模型（speech_tokenizer_v2.onnx 496 MB / v3 969 MB）若走 CUDA，
会额外占用约 0.5~1.0 GB 显存 —— 在 4.3 GB 预算下这是决定性的。

同时，若安装的是 **CPU-only 的 onnxruntime**（本项目选择，体积小且不会
隐式分配显存），硬编码传入 "CUDAExecutionProvider" 会直接抛
`ValueError: Specified provider 'CUDAExecutionProvider' is not in available provider names`
导致模型根本无法加载。

本补丁把 provider 选择改为环境变量驱动，默认全部走 CPU：

    COSYVOICE_ONNX_DEVICE=cpu   # 默认：campplus + speech_tokenizer 全走 CPU
    COSYVOICE_ONNX_DEVICE=gpu   # 恢复上游行为（speech_tokenizer 走 CUDA）

幂等：重复执行不会重复插入（以 `PATCH-VRAM-01` 标记判断）。

用法：
    python patches/apply_onnx_cpu_patch.py
"""
from __future__ import annotations

import os
import sys

TARGET = r"D:\podcast-ai\CosyVoice\cosyvoice\cli\frontend.py"
MARK = "PATCH-VRAM-01"

OLD = '''        option = onnxruntime.SessionOptions()
        option.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        option.intra_op_num_threads = 1
        self.campplus_session = onnxruntime.InferenceSession(campplus_model, sess_options=option, providers=["CPUExecutionProvider"])
        self.speech_tokenizer_session = onnxruntime.InferenceSession(speech_tokenizer_model, sess_options=option,
                                                                     providers=["CUDAExecutionProvider" if torch.cuda.is_available() else
                                                                                "CPUExecutionProvider"])
'''

NEW = '''        option = onnxruntime.SessionOptions()
        option.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
        option.intra_op_num_threads = 1
        # [PATCH-VRAM-01] ONNX provider 改为环境变量驱动，默认全部走 CPU。
        #   低显存机器（本项目：净可用约 4.3 GB）下，speech_tokenizer 走 CUDA 会
        #   额外占用 0.5~1.0 GB 显存；且 CPU-only 的 onnxruntime 传入
        #   CUDAExecutionProvider 会直接抛 ValueError，导致模型无法加载。
        #   COSYVOICE_ONNX_DEVICE=cpu(默认) | gpu
        _onnx_dev = os.environ.get('COSYVOICE_ONNX_DEVICE', 'cpu').strip().lower()
        _avail_providers = onnxruntime.get_available_providers()
        _want_cuda = (_onnx_dev in ('gpu', 'cuda', 'auto')) and torch.cuda.is_available() \\
            and ('CUDAExecutionProvider' in _avail_providers)
        _st_providers = ['CUDAExecutionProvider', 'CPUExecutionProvider'] if _want_cuda else ['CPUExecutionProvider']
        logging.info('[PATCH-VRAM-01] onnxruntime providers=%s (COSYVOICE_ONNX_DEVICE=%s, available=%s)',
                     _st_providers, _onnx_dev, _avail_providers)
        self.campplus_session = onnxruntime.InferenceSession(campplus_model, sess_options=option, providers=["CPUExecutionProvider"])
        self.speech_tokenizer_session = onnxruntime.InferenceSession(speech_tokenizer_model, sess_options=option,
                                                                     providers=_st_providers)
'''


def main() -> int:
    if not os.path.isfile(TARGET):
        print(f"[FAIL] 目标文件不存在: {TARGET}")
        return 2

    src = open(TARGET, encoding="utf-8").read()

    if MARK in src:
        print(f"[SKIP] 补丁已应用（找到标记 {MARK}）")
        return 0

    # 前置依赖：os / logging 必须在模块级绑定。
    # 注意：不能用子串匹配（"import logging" 会误命中
    # `from cosyvoice.utils.file_utils import logging, load_wav`），必须用 AST
    # 收集真实绑定名。该文件通过 `from cosyvoice.utils.file_utils import logging`
    # 间接获得标准 logging 模块，故 %s 惰性格式化可用。
    import ast as _ast
    bound: set[str] = set()
    for node in _ast.parse(src).body:
        if isinstance(node, _ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
    missing = [m for m in ("os", "logging") if m not in bound]
    if missing:
        print(f"[FAIL] frontend.py 模块级未绑定 {missing}，无法安全打补丁")
        print(f"       当前已绑定: {sorted(bound)}")
        return 3

    if src.count(OLD) != 1:
        print(f"[FAIL] 目标片段匹配 {src.count(OLD)} 次（期望 1 次），上游可能已变更")
        return 4

    with open(TARGET + ".orig", "w", encoding="utf-8", newline="") as fh:
        fh.write(src)

    patched = src.replace(OLD, NEW, 1)
    with open(TARGET, "w", encoding="utf-8", newline="") as fh:
        fh.write(patched)

    # 语法自检
    import ast
    try:
        ast.parse(patched)
    except SyntaxError as exc:
        with open(TARGET, "w", encoding="utf-8", newline="") as fh:
            fh.write(src)
        print(f"[FAIL] 补丁后语法错误，已回滚: {exc}")
        return 5

    print(f"[OK] 补丁已应用: {TARGET}")
    print(f"     备份: {TARGET}.orig")
    print("     默认 COSYVOICE_ONNX_DEVICE=cpu（全部 ONNX 走 CPU）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
