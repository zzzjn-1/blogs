"""
双人对话播客自动生成系统 —— 模型权重下载脚本（ModelScope 通道）

背景：本机实测 HuggingFace 不可达（HTTP 000），ModelScope 可达（302），
      因此权重统一走 modelscope SDK。

用法：
    python scripts/download_models.py                # 只下主模型
    python scripts/download_models.py --with-ttsfrd  # 连 ttsfrd 资源一起下

依赖：modelscope（见 requirements-win.txt）
"""
from __future__ import annotations

import argparse
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRETRAINED = os.path.join(PROJECT_ROOT, "pretrained_models")

# 官方 README 的 ID → 本地目录名约定（目录名必须与 AutoModel(model_dir=...) 一致）
MODELS = [
    # (modelscope_id, local_subdir, 说明)
    ("FunAudioLLM/Fun-CosyVoice3-0.5B-2512", "Fun-CosyVoice3-0.5B",
     "主模型：双音色零样本合成（官方强烈推荐，CER 1.21 / SS 78.0）"),
]

OPTIONAL_MODELS = [
    ("iic/CosyVoice-ttsfrd", "CosyVoice-ttsfrd",
     "可选：更优的文本正则化。不装则自动回退 wetext"),
]


def dir_size_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1024 / 1024


def download(models: list[tuple[str, str, str]]) -> int:
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("[FATAL] 未安装 modelscope，请先执行：")
        print('        pip install -r requirements-win.txt')
        return 2

    os.makedirs(PRETRAINED, exist_ok=True)
    failed: list[str] = []

    for model_id, subdir, note in models:
        target = os.path.join(PRETRAINED, subdir)
        print(f"\n{'=' * 66}")
        print(f"下载 {model_id}")
        print(f"说明 {note}")
        print(f"目标 {target}")
        print("=" * 66)
        t0 = time.time()
        try:
            snapshot_download(model_id, local_dir=target)
            print(f"[OK] 完成，耗时 {time.time() - t0:.0f}s，"
                  f"体积 {dir_size_mb(target):.1f} MB")
        except Exception as e:  # noqa: BLE001 - 需要把任何失败都记录下来继续
            print(f"[FAIL] {model_id}: {type(e).__name__}: {e}")
            failed.append(model_id)

    print("\n" + "=" * 66)
    print("汇总")
    print("=" * 66)
    for model_id, subdir, _ in models:
        target = os.path.join(PRETRAINED, subdir)
        ok = os.path.isdir(target) and dir_size_mb(target) > 1
        print(f"  {'[OK]  ' if ok else '[缺失]'} {subdir:<26} "
              f"{dir_size_mb(target):>9.1f} MB" if ok else
              f"  [缺失] {subdir:<26} {'-':>9}")

    if failed:
        print(f"\n失败项：{failed}")
        print("排查建议：")
        print("  1) 优先用官方镜像：pip 源换 https://mirrors.aliyun.com/pypi/simple/")
        print("  2) 网络抖动时直接重跑本脚本，modelscope 支持断点续传")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="下载 CosyVoice 模型权重（ModelScope 通道）")
    ap.add_argument("--with-ttsfrd", action="store_true",
                    help="同时下载可选的 ttsfrd 文本正则化资源")
    args = ap.parse_args()

    todo = list(MODELS)
    if args.with_ttsfrd:
        todo += OPTIONAL_MODELS

    print(f"项目根目录：{PROJECT_ROOT}")
    print(f"权重根目录：{PRETRAINED}")
    print(f"待下载 {len(todo)} 项")
    return download(todo)


if __name__ == "__main__":
    sys.exit(main())
