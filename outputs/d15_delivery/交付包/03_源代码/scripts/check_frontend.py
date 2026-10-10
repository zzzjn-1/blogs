"""前端统一门禁 —— 一条命令跑完前端全部质量门。

    1. 前后端字段契约   check_frontend_contract.py（防「后端加字段、前端类型没跟」的静默漂移）
    2. TypeScript 类型   tsc --noEmit
    3. 单元测试          vitest run
    4. 生产构建          vite build（--full 才跑）

用法：

    python scripts/check_frontend.py           # 契约 + 类型 + 单测（默认，快）
    python scripts/check_frontend.py --full    # 再加生产构建

退出码 0 = 全绿；非 0 = 第一处失败即停，后续步骤标 SKIP（不掩盖失败原因）。

**为什么不直接调 npm run**：本环境 shell 没有 coreutils，且 `npm.cmd` 直接 spawn
会报 EINVAL；因此统一用 node 去跑 `node_modules/` 下的入口文件，少一层中介。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend"

#: subprocess 输出里这些行是沙箱 shell 的噪音，没有诊断价值，统一丢掉。
_NOISE = (
    "shell-runtime-bash-env.sh",
    "dirname: command not found",
    "cd: null directory",
)


def _node() -> str:
    """定位 node 可执行文件：环境变量 > PATH > 本环境的托管版本。"""
    if env := os.environ.get("PODCAST_NODE"):
        if Path(env).exists():
            return env
    if which := shutil.which("node"):
        return which
    candidates = sorted(
        Path.home().glob(".workbuddy/binaries/node/versions/*/node.exe"),
        reverse=True,
    )
    for c in candidates:
        if c.exists():
            return str(c)
    raise SystemExit("找不到 node：请设置 PODCAST_NODE 或把 node 加入 PATH")


def _clean(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not any(n in ln for n in _NOISE)]


def _run(name: str, argv: list[str], cwd: Path, tail_ok: int = 10) -> tuple[bool, list[str]]:
    missing = argv[0] if not Path(argv[0]).exists() and "/" in argv[0] else None
    if missing:
        print(f"\n=== {name} ===")
        print(f"  -> FAIL 入口不存在：{missing}")
        return False, []

    print(f"\n=== {name} ===")
    print("  $ " + " ".join(argv))
    env = {**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, encoding="utf-8", errors="replace", env=env
    )
    lines = _clean((p.stdout or "") + (p.stderr or ""))
    if p.returncode == 0:
        # 通过时只留尾部几行：全量输出会淹掉真正需要看的信息
        for ln in lines[-tail_ok:]:
            print("    " + ln)
        print("  -> PASS")
        return True, lines
    # 失败时全量输出，不做截断 —— 截断过的报错等于二次调试
    for ln in lines:
        print("    " + ln)
    print(f"  -> FAIL (exit {p.returncode})")
    return False, lines


def main() -> int:
    full = "--full" in sys.argv[1:]
    node = _node()

    steps: list[tuple[str, list[str], Path]] = [
        ("1/4 前后端字段契约", [sys.executable, str(ROOT / "scripts" / "check_frontend_contract.py")], ROOT),
        ("2/4 TypeScript 类型检查", [node, str(FE / "node_modules" / "typescript" / "bin" / "tsc"), "--noEmit"], FE),
        ("3/4 单元测试 (vitest)", [node, str(FE / "node_modules" / "vitest" / "vitest.mjs"), "run"], FE),
    ]
    if full:
        steps.append(("4/4 生产构建 (vite)", [node, str(FE / "node_modules" / "vite" / "bin" / "vite.js"), "build"], FE))
    else:
        print("（跳过生产构建；加 --full 可一并跑）")

    results: list[tuple[str, str]] = []
    for name, argv, cwd in steps:
        ok, _ = _run(name, argv, cwd)
        results.append((name, "PASS" if ok else "FAIL"))
        if not ok:
            for n, _ in steps[len(results) :]:
                results.append((n, "SKIP"))
            break

    print("\n" + "=" * 56)
    for name, status in results:
        mark = {"PASS": "[ok]  ", "FAIL": "[FAIL]", "SKIP": "[skip]"}[status]
        print(f"  {mark} {name}")
    bad = [n for n, s in results if s == "FAIL"]
    print("=" * 56)
    print("结果：" + ("全部通过" if not bad else f"{len(bad)} 处失败"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
