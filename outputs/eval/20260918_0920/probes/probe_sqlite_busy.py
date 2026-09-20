# -*- coding: utf-8 -*-
"""最小复现：长事务未提交时，同进程第二个连接写同一张表 → 忙等 busy_timeout。

R21 结论的钉子。复刻 `TaskRunner._stage_synthesize` 的真实时序：

    session A:  db.get(Task) -> 改 read_text -> db.flush()      # 拿 RESERVED 锁，**不提交**
                ... 合成全程（几十秒~十几分钟）...
    session B:  db.get(Task) -> 改 progress  -> commit()        # 撞锁，忙等

观测：B 的 commit 耗时 ≈ `PRAGMA busy_timeout`（api/db.py 设成 5000ms）。
对照组：A 先 commit 再让 B 写 → B 应在毫秒级完成。

用法：python probe_sqlite_busy.py
只用临时库，不碰 data/podcast.db。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# 证据脚本被放在 outputs/ 产物目录下，不能靠 parents 推仓库根，直接写死。
ROOT = Path(r"D:\podcast-ai")
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, event, text  # noqa: E402

from api.db import _apply_sqlite_pragmas  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY,
    read_text TEXT,
    progress INTEGER,
    status TEXT
)
"""


def _engine(path: Path):
    """与 api.db.build_engine 完全同参（check_same_thread/timeout/pragmas）。"""
    eng = create_engine(f"sqlite:///{path}",
                        connect_args={"check_same_thread": False, "timeout": 15},
                        future=True)
    event.listen(eng, "connect", _apply_sqlite_pragmas)
    return eng


def _seed(eng) -> None:
    """把表恢复成初始状态（幂等，两次对照之间调用）。"""
    with eng.begin() as c:
        c.execute(text(DDL))
        c.execute(text("DELETE FROM task"))
        c.execute(text("INSERT INTO task(id, read_text, progress, status) "
                       "VALUES ('t1', '旧读文', 30, 'SYNTHESIZING')"))


def run_case(eng, *, hold_open: bool) -> tuple[float, str]:
    """返回 (第二个连接写入耗时秒, 结果描述)。"""
    conn_a = eng.connect()
    conn_b = eng.connect()
    try:
        # --- session A：改动一行并 flush（产生 UPDATE → 拿写锁），是否提交由 hold_open 决定 ---
        tx_a = conn_a.begin()
        conn_a.execute(text("UPDATE task SET read_text='规范化读文' WHERE id='t1'"))
        if not hold_open:
            tx_a.commit()

        # --- session B：写 progress（模拟 _on_synth_progress） ---
        t0 = time.perf_counter()
        try:
            tx_b = conn_b.begin()
            conn_b.execute(text("UPDATE task SET progress=70 WHERE id='t1'"))
            tx_b.commit()
            dt = time.perf_counter() - t0
            desc = "写入成功"
            # 顺带确认 A 的写是否还在（hold_open 时 A 未提交，B 成功后应看不到自己的值也不该丢）
        except Exception as exc:  # noqa: BLE001
            dt = time.perf_counter() - t0
            desc = f"{type(exc).__name__}: {exc}"
        finally:
            if hold_open:
                try:
                    tx_a.rollback()
                except Exception:  # noqa: BLE001
                    pass
        return dt, desc
    finally:
        conn_a.close()
        conn_b.close()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sqlite_busy_")) / "t.db"
    eng = _engine(tmp)
    print(f"临时库：{tmp}")

    _seed(eng)
    dt_open, what_open = run_case(eng, hold_open=True)
    print(f"\n[A 长事务未提交] B 写 progress：{dt_open:6.2f}s  → {what_open}")

    _seed(eng)
    dt_ok, what_ok = run_case(eng, hold_open=False)
    print(f"[A 已提交后    ] B 写 progress：{dt_ok:6.2f}s  → {what_ok}")

    with eng.connect() as c:
        bt = c.execute(text("PRAGMA busy_timeout")).scalar()
    print(f"\nPRAGMA busy_timeout = {bt} ms")
    print(f"差值 = {dt_open - dt_ok:.2f}s")

    eng.dispose()
    try:
        tmp.unlink()
        tmp.parent.rmdir()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
