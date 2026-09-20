# -*- coding: utf-8 -*-
"""阶段分解：把「合成阶段」拆成「单段 infer」「段间空隙」「后期 6 步」。

本报告第三章·第四章的核心数字（infer 5.02 s/段、空隙 6.30 s/段、后期 19 s/集、
空隙占合成 56%）全部由本脚本从后端日志时间戳重建，可复跑复核。

依赖的日志标记（都是代码里现有的 INFO/WARNING，不需改代码）：
  - `synthesis text <文本>`        一段推理开始（CosyVoice 前端）
  - `yield speech len <秒>, rtf ...` 首个（stream=False 下也是唯一）yield 的时刻
    **注意：该标记在 `token2wav()`（flow+hift 声码器）之后才打出**，
    所以 `synthesis text → yield speech len` 是「前端+LLM+声码器」，
    声码器**不在**段间空隙里。曾经的注释写成「不含声码器」，是错的
    （对照 `cosyvoice/cli/model.py` 的 else 分支）。
  - `动态片头已生成：...`            该集合成结束、进入后期
  - `[N/6] ...` / `[6/6] 导出 mp3`  后期 6 步的进度标记
  - `seg <k>/<N> -> seg_<k>.wav`    分段落盘

用法：python probe_log_stage_split.py [日志路径]
"""
from __future__ import annotations

import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

TS = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+)")
DEFAULT_LOG = Path(__file__).resolve().parents[1] / "uvicorn_run.log"


def ts(line: str) -> datetime | None:
    m = TS.search(line)
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f") if m else None


def parse(path: Path):
    seg_starts, seg_ends, posts, post_ends = [], [], [], []
    for line in path.open(encoding="utf-8", errors="replace"):
        t = ts(line)
        if not t:
            continue
        if "synthesis text" in line:
            seg_starts.append(t)
        elif "yield speech len" in line:
            seg_ends.append(t)
        elif "动态片头已生成" in line:
            posts.append(t)
        elif "[6/6] 导出 mp3" in line:
            post_ends.append(t)
    return seg_starts, seg_ends, posts, post_ends


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_LOG
    seg_starts, seg_ends, posts, post_ends = parse(path)
    print(f"日志：{path}")
    print(f"  seg_start={len(seg_starts)}  seg_end={len(seg_ends)}  "
          f"post_start={len(posts)}  post_end={len(post_ends)}")

    # ---- 逐集：合成跨度 / 后期跨度 ----
    print(f"\n{'集':>3} {'段数':>4} {'合成跨度s':>9} {'s/段':>6} {'后期s':>7}  区间")
    synth_total = post_total = 0.0
    for i, t_post in enumerate(posts):
        lo = posts[i - 1] if i else None
        segs = [s for s in seg_starts if (lo is None or s > lo) and s < t_post]
        if not segs:
            continue
        span = (t_post - segs[0]).total_seconds()
        ends = [e for e in post_ends if e > t_post]
        post_s = (ends[0] - t_post).total_seconds() if ends else float("nan")
        synth_total += span
        post_total += post_s
        print(f"{i+1:>3} {len(segs):>4} {span:>9.1f} {span/len(segs):>6.2f} {post_s:>7.1f}"
              f"  {segs[0].strftime('%H:%M:%S')}~{ends[0].strftime('%H:%M:%S') if ends else 'NA'}")

    # ---- 配对「起→止」：infer 本体耗时分布 ----
    pending, durs = None, []
    for line_ts, kind in _stream(path):
        if kind == "start":
            pending = line_ts
        elif kind == "end" and pending is not None:
            durs.append((line_ts - pending).total_seconds())
            pending = None
    # ---- 段间空隙：yield → 下一条 synthesis text ----
    gaps = []
    for i, t in enumerate(seg_ends):
        nxt = [s for s in seg_starts if s > t]
        if nxt:
            gaps.append((nxt[0] - t).total_seconds())

    def rep(name: str, xs: list[float]) -> None:
        if not xs:
            print(f"  {name}: 无样本")
            return
        q = sorted(xs)
        print(f"  {name}: n={len(xs)} 合计 {sum(xs):.0f}s 均值 {statistics.mean(xs):.2f}s "
              f"中位 {statistics.median(xs):.2f}s P90 {q[int(0.9*len(q))-1]:.2f}s 最大 {max(xs):.2f}s")

    print("\n=== infer 本体（synthesis text → yield speech len，仅含 LLM 出 token）===")
    rep("infer", durs)
    print("\n=== 段间空隙（yield → 下一条 synthesis text，含声码器/落盘/回调）===")
    rep("gap", gaps)
    if durs and gaps:
        ti, tg = sum(durs), sum(gaps)
        print(f"\n  infer 合计 {ti:.0f}s / 空隙合计 {tg:.0f}s → 空隙占合成阶段 {tg/(ti+tg):.0%}")
    if synth_total:
        print(f"  合成跨度合计 {synth_total:.0f}s / 后期合计 {post_total:.0f}s "
              f"→ 后期占全程 {post_total/(synth_total+post_total):.0%}")
    return 0


def _stream(path: Path):
    for line in path.open(encoding="utf-8", errors="replace"):
        t = ts(line)
        if not t:
            continue
        if "synthesis text" in line:
            yield t, "start"
        elif "yield speech len" in line:
            yield t, "end"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
