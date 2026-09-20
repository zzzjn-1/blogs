# -*- coding: utf-8 -*-
"""把后端日志里的日志模板枚举出来，用来定位各阶段的时间边界。

纪律备忘：中文 + 多行不要塞进 `python -c`（shell 会改编码）。
"""
from __future__ import annotations

import re
from collections import Counter

LOG = r"D:\podcast-ai\uvicorn_resume.log"
TS = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+")
NUM = re.compile(r"\d+(\.\d+)?")


def template(line: str) -> str:
    """抹掉时间戳、数字、进度条，得到可聚合的模板。"""
    line = line.split("it/s]")[-1]
    line = TS.sub("<T>", line)
    line = NUM.sub("<N>", line)
    return line.strip()[:110]


def main() -> int:
    text = open(LOG, encoding="utf-8", errors="replace").read()
    counter: Counter[str] = Counter()
    for line in text.splitlines():
        t = template(line)
        if t:
            counter[t] += 1
    for tpl, n in counter.most_common(60):
        print(f"{n:>5}  {tpl}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
