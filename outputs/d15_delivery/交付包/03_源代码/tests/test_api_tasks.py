# -*- coding: utf-8 -*-
"""任务路由测试（计划书 4.7 API 契约表：Tasks 与媒体端点）。

覆盖：创建→脚本就绪、归属校验 404、分页、编辑重置 seg_status、取消。
合成阶段（需 GPU）不在单元测试范围；本文件只走到 SCRIPT_READY / CANCELED。
"""
from __future__ import annotations


def _register(client, username: str) -> dict:
    r = client.post("/api/auth/register",
                    json={"username": username, "password": "pw123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_create_reaches_script_ready(client, wait):
    h = _register(client, "taskuser1")
    r = client.post("/api/tasks",
                    json={"topic": "测试主题", "target_duration_sec": 120},
                    headers=h)
    assert r.status_code == 200
    tid = r.json()["id"]
    info = wait(client, h, tid)
    assert info["status"] == "SCRIPT_READY", info
    # 脚本行已落库
    scr = client.get(f"/api/tasks/{tid}/script", headers=h).json()
    assert scr["line_count"] >= 1


def test_cross_user_get_returns_404(client, wait):
    ha = _register(client, "owner1")
    hb = _register(client, "other1")
    tid = client.post("/api/tasks",
                      json={"topic": "x", "target_duration_sec": 120},
                      headers=ha).json()["id"]
    wait(client, ha, tid)
    # 另一用户访问 → 404（防越权枚举，不暴露 403）
    r = client.get(f"/api/tasks/{tid}", headers=hb)
    assert r.status_code == 404


def test_pagination(client, wait):
    h = _register(client, "pager1")
    for i in range(3):
        client.post("/api/tasks",
                    json={"topic": f"t{i}", "target_duration_sec": 120},
                    headers=h)
    r = client.get("/api/tasks?page=1&page_size=2", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert body["total"] >= 3
    assert len(body["items"]) <= 2


def test_edit_resets_seg_status(client, wait):
    h = _register(client, "editor1")
    tid = client.post("/api/tasks",
                      json={"topic": "e", "target_duration_sec": 120},
                      headers=h).json()["id"]
    wait(client, h, tid)
    r = client.put(
        f"/api/tasks/{tid}/script",
        json={"lines": [{"speaker": "A", "text": "改过的第一句。"},
                         {"speaker": "B", "text": "改过的第二句。"}]},
        headers=h)
    assert r.status_code == 200
    scr = client.get(f"/api/tasks/{tid}/script", headers=h).json()
    assert len(scr["lines"]) == 2
    # 保存后所有行 seg_status 重置为 PENDING（计划书 4.7 契约）
    assert all(ln["seg_status"] == "PENDING" for ln in scr["lines"])


def test_cancel_from_ready(client, wait):
    h = _register(client, "cancel1")
    tid = client.post("/api/tasks",
                      json={"topic": "c", "target_duration_sec": 120},
                      headers=h).json()["id"]
    wait(client, h, tid)
    r = client.post(f"/api/tasks/{tid}/cancel", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELED"
