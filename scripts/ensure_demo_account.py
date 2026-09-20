# -*- coding: utf-8 -*-
"""给账号写入一个**合法可登录**的口令哈希（D14 演示前置）。

## 为什么需要它

命令行入口 `scripts/make_episode.py` 是 get-or-create 语义：`local` 不存在时
它会建一个 `password_hash="x"` 的账号。这是**故意的**——该账号原本只给 CLI/API 用。

后果是：**任何一次「全新环境按部署手册起服务」都会得到一个登不进 Web 的演示账号**，
因为数据库是新建的，`local` 又以占位哈希被创建。D14 预置演示数据时才暴露出来。

这条缺陷能活这么久，是因为项目此前所有验证都走命令行与 HTTP API，
**没有任何一条用例真的用浏览器登录过 `local`**。所以本脚本把
「写入」与「立刻回验」放在同一次运行里：`verify_password` 必须为真、
错口令必须为假，两个方向都过才算成功。

## 用法

    # 随机 16 位口令，写入并打印
    python scripts/ensure_demo_account.py

    # 指定口令
    python scripts/ensure_demo_account.py --password 'Podcast@2026'

    # 账号不存在时一并创建
    python scripts/ensure_demo_account.py --create --username local

    # 把口令同时写入凭据文件（演示前交给答辩人）
    python scripts/ensure_demo_account.py --out "D:/新建文件夹/blogs/.../凭据说明.md"

## 口径

- 口令走 `api/security.py` 的 `hash_password()`（bcrypt + sha256 预摘要，12 轮），
  **与 Web 登录校验用的是同一个函数**，不自己实现哈希。
- 口令**只打印到 stdout、只写入 `--out` 指定的文件**，不落日志、不进数据库明文字段。
- 已存在的账号默认**也要覆盖**口令（这是本脚本的用途）；加 `--if-unset` 则只在
  「当前哈希不可用」时才写，用于不想动既有口令的场合。
"""
from __future__ import annotations

import argparse
import secrets
import sqlite3
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.security import hash_password, verify_password  # noqa: E402

DEFAULT_DB = ROOT / "data" / "podcast.db"
ALPHABET = string.ascii_letters + string.digits


def usable(hash_value: str, probe: str = "probe-do-not-login") -> bool:
    """当前哈希能不能正常参与校验（`bcrypt.checkpw` 不抛异常即可视为结构合法）。

    `password_hash="x"` 这种占位串会让 `bcrypt.checkpw` 抛 `ValueError`，
    而 `verify_password` 把它吞成 False —— 于是「登录失败」看起来跟「口令打错」一模一样，
    排查时极容易走偏。这里把它单独判出来，好让脚本自己去修。
    """
    import bcrypt

    if not hash_value:
        return False
    try:
        bcrypt.checkpw(b"x", hash_value.encode("ascii"))
    except (ValueError, TypeError):
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="给账号写入合法口令哈希（演示前置）")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--username", default="local")
    ap.add_argument("--password", default=None, help="留空则随机生成 16 位")
    ap.add_argument("--create", action="store_true", help="账号不存在时创建")
    ap.add_argument("--if-unset", action="store_true", help="仅在当前哈希不可用时才写入")
    ap.add_argument("--out", default="", help="同时把口令写入该文件（UTF-8）")
    args = ap.parse_args(argv)

    pwd = args.password or "".join(secrets.choice(ALPHABET) for _ in range(16))

    con = sqlite3.connect(args.db, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT id, username, password_hash FROM users WHERE username=?",
                          (args.username,)).fetchone()
        if row is None:
            if not args.create:
                print(f"账号 {args.username} 不存在；加 --create 才会创建", file=sys.stderr)
                return 2
            cur = con.execute("INSERT INTO users (username, password_hash, created_at) "
                              "VALUES (?, '', datetime('now'))", (args.username,))
            con.commit()
            uid, was = cur.lastrowid, "created"
        else:
            uid, was = row["id"], "exists"

        old = con.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()[0]
        if args.if_unset and usable(old):
            print(f"账号 {args.username}(id={uid}) 现有哈希可用，--if-unset 生效：未改动")
            return 0

        con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(pwd), uid))
        con.commit()
        stored = con.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()[0]
    finally:
        con.close()

    # ---- 回验：两个方向都必须对，否则「写进去了」不等于「登得进去」----
    ok_right = verify_password(pwd, stored)
    ok_wrong = not verify_password(pwd + "\u00a0x", stored)
    print(f"账号　　: {args.username}(id={uid})　[{was}]")
    print(f"旧哈希　: {old!r}（结构合法={usable(old)}）")
    print(f"新哈希　: {stored[:7]}…（{len(stored)} 字节，结构合法={usable(stored)}）")
    print(f"回验　　: 正确口令被接受 -> {ok_right}　错误口令被拒绝 -> {ok_wrong}")
    if not (ok_right and ok_wrong):
        print("回验失败：哈希已写入但校验方向不对，请检查 api/security.py", file=sys.stderr)
        return 1
    print(f"口令　　: {pwd}")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"# 演示账号凭据（本文件含明文口令，勿随 PPT / 公开材料分发）\n\n"
            f"- 账号：`{args.username}`\n"
            f"- 口令：`{pwd}`\n"
            f"- 写入时刻：本次运行\n"
            f"- 生成方式：`python scripts/ensure_demo_account.py --username {args.username}`"
            f"（`api/security.py` 的 bcrypt 12 轮）\n",
            encoding="utf-8")
        print(f"凭据落盘：{out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
