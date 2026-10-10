"""前后端字段漂移守卫。

后端 `api/schemas.py` 与前端 `frontend/src/api/types.ts` 是手写同步的，
后端加字段时前端类型很容易忘记跟上 —— 而这类漂移**不会报错**：
前端只是拿不到新字段，界面少一行，没人会发现。

所以把「字段名集合必须相等」变成一条可重跑的命令：

    python scripts/check_frontend_contract.py

退出码 0 = 一致；1 = 漂移（会列出差异字段，便于直接补）。
**只比对字段名，不比对类型** —— TS 与 Pydantic 的类型写法没有稳定映射，
强行比对只会产生噪音，反而没人愿意跑。

注：前端 `types.ts` 里可选字段写 `foo?:`，后端必填/可选都用 `foo:`，
因此匹配时把 `?` 去掉再比。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "api" / "schemas.py"
TYPES = ROOT / "frontend" / "src" / "api" / "types.ts"

#: 需要保持同步的 (后端类名, 前端接口名) 组合。
PAIRS = [
    ("TaskOut", "TaskOut"),
    ("TaskPageOut", "TaskPageOut"),
    ("ScriptOut", "ScriptOut"),
    ("ScriptLineOut", "ScriptLineOut"),
    ("UserOut", "UserOut"),
    ("FeedOut", "FeedOut"),
]


def _py_fields(src: str, cls: str) -> set[str] | None:
    m = re.search(rf"^class {cls}\b.*?(?=^class |\Z)", src, re.M | re.S)
    if not m:
        return None
    return set(re.findall(r"^    ([a-z_][a-z0-9_]*)\s*:", m.group(0), re.M))


def _ts_fields(src: str, iface: str) -> set[str] | None:
    m = re.search(rf"export interface {iface}\b.*?(?=export interface |\Z)", src, re.S)
    if not m:
        return None
    return set(re.findall(r"^  ([a-z_][a-z0-9_]*)\??\s*:", m.group(0), re.M))


def main() -> int:
    py_src = SCHEMAS.read_text(encoding="utf-8")
    ts_src = TYPES.read_text(encoding="utf-8")

    bad = 0
    for cls, iface in PAIRS:
        be = _py_fields(py_src, cls)
        fe = _ts_fields(ts_src, iface)
        if be is None:
            print(f"[skip] {cls}: 后端未找到该类")
            continue
        if fe is None:
            print(f"[skip] {iface}: 前端未找到该接口")
            continue
        if be == fe:
            print(f"[ok]   {cls} / {iface}: {len(be)} 个字段一致")
            continue
        bad += 1
        print(f"[FAIL] {cls} / {iface}: 漂移")
        if be - fe:
            print(f"         后端有、前端缺 -> {sorted(be - fe)}")
        if fe - be:
            print(f"         前端有、后端无 -> {sorted(fe - be)}")

    print("\n结果：" + ("全部一致" if bad == 0 else f"{bad} 处漂移"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
