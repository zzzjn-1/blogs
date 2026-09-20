# -*- coding: utf-8 -*-
"""量化「段与段之间的空隙」——合成阶段的真正大头在哪。

已知（同一份日志）：
  415 段 infer 合计 ≈ 2080s（均值 5.02s/段，与引擎级探针一致）
  但合成阶段总跨度 4339s
  → 差额 ~2250s 落在 infer 之外。本脚本把它量出来，并按「空隙附着的事件」归类。

空隙里可能发生的事（按代码路径）：
  torchaudio.save 落盘 / _write_cache 写 wav+meta / shutil.copy2 命名副本 /
  self.vram() 查询 / on_progress 回调(DB 落库) /
  torch.cuda.empty_cache() ×2（清空分配器，下段要重新 cudaMalloc）
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime

LOG = r"D:\podcast-ai\uvicorn_resume.log"
TS = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)")
EVENTS = ("synthesis text", "yield speech len", "seg ", "动态片头已生成", "[1/6]", "[6/6]")


def ts(line: str) -> datetime | None:
    m = TS.search(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f") if m else None


def main() -> int:
    evs: list[tuple[datetime, str]] = []
    for line in open(LOG, encoding="utf-8", errors="replace"):
        t = ts(line)
        if not t:
            continue
        for k in EVENTS:
            if k in line:
                evs.append((t, k))
                break
    evs.sort(key=lambda e: e[0])
    print(f"事件总数 {len(evs)}")

    # 只统计「合成阶段」内的空隙：每条 infer 的 yield 之后、下一条 synthesis text 之前
    infers: list[float] = []
    gaps: list[float] = []
    seg_save_gap: list[float] = []      # yield -> 紧接着的 seg k/N 落盘
    i = 0
    while i < len(evs):
        if evs[i][1] == "synthesis text":
            start = evs[i][0]
            j = i + 1
            while j < len(evs) and evs[j][1] != "yield speech len":
                j += 1
            if j >= len(evs):
                break
            infers.append((evs[j][0] - start).total_seconds())
            # yield 之后到下一条 synthesis text 之间
            k = j + 1
            nxt = None
            while k < len(evs):
                if evs[k][1] == "synthesis text":
                    nxt = evs[k][0]
                    break
                k += 1
            if nxt:
                gaps.append((nxt - evs[j][0]).total_seconds())
            # 落盘事件在 yield 之后的第一个 seg 标记
            m = j + 1
            while m < len(evs) and evs[m][1] not in ("seg ", "synthesis text"):
                m += 1
            if m < len(evs) and evs[m][1] == "seg ":
                seg_save_gap.append((evs[m][0] - evs[j][0]).total_seconds())
            i = j + 1
        else:
            i += 1

    def rep(name: str, xs: list[float]) -> None:
        if not xs:
            print(f"  {name}: 无样本")
            return
        print(f"  {name}: n={len(xs)}  合计 {sum(xs):.0f}s  均值 {statistics.mean(xs):.2f}s  "
              f"中位 {statistics.median(xs):.2f}s  P90 {sorted(xs)[int(0.9*len(xs))-1]:.2f}s  "
              f"最大 {max(xs):.2f}s")

    print("\n=== infer 本体 ===")
    rep("infer", infers)
    print("\n=== 段间空隙（yield → 下一条 synthesis text）===")
    rep("gap", gaps)
    print("\n=== 其中：yield → 紧随的 seg 落盘 ===")
    rep("save", seg_save_gap)

    if infers and gaps:
        ti, tg = sum(infers), sum(gaps)
        print(f"\ninfer 合计 {ti:.0f}s / 空隙合计 {tg:.0f}s / 比 {tg/ti:.2f}"
              f"  → 空隙占合成阶段 {tg/(ti+tg):.0%}")
        print(f"推算每段空隙 {statistics.mean(gaps):.2f}s × 430 段 ≈ {statistics.mean(gaps)*430:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
