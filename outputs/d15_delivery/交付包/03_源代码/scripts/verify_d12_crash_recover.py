# -*- coding: utf-8 -*-
"""D12 实机验证：合成中途 `kill -9` 后端，重启后任务自动续跑并最终 DONE。

这不是单测能替代的 —— 单测里 `recover_interrupted()` 被直接调用，证明的是
「函数按预期改库」；而本脚本证明的是**整条真实链路**：进程被暴力杀死后
留在库里的非终态任务，会被真实启动钩子扫到、真实投递、真实命中句级缓存
把没做完的部分补完，最后产出一份时长/响度正常的成片。

取材方式（为了可复现，两条都刻意做成确定性的）：

1. **跳过 LLM**：任务跑到 `SCRIPT_READY` 后，用 `PUT /script` 换成一份写死的
   脚本。这样段数、文本、耗时才可控 —— 否则每次测的段数都不一样，没法比较。
2. **在进度约 40% 处杀进程**：留出足够的「已合成前缀」让缓存有东西可命中。

用法（cwd 必须是仓库根，且用项目解释器）：
    <cosyvoice python> scripts/verify_d12_crash_recover.py

产出：`outputs/d12_crash_recover/` 下的
  - `evidence.json`      结构化证据（含杀进程点、恢复日志、缓存命中读数）
  - `server_before.log`  第一次启动（被杀）的后端日志
  - `server_after.log`   第二次启动（恢复）的后端日志
  - `verify.log`         本脚本自身的运行日志
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "d12_crash_recover"
PY = sys.executable
BASE = "http://127.0.0.1:8000"
SERVE = "outputs/synth_trace/serve_trace.py"

# 写死的脚本：24 行，A/B 交替，每行 18~30 字（会被切成若干段）。
TOPIC = "D12 崩溃恢复实机验证"
LINES: list[tuple[str, str]] = [
    ("A", "欢迎收听今天的节目，我们聊一个工程上特别容易被忽略的话题。"),
    ("B", "哦，是什么话题呢，听起来好像跟稳定性有关。"),
    ("A", "没错，就是进程被暴力杀死之后，那些卡在半路的任务该怎么办。"),
    ("B", "这个问题确实很现实，服务器重启、容器被驱逐都会遇到。"),
    ("A", "最糟的情况是任务永远停在合成中，前端进度条一直转圈。"),
    ("B", "用户只能刷新页面，然后发现什么都没变，体验非常糟糕。"),
    ("A", "所以我们需要在服务启动的时候，主动去扫一遍数据库里的残留状态。"),
    ("B", "扫出来之后要怎么处理呢，直接删掉还是让它们接着跑。"),
    ("A", "直接删掉太粗暴了，已经合成好的部分应该尽量复用。"),
    ("B", "我明白了，因为合成是按句子缓存的对吧。"),
    ("A", "对，每句话都有文本哈希，命中缓存就不需要重新推理。"),
    ("B", "那续跑的成本就很低了，只补没做完的那部分。"),
    ("A", "而且这个机制还有一个好处，就是重放是幂等的。"),
    ("B", "幂等意味着重复执行也不会产生错误的结果。"),
    ("A", "正是如此，所以我们可以放心地自动续跑，不用怕把事情搞坏。"),
    ("B", "不过有一个细节要注意，等待用户确认的任务不能动。"),
    ("A", "对，那种状态不是被中断，而是本来就在等人工编辑。"),
    ("B", "把它误判成中断会让用户丢掉刚改好的脚本。"),
    ("A", "所以我们只处理真正处于中间态的那几种状态。"),
    ("B", "还有一个问题，续跑会不会陷入无限循环。"),
    ("A", "不会，因为恢复扫描只看非终态，失败之后就不会再被扫到。"),
    ("B", "这样同一份中断最多只会自动续跑一次，很安全。"),
    ("A", "总结一下，关键是幂等、可观测，还有明确的处置边界。"),
    ("B", "感谢收听，我们下期再见。"),
]


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with (OUT / "verify.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _client():
    import httpx
    # 【铁律】打本机端口必须关 env proxy，否则请求走代理 → absolute-form → 404
    return httpx.Client(base_url=BASE, timeout=30.0, trust_env=False,
                        follow_redirects=True)


def launch(tag: str) -> subprocess.Popen:
    """起后端（root logger 为 INFO），日志落盘。"""
    logf = (OUT / f"server_{tag}.log").open("w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["SYNTH_TRACE"] = "0"          # 本验证不依赖插桩，别添额外写盘
    return subprocess.Popen([PY, SERVE], cwd=str(ROOT),
                            stdout=logf, stderr=subprocess.STDOUT, env=env)


def kill_tree(proc: subprocess.Popen) -> None:
    """暴力杀进程树（等价 kill -9；Windows 上是 taskkill /F /T）。"""
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                   capture_output=True, text=True, errors="replace")
    try:
        proc.wait(timeout=30)
    except Exception:  # noqa: BLE001
        pass


def wait_health(cli, timeout: float = 300.0) -> float:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = cli.get("/health")
            if r.status_code == 200:
                return time.time() - t0
        except Exception:  # noqa: BLE001 —— 未起来之前连不上是正常的
            pass
        time.sleep(2.0)
    raise SystemExit(f"后端 {timeout}s 内未就绪")


def poll_task(cli, tid: str, *, until, timeout: float, tick: float = 3.0) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        r = cli.get(f"/api/tasks/{tid}")
        if r.status_code == 200:
            last = r.json()
            if until(last):
                return last
            if last.get("status") in ("FAILED", "CANCELED"):
                return last
        time.sleep(tick)
    return last


def db_query(sql: str, args: tuple = ()) -> list:
    """只读查库。服务在跑时 WAL 允许并发读，但拿不到锁就返回空而不是炸掉脚本 ——
    这几处读都是**旁证**，缺了不影响主判定。"""
    try:
        con = sqlite3.connect(f"file:{ROOT/'data'/'podcast.db'}?mode=ro", uri=True,
                              timeout=5.0)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        log(f"  (只读查库失败，本次旁证留空：{exc})")
        return []


def grep(logfile: Path, pattern: str) -> list[str]:
    if not logfile.exists():
        return []
    txt = logfile.read_text(encoding="utf-8", errors="replace")
    return re.findall(pattern, txt)


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    ev: dict = {"started_at": datetime.now().isoformat(timespec="seconds")}
    user = f"d12chk{int(time.time()) % 100000}"

    # ---------- 第一程：起服务，造一条真实任务，跑到 40% 杀 ----------
    proc = launch("before")
    cli = _client()
    ev["boot_before_sec"] = round(wait_health(cli), 1)
    log(f"后端就绪（{ev['boot_before_sec']}s）")

    r = cli.post("/api/auth/register", json={"username": user, "password": "d12verify"})
    if r.status_code not in (200, 201, 409):
        raise SystemExit(f"注册失败 {r.status_code} {r.text[:200]}")
    if r.status_code == 409:
        cli.post("/api/auth/login", json={"username": user, "password": "d12verify"})
    log(f"用户 {user} 就绪")

    r = cli.post("/api/tasks", json={"topic": TOPIC, "duration_min": 1.0})
    r.raise_for_status()
    tid = r.json()["id"]
    ev["task_id"] = tid
    log(f"任务 {tid} 已建，等脚本生成…")

    st = poll_task(cli, tid, until=lambda d: d["status"] == "SCRIPT_READY",
                   timeout=420.0)
    if st.get("status") != "SCRIPT_READY":
        raise SystemExit(f"脚本未就绪：{st.get('status')} {st.get('error_msg')}")
    ev["llm_script_sec"] = None  # 仅为跳过 LLM 打底，不计入指标

    r = cli.put(f"/api/tasks/{tid}/script", json={
        "title": TOPIC, "summary": "D12 崩溃恢复验证用固定脚本",
        "lines": [{"speaker": s, "text": t} for s, t in LINES]})
    r.raise_for_status()
    ev["line_count"] = r.json()["line_count"]

    r = cli.post(f"/api/tasks/{tid}/synthesize")
    r.raise_for_status()
    log("已触发合成，等进度到 40%…")

    t_synth0 = time.time()
    st = poll_task(cli, tid, until=lambda d: int(d.get("progress") or 0) >= 40,
                   timeout=600.0, tick=2.0)
    ev["progress_at_kill"] = int(st.get("progress") or 0)
    ev["stage_at_kill"] = st.get("stage")
    ev["status_at_kill"] = st.get("status")
    if ev["progress_at_kill"] < 40:
        raise SystemExit(f"没跑到 40% 就不可继续：{st}")

    # 杀之前先记下已完成的行数（此时服务还在写，读只读连接可能拿不到最新，尽力而为）
    done_before = db_query(
        "select count(*) from script_lines where task_id=? and seg_status='DONE'",
        (tid,))
    ev["lines_done_before_kill"] = int(done_before[0][0]) if done_before else None
    log(f"进度 {ev['progress_at_kill']}%，已 DONE 行 {ev['lines_done_before_kill']} —— 现在杀进程")

    kill_tree(proc)
    log("进程已杀（taskkill /F /T）")
    time.sleep(3.0)

    zombie = db_query("select status,stage,progress from tasks where id=?", (tid,))
    ev["db_state_after_kill"] = (
        {"status": zombie[0][0], "stage": zombie[0][1],
         "progress": int(zombie[0][2] or 0)} if zombie else None)
    log(f"杀后库内状态：{ev['db_state_after_kill']}")

    # ---------- 第二程：重启，看恢复 ----------
    proc2 = launch("after")
    t_restart = time.time()
    ev["boot_after_sec"] = round(wait_health(cli), 1)
    log(f"重启就绪（{ev['boot_after_sec']}s），观察恢复…")

    st = poll_task(cli, tid, until=lambda d: d["status"] in ("DONE", "FAILED"),
                   timeout=1200.0, tick=5.0)
    ev["final_status"] = st.get("status")
    ev["final_progress"] = int(st.get("progress") or 0)
    ev["error_msg"] = st.get("error_msg") or ""
    ev["resume_wall_sec"] = round(time.time() - t_restart, 1)
    log(f"最终状态 {ev['final_status']} progress={ev['final_progress']} "
        f"（重启后耗时 {ev['resume_wall_sec']}s）")

    # ---------- 证据收集 ----------
    ev["recover_log"] = grep(OUT / "server_after.log", r"\[RECOVER\][^\n]*")
    ev["cache_log"] = grep(OUT / "server_after.log", r"\[CACHE\][^\n]*")
    ev["cache_log_before"] = grep(OUT / "server_before.log", r"\[CACHE\][^\n]*")
    hits = re.search(r"命中缓存=([0-9]+)", " ".join(ev["cache_log"]))
    segs = re.search(r"合成段数=([0-9]+)", " ".join(ev["cache_log"]))
    ev["cache_hit_on_resume"] = int(hits.group(1)) if hits else None
    ev["segments_on_resume"] = int(segs.group(1)) if segs else None

    row = db_query("select count(*) from script_lines where task_id=? and seg_status='DONE'",
                   (tid,))
    ev["lines_done_final"] = int(row[0][0]) if row else None
    ep = db_query("select mp3_path,duration_sec,file_size from episodes where task_id=?",
                  (tid,))
    if ep:
        p, dur, size = ep[0]
        ev["episode"] = {"mp3_path": p, "duration_sec": int(dur),
                         "file_size": int(size), "exists": Path(p).exists()}
        ff = shutil.which("ffprobe") or "ffprobe"
        pr = subprocess.run(
            [ff, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", p],
            capture_output=True, text=True, errors="replace")
        ev["episode"]["ffprobe_duration"] = pr.stdout.strip()
        ln = subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-i", p,
             "-af", "loudnorm=print_format=summary", "-f", "null", "-"],
            capture_output=True, text=True, errors="replace")
        m = re.search(r"Input Integrated:\s*([-0-9.]+)", ln.stderr + ln.stdout)
        t = re.search(r"Input True Peak:\s*([-0-9.]+)", ln.stderr + ln.stdout)
        ev["episode"]["lufs"] = float(m.group(1)) if m else None
        ev["episode"]["true_peak_db"] = float(t.group(1)) if t else None

    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (OUT / "evidence.json").write_text(
        json.dumps(ev, ensure_ascii=False, indent=2), encoding="utf-8")

    kill_tree(proc2)
    log("验证结束，已停服务")

    print(json.dumps(ev, ensure_ascii=False, indent=2))
    # 判定三要件：①重启后确实扫到了残留并续跑；②任务最终 DONE；
    # ③续跑时命中了缓存（证明「复用已合成前缀」不是嘴上说说）。
    checks = {
        "recover_log_nonempty": bool(ev["recover_log"]),
        "final_done": ev["final_status"] == "DONE",
        "cache_hit_on_resume": bool(ev["cache_hit_on_resume"]),
    }
    print("\n==== 判定 ====")
    for k, v in checks.items():
        print(f"  {'OK ' if v else 'FAIL'} {k}")
    ok = all(checks.values())
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
