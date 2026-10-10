# -*- coding: utf-8 -*-
"""依赖注入：数据库会话、当前用户、任务归属校验（计划书 4.8 的 `deps.py`）。

## Cookie 与 Bearer 双通道（计划书 4.7.1）

浏览器的 `<audio>` / `<img>` / `<a download>` **不会携带 `Authorization` 头**，
因此「媒体接口标记为需要鉴权」在实现上会直接失效 —— 这是本类项目最常见的返工点。
本模块同时接受两种凭据来源：

| 来源 | 场景 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <jwt>` | Swagger「Authorize」、脚本调用、第三方集成 | 优先级最高 |
| `Cookie: <AUTH_COOKIE_NAME>=<jwt>` | `<audio src=...>` / `<a download>` 站内访问 | 登录时随令牌一并下发（HttpOnly） |

## 越权为什么返回 404 而不是 403

资源不属于当前用户时返回 **404**：403 等于确认「这个 id 存在，只是不归你」，
攻击者可据此枚举出有效任务 id。404 让「不存在」与「不属于你」不可区分（计划书 10.2 防越权下载）。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.db import get_session_factory
from api.models import Task, User
from api.security import AuthError, decode_access_token

log = logging.getLogger(__name__)

#: auto_error=False：未带头时不立刻 401，交给下面的双通道逻辑统一判定
_bearer_scheme = HTTPBearer(
    auto_error=False, scheme_name="BearerJWT",
    description="登录接口返回的 access_token；也可留空改用 HttpOnly Cookie")


def get_db(settings: Settings = Depends(get_settings)) -> Iterator[Session]:
    """每请求一个 Session，请求正常结束即 commit，异常回滚，最后关闭。

    ⚠️ 此前这里**只有 yield + close，没有 commit**——路由里 `db.add/flush` 的写入会在
    Session 关闭时被回滚，导致注册/建任务等写操作「看似 200、库里没有」。
    现在改为「yield 后 commit，异常时 rollback」，与 `api.db.session_scope` 的提交语义对齐。
    """
    db = get_session_factory(settings)()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def extract_token(request: Request,
                  credentials: HTTPAuthorizationCredentials | None,
                  settings: Settings) -> str | None:
    """按「Bearer 头 > Cookie」的优先级取凭据。"""
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    return request.cookies.get(settings.auth_cookie_name) or None


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    """解析当前登录用户；未登录/令牌无效一律 401。"""
    token = extract_token(request, credentials, settings)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"未登录：请提供 Authorization: Bearer 头或 {settings.auth_cookie_name} Cookie",
            headers={"WWW-Authenticate": "Bearer"})
    try:
        user_id = decode_access_token(token, settings)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"登录态无效：{exc}",
            headers={"WWW-Authenticate": "Bearer"}) from exc

    user = db.get(User, user_id)
    if user is None:
        # 令牌签发后用户被删：同属登录态失效
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="登录态无效：用户不存在",
                            headers={"WWW-Authenticate": "Bearer"})
    return user


def owned_task(
    task_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Task:
    """取当前用户名下的任务，并校验归属（见模块 docstring 的 404 说明）。

    路由里若同时注入 `db`，FastAPI 的依赖缓存会复用同一个 Session 实例
    （`get_db` 在单次请求内只执行一次），因此两者操作的是同一事务。
    """
    task = db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail=f"任务不存在：{task_id}")
    return task
