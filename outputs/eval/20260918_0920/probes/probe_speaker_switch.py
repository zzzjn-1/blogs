# -*- coding: utf-8 -*-
"""A/B 对照：**说话人来回切换**是否是「链路比引擎慢一倍」的元凶。

事实链：
  - 引擎级探针（只用 voice_a）: 16 字 → 5.5s，音频 5.1s，RTF 1.08
  - 真实链路（A/B 交替）      : 15.2 字/段 → 10.34 s/段，音频 4.78s，RTF 2.16
  - 已排除：empty_cache（开关无差异）、收敛守卫（430 段只触发 3 次）、后期（只占 4%）
  - 唯一显著的未验证差异：探针全程 voice_a；真实脚本是 A/B 交替
    → 若每次切换说话人都要重新准备 prompt（参考音频特征），就会给**每一段**加上固定开销。

设计：同一批文本、同一进程，三臂对照，逐条交替以抵消漂移
  arm1 全用 voice_a
  arm2 全用 voice_b
  arm3 A/B 交替（贴近真实）
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

REPS = 4
FILLER = "这是对照实验的探针文本用于测量合成耗时不关注语义"
TAG_CHARS = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉一二三四五六七八九十"
TARGET_LEN = 16


def make_text(idx: int) -> str:
    """第 idx 条唯一文本（无阿拉伯数字，避免 TN 改写）。"""
    tag = f"测{TAG_CHARS[idx // len(TAG_CHARS)]}{TAG_CHARS[idx % len(TAG_CHARS)]}号"
    text = tag + FILLER[:(TARGET_LEN - len(tag))]
    assert len(text) == TARGET_LEN
    return text


def main() -> int:
    s = get_settings()
    eng = TTSEngine.instance(s)
    t0 = time.perf_counter()
    eng.load()
    print(f"引擎加载 {time.perf_counter() - t0:.1f}s")

    arms: dict[str, list[float]] = {"only_a": [], "only_b": [], "alternate": [], "abab": []}
    idx = 0
    for rep in range(REPS):
        for arm in ("only_a", "only_b", "alternate"):
            if arm == "only_a":
                spk = "A"
            elif arm == "only_b":
                spk = "B"
            else:
                spk = "A" if idx % 2 == 0 else "B"
            text = make_text(idx)
            idx += 1
            t = time.perf_counter()
            r = eng.synthesize(text, spk, speed=1.0, tone="")
            dt = time.perf_counter() - t
            assert not r.cached
            arms[arm].append(dt)
            print(f"  [{arm:9s} spk={spk}] {dt:6.2f}s  音频 {r.duration_ms/1000:5.2f}s  "
                  f"RTF {dt/(r.duration_ms/1000):.3f}")

    # 单独一组：真·A/B/A/B 严格交替（上面对照里 only_* 段落会打断交替节奏）
    for rep in range(REPS):
        for k, spk in enumerate(("A", "B", "A", "B")):
            text = make_text(idx)
            idx += 1
            t = time.perf_counter()
            r = eng.synthesize(text, spk, speed=1.0, tone="")
            dt = time.perf_counter() - t
            assert not r.cached
            arms["abab"].append(dt)
            print(f"  [abab      spk={spk}] {dt:6.2f}s  音频 {r.duration_ms/1000:5.2f}s  "
                  f"RTF {dt/(r.duration_ms/1000):.3f}")

    print("\n==== 汇总（16 字文本）====")
    for arm, arr in arms.items():
        print(f"  {arm:9s}: 均值 {statistics.mean(arr):5.2f}s  中位 {statistics.median(arr):5.2f}s  "
              f"n={len(arr)}")
    base = statistics.mean(arms["only_a"])
    print(f"\n  交替 / 单说话人 倍数 = {statistics.mean(arms['abab']) / base:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
