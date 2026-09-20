# -*- coding: utf-8 -*-
"""D6 后端 API 端到端自检（不依赖 GPU / 网络）。

用 TestClient 走完整链路：注册 → 建任务 → 脚本就绪 → 确认合成（假引擎）→ 成片就绪 →
Range 播放 → 公开 RSS → 重置 token → 删除任务。每一步收集证据写入
`outputs/d6_verify_evidence.json`，便于人工复核「系统真的能跑通」。

用法（需在装有项目依赖的 Python 中运行，例如 conda env `cosyvoice`）：
    python scripts/verify_d6.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import wave
from pathlib import Path

# ---- 隔离到临时目录（必须在 import api 之前）----
_TMP = tempfile.mkdtemp(prefix="d6_verify_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["DB_PATH"] = os.path.join(_TMP, "data", "podcast_verify.db")
os.environ["CACHE_DIR"] = os.path.join(_TMP, "data", "cache")
os.environ["WORK_DIR"] = os.path.join(_TMP, "data", "work")
os.environ["AUDIO_DIR"] = os.path.join(_TMP, "data", "audio")
os.environ["PODCAST_DIR"] = os.path.join(_TMP, "data", "podcast")
os.environ["JWT_SECRET"] = "d6verifyjwt-" + "y" * 40
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["AUTH_COOKIE_SECURE"] = "false"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from api.config import get_settings
from api.services.task_runner import get_runner, reset_runner


class FakeGenerator:
    def generate(self, topic, duration_min=1.0, style=""):
        L = types.SimpleNamespace
        lines = [L(seq=1, speaker="A", text="验证脚本第一句。"),
                 L(seq=2, speaker="B", text="验证脚本第二句。")]
        return types.SimpleNamespace(
            script=L(lines=lines, title="验证单集", summary="D6 自检"),
            target_words=20)


class FakeEngine:
    """不加载 CosyVoice：直接写出可用的静音 wav，并给出与缓存一致的 key。

    仅用于 verify_d6 的端到端 API 自检，避免 ~11.6s 模型加载 + 真实 GPU 合成
    （后者会让后台流水线长事务持锁，触发 `/api/feeds/me` 的 database is locked）。
    后期仍走真实 `postprocess`（用本引擎产出的真 wav），以覆盖真实后期链路。
    """

    def __init__(self, settings=None):
        self.s = settings

    def cache_key(self, read_text, speaker, speed, tone):
        return f"ck|{speaker}|{read_text}|{speed}|{tone}"

    def synthesize_lines(self, norm_lines, out_dir, speed=1.0, tone="",
                          on_progress=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        results = []
        for i, nl in enumerate(norm_lines, 1):
            key = self.cache_key(nl.read_text, nl.speaker, speed, tone)
            wav = out / f"seg_{i}.wav"
            with wave.open(str(wav), "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(b"\x00\x00" * 200)
            results.append(types.SimpleNamespace(
                segment=types.SimpleNamespace(
                    line_seq=int(nl.seq), speaker=str(nl.speaker),
                    read_text=nl.read_text),
                text_hash=key, wav_path=str(wav), duration_ms=1000))
            if on_progress:
                on_progress(i, len(norm_lines), None)
        return results


reset_runner()
get_runner(get_settings(), make_generator=lambda: FakeGenerator(),
           make_engine=lambda: FakeEngine())

from fastapi.testclient import TestClient
from api.main import app

EVIDENCE: dict = {"steps": [], "ok": True}


def record(name: str, **kw):
    EVIDENCE["steps"].append({"step": name, **kw})
    ok = kw.get("ok", True)
    detail = {k: v for k, v in kw.items() if k != "ok"}
    print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")


def main():
    with TestClient(app) as client:
        # 1) 注册（即登录）
        r = client.post("/api/auth/register",
                        json={"username": "verify_user", "password": "pw123456"})
        pass1 = r.status_code == 200 and bool(r.json().get("access_token"))
        token = r.json().get("access_token", "")
        record("register", ok=pass1, status=r.status_code,
               has_token=bool(token))
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass1
        h = {"Authorization": f"Bearer {token}"}

        # 2) 建任务
        r = client.post("/api/tasks",
                        json={"topic": "D6 端到端验证", "target_duration_sec": 120},
                        headers=h)
        pass2 = r.status_code == 200 and bool(r.json().get("id"))
        tid = r.json().get("id", "")
        record("create_task", ok=pass2, status=r.status_code, task_id=tid)
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass2

        # 3) 轮询到脚本就绪
        status = None
        deadline = time.time() + 20
        while time.time() < deadline:
            rr = client.get(f"/api/tasks/{tid}", headers=h)
            if rr.status_code == 200:
                status = rr.json()["status"]
                if status in ("SCRIPT_READY", "FAILED", "CANCELED"):
                    break
            time.sleep(0.05)
        pass3 = status == "SCRIPT_READY"
        record("script_ready", ok=pass3, status=status)
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass3

        # 4) 确认合成
        r = client.post(f"/api/tasks/{tid}/synthesize", headers=h)
        pass4 = r.status_code == 202
        record("synthesize_accepted", ok=pass4, status=r.status_code)
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass4

        # 5) 轮询到成片就绪
        status = None
        deadline = time.time() + 30
        while time.time() < deadline:
            rr = client.get(f"/api/tasks/{tid}", headers=h)
            if rr.status_code == 200:
                status = rr.json()["status"]
                if status in ("DONE", "FAILED"):
                    break
            time.sleep(0.05)
        pass5 = status == "DONE"
        record("pipeline_done", ok=pass5, status=status)
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass5

        # 6) 成片 Range 播放
        r = client.get(f"/api/tasks/{tid}/audio",
                       headers={**h, "Range": "bytes=0-99"})
        pass6 = r.status_code in (200, 206) and len(r.content) > 0
        record("audio_range", ok=pass6, status=r.status_code,
               bytes=len(r.content))
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass6

        # 7) 频道配置 + 公开 RSS
        r = client.get("/api/feeds/me", headers=h)
        token0 = r.json().get("user_token", "") if r.status_code == 200 else ""
        rss0 = client.get(f"/feed/{token0}.xml")
        pass7 = bool(token0) and rss0.status_code == 200 \
            and "<rss" in rss0.text
        record("public_rss", ok=pass7, feed_token=token0,
               rss_status=rss0.status_code,
               has_rss_tag="<rss" in rss0.text)
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass7

        # 8) 重置 token：旧地址失效、新地址可用
        r = client.put("/api/feeds/me", json={"reset_token": True}, headers=h)
        token1 = r.json().get("user_token", "") if r.status_code == 200 else ""
        old = client.get(f"/feed/{token0}.xml")
        new = client.get(f"/feed/{token1}.xml")
        pass8 = token1 and token1 != token0 and old.status_code == 404 \
            and new.status_code == 200
        record("reset_token", ok=pass8, old_token=token0, new_token=token1,
               old_status=old.status_code, new_status=new.status_code)
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass8

        # 9) 删除任务
        r = client.delete(f"/api/tasks/{tid}", headers=h)
        pass9 = r.status_code == 200
        gone = client.get(f"/api/tasks/{tid}", headers=h).status_code == 404
        record("delete_task", ok=pass9 and gone, delete_status=r.status_code,
               get_after=("404" if gone else "non-404"))
        EVIDENCE["ok"] = EVIDENCE["ok"] and pass9 and gone

    # 落盘证据
    out_dir = os.path.join(_ROOT, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "d6_verify_evidence.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(EVIDENCE, fh, ensure_ascii=False, indent=2)
    print(f"\n证据已写入：{out_path}")
    print("RESULT:", "PASS" if EVIDENCE["ok"] else "FAIL")
    return 0 if EVIDENCE["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
