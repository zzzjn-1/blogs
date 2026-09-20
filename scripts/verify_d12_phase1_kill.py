# -*- coding: utf-8 -*-
"""D12 实机验证 · 阶段一：造任务 → 跑到 40% → **外部杀掉后端**。

为什么要拆成两个脚本（而不是一个脚本里 Popen 起两次后端）：
第一版就是在同一个 Python 进程里 `Popen` 起后端、杀、再 `Popen` 起第二个，
结果第二个后端起来几秒后无声无息地没了（没有任何 traceback、系统事件日志里
也查不到崩溃记录）。这与本项目早已记录的现象一致 —— **沙箱会清理挂在
Python 父进程下的子进程**。所以后端一律改由 shell 后台启动（`run_in_background`
+ 重定向到日志），脚本只负责「驱动业务 + 杀」，不负责「生」。

本脚本假定后端**已在 8000 端口运行**，其日志为 `--server-log` 指向的文件
（从中解析 `Started server process [PID]` 拿到 PID 用于 taskkill）。

用法（cwd 必须是仓库根）：
    <cosyvoice python> scripts/verify_d12_phase1_kill.py --server-log outputs/xxx/server1.log
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "d12_crash_recover"
BASE = "http://127.0.0.1:8000"

TOPIC = "D12 崩溃恢复实机验证（可控杀死）"

# 基础句（24 行 A/B 交替）。**光有基础句不够** —— 句级缓存按 `text_hash` 命中，
# 若两次运行用同一份文本，第二次会 100% 命中缓存、几秒就冲完，验证就退化成空跑
# （第一版就是这么翻车的：2 秒冲到 65%）。所以每一行再拼一个「本次随机尾句」，
# 让整份脚本每次运行都唯一 —— 这样命中的只可能是**本次运行自己**已完成的那部分。
BASE_LINES: list[tuple[str, str]] = [
    ("A", "欢迎收听今天的节目，我们聊一个工程上特别容易被忽略的话题"),
    ("B", "哦，是什么话题呢，听起来好像跟稳定性有关"),
    ("A", "没错，就是进程被暴力杀死之后，那些卡在半路的任务该怎么办"),
    ("B", "这个问题确实很现实，服务器重启、容器被驱逐都会遇到"),
    ("A", "最糟的情况是任务永远停在合成中，前端进度条一直转圈"),
    ("B", "用户只能刷新页面，然后发现什么都没变，体验非常糟糕"),
    ("A", "所以我们需要在服务启动的时候，主动去扫一遍数据库里的残留状态"),
    ("B", "扫出来之后要怎么处理呢，直接删掉还是让它们接着跑"),
    ("A", "直接删掉太粗暴了，已经合成好的部分应该尽量复用"),
    ("B", "我明白了，因为合成是按句子缓存的对吧"),
    ("A", "对，每句话都有文本哈希，命中缓存就不需要重新推理"),
    ("B", "那续跑的成本就很低了，只补没做完的那部分"),
    ("A", "而且这个机制还有一个好处，就是重放是幂等的"),
    ("B", "幂等意味着重复执行也不会产生错误的结果"),
    ("A", "正是如此，所以我们可以放心地自动续跑，不用怕把事情搞坏"),
    ("B", "不过有一个细节要注意，等待用户确认的任务不能动"),
    ("A", "对，那种状态不是被中断，而是本来就在等人工编辑"),
    ("B", "把它误判成中断会让用户丢掉刚改好的脚本"),
    ("A", "所以我们只处理真正处于中间态的那几种状态"),
    ("B", "还有一个问题，续跑会不会陷入无限循环"),
    ("A", "不会，因为恢复扫描只看非终态，失败之后就不会再被扫到"),
    ("B", "这样同一份中断最多只会自动续跑一次，很安全"),
    ("A", "总结一下，关键是幂等、可观测，还有明确的处置边界"),
    ("B", "感谢收听，我们下期再见"),
]

# 40 个口语化尾句池。每次运行按随机种子抽 24 个不重复的拼上去，
# 使「整行文本」跨运行不同，从而跨运行不串缓存。
TAILS: list[str] = [
    "，这也是我们这次要重点说明的地方。",
    "，这个判断在工程实践里非常关键。",
    "，听上去简单，做起来需要仔细权衡。",
    "，顺序决定了它最终的实际效果。",
    "，这一点往往被大家忽略掉。",
    "，所以在设计的时候要提前想清楚。",
    "，我把这里当作一条硬性约束。",
    "，这样处理之后逻辑就自洽了。",
    "，代价是代码会稍微复杂一点。",
    "，但收益明显大于额外的复杂度。",
    "，这也是为什么我说它值得单独讲。",
    "，可以把它理解成一次保险。",
    "，本质上是在用空间换时间。",
    "，前提是每一步都保持幂等。",
    "，否则就会出现难以排查的怪问题。",
    "，所以日志一定要写清楚。",
    "，这也是可观测性的意义所在。",
    "，把不确定的东西变成确定的。",
    "，这样出问题的时候才追得动。",
    "，经验告诉我们越早暴露越好。",
    "，别等到线上才发现。",
    "，这一点在处理边界条件时尤其重要。",
    "，所以我把边界单独列了一节。",
    "，避免把两件事混在一起讨论。",
    "，区分清楚之后方案就明确了。",
    "，这也是我们反复强调的原则。",
    "，先保证正确，再谈优化。",
    "，性能是可以后面慢慢补的。",
    "，但正确性没有商量的余地。",
    "，我觉得这句话值得记下来。",
    "，下次遇到类似的问题就能用上。",
    "，这就是所谓的经验沉淀。",
    "，写成文档才能传给下一个人。",
    "，否则这些坑还要再踩一遍。",
    "，所以文档和代码同样重要。",
    "，我的建议是两者一起维护。",
    "，改代码的时候顺手改文档。",
    "，这样才不会出现前后矛盾。",
    "，说到底还是流程的问题。",
    "，好了，这个话题就聊到这里。",
]


def build_lines(seed: int) -> list[tuple[str, str]]:
    """按种子生成一份**每次运行都唯一**的脚本（24 行）。"""
    import random
    rng = random.Random(seed)
    tails = rng.sample(TAILS, len(BASE_LINES))
    return [(spk, base + tail) for (spk, base), tail in zip(BASE_LINES, tails)]


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    with (OUT / "verify.log").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def client():
    import httpx
    return httpx.Client(base_url=BASE, timeout=30.0, trust_env=False,
                        follow_redirects=True)


def poll(cli, tid: str, *, until, timeout: float, tick: float = 2.0) -> dict:
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        try:
            r = cli.get(f"/api/tasks/{tid}")
        except Exception as exc:  # noqa: BLE001 —— 服务被杀了，如实记下
            raise SystemExit(f"轮询中断（服务可能已停）：{exc}")
        if r.status_code == 200:
            last = r.json()
            if until(last) or last.get("status") in ("FAILED", "CANCELED"):
                return last
        time.sleep(tick)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server-log", required=True)
    ap.add_argument("--progress", type=int, default=40)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    ev: dict = {"phase": 1, "started_at": datetime.now().isoformat(timespec="seconds")}
    user = f"d12p1{int(time.time()) % 100000}"
    seed = int(time.time())
    lines = build_lines(seed)
    ev["script_seed"] = seed
    ev["script_sample"] = lines[0][1]

    cli = client()
    r = cli.post("/api/auth/register", json={"username": user, "password": "d12verify"})
    if r.status_code == 409:
        cli.post("/api/auth/login", json={"username": user, "password": "d12verify"})
    elif r.status_code not in (200, 201):
        raise SystemExit(f"注册失败 {r.status_code} {r.text[:200]}")
    ev["user"] = user

    r = cli.post("/api/tasks", json={"topic": TOPIC, "duration_min": 1.0})
    r.raise_for_status()
    tid = r.json()["id"]
    ev["task_id"] = tid
    log(f"任务 {tid} 已建，等脚本生成…")

    st = poll(cli, tid, until=lambda d: d["status"] == "SCRIPT_READY", timeout=420.0)
    if st.get("status") != "SCRIPT_READY":
        raise SystemExit(f"脚本未就绪：{st.get('status')} {st.get('error_msg')}")

    r = cli.put(f"/api/tasks/{tid}/script", json={
        "title": TOPIC, "summary": "D12 可控杀死验证用唯一脚本",
        "lines": [{"speaker": s, "text": t} for s, t in lines]})
    r.raise_for_status()
    ev["line_count"] = r.json()["line_count"]

    r = cli.post(f"/api/tasks/{tid}/synthesize")
    r.raise_for_status()
    t0 = time.time()
    log(f"已触发合成，等进度到 {args.progress}%…")

    st = poll(cli, tid, until=lambda d: int(d.get("progress") or 0) >= args.progress,
              timeout=900.0)
    ev["progress_at_kill"] = int(st.get("progress") or 0)
    ev["status_at_kill"] = st.get("status")
    ev["synth_wall_sec_at_kill"] = round(time.time() - t0, 1)
    if ev["progress_at_kill"] < args.progress:
        raise SystemExit(f"没跑到 {args.progress}%：{st}")
    # 防退化守卫：若合成在极短时间内就冲到目标进度，说明整份脚本命中了历史缓存，
    # 这一轮**无法**验证「保住已合成前缀」——必须重新跑（换种子）。
    if ev["synth_wall_sec_at_kill"] < 20:
        raise SystemExit(
            f"脚本疑似整体命中历史缓存（{ev['synth_wall_sec_at_kill']}s 冲到 "
            f"{ev['progress_at_kill']}%）——本轮作废，请重跑以换随机种子")

    # 从后端日志解析 PID（uvicorn 启动行），用它做 taskkill /F /T
    logtxt = Path(args.server_log).read_text(encoding="utf-8", errors="replace")
    m = list(re.finditer(r"Started server process \[([0-9]+)\]", logtxt))
    if not m:
        raise SystemExit(f"未能在 {args.server_log} 中解析到后端 PID")
    pid = int(m[-1].group(1))
    ev["killed_pid"] = pid
    log(f"进度 {ev['progress_at_kill']}%，杀后端 PID={pid}")

    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True, text=True, errors="replace")
    time.sleep(3.0)

    # 杀之后确认端口已释放（阶段二才能重新绑定）
    import socket
    for _ in range(20):
        sk = socket.socket(); sk.settimeout(1)
        try:
            sk.connect(("127.0.0.1", 8000))
            sk.close(); time.sleep(1.0)
        except Exception:  # noqa: BLE001
            sk.close(); break

    ev["finished_at"] = datetime.now().isoformat(timespec="seconds")
    (OUT / "phase1.json").write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(json.dumps(ev, ensure_ascii=False, indent=2))
    log("阶段一完成：后端已杀，请用 shell 后台重启后端，再跑阶段二")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
