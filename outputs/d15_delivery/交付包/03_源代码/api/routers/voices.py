# -*- coding: utf-8 -*-
"""音色路由（计划书 4.7：GET /api/voices）。

返回预置音色列表，供前端选择 A/B 音色。音色档案来自 backend/voices/（VoiceRegistry 读取）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.deps import get_db
from api.deps import get_current_user
from api.models import User
from api.schemas import VoiceOut
from api.services.tts import VoiceRegistry

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/voices", tags=["voices"])


@router.get("", response_model=list[VoiceOut])
def list_voices(_user: User = Depends(get_current_user),
                db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)) -> list[VoiceOut]:
    """当前预置音色列表（需登录；音色是共享资产，所有用户看到的相同）。"""
    reg = VoiceRegistry(settings.voices_path)
    profiles = reg.load()
    return [VoiceOut(**p.to_dict()) for p in profiles]
