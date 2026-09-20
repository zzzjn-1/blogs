# -*- coding: utf-8 -*-
"""R21 诊断专用启动器：**显式把 root logger 设成 INFO** 再起 uvicorn。

为什么需要它：uvicorn 的 `LOGGING_CONFIG` 只配 `uvicorn` / `uvicorn.error` /
`uvicorn.access` 三个 logger，**没有 root**，于是 root 停在默认 WARNING ——
CosyVoice 用 `logging.info('synthesis text ...')` / `logging.info('yield speech len ...')`
打的阶段标记会被整段丢掉，插桩里的 `llm_end` 永远是 null。

（D10 那份 uvicorn_run.log 之所以有这些行，是 `cosyvoice/utils/file_utils.py` 在
import 时顺手调了 `logging.basicConfig(level=DEBUG)`。这次 import 顺序不同就全丢了 ——
所以别依赖别人的副作用，自己显式配。）

用法（cwd 必须是仓库根）：
    <cosyvoice python> outputs/synth_trace/serve_trace.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# 脚本放在 outputs/synth_trace/ 下，仓库根是 parents[2]，得手动塞进 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 先配日志，再 import api.main（后者会 import cosyvoice / torch）
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

import uvicorn  # noqa: E402

from api.main import app  # noqa: E402

if __name__ == "__main__":
    root = logging.getLogger()
    print(f"[serve_trace] root level={logging.getLevelName(root.level)} "
          f"handlers={len(root.handlers)}", flush=True)
    # access_log=False：压测轮询每 3s 一条，会把阶段标记淹掉
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info", access_log=False)
