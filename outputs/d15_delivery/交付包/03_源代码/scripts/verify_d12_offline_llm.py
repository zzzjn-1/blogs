# -*- coding: utf-8 -*-
"""D12 实机验证 · 「人为断网」：LLM 不可达时任务必须**降级为 FAILED 且原因可读**。

做法：把后端指向一个**必然连不上**的地址（`127.0.0.1:9`，discard 端口，本机无人监听），
然后走一遍正常建任务流程。期望：

1. 任务**不会**永远卡在 `SCRIPTING`（那是「静默失败」，比报错更糟）；
2. 最终落到 `FAILED`，且 `error_msg` 是**人能看懂的中文**，而不是裸 traceback 或空串。

这是 D12 完成判定里「人为断网」那半句的实机取证。断网由**环境变量**制造
（本机循环地址无人监听，等价于网络不可达，且不受真实网络环境影响，可复现）。

用法（cwd 必须是仓库根；后端需以 LLM_BASE_URL=http://127.0.0.1:9 启动）：
    <cosyvoice python> scripts/verify_d12_offline_llm.py
"""
from __future__ import annotations

import json
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


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    import httpx
    cli = httpx.Client(base_url=BASE, timeout=180.0, trust_env=False,
                       follow_redirects=True)

    user = f"d12net{int(time.time()) % 100000}"
    r = cli.post("/api/auth/register", json={"username": user, "password": "d12verify"})
    if r.status_code == 409:
        cli.post("/api/auth/login", json={"username": user, "password": "d12verify"})
    elif r.status_code not in (200, 201):
        raise SystemExit(f"注册失败 {r.status_code} {r.text[:200]}")

    r = cli.post("/api/tasks", json={"topic": "断网场景验证：LLM 不可达", "duration_min": 1.0})
    r.raise_for_status()
    tid = r.json()["id"]
    log(f"任务 {tid} 已建（后端已指向不可达 LLM）")

    t0 = time.time()
    last = {}
    while time.time() - t0 < 240.0:
        r = cli.get(f"/api/tasks/{tid}")
        if r.status_code != 200:
            raise SystemExit(f"读任务失败 {r.status_code} {r.text[:200]}")
        last = r.json()
        if last.get("status") in ("FAILED", "DONE", "CANCELED"):
            break
        time.sleep(3.0)

    ev = {
        "phase": "offline",
        "user": user,
        "task_id": tid,
        "elapsed_sec": round(time.time() - t0, 1),
        "final_status": last.get("status"),
        "error_msg": last.get("error_msg") or "",
        "progress": int(last.get("progress") or 0),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    (OUT / "offline.json").write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(ev, ensure_ascii=False, indent=2))

    # 判定：必须 FAILED；error_msg 非空；且**不是**裸 traceback（要有中文说明）
    msg = ev["error_msg"]
    checks = {
        "reached_terminal": ev["final_status"] in ("FAILED", "CANCELED"),
        "is_failed": ev["final_status"] == "FAILED",
        "error_msg_nonempty": bool(msg.strip()),
        "error_msg_readable": bool(msg.strip())
        and ("Traceback" not in msg)
        and any("\u4e00" <= ch <= "\u9fff" for ch in msg),
    }
    print("\n==== 判定 ====")
    for k, v in checks.items():
        print(f"  {'OK ' if v else 'FAIL'} {k}")
    print(f"  error_msg = {msg[:300]}")
    ok = all(checks.values())
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
