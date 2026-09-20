# -*- coding: utf-8 -*-
"""分析 `synth_trace` 落下的逐段阶段时间线，定位「段间固定开销」（R21）。

用法：
    python scripts/analyze_synth_trace.py outputs/synth_trace/run1.jsonl

它回答三个问题：

1. 一段的时间**花在哪些阶段**（按总耗时排序，带占比）——直接看出谁是大头；
2. 一次 infer 里，**首个 yield 之前 / 之后**各占多少（靠 cosyvoice 打的
   `yield speech len` 切分）。**注意：这个标记切不出「LLM vs 声码器」**——
   `stream=False` 路径上 `token2wav()`(flow+hift) 在 `yield` 之前完成，
   声码器被算进了前段，详见 `split_at_yield` 的 docstring；
3. **段与段之间**还有多少没被打点的残余（循环迭代、调度）。

设计上只做纯函数 + 一个渲染函数，便于单测与变异测试。所有耗时按
「相邻打点之差」计算 —— 打点是绝对单调时钟，不是累计值。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

#: 打点名 → 人类可读的阶段说明。缺名不影响计算，只是表里显示原名。
STAGE_LABELS: dict[str, str] = {
    "l1_cache_key": "循环起手（算缓存键）",
    "s0_enter": "进入 synthesize()",
    "s1_load": "engine.load()（已加载则近零）",
    "s2_cache_lookup": "查句级缓存（未命中）",
    "s3_tmpdir": "建缓存目录",
    "s4_empty_cache_pre": "torch.cuda.empty_cache()（段前）",
    "s5_infer": "★ infer 本体（前端+LLM+flow+hift）",
    "s6_guard": "收敛守卫（未触发则近零）",
    "s7_save_wav": "torchaudio.save（写缓存 wav）",
    "s8_write_cache": "_write_cache（copy2 + json）",
    "s9_vram": "读显存 vram()",
    "s10_empty_cache_post": "torch.cuda.empty_cache()（段后）",
    "l2_named_copy": "收尾 + 命名副本 copy2",
    "l3_progress_cb": "SQLite 落库回调 on_progress",
    "_entry": "首打点之前（算缓存键）",
    "_tail": "末打点之后（返回 + 收尾）",
}


def load_records(path: str | Path) -> list[dict]:
    """读 jsonl。坏行跳过并计数，不让一行脏数据废掉整份诊断。"""
    out: list[dict] = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("marks"):
            out.append(rec)
    return out


def stage_durations(rec: dict) -> list[tuple[str, float]]:
    """把一条记录的有序打点折成 `[(阶段名, 毫秒)]`。

    **归属约定**：`mark(name)` 是在该阶段**做完之后**打的，所以
    `marks[i-1] → marks[i]` 这段耗时归给 `marks[i]` 的名字。
    首打点之前的耗时记为 `_entry`，末打点之后的记为 `_tail`。

    这两条不是随便定的 —— 反过来归属（把区间算给起始打点）会让整张表**错位一格**：
    `empty_cache(pre) → infer` 那段会被标成「empty_cache」，于是 4.9 s 的
    infer 本体被显示成 empty_cache 开销，结论直接指向错误的优化方向。
    实际就是这么错过一次。
    """
    marks = rec.get("marks") or []
    if len(marks) < 2:
        return []
    out: list[tuple[str, float]] = []
    if marks[0][1] > 1e-6:
        out.append(("_entry", marks[0][1]))
    for (_, d0), (n1, d1) in zip(marks, marks[1:]):
        out.append((n1, max(0.0, d1 - d0)))
    tail = rec.get("dur_ms", 0.0) - marks[-1][1]
    if tail > 1e-6:
        out.append(("_tail", tail))
    return out


def aggregate(records: list[dict]) -> list[dict]:
    """按阶段聚合：总耗时、均值、中位、P90、最大、占比。占比按总耗时算。"""
    buckets: dict[str, list[float]] = {}
    for rec in records:
        for name, ms in stage_durations(rec):
            buckets.setdefault(name, []).append(ms)
    grand = sum(sum(v) for v in buckets.values()) or 1.0
    rows = []
    for name, xs in buckets.items():
        s = sorted(xs)
        rows.append({
            "stage": name,
            "label": STAGE_LABELS.get(name, name),
            "n": len(xs),
            "total_ms": round(sum(xs), 1),
            "mean_ms": round(statistics.fmean(xs), 1),
            "median_ms": round(statistics.median(xs), 1),
            "p90_ms": round(s[min(len(s) - 1, int(0.9 * len(s)))], 1),
            "max_ms": round(s[-1], 1),
            "share_pct": round(sum(xs) / grand * 100, 1),
        })
    rows.sort(key=lambda r: -r["total_ms"])
    return rows


def split_at_yield(rec: dict) -> tuple[float, float] | None:
    """把一次 infer 在「首个 yield 时刻」切成两半，单位毫秒：`(pre_yield, post_yield)`。

    infer 区间 = `s4_empty_cache_pre` 打点 → `s5_infer` 打点；
    `llm_end` 是 cosyvoice 打 `yield speech len` 的绝对时刻（挂在 root logger 上抓的）。

    **这个切分不区分 LLM 与声码器 —— 别把它读成占比。**
    `stream=False` 路径上 `CosyVoice2Model.tts` 的顺序是
    `p.join()`（等 LLM 线程出完 token）→ `self.token2wav(...)`（flow + hift 声码器）
    → `yield`，而 `yield speech len` 是在**消费到这个 yield 之后**才打的
    （`cosyvoice/cli/model.py` 的 else 分支，可自行核对）。于是声码器落在前段里，
    后段只剩生成器收尾（弹 uuid 字典等）+ 消费者拿到最后一帧。

    缺 `llm_end`（日志级别没到 INFO）时返回 None —— 不能拿 0 冒充「前段不耗时」。
    """
    marks = rec.get("marks") or []
    t0 = rec.get("t0")
    llm_end = rec.get("llm_end")
    if not marks or t0 is None or llm_end is None:
        return None
    pos = {n: d for n, d in marks}
    if "s4_empty_cache_pre" not in pos or "s5_infer" not in pos:
        return None
    start = t0 + pos["s4_empty_cache_pre"] / 1000.0
    end = t0 + pos["s5_infer"] / 1000.0
    pre_ms = (llm_end - start) * 1000.0
    post_ms = (end - llm_end) * 1000.0
    if pre_ms < 0 or post_ms < 0:
        return None
    return round(pre_ms, 1), round(post_ms, 1)


def inter_segment_gaps(records: list[dict]) -> list[float]:
    """上一段 t1 → 下一段 t0 的残余（毫秒）。负值（时钟回退/乱序）丢弃。"""
    gaps: list[float] = []
    for a, b in zip(records, records[1:]):
        g = (b.get("t0", 0.0) - a.get("t1", 0.0)) * 1000.0
        if g >= 0:
            gaps.append(round(g, 1))
    return gaps


def _stat_line(name: str, xs: list[float]) -> str:
    if not xs:
        return f"  {name}: 无样本"
    s = sorted(xs)
    return (f"  {name}: n={len(xs)} 总 {sum(xs)/1000:.1f}s "
            f"均值 {statistics.fmean(xs):.0f}ms 中位 {statistics.median(xs):.0f}ms "
            f"P90 {s[min(len(s)-1, int(0.9*len(s)))]:.0f}ms 最大 {s[-1]:.0f}ms")


def render_report(records: list[dict], *, source: str = "") -> str:
    out: list[str] = []
    total_seg = len(records)
    fresh = [r for r in records if not r.get("cached")]
    total_ms = sum(r.get("dur_ms", 0.0) for r in fresh)
    out.append(f"来源：{source}")
    out.append(f"段数：{total_seg}（其中缓存未命中 {len(fresh)}）"
               f"｜逐段总耗时 {total_ms/1000:.1f}s"
               f"｜段均 {total_ms/len(fresh) if fresh else 0:.0f}ms")
    out.append("")

    out.append("=== 阶段耗时（按未命中段聚合，按总耗时排序）===")
    out.append(f"  {'阶段':<34} {'n':>4} {'总 s':>8} {'均值 ms':>8} "
               f"{'中位 ms':>8} {'P90 ms':>8} {'占比':>6}")
    for r in aggregate(fresh):
        out.append(f"  {r['label'][:34]:<34} {r['n']:>4} {r['total_ms']/1000:>8.1f} "
                   f"{r['mean_ms']:>8.0f} {r['median_ms']:>8.0f} "
                   f"{r['p90_ms']:>8.0f} {r['share_pct']:>5.1f}%")

    out.append("")
    out.append("=== infer 在首个 yield 处切分（≠ LLM / 声码器占比）===")
    pairs = [p for p in (split_at_yield(r) for r in fresh) if p]
    if pairs:
        out.append(_stat_line("至首个 yield（前端+LLM+flow+hift）", [p[0] for p in pairs]))
        out.append(_stat_line("yield 之后（生成器收尾）", [p[1] for p in pairs]))
        tot = sum(a + b for a, b in pairs)
        out.append(f"  前段占 {sum(a for a, _ in pairs)/tot*100:.1f}%"
                   "　—— 该标记**不**把 LLM 与声码器分开，声码器也在前段里")
    else:
        out.append("  无样本：日志里没有 `yield speech len`。"
                   "root logger 级别需为 INFO（uvicorn --log-level info）才能抓到。")

    out.append("")
    out.append("=== 段与段之间的残余（未被打点覆盖的循环/调度）===")
    gaps = inter_segment_gaps(fresh)
    if gaps:
        out.append(_stat_line("残余", gaps))
        out.append(f"  → 逐段合计 {sum(gaps)/1000:.1f}s")
    else:
        out.append("  无样本（可能只有一段）")
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = sys.argv[1]
    try:
        recs = load_records(src)
    except FileNotFoundError:
        print(f"文件不存在：{src}")
        return 1
    if not recs:
        print(f"没读到有效记录：{src}")
        return 1
    print(render_report(recs, source=src))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
