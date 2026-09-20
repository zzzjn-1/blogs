#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
模型权重下载（ModelScope 直连 HTTP，不依赖 modelscope SDK）。

为什么不用 SDK：SDK 需先装 modelscope（依赖 torch 等），而权重下载本身是纯 HTTP；
ModelScope 大文件通道实测约 20 MB/s，直连更快且可与依赖安装并行。

**按需下载**：逐行核对了 cosyvoice/cli/cosyvoice.py 的加载逻辑（v2 在 L141 起的
CosyVoice2 类，v3 在 L191 起的 CosyVoice3 类），只为推理必需的文件付费：

CosyVoice3 跳过（共省约 4.3 GB）
  - llm.rl.pt                     2.02 GB  RL 微调变体，代码只加载 llm.pt
  - flow.decoder.estimator.fp32.onnx 1.33 GB  仅 load_trt=True 时加载
  - speech_tokenizer_v3.batch.onnx   0.97 GB  代码只加载非 batch 版
  - asset/dingding.png              0.12 MB  示例图，与推理无关
CosyVoice2 跳过
  - flow.decoder.estimator.fp32.onnx 286 MB  仅 load_trt 时用
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _dl import download  # noqa: E402

ROOT = r"D:\podcast-ai"
PRETRAINED = os.path.join(ROOT, "pretrained_models")

BLANKEN = [
    "CosyVoice-BlankEN/config.json",
    "CosyVoice-BlankEN/generation_config.json",
    "CosyVoice-BlankEN/merges.txt",
    "CosyVoice-BlankEN/model.safetensors",
    "CosyVoice-BlankEN/tokenizer_config.json",
    "CosyVoice-BlankEN/vocab.json",
]

MODELS = {
    "cosyvoice3": {
        "id": "FunAudioLLM/Fun-CosyVoice3-0.5B-2512",
        "subdir": "Fun-CosyVoice3-0.5B",
        "files": [
            "campplus.onnx",
            "configuration.json",
            "cosyvoice3.yaml",
            "flow.pt",
            "hift.pt",
            "llm.pt",
            "speech_tokenizer_v3.onnx",
            "README.md",
        ] + BLANKEN,
    },
    "cosyvoice2": {
        "id": "iic/CosyVoice2-0.5B",
        "subdir": "CosyVoice2-0.5B",
        "files": [
            "campplus.onnx",
            "configuration.json",
            "cosyvoice2.yaml",
            "flow.pt",
            "hift.pt",
            "llm.pt",
            "speech_tokenizer_v2.onnx",
            "flow.encoder.fp16.zip",
            "README.md",
        ] + BLANKEN,
    },
}


def list_files(model_id: str) -> dict[str, int]:
    url = (f"https://www.modelscope.cn/api/v1/models/{model_id}"
           f"/repo/files?Revision=master&Recursive=true")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40) as r:
        d = json.loads(r.read().decode("utf-8"))
    out = {}
    for f in d.get("Data", {}).get("Files", []):
        if f.get("Type") == "tree":
            continue
        out[f["Path"]] = f.get("Size") or 0
    return out


def file_url(model_id: str, path: str) -> str:
    return (f"https://www.modelscope.cn/api/v1/models/{model_id}"
            f"/repo?Revision=master&FilePath={urllib.parse.quote(path)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", default="cosyvoice3",
                    choices=["cosyvoice3", "cosyvoice2", "both"])
    args = ap.parse_args()

    todo = ["cosyvoice3", "cosyvoice2"] if args.which == "both" else [args.which]
    failures = []

    for key in todo:
        spec = MODELS[key]
        mid, subdir, files = spec["id"], spec["subdir"], spec["files"]
        target = os.path.join(PRETRAINED, subdir)
        print("=" * 70)
        print(f"[{key}] {mid}  ->  {target}")
        print("=" * 70)
        try:
            remote = list_files(mid)
        except Exception as e:  # noqa: BLE001
            print(f"[FAIL] 取文件清单失败: {type(e).__name__}: {e}")
            failures.append(subdir)
            continue

        total = sum(remote.get(f, 0) for f in files)
        print(f"  远端 {len(remote)} 个文件；本次按需下载 {len(files)} 个，"
              f"合计约 {total / 1e9:.2f} GB\n")

        for i, f in enumerate(files, 1):
            size = remote.get(f, 0)
            dest = os.path.join(target, f.replace("/", os.sep))
            if os.path.exists(dest) and os.path.getsize(dest) == size and size:
                print(f"  [{i}/{len(files)}] {f}  已存在 ({size / 1e6:.1f} MB)")
                continue
            print(f"  [{i}/{len(files)}] {f}  ({size / 1e6:.1f} MB)")
            ok, got, err = download(file_url(mid, f), dest, size or None)
            if not ok:
                print(f"      [FAIL] {err}")
                failures.append(f"{subdir}/{f}")
            elif size and got != size:
                print(f"      [FAIL] 长度不符 {got}/{size}")
                failures.append(f"{subdir}/{f}")

    print("\n" + "=" * 70)
    for key in todo:
        p = os.path.join(PRETRAINED, MODELS[key]["subdir"])
        n = sum(os.path.getsize(os.path.join(r, x))
                for r, _d, fs in os.walk(p) for x in fs) if os.path.isdir(p) else 0
        print(f"  {MODELS[key]['subdir']:<26} {n / 1e9:>7.2f} GB")
    if failures:
        print(f"\n失败 {len(failures)} 项：{failures[:8]}")
        return 1
    print("\n全部就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
