# -*- coding: utf-8 -*-
"""A/B 对照：`TTS_EMPTY_CACHE_EACH_SEG` 是否就是「每行 ~5s 固定开销」的元凶。

背景：D10 实测每行合成耗时 ~11s 且**与行长几乎无关**（行长 13.6~19.9 字），
而每行音频仅 ~5.5s → 行级 RTF 恒为 2.0~2.3，远高于 D3 基线稳态 1.037。
怀疑点：`synthesize()` 在每句**前后各调一次** `torch.cuda.empty_cache()`
（tts.py:641 与 653）。清空分配器后下一句要重新 cudaMalloc ~2.4GB，
Windows/WDDM 上这一步很贵。

方法：同一进程内、同一探针文本集合，**逐对交替**跑 flag=开/关，
抵消时钟与显存状态漂移。探针文本每次唯一（带随机 nonce），避免命中句级缓存。
"""
from __future__ import annotations

import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(r"D:\podcast-ai")
sys.path.insert(0, str(ROOT))

from api.config import get_settings          # noqa: E402
from api.services.tts import TTSEngine       # noqa: E402

PAIRS = 8
BASE = "这是用于对照实验的探针句子请忽略其语义只关注耗时表现"
NONCE = uuid.uuid4().hex[:6]
TEXTS = [f"{BASE}{NONCE}{i}" for i in range(PAIRS)]


def main() -> int:
    s = get_settings()
    print(f"探针 nonce={NONCE}  对数={PAIRS}  max_chars_per_seg={s.max_chars_per_seg}")
    eng = TTSEngine.instance(s)
    t0 = time.perf_counter()
    eng.load()
    print(f"引擎加载 {time.perf_counter() - t0:.1f}s  常驻 allocated={eng.vram().get('allocated_mb')}MB")

    # 逐对交替：A(开) / B(关)
    on_ts: list[float] = []
    off_ts: list[float] = []
    for i, text in enumerate(TEXTS):
        for label, flag in (("on", True), ("off", False)):
            s.tts_empty_cache_each_seg = flag
            t = time.perf_counter()
            r = eng.synthesize(text, "voice_a", speed=1.0, tone="")
            dt = time.perf_counter() - t
            assert not r.cached, "探针命中了缓存，实验无效"
            (on_ts if flag else off_ts).append(dt)
            print(f"  [{i}][{label:3s}] {dt:6.2f}s  音频 {r.duration_ms/1000:.2f}s  "
                  f"RTF {dt / (r.duration_ms / 1000):.3f}  字符 {len(text)}")

    s.tts_empty_cache_each_seg = True           # 还原
    for name, arr in (("empty_cache=开", on_ts), ("empty_cache=关", off_ts)):
        print(f"{name}: 均值 {statistics.mean(arr):.2f}s/行  中位 {statistics.median(arr):.2f}s/行  "
              f"范围 {min(arr):.2f}~{max(arr):.2f}")
    d = statistics.mean(on_ts) - statistics.mean(off_ts)
    print(f"\n差值（开 - 关）= {d:+.2f}s/行  → 60 行约 {d * 60:+.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
