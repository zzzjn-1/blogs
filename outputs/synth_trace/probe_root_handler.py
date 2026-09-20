# -*- coding: utf-8 -*-
"""最小复现：uvicorn 的 `dictConfig` 之后，root 上的自定义 handler 还能收到记录吗？

背景（R21 收尾）：`serve_trace.py` 先 `basicConfig(level=INFO)` 再 `uvicorn.run(...)`，
服务端日志里确实出现了 `yield speech len`（31 条），可 synth_trace 的 `llm_end`
**全为 null** —— 说明挂在 root 上的 `_YieldLenHandler` 没被调用。

嫌疑：uvicorn 的 `configure_logging()` 会 `logging.config.dictConfig(LOGGING_CONFIG)`，
非 incremental 分支先跑 `_clearExistingHandlers()`；`_handlerList` 里的 handler 会被
`logging.shutdown()` **关掉**（`StreamHandler.close()` 把 `self.stream` 置 None）。
本脚本把这条链路逐步拆开打印，看 handler 到底在哪一步失效。
"""
from __future__ import annotations

import logging
import logging.config
import sys

import uvicorn.config as uc


def _state(tag: str) -> None:
    root = logging.getLogger()
    hs = root.handlers
    desc = []
    for h in hs:
        stream = getattr(h, "stream", "<no attr>")
        desc.append(f"{type(h).__name__}(level={logging.getLevelName(h.level)}, "
                    f"stream={'None' if stream is None else 'SET'})")
    print(f"[{tag}] root.level={logging.getLevelName(root.level)} n_handlers={len(hs)} {desc}")


class _Probe(logging.Handler):
    def __init__(self, sink):
        super().__init__(level=logging.INFO)
        self.sink = sink

    def emit(self, record):  # noqa: D102
        self.sink.append(record.getMessage())


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sink: list[str] = []

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    _state("1 after basicConfig")

    logging.config.dictConfig(uc.LOGGING_CONFIG)
    _state("2 after dictConfig(uvicorn)")

    logging.getLogger().addHandler(_Probe(sink))
    _state("3 after addHandler(probe)")

    logging.getLogger().info("yield speech len 4.04, rtf 1.37")
    print(f"[4] probe 收到 {len(sink)} 条：{sink}")

    # 对照：把 level 显式设回 INFO 再看
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger().info("yield speech len 5.00, rtf 1.00")
    print(f"[5] setLevel(INFO) 后再发，probe 收到 {len(sink)} 条：{sink}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
