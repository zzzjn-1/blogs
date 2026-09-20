"""前端变异测试台 —— 证明单测真的会红。

「一个不能失败的检查等于没有检查」。本脚本对 `taskView.ts` / `TaskDetail.tsx` /
`History.tsx` 注入 6 个**真实可能犯的错误**，逐个要求：

    注入 → 必须变红 → 立即还原（字节级校验）→ 全部还原后必须变绿

其中 F3/F4/F5 是**纯函数测试永远抓不到**的（条件写错、JSX 漏判断），
它们只能被组件渲染测试抓住 —— 这正是「引入测试运行器」的价值所在。
所以脚本会额外打印**是哪个测试文件红的**，用来证明覆盖归属，而不是只看退出码。

用法：python scripts/mutation_check_frontend.py
退出码 0 = 全部如期变红且还原干净。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend"
SRC = FE / "src"

_NOISE = ("shell-runtime-bash-env.sh", "dirname: command not found", "cd: null directory")

#: (编号, 文件, 原文, 注入后, 说明)
MUTATIONS: list[tuple[str, Path, str, str, str]] = [
    (
        "F1",
        SRC / "lib" / "taskView.ts",
        "if (pos <= 0) return '正在合成'",
        "if (pos < 0) return '正在合成'",
        "把「0 = 正在跑」改成「0 = 前面还有 0 个」",
    ),
    (
        "F2",
        SRC / "lib" / "taskView.ts",
        "if (seg <= 0) return null",
        "if (seg < 0) return null",
        "seg=0 时不再隐藏读数（未算 ≠ 0%）",
    ),
    (
        "F3",
        SRC / "pages" / "TaskDetail.tsx",
        "const waiting = showQueue(task) && task.queue_position !== 0 && qLabel !== null",
        "const waiting = qLabel !== null",
        "去掉全部条件：pos=0（自己正在跑）也弹队列提示（仅组件测试可抓）",
    ),
    (
        "F4",
        SRC / "pages" / "History.tsx",
        "{showQueue(t) && (",
        "{false && (",
        "列表页不再显示排队位次（仅组件测试可抓）",
    ),
    (
        "F5",
        SRC / "pages" / "TaskDetail.tsx",
        "const cLabel = showCache(task) ? cacheLabel(task) : null",
        "const cLabel = cacheLabel(task)",
        "去掉阶段白名单：脚本阶段也显示缓存读数（仅组件测试可抓）",
    ),
    (
        "F6",
        SRC / "lib" / "taskView.ts",
        "return QUEUE_VISIBLE.has(t.status) && queueLabel(t) !== null",
        "return queueLabel(t) !== null",
        "去掉 showQueue 的状态白名单：终态也显示排队提示",
    ),
    (
        "F7",
        SRC / "pages" / "TaskDetail.tsx",
        "const waiting = showQueue(task) && task.queue_position !== 0 && qLabel !== null",
        "const waiting = qLabel !== null && (task.queue_position === null || task.queue_position > 0)",
        "详情页退回「自己写一套判断」的旧写法（漏状态白名单）",
    ),
]


def _node() -> str:
    if env := os.environ.get("PODCAST_NODE"):
        if Path(env).exists():
            return env
    import shutil as _sh

    if which := _sh.which("node"):
        return which
    for c in sorted(Path.home().glob(".workbuddy/binaries/node/versions/*/node.exe"), reverse=True):
        if c.exists():
            return str(c)
    raise SystemExit("找不到 node：请设置 PODCAST_NODE")


def _vitest(node: str, json_out: Path) -> tuple[int, list[str]]:
    """跑一轮 vitest，返回 (退出码, 变红的测试文件列表)。"""
    argv = [
        node,
        str(FE / "node_modules" / "vitest" / "vitest.mjs"),
        "run",
        "--reporter=json",
        f"--outputFile={json_out}",
    ]
    env = {**os.environ, "NO_COLOR": "1", "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run(
        argv, cwd=str(FE), capture_output=True, encoding="utf-8", errors="replace", env=env
    )
    files: list[str] = []
    if json_out.exists():
        try:
            data = json.loads(json_out.read_text(encoding="utf-8"))
            for tr in data.get("testResults", []):
                if tr.get("status") == "failed":
                    files.append(Path(tr.get("name", "?")).name)
        except Exception:
            pass
        json_out.unlink(missing_ok=True)
    if not files and p.returncode != 0:
        # JSON 解析不到的兜底：从文本里捞测试文件名
        files = sorted(set(re.findall(r"src[\\/][\w.\\/-]+\.test\.tsx?", (p.stdout or "") + (p.stderr or ""))))
    return p.returncode, files


def main() -> int:
    node = _node()
    print(f"node: {node}")
    print(f"工作目录: {FE}\n")

    originals: dict[Path, bytes] = {}
    for _, path, old, new, _ in MUTATIONS:
        if path not in originals:
            originals[path] = path.read_bytes()

    bad = 0
    tmp = Path(tempfile.gettempdir()) / "vitest_mutation.json"

    for mid, path, old, new, desc in MUTATIONS:
        text = originals[path].decode("utf-8")
        n = text.count(old)
        print(f"--- {mid} {path.name} :: {desc}")
        if n != 1:
            print(f"    [FAIL] 锚点命中 {n} 次（要求恰好 1 次）——锚点已漂移，变异无效")
            bad += 1
            continue
        if old == new:
            # 曾经踩过：注入值与原值相同 → 变异退化成空操作，却显示「变红」或「变绿」都无意义
            print("    [FAIL] 注入值与原文相同，变异是空操作")
            bad += 1
            continue

        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        try:
            code, files = _vitest(node, tmp)
        finally:
            path.write_bytes(originals[path])  # 立即还原，绝不留注入态

        if path.read_bytes() != originals[path]:
            print("    [FAIL] 还原后字节不一致！")
            bad += 1
            continue

        if code == 0:
            print("    [FAIL] 注入后仍然全绿 —— 这条缺陷没有被任何测试覆盖")
            bad += 1
        else:
            print(f"    [ok]   如期变红；变红文件：{', '.join(files) or '(未解析到)'}")

    # 全部还原后必须恢复全绿
    print("\n--- 还原复跑（必须全绿）")
    code, files = _vitest(node, tmp)
    if code == 0:
        print("    [ok]   全绿，工作树已干净")
    else:
        print(f"    [FAIL] 还原后仍有失败：{', '.join(files)}")
        bad += 1

    print("\n" + "=" * 56)
    print(f"结果：{len(MUTATIONS) - bad} / {len(MUTATIONS)} 如期变红" if bad else f"结果：{len(MUTATIONS)} / {len(MUTATIONS)} 全部如期变红，且还原干净")
    print("=" * 56)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
