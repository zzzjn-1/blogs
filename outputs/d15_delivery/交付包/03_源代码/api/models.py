# -*- coding: utf-8 -*-
"""ORM 模型（对应开发计划书 4.6 数据模型 ER 图）。

字段名与 ER 图逐列对齐；**三处超出 ER 的补充**已在 `[DEV-SCHEMA-01]` 处集中标注，
原因是 ER 图缺这些列会导致契约无法实现（详见各字段注释）。

## 与 ER 图的三条一致性要点（都是踩过坑的地方）

1. `audio_cache.text_hash` 是**全局唯一主键**（跨任务共享的句级缓存），
   `script_lines.text_hash` 是**外键**指向它。若把 text_hash 设成 script_lines 的唯一键，
   「任务 B 复用任务 A 已合成的句子」会插入冲突（计划书 4.6 设计说明原文）。
2. `script_lines.text_hash` **必须可空**：脚本在 SCRIPT_READY 阶段落库时尚未合成，
   强制非空会让「先存脚本、后合成」这条主流程直接插不进去。
3. 时间统一存 **naive UTC**。SQLite 不保存时区，混存 aware/naive 会让两个时间相减直接抛
   `TypeError: can't subtract offset-naive and offset-aware datetimes`（保留期清理会踩）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.db import Base


def utcnow() -> datetime:
    """naive UTC 时间戳（见模块 docstring 第 3 条）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# 状态词汇表（计划书 4.5 任务状态机）
# --------------------------------------------------------------------------- #

class TaskStatus:
    """任务状态取值。**合法迁移关系在 `api.services.task_runner.TRANSITIONS`**，
    此处只定义词汇，不定义图——避免同一份状态机有两处会飘的定义。"""

    PENDING = "PENDING"
    SCRIPTING = "SCRIPTING"
    SCRIPT_READY = "SCRIPT_READY"
    SYNTHESIZING = "SYNTHESIZING"
    POSTPROCESSING = "POSTPROCESSING"
    PACKAGING = "PACKAGING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"

    ALL = (PENDING, SCRIPTING, SCRIPT_READY, SYNTHESIZING, POSTPROCESSING,
           PACKAGING, DONE, FAILED, CANCELED)
    #: 终态：进入后不再自动流转，且触发中间产物清理（计划书 10.2）
    TERMINAL = (DONE, FAILED, CANCELED)


class SegStatus:
    """句级状态（计划书 4.6 script_lines.seg_status：PENDING / DONE）。"""

    PENDING = "PENDING"
    DONE = "DONE"


# --------------------------------------------------------------------------- #
# 表定义
# --------------------------------------------------------------------------- #

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True)
    feed: Mapped["Feed | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True,
        uselist=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "username": self.username,
                "created_at": self.created_at.isoformat() if self.created_at else None}


class Feed(Base):
    """播客频道配置（计划书 4.6 FEEDS，主键即 user_id，一用户一频道）。"""

    __tablename__ = "feeds"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    cover_url: Mapped[str] = mapped_column(String(500), default="")
    category: Mapped[str] = mapped_column(String(64), default="Technology")
    explicit: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    # [DEV-SCHEMA-01c] ER 未列，但计划书 4.7.1 要求 enclosure 路径含
    # 「不可猜测的随机 token」且「可一键重置」。挂在 feeds 上（而非 users）是因为
    # 它的唯一用途就是该用户的订阅源寻址，随频道重置最自然。
    user_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    user: Mapped[User] = relationship(back_populates="feed")

    def to_dict(self, *, base_url: str = "") -> dict:
        token = self.user_token
        return {
            "title": self.title, "description": self.description,
            "cover_url": self.cover_url, "category": self.category,
            "explicit": bool(self.explicit),
            "user_token": token,
            "feed_url": f"{base_url}/feed/{token}.xml" if base_url else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Task(Base):
    """一次「主题 → 成片」的生成任务（计划书 4.6 TASKS）。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)   # uuid4
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    topic: Mapped[str] = mapped_column(String(300))
    target_duration_sec: Mapped[int] = mapped_column(Integer, default=300)
    #: 由 target_duration_sec 按 WORDS_PER_MINUTE 反推（计划书 4.6 注释）
    target_word_count: Mapped[int] = mapped_column(Integer, default=0)
    style: Mapped[str] = mapped_column(String(200), default="")
    voice_a: Mapped[str] = mapped_column(String(64), default="")
    voice_b: Mapped[str] = mapped_column(String(64), default="")
    speed: Mapped[float] = mapped_column(Float, default=1.0)
    tone: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(
        String(24), default=TaskStatus.PENDING, index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)       # 0~100
    stage: Mapped[str] = mapped_column(String(48), default="")
    error_msg: Mapped[str] = mapped_column(Text, default="")
    content_flagged: Mapped[bool] = mapped_column(Boolean, default=False)

    # [DEV-SCHEMA-01a/01b] ER 未列。脚本标题与摘要必须在 PACKAGING 之前可达
    # ——episodes.title 与 RSS item 描述都取自它们，而 episodes 行是 PACKAGING 阶段才建的。
    # 不存这两列就得把标题塞进 script_lines（污染表语义）或写进 work 目录（终态即删，不可靠）。
    script_title: Mapped[str] = mapped_column(String(300), default="")
    script_summary: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # [D12 缓存命中率] 合成阶段最近一次的真实命中读数。
    # 只有「实际跑过合成」的任务才非空；重启续跑会把两个计数**整体重算**
    # （续跑时绝大多数段命中缓存，正是这两个数字要体现的东西）。
    # 不加表关联、不加索引：它只用于展示与验证，不参与任何查询过滤。
    cache_hit_count: Mapped[int] = mapped_column(Integer, default=0)
    cache_seg_count: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(back_populates="tasks")
    script_lines: Mapped[list["ScriptLine"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True,
        order_by="ScriptLine.seq")
    episode: Mapped["Episode | None"] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True,
        uselist=False)

    __table_args__ = (
        # 计划书 4.6 指定的核心索引：历史任务列表按 (user_id, status) 过滤
        Index("ix_tasks_user_status", "user_id", "status"),
    )

    def to_dict(self, *, with_lines: bool = False) -> dict:
        segs = self.script_lines or []
        d = {
            "id": self.id, "topic": self.topic,
            "status": self.status, "progress": int(self.progress or 0),
            "stage": self.stage or "", "error_msg": self.error_msg or "",
            "target_duration_sec": self.target_duration_sec,
            "target_word_count": self.target_word_count,
            "style": self.style, "voice_a": self.voice_a, "voice_b": self.voice_b,
            "speed": self.speed, "tone": self.tone,
            "content_flagged": bool(self.content_flagged),
            "script_title": self.script_title or "",
            "script_summary": self.script_summary or "",
            "line_count": len(segs),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            # [D12] 缓存命中率指标：hit/seg 由 _stage_synthesize 落库，
            # rate 这里派生（seg=0 时给 None 而不是 0，避免「0%」被误读成完全未命中）。
            "cache_hit_count": int(self.cache_hit_count or 0),
            "cache_seg_count": int(self.cache_seg_count or 0),
            "cache_hit_rate": (round(int(self.cache_hit_count or 0)
                                    / int(self.cache_seg_count), 4)
                               if int(self.cache_seg_count or 0) else None),
        }
        if with_lines:
            d["lines"] = [ln.to_dict() for ln in segs]
        return d


class ScriptLine(Base):
    """脚本行（计划书 4.6 SCRIPT_LINES）。"""

    __tablename__ = "script_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)              # 行序号，1 起连续
    speaker: Mapped[str] = mapped_column(String(8))        # A / B
    text: Mapped[str] = mapped_column(Text)                # 脚本文本原文
    read_text: Mapped[str] = mapped_column(Text, default="")   # 规范化后可读文本
    #: 缓存键，指向 audio_cache。**可空**——脚本阶段落库时尚未合成（见模块 docstring 第 2 条）
    text_hash: Mapped[str | None] = mapped_column(
        ForeignKey("audio_cache.text_hash"), nullable=True)
    #: 该行**全部**分段的音频总时长（一行可能被切成多句 → 多段）
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    seg_status: Mapped[str] = mapped_column(String(16), default=SegStatus.PENDING)

    task: Mapped[Task] = relationship(back_populates="script_lines")

    __table_args__ = (
        # 计划书 4.6 核心索引；同时强制「同一任务内 seq 唯一」
        Index("ix_script_lines_task_seq", "task_id", "seq", unique=True),
    )

    def to_dict(self) -> dict:
        return {"seq": self.seq, "speaker": self.speaker, "text": self.text,
                "read_text": self.read_text, "text_hash": self.text_hash,
                "duration_ms": int(self.duration_ms or 0),
                "seg_status": self.seg_status}


class AudioCache(Base):
    """跨任务共享的句级音频缓存（计划书 4.6 AUDIO_CACHE）。"""

    __tablename__ = "audio_cache"

    #: sha1(voice_id|voice_fp|model_version|seed_tag|max_token_text_ratio|read_text|speed|tone)
    text_hash: Mapped[str] = mapped_column(String(40), primary_key=True)
    speaker: Mapped[str] = mapped_column(String(64), default="")
    wav_path: Mapped[str] = mapped_column(String(500))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_dict(self) -> dict:
        return {"text_hash": self.text_hash, "speaker": self.speaker,
                "wav_path": self.wav_path, "duration_ms": int(self.duration_ms or 0)}


class Episode(Base):
    """单集（计划书 4.6 EPISODES）：一次成功任务封装出的成片记录。"""

    __tablename__ = "episodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)   # uuid4
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    mp3_path: Mapped[str] = mapped_column(String(500), default="")
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    #: RSS enclosure 的 length 必须是**字节数**，不是时长（计划书 4.9）
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    feed_guid: Mapped[str] = mapped_column(String(64), default="")
    pub_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped[Task] = relationship(back_populates="episode")

    def to_dict(self, *, base_url: str = "", user_token: str = "") -> dict:
        url = None
        if base_url and user_token and self.feed_guid:
            url = f"{base_url}/feed/{user_token}/{self.feed_guid}.mp3"
        return {"id": self.id, "task_id": self.task_id, "title": self.title,
                "duration_sec": int(self.duration_sec or 0),
                "file_size": int(self.file_size or 0),
                "feed_guid": self.feed_guid,
                "audio_url": url,
                "pub_date": self.pub_date.isoformat() if self.pub_date else None}
