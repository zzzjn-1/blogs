# -*- coding: utf-8 -*-
"""SQLAlchemy 引擎与 Session（对应开发计划书 4.8 工程目录结构）。

三个刻意的设计决定：

1. **SQLite 默认关外键**——不显式 `PRAGMA foreign_keys=ON`，`ForeignKey(...)` 只是一句注释。
   本项目 `script_lines.text_hash` 指向 `audio_cache`、`tasks.user_id` 指向 `users`，
   靠外键拦住悬空引用比靠应用层自觉可靠。
2. **WAL 日志模式**：任务队列工作线程写库的同时，HTTP 请求线程要轮询 `GET /api/tasks/{id}`
   读进度。默认的 rollback journal 会让读被写阻塞（`database is locked`），WAL 下读写互不阻塞。
3. **engine 按进程单例**：SQLite 单文件库开多个 engine 会各自持有连接池，
   在 WAL 之外再引入一层锁竞争；测试换库路径时必须调用 `reset_engine()`。
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from api.config import Settings, get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """ORM 声明式基类（SQLAlchemy 2.0 风格）。"""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _apply_sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
    finally:
        cur.close()


def build_engine(settings: Settings | None = None) -> Engine:
    """新建一个 engine（不落缓存）。"""
    s = settings or get_settings()
    s.data_path.mkdir(parents=True, exist_ok=True)
    eng = create_engine(
        s.db_url,
        # 请求线程与队列工作线程共用同一 engine；SQLite 默认禁止连接跨线程复用，
        # 而我们要的恰恰是「连接可在任意线程取用，但每个线程各自持有自己的连接」。
        connect_args={"check_same_thread": False, "timeout": 15},
        future=True,
    )
    event.listen(eng, "connect", _apply_sqlite_pragmas)
    return eng


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine(settings)
        log.debug("SQLite engine 已创建：%s", _engine.url)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings), expire_on_commit=False, future=True)
    return _session_factory


def reset_engine() -> None:
    """丢弃缓存的 engine / sessionmaker。

    换库路径（测试夹具、迁移）时必须调用——否则新 Settings 会被旧的单例静默吃掉，
    表现为「明明换了 DB_PATH，数据却还落在旧库里」。
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """短事务上下文：正常提交、异常回滚、始终关闭。

    供队列工作线程与后台任务使用；HTTP 请求线程请走 `api.deps.get_db`（每请求一个 Session）。
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


#: 后续新增的列必须登记在这里（`create_all` 只建新表，**不会给既有表加列**）。
#: 形如 `(表名, 列名, 列定义 SQL)`；已存在的列会被跳过，可重复执行。
#: D13 若引入 Alembic，这份清单即初始迁移的内容。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    # D12 缓存命中率（见 models.Task）
    ("tasks", "cache_hit_count", "INTEGER NOT NULL DEFAULT 0"),
    ("tasks", "cache_seg_count", "INTEGER NOT NULL DEFAULT 0"),
)


def _ensure_columns(eng: Engine) -> list[str]:
    """把 `_ADDED_COLUMNS` 里缺的列补上，返回实际执行的 `ALTER TABLE` 语句。

    为什么需要它：`Base.metadata.create_all` 对**已存在**的表完全不动，
    于是「给老库加一列」会静默不生效 —— 表现为 ORM 查列报
    `no such column: tasks.cache_hit_count`，而新库一切正常（只有老库炸）。
    本项目 D13 前不引 Alembic，用这份显式清单兜住。
    """
    applied: list[str] = []
    with eng.begin() as conn:
        for table, column, ddl in _ADDED_COLUMNS:
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            if not rows:            # 表还不存在（首次建库），create_all 已带该列
                continue
            existing = {r[1] for r in rows}
            if column in existing:
                continue
            stmt = f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
            conn.exec_driver_sql(stmt)
            applied.append(stmt)
    if applied:
        log.info("数据库列迁移：%d 条已执行 —— %s", len(applied), "；".join(applied))
    return applied


def init_db(settings: Settings | None = None) -> Engine:
    """建表（幂等）+ 补列（幂等）。

    D13 产出《数据库设计文档》前以 `create_all` 代替迁移脚本；
    该文档落地后如引入字段变更，须改用 Alembic（或显式的 `ALTER TABLE` 迁移清单）——
    后者已由 `_ADDED_COLUMNS` 落地，可直接搬进 Alembic 的初始迁移。
    """
    eng = get_engine(settings)
    from api import models  # noqa: F401,PLC0415  # 触发 mapper 注册

    Base.metadata.create_all(bind=eng)
    _ensure_columns(eng)
    return eng
