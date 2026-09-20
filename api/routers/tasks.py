# -*- coding: utf-8 -*-
"""任务路由（计划书 4.7 API 契约表：Tasks 与媒体端点）。

状态机迁移严格遵守 `api.services.task_runner.TRANSITIONS`；任何非法迁移由 runner 抛
`IllegalTransition`，路由转 409。媒体接口走 Cookie 鉴权（见 api/deps.py 双通道），
`FileResponse` 由 Starlette 原生支持 HTTP Range（可拖动进度条，4.7.1）。
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.deps import get_db
from api.deps import get_current_user, owned_task
from api.models import (
    AudioCache,
    Episode,
    Feed,
    ScriptLine,
    SegStatus,
    Task,
    TaskStatus,
    User,
)
from api.schemas import (
    ScriptLineIn,
    ScriptOut,
    ScriptSaveIn,
    TaskCreateIn,
    TaskOut,
    TaskPageOut,
)
from api.services.task_runner import TaskRunner, get_runner

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _runner() -> TaskRunner:
    return get_runner()


def _task_out(task: Task) -> TaskOut:
    """`TaskOut` + **队列位置**。

    队列位置是运行时信息（不落库，见 task_runner.queue_snapshot），所以只能在
    有 runner 的路由层注入，不能放进 `Task.to_dict()`。取不到就保持 None ——
    「不在队列」和「正在跑」是两件事，绝不能用 0 兜底（0 表示正在跑）。
    """
    out = TaskOut(**task.to_dict())
    try:
        out.queue_position = _runner().queue_position(task.id)
    except Exception:  # noqa: BLE001
        log.warning("读取队列位置失败 task=%s（保持 None）", task.id, exc_info=True)
    return out


# --------------------------------------------------------------------------- #
# 创建 / 列表 / 查询
# --------------------------------------------------------------------------- #

@router.post("", response_model=TaskOut, status_code=status.HTTP_200_OK)
def create_task(body: TaskCreateIn,
                user: User = Depends(get_current_user),
                db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)) -> TaskOut:
    """创建任务并投递脚本作业（PENDING → SCRIPTING）。"""
    duration_sec = int(body.target_duration_sec)
    if body.duration_min is not None:
        duration_sec = int(round(body.duration_min * 60))
    target_words = max(60, int(round(duration_sec / 60.0 * settings.words_per_minute)))
    task = Task(
        id=str(uuid.uuid4()), user_id=user.id, topic=body.topic,
        target_duration_sec=duration_sec, target_word_count=target_words,
        style=body.style or "",
        voice_a=body.voice_a or settings.speaker_a_voice,
        voice_b=body.voice_b or settings.speaker_b_voice,
        speed=float(body.speed), tone=body.tone or "",
        status=TaskStatus.PENDING, progress=0, stage="已创建")
    db.add(task)
    db.flush()
    # 先落库再投递后台作业：避免单并发队列的工作线程在请求事务提交前就
    # 读到「任务不存在」而空跑（任务停留在 PENDING）。
    db.commit()
    _runner().submit_script(task.id)
    db.refresh(task)
    return _task_out(task)


@router.get("", response_model=TaskPageOut)
def list_tasks(page: int = Query(default=1, ge=1),
               page_size: int = Query(default=20, ge=1, le=100),
               user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> TaskPageOut:
    """历史任务分页列表（按创建时间倒序）。"""
    total = db.scalar(
        select(func.count()).select_from(Task).where(Task.user_id == user.id)) or 0
    rows = (db.execute(
        select(Task).where(Task.user_id == user.id)
        .order_by(Task.created_at.desc())
        .limit(page_size).offset((page - 1) * page_size)).scalars().all())
    pages = max(1, (int(total or 0) + page_size - 1) // page_size) if page_size else 1
    return TaskPageOut(total=int(total or 0), page=page, page_size=page_size, pages=pages,
                        items=[_task_out(t) for t in rows])


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task: Task = Depends(owned_task)) -> TaskOut:
    """查询任务状态与进度。"""
    return _task_out(task)


# --------------------------------------------------------------------------- #
# 脚本
# --------------------------------------------------------------------------- #

@router.get("/{task_id}/script", response_model=ScriptOut)
def get_script(task: Task = Depends(owned_task),
               db: Session = Depends(get_db)) -> ScriptOut:
    """获取对话脚本（结构化行）。"""
    lines = (db.execute(
        select(ScriptLine).where(ScriptLine.task_id == task.id)
        .order_by(ScriptLine.seq)).scalars().all())
    return ScriptOut(task_id=task.id, title=task.script_title, summary=task.script_summary,
                     status=task.status, line_count=len(lines),
                     lines=[ln.to_dict() for ln in lines])


@router.put("/{task_id}/script", response_model=ScriptOut)
def save_script(body: ScriptSaveIn, task: Task = Depends(owned_task),
                db: Session = Depends(get_db)) -> ScriptOut:
    """保存人工编辑后的脚本（整份替换）。

    仅 SCRIPT_READY 允许编辑；保存后**重置所有行 seg_status=PENDING**（计划书 4.7 契约），
    因为合成阶段会重新规范化并按新文本哈希落库，旧 seg_status=DONE 会误导「已合成」。
    """
    if task.status not in (TaskStatus.SCRIPT_READY, TaskStatus.FAILED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅 SCRIPT_READY 阶段可编辑脚本（当前 {task.status}）")
    # 整份替换
    for old in list(task.script_lines):
        db.delete(old)
    db.flush()
    for i, ln in enumerate(body.lines, 1):
        db.add(ScriptLine(
            task_id=task.id, seq=i, speaker=str(ln.speaker or "A")[:1],
            text=ln.text, read_text="", seg_status=SegStatus.PENDING))
    if body.title is not None:
        task.script_title = body.title
    if body.summary is not None:
        task.script_summary = body.summary
    task.progress = 15
    db.flush()
    lines = (db.execute(
        select(ScriptLine).where(ScriptLine.task_id == task.id)
        .order_by(ScriptLine.seq)).scalars().all())
    return ScriptOut(task_id=task.id, title=task.script_title, summary=task.script_summary,
                     status=task.status, line_count=len(lines),
                     lines=[ln.to_dict() for ln in lines])


# --------------------------------------------------------------------------- #
# 流程控制
# --------------------------------------------------------------------------- #

@router.post("/{task_id}/synthesize", response_model=TaskOut,
             status_code=status.HTTP_202_ACCEPTED)
def confirm_synthesize(task: Task = Depends(owned_task),
                       db: Session = Depends(get_db)) -> TaskOut:
    """确认脚本，触发合成流水线（SCRIPT_READY → SYNTHESIZING）。"""
    if task.status != TaskStatus.SCRIPT_READY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅 SCRIPT_READY 可触发合成（当前 {task.status}）")
    _runner().submit_synthesize(task.id)
    db.refresh(task)
    return _task_out(task)


@router.post("/{task_id}/retry", response_model=TaskOut,
             status_code=status.HTTP_202_ACCEPTED)
def retry_task(task: Task = Depends(owned_task),
               db: Session = Depends(get_db)) -> TaskOut:
    """失败任务断点续跑（FAILED → SYNTHESIZING，跳过已命中的缓存句）。"""
    if task.status != TaskStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅 FAILED 可重试（当前 {task.status}）")
    _runner().submit_synthesize(task.id)
    db.refresh(task)
    return _task_out(task)


@router.post("/{task_id}/cancel", response_model=TaskOut)
def cancel_task(task: Task = Depends(owned_task),
                db: Session = Depends(get_db)) -> TaskOut:
    """取消任务（仅 PENDING / SCRIPT_READY 生效）。"""
    if task.status not in (TaskStatus.PENDING, TaskStatus.SCRIPT_READY):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"仅 PENDING/SCRIPT_READY 可取消（当前 {task.status}）")
    _runner().request_cancel(task.id)
    # 直接置终态（合成中取消不在状态机内）
    from api.services.task_runner import _transition
    _transition(db, task, TaskStatus.CANCELED, stage="已取消")
    db.flush()
    db.refresh(task)
    return _task_out(task)


@router.delete("/{task_id}", status_code=status.HTTP_200_OK)
def delete_task(task: Task = Depends(owned_task),
                db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)) -> dict:
    """删除任务（级联清理脚本、成片与单集记录；**不清句级缓存**）。"""
    # 终态即时清理中间产物 work（10.2）
    work = settings.work_path / task.id
    if work.is_dir():
        shutil.rmtree(work, ignore_errors=True)
    audio = settings.audio_path / task.id
    if audio.is_dir():
        shutil.rmtree(audio, ignore_errors=True)
    db.delete(task)   # 级联删除 script_lines / episode
    db.flush()
    return {"detail": f"任务已删除：{task.id}"}


# --------------------------------------------------------------------------- #
# 媒体（Cookie 鉴权 + Range，见 4.7.1）
# --------------------------------------------------------------------------- #

@router.get("/{task_id}/segments/{seq}/audio")
def segment_audio(task: Task = Depends(owned_task),
                  seq: int = 0,
                  db: Session = Depends(get_db)) -> FileResponse:
    """逐句试听（命中缓存直返，支持 Range）。合成未完成或该行未就绪返回 404。"""
    ln = db.scalar(select(ScriptLine).where(
        ScriptLine.task_id == task.id, ScriptLine.seq == seq))
    if ln is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"行不存在：{seq}")
    if ln.seg_status != SegStatus.DONE or not ln.text_hash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该行尚未合成完成（seg_status={ln.seg_status}），无法试听")
    ac = db.get(AudioCache, ln.text_hash)
    if ac is None or not Path(ac.wav_path).is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="句级缓存缺失，无法试听")
    return FileResponse(ac.wav_path, media_type="audio/wav",
                        filename=f"seg_{seq:04d}.wav")


@router.get("/{task_id}/audio")
def task_audio(task: Task = Depends(owned_task),
               settings: Settings = Depends(get_settings)) -> FileResponse:
    """成片在线播放（支持 Range）。"""
    if task.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"成片尚未就绪（当前 {task.status}）")
    mp3 = settings.audio_path / task.id / "final.mp3"
    if not mp3.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成片文件缺失")
    return FileResponse(mp3, media_type="audio/mpeg", filename="final.mp3")


@router.get("/{task_id}/download")
def task_download(task: Task = Depends(owned_task),
                  settings: Settings = Depends(get_settings)) -> FileResponse:
    """成片下载 mp3。"""
    if task.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"成片尚未就绪（当前 {task.status}）")
    mp3 = settings.audio_path / task.id / "final.mp3"
    if not mp3.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="成片文件缺失")
    return FileResponse(
        mp3, media_type="audio/mpeg", filename="final.mp3",
        headers={"Content-Disposition": 'attachment; filename="final.mp3"'})
