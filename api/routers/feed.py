# -*- coding: utf-8 -*-
"""频道与 RSS 路由（计划书 4.7 / 4.9）。

- `/api/feeds/me`：站内频道配置读写（需登录）。
- `/feed/{user_token}.xml`：公开 RSS 订阅源（无登录态）。
- `/feed/{user_token}/{guid}.mp3`：RSS enclosure 音频（不可猜测路径，无登录态，4.7.1）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.deps import get_db
from api.deps import get_current_user
from api.models import Episode, Feed, Task, User
from api.schemas import FeedOut, FeedUpdateIn
from api.services import podcast_rss

log = logging.getLogger(__name__)

router = APIRouter(tags=["feed"])

router_api = APIRouter(prefix="/api/feeds", tags=["feed"])  # 站内频道（带 /api 前缀）


# --------------------------------------------------------------------------- #
# 站内频道配置
# --------------------------------------------------------------------------- #

@router_api.get("/me", response_model=FeedOut)
def get_my_feed(user: User = Depends(get_current_user),
                db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)) -> FeedOut:
    feed = podcast_rss.ensure_feed(db, user, settings)
    db.flush()
    return FeedOut(**feed.to_dict(base_url=settings.public_base_url))


@router_api.put("/me", response_model=FeedOut)
def update_my_feed(body: FeedUpdateIn,
                   user: User = Depends(get_current_user),
                   db: Session = Depends(get_db),
                   settings: Settings = Depends(get_settings)) -> FeedOut:
    feed = podcast_rss.ensure_feed(db, user, settings)
    if body.title is not None:
        feed.title = body.title
    if body.description is not None:
        feed.description = body.description
    if body.cover_url is not None:
        feed.cover_url = body.cover_url
    if body.category is not None:
        feed.category = body.category
    if body.explicit is not None:
        feed.explicit = bool(body.explicit)
    if body.reset_token:
        # 使旧 RSS 地址立即失效（计划书 4.7.1 安全兜底）。
        # ⚠️ 必须先捕捉「旧」token，再改写 feed.user_token，否则下面删的是
        # 还没生成的新文件，旧 {token0}.xml 始终留在磁盘 → 旧地址仍可访问、
        # 新地址 404（D6 验证脚本暴露，[FIX-FEED-RESET-01]）。
        old_token = feed.user_token
        feed.user_token = podcast_rss.new_user_token(settings)
        old_xml = settings.podcast_path / f"{old_token}.xml"
        if old_xml.is_file():
            old_xml.unlink()
        # 用新 token 重新落盘 RSS，否则 /feed/{新 token}.xml 无文件可服务
        podcast_rss.write_feed_xml(db, user=user, settings=settings)
    else:
        # 仅改元数据：若已落盘 RSS 则刷新，避免公开源与库不一致
        cur_xml = settings.podcast_path / f"{feed.user_token}.xml"
        if cur_xml.is_file():
            podcast_rss.write_feed_xml(db, user=user, settings=settings)
    db.flush()
    feed = podcast_rss.ensure_feed(db, user, settings)
    return FeedOut(**feed.to_dict(base_url=settings.public_base_url))


# --------------------------------------------------------------------------- #
# 公开 RSS / enclosure（无登录态）
# --------------------------------------------------------------------------- #

@router.get("/feed/{user_token}.xml")
def feed_xml(user_token: str,
             settings: Settings = Depends(get_settings)) -> FileResponse:
    """公开 RSS 订阅源（不可猜测 token）。"""
    xml = settings.podcast_path / f"{user_token}.xml"
    if not xml.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="订阅源不存在或尚未生成")
    return FileResponse(xml, media_type="application/rss+xml",
                        filename=f"{user_token}.xml")


@router.get("/feed/{user_token}/cover.jpg")
def feed_cover(user_token: str,
               db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings)) -> FileResponse:
    """公开播客封面（随订阅 token 保护，不可猜测）。"""
    feed = db.scalar(select(Feed).where(Feed.user_token == user_token))
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="订阅源不存在")
    # 优先返回显式配置的 cover_url（若是本地路径）；否则回退到默认封面
    raw = (feed.cover_url or "").strip()
    if raw.startswith(settings.public_base_url.rstrip("/")):
        # 由系统默认映射生成的 /feed/{token}/cover.jpg：返回默认封面文件
        cover = settings.path("backend/assets/cover.jpg")
    else:
        cover = Path(raw) if raw else settings.path("backend/assets/cover.jpg")
    if not cover.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="封面文件不存在")
    return FileResponse(cover, media_type="image/jpeg",
                        filename="cover.jpg")


@router.get("/feed/{user_token}/{guid}.mp3")
def feed_audio(user_token: str, guid: str,
               db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings)) -> FileResponse:
    """RSS enclosure 音频（不可猜测路径，无登录态）。"""
    feed = db.scalar(select(Feed).where(Feed.user_token == user_token))
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="订阅源不存在")
    ep = db.scalar(select(Episode).where(Episode.feed_guid == guid))
    if ep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="单集不存在")
    task = db.get(Task, ep.task_id)
    if task is None or task.user_id != feed.user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="单集不存在")
    mp3 = Path(ep.mp3_path)
    if not mp3.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="音频文件缺失")
    return FileResponse(mp3, media_type="audio/mpeg", filename=f"{guid}.mp3")
