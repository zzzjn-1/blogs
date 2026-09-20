# -*- coding: utf-8 -*-
"""D12 实机验证 · 阶段二：等重启后的任务续跑成 DONE，并收集全部证据。

配合 `verify_d12_phase1_kill.py` 使用。跑本脚本时，后端应已由 shell 后台重启
（重启后启动钩子会自动扫到被中断的任务并续跑）。

用法（cwd 必须是仓库根）：
    <cosyvoice python> scripts/verify_d12_phase2_resume.py \
        --server-log outputs/xxx/server2.log --task-id <阶段一打印的 id>
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "d12_crash_recover"
BASE = "http://127.0.0.1:8000"


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with (OUT / "verify.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def db_query(sql: str, args: tuple = ()) -> list:
    try:
        con = sqlite3.connect(f"file:{ROOT/'data'/'podcast.db'}?mode=ro", uri=True,
                              timeout=5.0)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001 —— 旁证，拿不到不致命
        log(f"  (只读查库失败：{exc})")
        return []


def grep(path: Path, pattern: str) -> list[str]:
    if not path.exists():
        return []
    return re.findall(pattern, path.read_text(encoding="utf-8", errors="replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-log", required=True,
                    help="重启后那份后端日志（恢复与缓存日志的唯一来源）")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--phase1-json", default=str(OUT / "phase1.json"),
                    help="阶段一产物，用来拿到任务归属用户名以便登录")
    ap.add_argument("--password", default="d12verify")
    ap.add_argument("--timeout", type=float, default=1500.0)
    args = ap.parse_args()

    tid = args.task_id
    ev: dict = {"phase": 2, "task_id": tid,
                "started_at": datetime.now().isoformat(timespec="seconds")}
    logf = Path(args.server_log)

    import httpx
    cli = httpx.Client(base_url=BASE, timeout=30.0, trust_env=False,
                       follow_redirects=True)

    # 【必须登录】任务详情是「归属校验」接口：不带头就是 401。
    # 第一版漏了这一步，轮询把非 200 静默跳过 → 任务明明早已 DONE，
    # 脚本却白等满 25 分钟超时。教训与项目里其它地方一致：
    # **不要把「拿不到」当成「还没好」**。
    p1 = {}
    if Path(args.phase1_json).is_file():
        p1 = json.loads(Path(args.phase1_json).read_text(encoding="utf-8"))
    user = p1.get("user")
    if not user:
        raise SystemExit("phase1.json 里没有 user，无法登录（用 --phase1-json 指定）")
    r = cli.post("/api/auth/login", json={"username": user, "password": args.password})
    if r.status_code != 200:
        raise SystemExit(f"登录失败 {r.status_code} {r.text[:200]}")
    ev["user"] = user
    log(f"已登录 {user}")

    ev["recover_log"] = grep(logf, r"\[RECOVER\][^\n]*")
    ev["cache_log"] = grep(logf, r"\[CACHE\][^\n]*")
    hit = re.search(r"命中缓存=([0-9]+)", " ".join(ev["cache_log"]))
    seg = re.search(r"合成段数=([0-9]+)", " ".join(ev["cache_log"]))
    ev["cache_hit_on_resume"] = int(hit.group(1)) if hit else None
    ev["segments_on_resume"] = int(seg.group(1)) if seg else None
    log(f"恢复日志：{ev['recover_log']}")
    log(f"缓存日志：{ev['cache_log']}")

    if not ev["recover_log"]:
        raise SystemExit("重启日志里没有 [RECOVER] 行 —— 恢复钩子没跑或日志不对")

    # 轮询前先确认「读得到」：这一条在 60s 内必须 200，否则是凭据/路由问题，
    # 应当立刻失败，而不是等超时。
    r = cli.get(f"/api/tasks/{tid}")
    if r.status_code != 200:
        raise SystemExit(f"读任务失败 {r.status_code} {r.text[:200]} —— "
                         "别把它当「还没跑完」，这是凭据或路由问题")
    last = r.json()

    t0 = time.time()
    while last.get("status") not in ("DONE", "FAILED", "CANCELED"):
        if time.time() - t0 > args.timeout:
            log(f"⚠ 轮询超时 {args.timeout}s，任务仍为 {last.get('status')}")
            break
        time.sleep(5.0)
        r = cli.get(f"/api/tasks/{tid}")
        if r.status_code != 200:
            raise SystemExit(f"轮询中读任务失败 {r.status_code} {r.text[:200]}")
        last = r.json()

    # 合成完成后再**重读一次**日志：`[CACHE]` 行是在整段合成结束时才打的，
    # 开头那次读必然还是空的（第一版就因此把命中数记成了 null）。
    ev["recover_log"] = grep(logf, r"\[RECOVER\][^\n]*") or ev["recover_log"]
    ev["cache_log"] = grep(logf, r"\[CACHE\][^\n]*")
    hit = re.search(r"命中缓存=([0-9]+)", " ".join(ev["cache_log"]))
    seg = re.search(r"合成段数=([0-9]+)", " ".join(ev["cache_log"]))
    ev["cache_hit_on_resume"] = int(hit.group(1)) if hit else None
    ev["segments_on_resume"] = int(seg.group(1)) if seg else None
    log(f"缓存日志（终读）：{ev['cache_log']}")

    ev["final_status"] = last.get("status")
    ev["final_progress"] = int(last.get("progress") or 0)
    ev["error_msg"] = last.get("error_msg") or ""
    ev["resume_wall_sec"] = round(time.time() - t0, 1)
    ev["cache_hit_count_final"] = last.get("cache_hit_count")
    ev["cache_seg_count_final"] = last.get("cache_seg_count")
    ev["cache_hit_rate_final"] = last.get("cache_hit_rate")
    log(f"最终 {ev['final_status']} progress={ev['final_progress']} "
        f"用时 {ev['resume_wall_sec']}s")

    rows = db_query("select seg_status,count(*) from script_lines where task_id=? "
                    "group by seg_status", (tid,))
    ev["line_states"] = {r[0]: int(r[1]) for r in rows}

    ep = db_query("select mp3_path from episodes where task_id=?", (tid,))
    if ep:
        p = ep[0][0]
        ev["episode"] = {"mp3_path": p, "exists": Path(p).is_file()}
        if Path(p).is_file():
            fpr = subprocess.run(
                [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
                 "format=duration,size", "-of", "default=nw=1", p],
                capture_output=True, text=True, errors="replace")
            ev["episode"]["ffprobe"] = fpr.stdout.strip().splitlines()
            ln = subprocess.run(
                [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-i", p,
                 "-af", "loudnorm=print_format=summary", "-f", "null", "-"],
                capture_output=True, text=True, errors="replace")
            m = re.search(r"Input Integrated:\s*([-0-9.]+)", ln.stderr + ln.stdout)
            tp = re.search(r"Input True Peak:\s*([-0-9.]+)", ln.stderr + ln.stdout)
            ev["episode"]["lufs"] = float(m.group(1)) if m else None
            ev["episode"]["true_peak_db"] = float(tp.group(1)) if tp else None

    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (OUT / "phase2.json").write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(ev, ensure_ascii=False, indent=2))

    checks = {
        "recover_log_nonempty": bool(ev["recover_log"]),
        "auto_resumed": bool(re.search(r"续跑 [1-9]", " ".join(ev["recover_log"]))),
        "final_done": ev["final_status"] == "DONE",
        "cache_reused": bool(ev["cache_hit_on_resume"]),
        # 「部分命中」才是本验证的关键：全命中说明脚本整体命中了历史缓存（那
        # 这轮压根没测到续跑），全未命中说明前面积累的成果没被复用。
        "cache_partial": bool(
            ev["cache_hit_on_resume"] and ev["segments_on_resume"]
            and 0 < ev["cache_hit_on_resume"] < ev["segments_on_resume"]),
        "all_lines_done": bool(ev["line_states"])
        and set(ev["line_states"]) == {"DONE"},
    }
    print("\n==== 判定 ====")
    for k, v in checks.items():
        print(f"  {'OK ' if v else 'FAIL'} {k}")
    ok = all(checks.values())
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
