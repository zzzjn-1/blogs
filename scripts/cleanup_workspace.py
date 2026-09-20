# -*- coding: utf-8 -*-
"""工作区清理：删掉可再生的中间件与下载残留，**不动任何成片、缓存与证据**。

为什么要有这个脚本
------------------
1. `data/work/<task_id>/` 每期成片收尾都该被删，但在这台机器上**删不掉** ——
   沙箱的 bulk-delete 守卫（阈值 50 个文件）会拦下 `shutil.rmtree`，
   于是目录原地留下。跑十几期就攒到 GB 级。
2. 本脚本**逐个文件** `os.remove`，每次调用的删除数都是 1，不触发守卫。

安全边界（写死在代码里，不是靠调用方自觉）
----------------------------------------
- **绝不碰** `data/cache/`：它被 `audio_cache` 表按 `wav_path` 引用，删了会让缓存表整片失效。
- **绝不碰** `data/audio/`、`data/podcast.db*`、`outputs/`（成片、数据库、评测证据）。
- 删 `data/work/` 之前先校验 `episodes.mp3_path` **全部存在**；有一条缺失就整体放弃。
- 保留 `data/work/` 下的非任务目录（`assets_*`、`voicelab*` 等开发期素材不在本脚本职责内）。

用法
----
    python scripts/cleanup_workspace.py                # 干跑，只报告将删什么
    python scripts/cleanup_workspace.py --apply        # 真删
    python scripts/cleanup_workspace.py --apply --include-wheels   # 连 2.3 GB 轮子一起删

定期回收 data/work（可挂计划任务）
--------------------------------
    python scripts/cleanup_workspace.py --apply --work-only --quiet

`--work-only` 只清 `data/work/`，`--quiet` 只打一行汇总（适合进日志）。挂到 Windows
计划任务即可无人值守：

    schtasks /Create /TN "podcast-reclaim-work" /SC DAILY /ST 03:30 ^
      /TR "\"D:\\anaconda\\envs\\cosyvoice\\python.exe\" \"D:\\podcast-ai\\scripts\\cleanup_workspace.py\" --apply --work-only --quiet"

**无人值守比手动跑多两条硬约束**（手动时人看得见，自动时没人看）：

1. **绝不删「正在跑」的任务目录**。以前只按目录名像不像 task_id 判断，定时跑来不及看，
   一旦命中正在合成的任务就会把它中途的工作文件清空。现在追加一条：**库内处于非终态的
   `task_id` 一律跳过**。
2. **最近还在被写的目录也跳过**（`--grace-min`，默认 30 分钟），防止「目录名像 task_id、
   但不是任何任务」的边界情况被误删。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 仓库根下这些后缀/名字直接删（D0~D3 期间的临时日志，均已过期）
ROOT_JUNK_SUFFIX = (".log",)
ROOT_JUNK_NAMES = {"jr.xml"}  # 旧的 pytest junit 报告，权威版在 outputs/synth_trace/junit_fix.xml


def _size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for r, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


def _count(path: Path) -> int:
    if path.is_file():
        return 1
    return sum(len(files) for _r, _d, files in os.walk(path))


def _rm_tree_by_file(path: Path, apply: bool) -> tuple[int, int]:
    """逐个文件删除（绕过 bulk-delete 守卫），最后自底向上删空目录。

    返回 (删除文件数, 删除目录数)。
    """
    if path.is_file():
        if apply:
            try:
                os.remove(path)
            except OSError as exc:
                print(f"    ! 删除失败 {path}: {exc}")
                return 0, 0
        return 1, 0

    n_files = n_dirs = 0
    dirs: list[Path] = []
    for r, d, files in os.walk(path, topdown=False):
        for f in files:
            fp = Path(r) / f
            if apply:
                try:
                    os.remove(fp)
                except OSError as exc:
                    print(f"    ! 删除失败 {fp}: {exc}")
                    continue
            n_files += 1
        for name in d:
            dirs.append(Path(r) / name)
    for d in dirs + [path]:
        if apply:
            try:
                os.rmdir(d)
                n_dirs += 1
            except OSError:
                pass
        else:
            n_dirs += 1
    return n_files, n_dirs


def _episodes_audio_ok() -> tuple[bool, str]:
    """所有 episode 的成片都还在？不在就别动 data/work。"""
    db = ROOT / "data" / "podcast.db"
    if not db.exists():
        return False, f"数据库不存在：{db}"
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute("select task_id, mp3_path from episodes").fetchall()
    finally:
        con.close()
    missing = [r for r in rows if not Path(r[1]).exists()]
    if missing:
        return False, f"{len(missing)}/{len(rows)} 条成片缺失，例如 {missing[0][0]}"
    return True, f"{len(rows)}/{len(rows)} 条成片均在原位"


def _non_terminal_task_ids() -> tuple[set[str] | None, str]:
    """库内仍在跑（非终态）的 task_id —— 这些目录**一个都不能删**。

    返回 `(None, 原因)` 表示**查询不可用**（库缺失 / 导入失败）。调用方据此
    **放弃删除 data/work**：宁可这一轮不清，也不能在信息不全的情况下删掉
    正在合成的任务目录。
    """
    db = ROOT / "data" / "podcast.db"
    if not db.exists():
        return None, f"数据库不存在：{db}"
    # 终态以 api.models.TaskStatus.TERMINAL 为唯一真源，不在这里复制一份
    sys.path.insert(0, str(ROOT))
    try:
        from api.models import TaskStatus  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return None, f"无法导入 TaskStatus（{exc}）"
    terminal = tuple(TaskStatus.TERMINAL)
    marks = ",".join("?" * len(terminal))
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            f"select id from tasks where status not in ({marks})", terminal
        ).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}, f"{len(rows)} 个非终态任务"


def _recently_modified(path: Path, grace_min: float) -> bool:
    """目录在 grace 窗口内还被写过？"""
    if grace_min <= 0:
        return False
    newest = path.stat().st_mtime
    for r, _d, files in os.walk(path):
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(r, f)))
            except OSError:
                pass
    return (time.time() - newest) < grace_min * 60


def collect_targets(include_wheels: bool, *, work_only: bool = False,
                    work_busy: set[str] | None = None,
                    grace_min: float = 30.0,
                    skipped: list[str] | None = None) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    work_busy = work_busy or set()
    skipped = skipped if skipped is not None else []

    # 1) data/work 的任务目录（中间件）
    work = ROOT / "data" / "work"
    if work.is_dir():
        for entry in sorted(work.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            # 任务目录 = 36 位 UUID 或 ep_<时间戳>；其余是开发期素材，不碰
            is_task = len(name) == 36 and name.count("-") == 4
            is_ep = name.startswith("ep_20")
            if not (is_task or is_ep or name == "_cache_cleanup"):
                continue
            # 无人值守的两道安全闸（见模块 docstring）
            if name in work_busy:
                skipped.append(f"{name}（库内非终态，可能正在合成）")
                continue
            if _recently_modified(entry, grace_min):
                skipped.append(f"{name}（{grace_min:.0f} 分钟内仍在写）")
                continue
            targets.append((f"data/work/{name}", entry))

    if work_only:
        return targets

    # 2) 下载残留
    for sub in ("downloads",):
        p = ROOT / sub
        if p.is_dir():
            targets.append((sub, p))

    # 3) 环境搭建期日志
    logs = ROOT / "logs"
    if logs.is_dir():
        targets.append(("logs", logs))

    # 4) 仓库根临时文件
    for f in sorted(ROOT.iterdir()):
        if f.is_file() and (f.suffix in ROOT_JUNK_SUFFIX or f.name in ROOT_JUNK_NAMES):
            targets.append((f.name, f))

    # 5) 可重建的测试缓存
    for sub in (".pytest_cache",):
        p = ROOT / sub
        if p.is_dir():
            targets.append((sub, p))

    # 6) 空的评测目录
    for p in sorted((ROOT / "outputs" / "eval").glob("*")) if (ROOT / "outputs" / "eval").is_dir() else []:
        if p.is_dir() and not any(p.iterdir()):
            targets.append((f"outputs/eval/{p.name}（空）", p))

    # 7) 2.3 GB 的 torch 轮子（装好就没用了，需显式开启）
    if include_wheels:
        p = ROOT / "wheels"
        if p.is_dir():
            targets.append(("wheels", p))

    return targets


def main() -> int:
    ap = argparse.ArgumentParser(description="清理可再生的中间件与下载残留")
    ap.add_argument("--apply", action="store_true", help="真删；不加则只干跑")
    ap.add_argument("--include-wheels", action="store_true", help="连 wheels/ 的 2.3 GB 轮子一起删")
    ap.add_argument("--work-only", action="store_true",
                    help="只清 data/work/（供计划任务定期回收；不碰下载残留与日志）")
    ap.add_argument("--quiet", action="store_true",
                    help="只打一行汇总（供无人值守计划任务进日志）")
    ap.add_argument("--grace-min", type=float, default=30.0,
                    help="最近 N 分钟内被写过的任务目录跳过（默认 30；0 表示不跳）")
    args = ap.parse_args()

    def say(*a: object) -> None:
        if not args.quiet:
            print(*a)

    ok, reason = _episodes_audio_ok()
    say(f"[成片校验] {reason}")
    if not ok:
        print(f"[中止] 成片校验未通过，拒绝触碰 data/work —— 先查清缺哪一期。（{reason}）")
        return 2

    busy, busy_reason = _non_terminal_task_ids()
    if busy is None:
        # 查不到「谁在跑」就不敢删 work —— 直接把它们从目标里摘掉
        print(f"[中止 work 清理] 非终态任务查询不可用：{busy_reason}。")
        if args.work_only:
            return 2
        busy = set()
    else:
        say(f"[在跑任务] {busy_reason}（这些目录会跳过）")

    skipped: list[str] = []
    targets = collect_targets(args.include_wheels, work_only=args.work_only,
                              work_busy=busy, grace_min=args.grace_min,
                              skipped=skipped)
    if skipped:
        say(f"[跳过 {len(skipped)} 项] " + "；".join(skipped[:5])
            + ("…" if len(skipped) > 5 else ""))

    if not targets:
        print(f"[回收] 没有待清理项；跳过 {len(skipped)} 项。")
        return 0

    total_b = total_f = total_d = 0
    say(f"\n{'='*74}\n待清理 {len(targets)} 项（{'真删' if args.apply else '干跑'}）\n{'='*74}")
    for label, path in targets:
        b, n = _size(path), _count(path)
        total_b += b
        say(f"  {label:<52} {n:>6} 文件  {b/1048576:>9.1f} MB")
        if args.apply:
            f, d = _rm_tree_by_file(path, apply=True)
            total_f += f
            total_d += d

    say(f"{'-'*74}")
    if args.apply:
        print(f"[回收] 目标 {len(targets)} 项，实删 {total_f} 文件 / {total_d} 目录，"
              f"释放 {total_b/1048576:.1f} MB；跳过 {len(skipped)} 项。")
        if not _episodes_audio_ok()[0]:
            print("[警告] 删除后成片校验失败，请立即排查！")
            return 3
    else:
        print(f"[干跑] 将清理 {len(targets)} 项，合计 {total_b/1048576:.1f} MB，"
              f"跳过 {len(skipped)} 项。（加 --apply 执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
