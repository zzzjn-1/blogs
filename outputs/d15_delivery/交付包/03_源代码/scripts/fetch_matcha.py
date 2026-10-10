#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
拉取 Matcha-TTS 子模块（CosyVoice 推理必需）。

背景：GitHub 直连在本机极不稳定（20~60 KB/s，git fetch 频繁 `curl 18 / early EOF`）。
      改为「多候选镜像 + 单流断点续传 + zip 完整性校验」下载源码 zip 后解压。
"""
from __future__ import annotations

import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _dl import download  # noqa: E402

REPO = "shivammehta25/Matcha-TTS"
ROOT = r"D:\podcast-ai"
DEST = os.path.join(ROOT, r"CosyVoice\third_party\Matcha-TTS")
ZIP_PATH = os.path.join(ROOT, "downloads", "Matcha-TTS.zip")

CANDIDATES = [
    f"https://codeload.github.com/{REPO}/zip/refs/heads/main",
    f"https://ghfast.top/https://github.com/{REPO}/archive/refs/heads/main.zip",
    f"https://ghproxy.net/https://github.com/{REPO}/archive/refs/heads/main.zip",
    f"https://github.com/{REPO}/archive/refs/heads/main.zip",
]

NEEDED = ["setup.py", "matcha", os.path.join("matcha", "__init__.py"),
          os.path.join("matcha", "models"), "requirements.txt"]


def valid_zip(path: str) -> bool:
    if not os.path.exists(path):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None and len(z.namelist()) > 20
    except Exception:
        return False


def main() -> int:
    print("=== 1) 下载源码 zip ===")
    ok = False
    for url in CANDIDATES:
        print("  尝试:", url[:90])
        if valid_zip(ZIP_PATH):
            print("    已有可用 zip，跳过下载")
            ok = True
            break
        got, size, err = download(url, ZIP_PATH)
        print("    结果:", "OK" if got else "FAIL", err)
        if got and valid_zip(ZIP_PATH):
            ok = True
            break
        if os.path.exists(ZIP_PATH):
            os.remove(ZIP_PATH)          # 损坏则丢弃，换下一个镜像
    if not ok:
        print("[FAIL] 所有镜像均未取到可用 zip")
        return 1

    with zipfile.ZipFile(ZIP_PATH) as z:
        print(f"  zip 校验通过：{len(z.namelist())} 条目")

    print("\n=== 2) 解压到 third_party/Matcha-TTS ===")
    tmp = DEST + "__tmp"
    for p in (tmp,):
        if os.path.exists(p):
            shutil.rmtree(p)
    with zipfile.ZipFile(ZIP_PATH) as z:
        root = z.namelist()[0].split("/")[0]
        z.extractall(tmp)
    if os.path.exists(DEST):
        shutil.rmtree(DEST)
    os.replace(os.path.join(tmp, root), DEST)
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== 3) 关键文件校验 ===")
    allok = True
    for n in NEEDED:
        p = os.path.join(DEST, n)
        e = os.path.exists(p)
        allok &= e
        print(f"  {'OK  ' if e else 'MISS'}  {n}")
    print("\n完成:", DEST)
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
