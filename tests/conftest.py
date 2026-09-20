# -*- coding: utf-8 -*-
"""D6 测试公共夹具（计划书 4.7 / 4.8 服务化）。

关键隔离策略：
- **所有落盘路径指向临时目录**（在导入 api 之前改环境变量），避免动到真实库 / 成片。
- **任务生成器换成假实现**：`TaskRunner` 单例注入 `FakeGenerator`，绕过 DeepSeek 网络调用
  与 GPU，使「建任务 → 脚本就绪」可离线、可重复地跑通（详见 api/services/task_runner.py 的可测性说明）。
- 每个测试前 `reset_engine()`，保证 SQLAlchemy 引擎单例重新绑定到本测试的临时库。

注意：`get_settings()` 是进程内 lru_cache，conftest 在导入 api 前已设好环境变量，
故后续所有 `get_settings()` 都返回同一份指向临时目录的配置。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types

# ---- 1) 先把所有路径改到临时目录（必须在 import api 之前）----
_TMP = tempfile.mkdtemp(prefix="podcast_d6_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["DB_PATH"] = os.path.join(_TMP, "data", "podcast_test.db")
os.environ["CACHE_DIR"] = os.path.join(_TMP, "data", "cache")
os.environ["WORK_DIR"] = os.path.join(_TMP, "data", "work")
os.environ["AUDIO_DIR"] = os.path.join(_TMP, "data", "audio")
os.environ["PODCAST_DIR"] = os.path.join(_TMP, "data", "podcast")
os.environ["JWT_SECRET"] = "test-secret-" + "x" * 40
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["AUTH_COOKIE_SECURE"] = "false"

# 让 `api` 包可导入
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api.config import get_settings
from api.db import init_db, reset_engine
from api.services.task_runner import get_runner, reset_runner


class FakeGenerator:
    """固定产出一段 3 行双人脚本，替代真实 LLM 调用。"""

    def generate(self, topic, duration_min=1.0, style=""):
        L = types.SimpleNamespace
        lines = [
            L(seq=1, speaker="A", text="你好，欢迎收听本期测试节目。"),
            L(seq=2, speaker="B", text="今天我们聊聊双人对话播客的自动化。"),
            L(seq=3, speaker="A", text="希望能帮你省下剪辑的时间。"),
        ]
        script = L(lines=lines, title="测试单集", summary="自动化测试摘要")
        return L(script=script, target_words=30)


# ---- 2) 进程内任务运行单例：注入假生成器（不碰网络 / GPU）----
reset_runner()
get_runner(get_settings(), make_generator=lambda: FakeGenerator())

import pytest


@pytest.fixture()
def settings():
    """每个测试重建引擎并初始化指向临时目录的库。"""
    s = get_settings()
    s.ensure_dirs()
    reset_engine()
    init_db(s)
    return s


@pytest.fixture()
def client(settings):
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def wait():
    """轮询任务直到进入终态（或 SCRIPT_READY），返回最终 task JSON。"""

    def _wait(client, headers, task_id, timeout: float = 20):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            r = client.get(f"/api/tasks/{task_id}", headers=headers)
            if r.status_code == 200:
                last = r.json()
                if last["status"] in ("SCRIPT_READY", "FAILED", "CANCELED",
                                       "DONE", "PENDING"):
                    # PENDING 表示线程还没起跑；继续等到脚本阶段有结论
                    if last["status"] != "PENDING":
                        return last
            time.sleep(0.05)
        if last is None:
            raise AssertionError(f"轮询超时：任务 {task_id} 始终无响应")
        return last

    return _wait
