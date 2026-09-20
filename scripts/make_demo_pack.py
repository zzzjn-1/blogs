# -*- coding: utf-8 -*-
"""D14 演示数据预置：为 `local` 账号批量跑出「演示级」成片（约 900 字 / 5 分钟）。

## 为什么需要这个驱动脚本，而不是在 shell 里连着敲三条命令

**中文参数不能经 shell 传递。** 本项目已经踩过一次并留下了现场：

    tasks.topic = '?????????'      # 9 个问号，对应 9 个汉字「熬夜之后怎么补回来」
    tasks.script_title = '熬夜之后怎么补回来'   # 脚本本身是对的，只有 topic 字段被吃掉

所以这里把选题写进 **Python 源文件**（UTF-8 落盘，不经过任何命令行编码层），
再用 `subprocess.run(cmd_list)` 的**列表形式**调用子进程（`shell=False`），
并显式钉死 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`。

## 它与「演示数据预置」的关系（一石二鸟）

`scripts/make_episode.py` 走的是 `TaskRunner` 完整流水线（LLM 脚本 → 双音色合成 →
动态片头尾 → 打包 → **写订阅源**），且固定归属 `local` 账号。因此每跑一期都会顺带：
1. 给 `local` 补一期**演示级成片**（历史页与订阅源真正有内容可看）；
2. 在**最终代码**上再跑一遍端到端（含 `[FIX-FEED-LATEST-01]` 的订阅源修复）；
3. 让新一期**立刻**出现在 `local` 的 feed.xml 里 —— 这是对订阅源修复的生产路径复证。

## 可续跑

每个选题跑完即落盘；重跑时若该选题已有 `DONE` 任务则跳过（`--force` 可强制重跑）。
中断后直接再执行本脚本即可，不会重复烧 GPU。

用法::
    python scripts/make_demo_pack.py            # 跑完全部选题（已完成的跳过）
    python scripts/make_demo_pack.py --dry-run  # 只生成脚本不合成
    python scripts/make_demo_pack.py --list     # 只打印选题清单
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs" / "d14_demo"
PY = sys.executable  # 必须与调用方同一个解释器（cosyvoice env）

# --------------------------------------------------------------------------- #
# 演示选题清单
#
# 选取口径：跨领域（科技 / 生活健康 / 社会经济）、标题自带吸引力、
# 与库中已有选题不重复、目标字数统一 900（成片约 4.5~5.5 分钟）。
# --------------------------------------------------------------------------- #
TOPICS: list[tuple[str, int, str]] = [
    ("人工智能会不会取代程序员", 900, "科技与职业"),
    ("为什么周末休息完反而更累", 900, "生活与健康"),
    ("小城市正在变成新的机会洼地吗", 900, "社会与经济"),
]


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip(), flags=re.UNICODE)
    return re.sub(r"-+", "-", s).strip("-")[:limit] or "episode"


def done_topics() -> set[str]:
    """库里已存在的 DONE 选题（用于跳过，避免重复烧 GPU）。"""
    import sqlite3

    db = ROOT / "data" / "podcast.db"
    if not db.is_file():
        return set()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT DISTINCT topic FROM tasks WHERE status='DONE'")
        return {r[0] for r in rows if r[0]}
    finally:
        con.close()


def run_one(idx: int, topic: str, words: int, *, dry_run: bool) -> dict:
    log = OUT / f"{idx:02d}_{slugify(topic)}.log"
    cmd = [PY, str(ROOT / "scripts" / "make_episode.py"),
           "--topic", topic, "--words", str(words)]
    if dry_run:
        cmd.append("--dry-run")

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)

    print(f"[{idx}] 开始：{topic}（{words} 字）-> {log.name}", flush=True)
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as fh:
        fh.write("CMD: %s\n\n" % " ".join(cmd))
        fh.flush()
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fh,
                              stderr=subprocess.STDOUT, env=env)
    dt = time.time() - t0
    ok = proc.returncode == 0
    print(f"[{idx}] {'完成' if ok else '失败'}：rc={proc.returncode} 耗时 {dt:.1f}s", flush=True)
    return {"idx": idx, "topic": topic, "words": words, "rc": proc.returncode,
            "ok": ok, "seconds": round(dt, 1), "log": str(log)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D14 演示数据预置（为 local 批量跑演示级成片）")
    ap.add_argument("--dry-run", action="store_true", help="只生成脚本不合成")
    ap.add_argument("--list", action="store_true", help="只打印选题清单")
    ap.add_argument("--force", action="store_true", help="已有 DONE 的选题也重跑")
    args = ap.parse_args(argv)

    if args.list:
        for i, (t, w, kind) in enumerate(TOPICS, 1):
            print(f"  {i}. [{kind}] {t}  ({w} 字)")
        return 0

    have = done_topics()
    print(f"库中已有 DONE 选题 {len(have)} 个")

    results: list[dict] = []
    skipped: list[str] = []
    t_all = time.time()
    for i, (topic, words, kind) in enumerate(TOPICS, 1):
        if topic in have and not args.force:
            print(f"[{i}] 跳过（已 DONE）：{topic}", flush=True)
            skipped.append(topic)
            results.append({"idx": i, "topic": topic, "words": words,
                            "rc": 0, "ok": True, "seconds": 0.0,
                            "log": "", "kind": kind, "skipped": True})
            continue
        r = run_one(i, topic, words, dry_run=args.dry_run)
        r["kind"] = kind
        results.append(r)

    total = time.time() - t_all
    n_ok = sum(1 for r in results if r["ok"])
    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "total_seconds": round(total, 1),
        "ok": n_ok, "failed": len(results) - n_ok,
        "skipped": skipped,
        "results": results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "demo_pack_run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 70)
    print(f"合计 {len(results)} 个选题：成功 {n_ok}，失败 {len(results) - n_ok}，"
          f"跳过 {len(skipped)}；总耗时 {total:.1f}s")
    for r in results:
        mark = "SKIP" if r.get("skipped") else ("OK  " if r["ok"] else "FAIL")
        print(f"  [{mark}] {r['topic']}  {r['seconds']}s  {r['log']}")
    print(f"汇总落盘：{OUT / 'demo_pack_run.json'}")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
