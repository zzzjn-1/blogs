# -*- coding: utf-8 -*-
"""复核 D10 报告的「段间空隙」分布：把 `yield speech len` → 下一条 `synthesis text`
的逐段间隔原样打出来（含直方图），判断它到底是「固定耗时」还是我的探针配对偏差。

用法：python probe_gap_detail.py [日志路径]
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

LOG = sys.argv[1] if len(sys.argv) > 1 else r"D:\podcast-ai\outputs\eval\20260918_0920\uvicorn_run.log"

TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")


def stamps(path: str) -> tuple[list, list]:
    starts, ends = [], []
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = TS.search(raw)
        if not m:
            continue
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
            microsecond=int(m.group(2)) * 1000)
        if "yield speech len" in raw:
            ends.append((t, raw.strip()[-60:]))
        elif "synthesis text" in raw and "too short than prompt text" not in raw:
            starts.append((t, raw.strip()[-60:]))
    return starts, ends


def main() -> None:
    starts, ends = stamps(LOG)
    print(f"日志：{LOG}")
    print(f"  synthesis text（段开始）: {len(starts)}")
    print(f"  yield speech len（段结束）: {len(ends)}")

    # 逐段配对：每段 = 一条 start 到其后第一条 end；间隙 = 该 end 到下一条 start
    gaps, infer, rows = [], [], []
    for i, (t0, txt) in enumerate(starts):
        nxt_end = next(((t, s) for t, s in ends if t >= t0), None)
        if nxt_end is None:
            break
        t1, _ = nxt_end
        nxt_start = starts[i + 1][0] if i + 1 < len(starts) else None
        infer.append((t1 - t0).total_seconds())
        if nxt_start is not None:
            g = (nxt_start - t1).total_seconds()
            if 0 <= g < 300:
                gaps.append(g)
                rows.append((i + 1, infer[-1], g, txt))

    def stat(name, xs):
        xs = sorted(xs)
        n = len(xs)
        if not n:
            print(f"  {name}: 无样本")
            return
        q = lambda p: xs[min(n - 1, int(p * n))]
        print(f"  {name}: n={n} 均值 {sum(xs)/n:.2f} 中位 {q(.5):.2f} "
              f"P10 {q(.1):.2f} P90 {q(.9):.2f} 最小 {xs[0]:.2f} 最大 {xs[-1]:.2f} "
              f"标准差 {(sum((x-sum(xs)/n)**2 for x in xs)/n)**0.5:.2f}")

    print()
    stat("infer（start→end）", infer)
    stat("gap（end→下个 start）", gaps)

    print("\n--- gap 直方图（0.5s 桶）---")
    hist = Counter(int(g / 0.5) * 0.5 for g in gaps)
    for bucket in sorted(hist):
        bar = "#" * max(1, round(hist[bucket] / max(hist.values()) * 50))
        print(f"  {bucket:5.1f}~{bucket+0.5:4.1f}s | {hist[bucket]:>4}  {bar}")

    print("\n--- 前 25 段原始配对 ---")
    print(f"  {'#':>4} {'infer s':>8} {'gap s':>7}  文本")
    for i, inf, g, txt in rows[:25]:
        print(f"  {i:>4} {inf:>8.2f} {g:>7.2f}  {txt}")

    print("\n--- gap 最大/最小的 10 条 ---")
    for i, inf, g, txt in sorted(rows, key=lambda r: -r[2])[:10]:
        print(f"  max #{i:>4} infer {inf:5.2f} gap {g:6.2f}  {txt}")


if __name__ == "__main__":
    main()
