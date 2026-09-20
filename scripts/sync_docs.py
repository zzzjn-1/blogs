# -*- coding: utf-8 -*-
"""把交付文档从工作区单向同步进仓库 docs/ 镜像目录。

为什么需要它
------------
交付文档的**唯一真源**在工作区 `D:\\新建文件夹\\blogs\\双人对话播客自动生成系统\\`
（该目录受 `doc-version-archive` 约定管理：升版=重命名+归档）。但 GitHub 仓库根是
`D:\\podcast-ai`，两者不在同一目录树。为了让仓库自带完整交付文档，这里把源目录
**单向镜像**到 `docs/`。

纪律
----
1. **单向**：只从源 → docs/，绝不反向。docs/ 下禁止手工编辑，改了会被下次同步覆盖。
2. **镜像而非合并**：源里删掉的文件，docs/ 里也要删掉（否则仓库里会留下幽灵文档）。
3. **字节级校验**：复制后立刻逐文件比对字节数与 sha256，不一致即报错退出。
4. `--check` 模式**不写盘**，只报告是否存在漂移 —— 可当门禁用。

用法
----
    python scripts/sync_docs.py            # 执行同步
    python scripts/sync_docs.py --check    # 只检查漂移，非 0 退出表示有漂移
    python scripts/sync_docs.py --src <目录> --dst <目录>   # 覆盖路径（供自测）
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys

DEFAULT_SRC = r"D:\新建文件夹\blogs\双人对话播客自动生成系统"
DEFAULT_DST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

# 源目录里不参与镜像的噪音（本工具自身的工作产物）
SKIP_NAMES = {"__pycache__"}
SKIP_SUFFIX = (".pyc", ".tmp", ".bak")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def walk_files(root: str) -> dict[str, str]:
    """返回 {相对路径: 绝对路径}。路径分隔符统一成 '/' 以便跨平台比较。"""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES]
        for name in filenames:
            if name.endswith(SKIP_SUFFIX):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            out[rel] = full
    return out


def diff(src_root: str, dst_root: str) -> dict[str, list[str]]:
    src = walk_files(src_root)
    dst = walk_files(dst_root) if os.path.isdir(dst_root) else {}

    added, changed, same = [], [], []
    for rel, sfull in src.items():
        if rel not in dst:
            added.append(rel)
            continue
        if sha256(sfull) == sha256(dst[rel]):
            same.append(rel)
        else:
            changed.append(rel)
    removed = [rel for rel in dst if rel not in src]
    return {
        "added": sorted(added),
        "changed": sorted(changed),
        "removed": sorted(removed),
        "same": sorted(same),
    }


def apply_sync(src_root: str, dst_root: str) -> None:
    src = walk_files(src_root)
    dst = walk_files(dst_root) if os.path.isdir(dst_root) else {}

    for rel in dst:
        if rel not in src:
            os.remove(dst[rel])

    for rel, sfull in src.items():
        target = os.path.join(dst_root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(sfull, target)


def prune_empty_dirs(root: str) -> None:
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if not os.listdir(dirpath):
            os.rmdir(dirpath)


def verify_identical(src_root: str, dst_root: str) -> list[str]:
    """逐文件字节级校验，返回不一致清单（空 = 完全一致）。"""
    problems: list[str] = []
    src = walk_files(src_root)
    dst = walk_files(dst_root)
    for rel in sorted(set(src) | set(dst)):
        if rel not in src:
            problems.append("仅存在于 docs/：%s" % rel)
        elif rel not in dst:
            problems.append("docs/ 缺失：%s" % rel)
        elif os.path.getsize(src[rel]) != os.path.getsize(dst[rel]):
            problems.append(
                "字节数不一致：%s (%d vs %d)"
                % (rel, os.path.getsize(src[rel]), os.path.getsize(dst[rel]))
            )
        elif sha256(src[rel]) != sha256(dst[rel]):
            problems.append("内容不一致：%s" % rel)
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="交付文档单向镜像 → 仓库 docs/")
    ap.add_argument("--src", default=DEFAULT_SRC, help="文档真源目录")
    ap.add_argument("--dst", default=DEFAULT_DST, help="仓库内镜像目录")
    ap.add_argument("--check", action="store_true", help="只检查漂移，不写盘")
    args = ap.parse_args(argv)

    src_root, dst_root = args.src, args.dst
    print("源目录：%s" % src_root)
    print("镜像  ：%s" % dst_root)
    print()

    if not os.path.isdir(src_root):
        print("错误：源目录不存在 → %s" % src_root)
        return 2

    d = diff(src_root, dst_root)
    print(
        "差异：新增 %d / 变更 %d / 删除 %d / 相同 %d"
        % (len(d["added"]), len(d["changed"]), len(d["removed"]), len(d["same"]))
    )
    for rel in d["added"]:
        print("  + %s" % rel)
    for rel in d["changed"]:
        print("  M %s" % rel)
    for rel in d["removed"]:
        print("  - %s" % rel)
    print()

    if args.check:
        drift = len(d["added"]) + len(d["changed"]) + len(d["removed"])
        if drift:
            print("检查结论：docs/ 与源目录存在 %d 处漂移（未写盘）。" % drift)
            return 1
        print("检查结论：docs/ 与源目录完全一致。")
        return 0

    if not os.path.isdir(dst_root):
        os.makedirs(dst_root, exist_ok=True)
    apply_sync(src_root, dst_root)
    prune_empty_dirs(dst_root)

    problems = verify_identical(src_root, dst_root)
    print("同步完成，字节级校验：")
    if problems:
        print("  发现 %d 个问题：" % len(problems))
        for p in problems:
            print("   !! %s" % p)
        return 1

    files = walk_files(dst_root)
    total = sum(os.path.getsize(p) for p in files.values())
    print("  全部一致：%d 个文件 / %.2f MB" % (len(files), total / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
