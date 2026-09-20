# -*- coding: utf-8 -*-
"""A/B 对照：**CPU/DB 争用**是否就是「链路比引擎探针慢一倍」的原因。

事实链（全部来自实测）：
  - 链路日志：infer 本体 5.02 s/段（LLM 出 token），**段间空隙 6.30 s/段**（占合成阶段 56%）
  - 同一引擎、无并发的在进程内探针：整段只需 ~5.5 s
  - 空隙分布极规整（中位 5.60 / P90 5.62）→ 不是锁争用的尖刺，是固定开销被放大
  - 环境：16 逻辑核，torch 8 线程；onnxruntime 只有 CPUExecutionProvider（无 CUDA EP）
  - 链路里每段都要开 SQLAlchemy Session 落库，同时 HTTP 轮询一直在打

设计：同一进程、同一文本集合，安静 vs 有竞争两路交替：
  竞争负载 = 8 个 CPU 忙线程（Python 侧计算）+ 1 个每 50ms 提交一次的 SQLite 写线程
"""
from __future__ import annotations

import statistics
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(r"D:\podcast-ai")
sys.path.insert(0, str(ROOT))

from api.config import get_settings          # noqa: E402
from api.services.tts import TTSEngine       # noqa: E402

REPS = 5
LOAD_THREADS = 8
FILLER = "这是对照实验的探针文本用于测量合成耗时不关注语义"
TAG_CHARS = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉一二三四五六七八九十"
TARGET_LEN = 16

_stop = threading.Event()


def cpu_burn() -> None:
    x = 0
    while not _stop.is_set():
        for i in range(50_000):
            x += i * i
        x = 0


def db_burn(path: Path) -> None:
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS probe(k INTEGER PRIMARY KEY, v TEXT)")
    i = 0
    while not _stop.is_set():
        con.execute("INSERT OR REPLACE INTO probe(k,v) VALUES(?,?)", (i % 50, "x" * 200))
        con.commit()
        i += 1
        time.sleep(0.05)
    con.close()


def make_text(idx: int) -> str:
    tag = f"竞{TAG_CHARS[idx // len(TAG_CHARS)]}{TAG_CHARS[idx % len(TAG_CHARS)]}号"
    text = tag + FILLER[:(TARGET_LEN - len(tag))]
    assert len(text) == TARGET_LEN
    return text


def main() -> int:
    s = get_settings()
    eng = TTSEngine.instance(s)
    eng.load()
    db_path = Path(r"D:\podcast-ai\_probe_burn.db")
    if db_path.exists():
        db_path.unlink()

    quiet: list[float] = []
    loaded: list[float] = []
    idx = 0
    for rep in range(REPS):
        for arm in ("quiet", "loaded"):
            threads: list[threading.Thread] = []
            if arm == "loaded":
                _stop.clear()
                for _ in range(LOAD_THREADS):
                    t = threading.Thread(target=cpu_burn, daemon=True)
                    t.start()
                    threads.append(t)
                t = threading.Thread(target=db_burn, args=(db_path,), daemon=True)
                t.start()
                threads.append(t)
                time.sleep(0.3)
            text = make_text(idx)
            idx += 1
            t0 = time.perf_counter()
            r = eng.synthesize(text, "A", speed=1.0, tone="")
            dt = time.perf_counter() - t0
            assert not r.cached
            if arm == "loaded":
                _stop.set()
                for t in threads:
                    t.join(timeout=2)
                time.sleep(0.2)
            (quiet if arm == "quiet" else loaded).append(dt)
            print(f"  [{arm:6s}] {dt:6.2f}s  音频 {r.duration_ms/1000:5.2f}s  "
                  f"RTF {dt/(r.duration_ms/1000):.3f}")

    print("\n==== 汇总（16 字文本，单段 synthesize 全程）====")
    print(f"  quiet : 均值 {statistics.mean(quiet):5.2f}s  中位 {statistics.median(quiet):5.2f}s")
    print(f"  loaded: 均值 {statistics.mean(loaded):5.2f}s  中位 {statistics.median(loaded):5.2f}s")
    print(f"  倍数 = {statistics.mean(loaded)/statistics.mean(quiet):.2f}x")
    if db_path.exists():
        db_path.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
