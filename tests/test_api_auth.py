# -*- coding: utf-8 -*-
"""鉴权路由测试（计划书 4.7.1 双通道 + 4.6 USERS）。

覆盖：注册即登录（写 Cookie）、重复注册 409、登录、错误口令 401、登出。
媒体接口走 Cookie 这一通道由 /api/voices 间接验证（需登录）。
"""
from __future__ import annotations


def test_register_sets_session_and_cookie(client):
    r = client.post("/api/auth/register",
                     json={"username": "alice", "password": "pw123456"})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    assert body["user"]["username"] == "alice"
    # 注册即下发 HttpOnly Cookie（媒体接口据此鉴权）
    assert "podcast_token" in client.cookies


def test_duplicate_register_conflict(client):
    client.post("/api/auth/register",
                json={"username": "bob", "password": "pw123456"})
    r = client.post("/api/auth/register",
                    json={"username": "bob", "password": "pw123456"})
    assert r.status_code == 409


def test_login_and_wrong_password(client):
    client.post("/api/auth/register",
                json={"username": "carol", "password": "pw123456"})
    ok = client.post("/api/auth/login",
                     json={"username": "carol", "password": "pw123456"})
    assert ok.status_code == 200
    assert ok.json()["access_token"]

    bad = client.post("/api/auth/login",
                      json={"username": "carol", "password": "wrong"})
    assert bad.status_code == 401


def test_voices_requires_login(client):
    # 未登录：401
    assert client.get("/api/voices").status_code == 401
    # 登录后：200（返回列表，可能为空）
    client.post("/api/auth/register",
                json={"username": "dave", "password": "pw123456"})
    assert client.get("/api/voices").status_code == 200


def test_logout_clears_cookie(client):
    client.post("/api/auth/register",
                json={"username": "erin", "password": "pw123456"})
    assert "podcast_token" in client.cookies
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    # 登出后 Cookie 被清除（值置空且过期）
    assert client.cookies.get("podcast_token") in (None, "")
