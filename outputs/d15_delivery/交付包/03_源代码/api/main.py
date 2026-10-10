# -*- coding: utf-8 -*-
"""FastAPI 应用装配（计划书 4.7 / 4.8）。

装配顺序要点：
1. **最先调用 `apply_runtime_env`**：在 torch 等重依赖被 import 之前设置
   `TOKENIZERS_PARALLELISM` 等环境变量（见 api/config.py 注释；误设
   `expandable_segments` 在 Windows 无效，已剔除）。
2. 挂载四个业务路由 + 一个公开 RSS 路由（无 /api 前缀，无登录态）。
3. 注册全局异常处理器：状态机非法迁移 → 409；鉴权错误 → 401/500。
4. 启动事件：建表 + 确保目录存在（幂等）。**不在此加载 TTS 引擎**——
   引擎是进程内单例、懒加载到首条合成流水线才实例化（计划书 2.5 显存约束）。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# 必须在任何 `api.services.*` 之前执行（这些模块会在 import 时拉起 torch / transformers）
from api.config import get_settings, apply_runtime_env
apply_runtime_env()

from api.db import init_db
from api.routers import auth, feed, tasks, voices
from api.security import AuthConfigError, AuthError
from api.services.task_runner import IllegalTransition

log = logging.getLogger(__name__)

DESCRIPTION = """
双人对话播客自动生成系统 · 后端 API（计划书 4.7）。

- 鉴权：JWT（Bearer 头 或 HttpOnly Cookie 双通道，4.7.1）
- 任务：主题 → 脚本 → 双音色合成 → 后期 → 成片 + RSS（状态机见 4.5）
- 媒体接口支持 HTTP Range（可拖动播放）
"""

app = FastAPI(
    title="双人对话播客自动生成系统 · 后端 API",
    version="1.0.0",
    description=DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


# --------------------------------------------------------------------------- #
# 路由装配
# --------------------------------------------------------------------------- #
app.include_router(auth.router)          # /api/auth/register|login|logout
app.include_router(voices.router)        # /api/voices
app.include_router(tasks.router)         # /api/tasks ...
app.include_router(feed.router_api)      # /api/feeds/me
app.include_router(feed.router)          # /feed/{token}.xml, /feed/{token}/{guid}.mp3（公开）


# --------------------------------------------------------------------------- #
# 全局异常处理器
# --------------------------------------------------------------------------- #

@app.exception_handler(IllegalTransition)
async def _handle_illegal_transition(_request: Request, exc: IllegalTransition) -> JSONResponse:
    """状态机非法迁移 → 409（与路由层 4xx 语义一致）。"""
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(AuthError)
async def _handle_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
    """令牌无效 / 解析失败 → 401。"""
    return JSONResponse(
        status_code=401,
        content={"detail": f"登录态无效：{exc}"},
        headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(AuthConfigError)
async def _handle_auth_config_error(_request: Request, exc: AuthConfigError) -> JSONResponse:
    """密钥配置异常（如 JWT_SECRET 缺失）→ 500。"""
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# 启动 / 健康检查
# --------------------------------------------------------------------------- #

@app.on_event("startup")
def _startup() -> None:
    """建表（含补列）+ 确保数据目录 + **崩溃恢复扫描**（全部幂等）。"""
    s = get_settings()
    s.ensure_dirs()
    init_db(s)
    log.info("API 启动：数据库=%s，数据目录=%s", s.db_file, s.data_path)
    # [D12] 清掉上次进程被杀留下的非终态任务，并按配置自动续跑。
    # 必须在 init_db 之后（表/列要就位），且用 runner 单例（恢复会向同一个
    # 单并发队列投递作业，不能另起一个 executor）。
    try:
        from api.services.task_runner import get_runner
        get_runner(s).recover_interrupted()
    except Exception:  # noqa: BLE001
        # 恢复失败绝不能让服务起不来 —— 大不了这些任务继续以非终态躺着，
        # 用户仍可在页面上手动点重试。但必须留下完整堆栈，不许静默。
        log.exception("启动崩溃恢复失败（服务继续启动，残留任务需人工处理）")


@app.get("/health", tags=["meta"])
def health() -> dict:
    """简易存活探针。"""
    return {"status": "ok"}
