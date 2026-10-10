# -*- coding: utf-8 -*-
"""反向重建 D12 报告 V1.1.0 的归档副本（一次性的补救脚本，保留作审计轨迹）。

## 为什么要重建

文件名即版本号。本报告在升到 V1.1.0 之后、**归档之前**就被原地改成了 V1.2.0 的内容，
于是 `archive/D12...V1.1.0.md` 里装的是 V1.2.0 —— 名义与内容不符，审计时会直接暴露。

**正确顺序本应是「先 copy2 进 archive，再动正文」**（见 skill `doc-version-archive`）。
这次顺序错了，只能用「把 V1.2.0 的改动反向应用」来补。

## 黄金校验

升到 V1.1.0 的那次 `os.rename` 打印过 `36822 B`，这就是 V1.1.0 的**真实字节数**。
重建结果必须精确等于它 —— 这是唯一能证明「反向应用没漏项、没多删」的硬证据。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

DOC_DIR = Path(r"D:\新建文件夹\blogs\双人对话播客自动生成系统")
CURRENT = DOC_DIR / "D12性能优化与稳定性加固实施报告_V1.2.0.md"
ARCHIVE = DOC_DIR / "archive" / "D12性能优化与稳定性加固实施报告_V1.1.0.md"

GOLDEN_BYTES = 36822  # 升版到 V1.1.0 时 rename 打印的字节数

#: V1.2.0 才引入的内容，重建后**必须一个都不剩**。
MUST_BE_GONE = ["V1.2.0", "10/10", "359", "M9", "M10", "386.6 MB",
                "junit_cleanup", "mutation_d12_v2", "grace-min", "§3.5",
                "工作目录回收"]
#: V1.1.0 就该有的内容，重建后**必须在场**。
MUST_BE_PRESENT = ["V1.1.0", "356", "8/8", "§3.4", "esbuild",
                   "18 通过 / 0 失败", "check_frontend_contract.py"]


def drop_lines(text: str, pred, what: str, *, expect: int) -> str:
    """按下标/内容判定删掉整行，并断言命中条数。"""
    out, hit = [], 0
    for line in text.split("\n"):
        if pred(line):
            hit += 1
            continue
        out.append(line)
    if hit != expect:
        raise SystemExit(f"[{what}] 命中 {hit} 行，期望 {expect} —— 反向重建锚点已失效")
    return "\n".join(out)


def swap(text: str, old: str, new: str, what: str) -> str:
    """整块精确替换，要求 old 恰好出现一次。"""
    n = text.count(old)
    if n != 1:
        raise SystemExit(f"[{what}] 锚点出现 {n} 次（应为 1）")
    return text.replace(old, new, 1)


def delete_span(text: str, start_pat: str, end_pat: str, what: str) -> str:
    """删除 [start_pat 所在行, end_pat 所在行) 之间的整块。"""
    lines = text.split("\n")
    si = ei = -1
    for i, ln in enumerate(lines):
        if si < 0 and re.match(start_pat, ln):
            si = i
        if si >= 0 and re.match(end_pat, ln):
            ei = i
            break
    if si < 0 or ei < 0 or ei <= si:
        raise SystemExit(f"[{what}] 未圈出区域 start={si} end={ei}")
    return "\n".join(lines[:si] + lines[ei:])


def main() -> int:
    src = CURRENT.read_text(encoding="utf-8")
    t = src

    # 1) 版本记录里的 V1.2.0 行
    t = drop_lines(t, lambda ln: ln.startswith("| V1.2.0 |"), "版本记录行", expect=1)

    # 2) 「V1.2.0 的性质」引述块（含它前面那行分隔用的 ">"）
    i = t.find("> **V1.2.0 的性质**")
    if i < 0:
        raise SystemExit("[性质块] 未找到")
    j = t.find("\n", i) + 1
    if t[:i].endswith(">\n"):
        i -= 2
    t = t[:i] + t[j:]

    # 3) §1.2 交付物里 V1.2.0 新增的行（限定「类型」列，别误伤 §4.1/附.3 的同名行）
    t = drop_lines(t, lambda ln: ln.startswith("| ") and ln.count("|") >= 3
                   and ln.split("|")[1].strip() in {"代码", "脚本", "测试", "证据"}
                   and "V1.2.0" in ln,
                   "交付物行", expect=5)

    # 4) §2 判定口径第 6 条
    t = drop_lines(t, lambda ln: ln.startswith("6. **`data/work` 的删除有两条闸门**"),
                   "判定口径第6条", expect=1)

    # 5) §3.5 整节（保留它后面原有的 --- 分隔线）
    t = delete_span(t, r"^### 3\.5 工作目录回收", r"^---$", "§3.5")

    # 6) §4.1 全量回归
    t = swap(
        t,
        "pytest tests/ --junitxml=outputs/synth_trace/junit_cleanup.xml\n"
        "→ tests=359  failures=0  errors=0  skipped=0        # V1.2.0（当前）\n"
        "```\n"
        "\n"
        "| 版本 | 命令产物 | 结果 | 说明 |\n"
        "| --- | --- | --- | --- |\n"
        "| V1.0.0 | `junit_d12.xml` | **356 / 0 / 0 / 0** | 基线 335 → 356（`test_d12_resilience.py` 19 项 + 既有文件补 2 项） |\n"
        "| **V1.2.0** | `junit_cleanup.xml` | **359 / 0 / 0 / 0** | **356 → 359**（`test_task_runner.py` 因 `_cleanup_work` 加固新增 3 项） |\n"
        "\n"
        "> 两次计数都以 `--junitxml` 的机读产物为准（§口径：不引用记忆里的数字）。V1.1.0 未动后端，故未单独重跑 —— 其有效性由「`junit_d12.xml` 的 mtime 晚于所有 `api/**/*.py`」这一事实佐证。\n",
        "pytest tests/ --junitxml=outputs/synth_trace/junit_d12.xml\n"
        "→ tests=356  failures=0  errors=0  skipped=0\n"
        "```\n"
        "\n"
        "基线 335 → **356**（+21：`test_d12_resilience.py` 19 项 + 既有文件因本次改动补的 2 项）。分文件计数以 `junit_d12.xml` 为准。\n",
        "§4.1",
    )

    # 7) §4.2 变异测试：条数回退 + 摘掉 M9/M10 与新增说明
    t = swap(t, "→ 变异测试全部通过：10/10", "→ 变异测试全部通过：8/8", "§4.2 条数")
    t = drop_lines(t, lambda ln: ln.startswith("| **M9** |") or ln.startswith("| **M10** |"),
                   "M9/M10 行", expect=2)
    # 说明段连同它**前面那个空行**一起删（只删行会剩下一个孤立空行 —— 差 1 字节，正是黄金校验抓到的）
    t = swap(
        t,
        "\n> M9/M10 为 V1.2.0 新增（§3.5）。它们与 M8 是**同一条纪律的三个面**：M8 盯「事务横跨」、"
        "M9 盯「失败被静默」、M10 盯「失败被过度上报」。把三者都钉住，是因为「清理/回写失败」这一族"
        "**天然容易被写成两种极端** —— 要么吞掉、要么升级，而正确做法两者都不是。\n",
        "",
        "M9/M10 说明段",
    )

    # 8) §7.1 第 7 条
    t = drop_lines(t, lambda ln: ln.startswith("7. **V1.2.0 把「3.47 GB 静默累积」的根因也修了"),
                   "§7.1 第7条", expect=1)

    # 9) §7.2 下一步
    t = swap(
        t,
        "1. ~~为 `scripts/cleanup_workspace.py` 加一条定期清理 `data/work` 的入口~~ ✅ **已于 V1.2.0 完成**（§3.5，含无人值守三道安全闸）；**剩下的一步是把它真的挂进计划任务**（命令已写入 §3.5，需在有权限的环境执行一次 `schtasks /Create`）。\n",
        "1. 为 `scripts/cleanup_workspace.py` 加一条**定期清理 `data/work`** 的入口 —— 该目录曾被 safe-delete 守卫拦住而累积到 **3.47 GB**（这是唯一会持续增长且无人回收的目录）。\n",
        "§7.2",
    )

    # 10) 附.1 复跑命令
    t = swap(
        t,
        "  --junitxml=outputs/synth_trace/junit_cleanup.xml     # V1.2.0：359 项\n"
        "\n"
        "# 变异测试：10 条注入，要求「该红必须红」\n"
        "\"D:/anaconda/envs/cosyvoice/python.exe\" scripts/mutation_check_d12.py\n"
        "# 只跑某一条（例如新加的 M9）\n"
        "\"D:/anaconda/envs/cosyvoice/python.exe\" scripts/mutation_check_d12.py M9\n"
        "\n"
        "# data/work 回收（V1.2.0；手动跑先干跑看清单，定期跑用下面那行）\n"
        "\"D:/anaconda/envs/cosyvoice/python.exe\" scripts/cleanup_workspace.py --work-only\n"
        "\"D:/anaconda/envs/cosyvoice/python.exe\" scripts/cleanup_workspace.py --apply --work-only --quiet\n"
        "\n"
        "# 前端接入的自检（V1.1.0；详见附.4）\n"
        "cd frontend && \"C:/Users/zzz/.workbuddy/binaries/node/versions/22.22.2-3/node.exe\" \\\n"
        "  scripts/verify_taskview.mjs\n",
        "  --junitxml=outputs/synth_trace/junit_d12.xml\n"
        "\n"
        "# 变异测试：8 条注入，要求「该红必须红」\n"
        "\"D:/anaconda/envs/cosyvoice/python.exe\" scripts/mutation_check_d12.py\n"
        "# 只跑某一条\n"
        "\"D:/anaconda/envs/cosyvoice/python.exe\" scripts/mutation_check_d12.py M6\n",
        "附.1",
    )

    # 11) 附.3 证据文件里的 V1.2.0 行
    t = drop_lines(t, lambda ln: ln.startswith("| ") and ln.count("|") > 2
                   and "**V1.2.0**：" in ln, "附.3 行", expect=3)

    # ---- 校验 ----
    gone = [m for m in MUST_BE_GONE if m in t]
    missing = [m for m in MUST_BE_PRESENT if m not in t]
    if gone:
        raise SystemExit(f"重建后仍残留 V1.2.0 标记：{gone}")
    if missing:
        raise SystemExit(f"重建后缺少 V1.1.0 标记：{missing}")

    ARCHIVE.write_text(t, encoding="utf-8", newline="\n")
    n = os.path.getsize(ARCHIVE)
    print(f"归档副本写入：{ARCHIVE.name}")
    print(f"字节数 = {n}（黄金值 {GOLDEN_BYTES}）-> {'✅ 精确吻合' if n == GOLDEN_BYTES else '❌ 不吻合'}")
    return 0 if n == GOLDEN_BYTES else 1


if __name__ == "__main__":
    sys.exit(main())
