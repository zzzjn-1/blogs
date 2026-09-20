# -*- coding: utf-8 -*-
"""A/B 对照：定位「每行固定开销」。

D10 实测（12 条真实链路）：
  - 每行合成耗时 ~11s，且**与行长几乎无关**（行长 13.6~19.9 字，s/行 10.7~12.3）
  - 每行音频仅 ~5.5s → 行级 RTF 恒为 2.0~2.3，而 D3 基线稳态 1.037
单点探针（33 字文本）却给出 10.21s / 音频 10.00s → RTF 1.02。
=> 强烈提示存在**与文本长度无关的固定开销**，短句被它拖累。

本探针用「行长阶梯 × empty_cache 开关」二维对照，把两个嫌疑一次隔离：
  H1 固定开销：短文本也耗时 ~10s（则 s/字 随行长急剧下降）
  H2 `tts_empty_cache_each_seg`：每句前后各 empty_cache 一次，清空分配器后
     下一句需重新 cudaMalloc ~2.5GB（Windows/WDDM 上很贵）

纪律：每路探针文本**全局唯一**（带唯一标签），避免命中句级缓存串味。
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\podcast-ai")
sys.path.insert(0, str(ROOT))

from api.config import get_settings          # noqa: E402
from api.services.tts import TTSEngine       # noqa: E402

LENGTHS = [8, 16, 32]
REPS = 3
FILLER = "对照实验的探针文本用于测量合成耗时不关注语义内容"
TAG_CHARS = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉一二三四五六七八九十"


def make_text(idx: int, length: int) -> str:
    """生成第 idx 条、恰好 length 个汉字的唯一文本（无阿拉伯数字，TN 不改写）。"""
    tag = f"探针{TAG_CHARS[idx // len(TAG_CHARS)]}{TAG_CHARS[idx % len(TAG_CHARS)]}号"
    body = FILLER * 8
    text = tag + body[:(length - len(tag))]
    assert len(text) == length, (len(text), length)
    return text


def main() -> int:
    s = get_settings()
    eng = TTSEngine.instance(s)
    t0 = time.perf_counter()
    eng.load()
    print(f"引擎加载 {time.perf_counter() - t0:.1f}s  常驻 {eng.vram().get('allocated_mb')}MB")

    # 逐条交替两路，抵消时钟/显存漂移
    rows: list[dict] = []
    idx = 0
    for rep in range(REPS):
        for length in LENGTHS:
            for cache_mode in (True, False):
                s.tts_empty_cache_each_seg = cache_mode
                text = make_text(idx, length)
                idx += 1
                t = time.perf_counter()
                r = eng.synthesize(text, "voice_a", speed=1.0, tone="")
                dt = time.perf_counter() - t
                assert not r.cached, "命中缓存，实验无效"
                rows.append({"len": len(text), "cache": cache_mode, "t": dt,
                             "audio": r.duration_ms / 1000.0})
                print(f"  [{len(text):>2}字][{'on ' if cache_mode else 'off'}] "
                      f"{dt:6.2f}s  音频 {r.duration_ms/1000:5.2f}s  "
                      f"RTF {dt/(r.duration_ms/1000):.3f}  s/字 {dt/len(text):.3f}")
    s.tts_empty_cache_each_seg = True

    print("\n==== 汇总（按行长 × empty_cache）====")
    for length in LENGTHS:
        for cache_mode in (True, False):
            arr = [r["t"] for r in rows if r["len"] == length and r["cache"] == cache_mode]
            au = [r["audio"] for r in rows if r["len"] == length and r["cache"] == cache_mode]
            print(f"  {length:>2}字 empty_cache={'on ' if cache_mode else 'off'} : "
                  f"均值 {statistics.mean(arr):5.2f}s  音频 {statistics.mean(au):5.2f}s  "
                  f"RTF {statistics.mean(arr)/statistics.mean(au):.3f}  "
                  f"s/字 {statistics.mean(arr)/length:.3f}")

    print("\n==== 固定开销估计 ====")
    for cache_mode in (True, False):
        pts = []
        for length in LENGTHS:
            arr = [r["t"] for r in rows if r["len"] == length and r["cache"] == cache_mode]
            pts.append((length, statistics.mean(arr)))
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        slope = (y2 - y1) / (x2 - x1)
        intercept = y1 - slope * x1
        print(f"  empty_cache={'on ' if cache_mode else 'off'} : "
              f"T = {slope:.3f} s/字 × 字数 + {intercept:.2f} s   "
              f"（{x1}字={y1:.2f}s, {x2}字={y2:.2f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
