# -*- coding: utf-8 -*-
"""把日志里每段 infer 的「起 (synthesis text) → 止 (yield speech len)」配对，得单段耗时分布。

目的：判断链路的 ~10 s/段 是**普遍现象**，还是被少数「未收敛重试/跑飞」的离群段拉高的。
两者的处置完全相反：
  - 普遍偏慢 → 查链路环境（并发/落库/进程内争用）
  - 少数离群   → 查 B 声稳定性与守卫重试（重试 = 一次完整再推理）
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime

LOG = r"D:\podcast-ai\uvicorn_resume.log"
TS = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)")


def ts(line: str) -> datetime | None:
    m = TS.search(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f") if m else None


def main() -> int:
    pending: datetime | None = None
    pending_txt = ""
    durs: list[tuple[float, str]] = []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        t = ts(line)
        if not t:
            continue
        if "synthesis text" in line:
            pending, pending_txt = t, line.strip()[-45:]
        elif "yield speech len" in line and pending is not None:
            durs.append(((t - pending).total_seconds(), pending_txt))
            pending = None

    xs = sorted(d for d, _ in durs)
    n = len(xs)
    print(f"配成 {n} 段 infer")
    print(f"  均值 {statistics.mean(xs):.2f}s  中位 {statistics.median(xs):.2f}s")
    for q, label in ((0.10, "P10"), (0.25, "P25"), (0.50, "P50"), (0.75, "P75"),
                     (0.90, "P90"), (0.95, "P95"), (0.99, "P99")):
        print(f"  {label}: {xs[min(n - 1, int(q * n))]:.2f}s")
    print(f"  最大 {xs[-1]:.2f}s  最小 {xs[0]:.2f}s")

    # 分布直方
    buckets = [(0, 2), (2, 4), (4, 6), (6, 8), (8, 10), (10, 12), (12, 16), (16, 24),
               (24, 999)]
    print("\n  分桶：")
    for lo, hi in buckets:
        c = sum(1 for x in xs if lo <= x < hi)
        print(f"    {lo:>2}~{hi:<3}s: {c:>4} 段  {c/n:>6.1%}")

    # 均值被谁拖累：去掉最慢 5%
    cut = int(0.95 * n)
    print(f"\n  去掉最慢 5%（{n - cut} 段）后均值: {statistics.mean(xs[:cut]):.2f}s")
    print("  最慢 8 段：")
    for d, txt in sorted(durs, reverse=True)[:8]:
        print(f"    {d:6.2f}s  {txt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
