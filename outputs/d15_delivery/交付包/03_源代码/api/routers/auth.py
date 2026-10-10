# -*- coding: utf-8 -*-
"""鉴权路由（计划书 4.7：register / login / logout；4.7.1 媒体鉴权）。

登录同时把 JWT 写进 HttpOnly Cookie —— 浏览器的 `<audio>` / `<a download>` 不会带
`Authorization` 头，只能靠 Cookie 携带凭据（详见 api/deps.py 的 404/双通道说明）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.deps import get_db
from api.models import User
from api.schemas import LoginIn, RegisterIn, TokenOut, UserOut
from api.security import (
    AuthConfigError,
    create_access_token,
    hash_password,
    verify_password,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_auth_cookie(resp: Response, token: str, settings: Settings) -> None:
    resp.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        httponly=True,
        secure=bool(settings.auth_cookie_secure),
        samesite=settings.auth_cookie_samesite,
        path=settings.auth_cookie_path,
        max_age=int(settings.jwt_expire_minutes) * 60,
    )


def _issue(user: User, settings: Settings) -> tuple[str, int]:
    return create_access_token(user.id, settings)


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_200_OK)
def register(body: RegisterIn, resp: Response,
             db: Session = Depends(get_db),
             settings: Settings = Depends(get_settings)) -> TokenOut:
    """注册并直接登录（签发 JWT + 写 Cookie）。用户名冲突返回 409。"""
    try:
        existing = db.scalar(select(User).where(User.username == body.username))
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"用户名已存在：{body.username}")
        user = User(username=body.username,
                    password_hash=hash_password(body.password, rounds=4))
        db.add(user)
        db.flush()
        token, exp = _issue(user, settings)
        _set_auth_cookie(resp, token, settings)
        return TokenOut(access_token=token, token_type="bearer", expires_in=exp,
                        user=UserOut(id=user.id, username=user.username,
                                     created_at=user.created_at))
    except AuthConfigError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=str(exc)) from exc


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, resp: Response,
          db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)) -> TokenOut:
    """登录。签发 JWT + 写 HttpOnly Cookie。口令错误返回 401。"""
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="用户名或口令错误")
    try:
        token, exp = _issue(user, settings)
    except AuthConfigError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=str(exc)) from exc
    _set_auth_cookie(resp, token, settings)
    return TokenOut(access_token=token, token_type="bearer", expires_in=exp,
                    user=UserOut(id=user.id, username=user.username,
                                 created_at=user.created_at))


@router.post("/logout", status_code=status.HTTP_200_OK)
def logout(resp: Response,
           settings: Settings = Depends(get_settings)) -> dict:
    """清除会话 Cookie。"""
    resp.delete_cookie(key=settings.auth_cookie_name, path=settings.auth_cookie_path)
    return {"detail": "已退出登录"}
