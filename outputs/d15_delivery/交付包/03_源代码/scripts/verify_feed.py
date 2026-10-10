# -*- coding: utf-8 -*-
"""RSS 订阅源的**跨产物**验收自检（D13 收口 D7~D9 遗留的「feed.xml 校验」）。

## 为什么单独写一个脚本，而不是把字段检查再写一遍

`api/services/podcast_rss.validate_feed_xml()` 已经负责「必填字段在不在」，
并且有单测锁着（`tests/test_podcast_rss.py`）。本脚本**刻意不重复那部分**，
只做它结构上做不到、而计划书 4.9 又反复强调的那一类核对：
把 **XML ↔ 数据库 ↔ 磁盘** 三边对起来。

| 检查码 | 内容 |
| --- | --- |
| `E-WELLFORMED` | XML 可解析 |
| `E-TOKEN` | 文件名 token == `<atom:link rel="self">` 的 basename，且该 token 在 `feeds` 表中存在 |
| `E-COUNT` | `<item>` 条数 == min(该用户 DONE 单集数, `FEED_EPISODE_LIMIT`) |
| `E-GUID-DUP` | `guid` 在单个 feed 内唯一 |
| `E-GUID-ORPHAN` | 每条 `guid` 都能在 `episodes.feed_guid` 里找到 |
| `E-OWNER` | 该单集的任务属主与 feed 属主一致 |
| `E-STATUS` | 该单集的任务状态为 `DONE`（未完成的不得出现在公开源里） |
| `E-ENC-URL` | `enclosure@url` 的路径形如 `/feed/{token}/{guid}.mp3` 且 token/guid 与本条一致 |
| `E-ENC-TYPE` | `enclosure@type` == `audio/mpeg` |
| `E-LEN-3WAY` | **`enclosure@length` == `episodes.file_size` == 磁盘实际字节数**（4.9 特意点名：必须是字节数，不是时长、不是 0） |
| `E-DUR-MISMATCH` | `itunes:duration` == 四舍五入后的 `episodes.duration_sec` |
| `E-PUBDATE` | `pubDate` 可被 `email.utils` 解析 |
| `E-FP-BOZO` / `E-FP-COUNT` | **独立第三方解析器**（`feedparser`）解析无异常、条目数一致 |
| `W-STALE-BASE` | 仅警告：feed 里的公网基址与当前 `PUBLIC_BASE_URL` 不一致（改过 `.env` 的旧 feed） |

## 与「W3C 在线校验」的关系（必须如实声明，不许含糊）

计划书要求「过 W3C Feed Validator」。**本机满足不了它的提交前提**：在线校验器需要一个
**公网可访问的 feed URL**，而本项目跑在 `http://127.0.0.1:8000` / 局域网地址，W3C 服务抓不到。
所以这里的定位是三步里的前两步：

1. **离线结构 + 跨产物核对**（本脚本）；
2. **独立第三方解析器复核**（`feedparser`）——证明「不是只有我们自己读得懂这份 XML」，
   这是「播客客户端可识别」在离线条件下的可自动化近似；
3. **在线 W3C 校验 + 真实客户端收录**：属**部署后**动作，须在公网环境执行，见《部署运维手册》。

**纪律**：不要把 1+2 说成「W3C 校验通过」；也不要因为 1+2 通过就省掉 3。

用法::

    python scripts/verify_feed.py                       # 全量核对 data/podcast/*.xml
    python scripts/verify_feed.py --json outputs/x.json
    python scripts/verify_feed.py --self-test           # 注入 4 类缺陷，必须全部变红
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"}

try:  # 独立第三方解析器：可选依赖，缺失时明报 skipped 而不是假装通过
    import feedparser  # type: ignore
except Exception:  # pragma: no cover - 取决于运行环境
    feedparser = None


# --------------------------------------------------------------------------- #
# 上下文
# --------------------------------------------------------------------------- #

def load_context(db_path: Path) -> dict:
    """只读打开 SQLite，取出核对需要的三张表。"""
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        feeds = {r["user_token"]: dict(r) for r in con.execute("SELECT * FROM feeds")}
        episodes = {r["feed_guid"]: dict(r) for r in con.execute("SELECT * FROM episodes")}
        tasks = {r["id"]: dict(r) for r in con.execute("SELECT id, user_id, status FROM tasks")}
    finally:
        con.close()
    return {"feeds": feeds, "episodes": episodes, "tasks": tasks}


def env_base_url(env_path: Path) -> str:
    """从 `.env` 读 PUBLIC_BASE_URL（只用于 W-STALE-BASE 警告，不参与失败判定）。"""
    if not env_path.is_file():
        return ""
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("PUBLIC_BASE_URL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# --------------------------------------------------------------------------- #
# 单项核对
# --------------------------------------------------------------------------- #

def _txt(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _fmt_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def check_feed(path: Path, ctx: dict, *, base_url: str = "",
               limit: int = 100, use_feedparser: bool = True) -> dict:
    """核对单个 feed 文件，返回 {"problems": [...], "stats": {...}}。

    problems 里每条都是 `CODE: 说明`；`E-` 为失败、`W-` 为警告。
    """
    problems: list[str] = []
    stats: dict = {"file": path.name, "items": 0}
    xml = path.read_text(encoding="utf-8")

    # ---- 1. 可解析 ---------------------------------------------------------
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return {"problems": [f"E-WELLFORMED: {exc}"], "stats": stats}
    channel = root.find("channel")
    if channel is None:
        return {"problems": ["E-WELLFORMED: 缺少 <channel>"], "stats": stats}

    token_from_name = path.stem

    # ---- 2. token 与寻址 ---------------------------------------------------
    self_link = channel.find("{http://www.w3.org/2005/Atom}link")
    if self_link is None or self_link.get("rel") != "self":
        problems.append('E-TOKEN: channel 缺 <atom:link rel="self">')
    else:
        href = self_link.get("href") or ""
        if Path(href).name != f"{token_from_name}.xml":
            problems.append(
                f"E-TOKEN: atom:link 指向 {Path(href).name}，与文件名 {token_from_name}.xml 不一致")
    feed_row = ctx["feeds"].get(token_from_name)
    if feed_row is None:
        problems.append(f"E-TOKEN: token {token_from_name!r} 不在 feeds 表中")
        return {"problems": problems, "stats": stats}
    stats["user_id"] = feed_row["user_id"]

    if base_url:
        link = _txt(channel.find("link"))
        if not link.startswith(base_url.rstrip("/")):
            problems.append(
                f"W-STALE-BASE: feed 基址 {link!r} 与当前 PUBLIC_BASE_URL {base_url!r} 不一致")

    # ---- 3. item 条数 ------------------------------------------------------
    items = channel.findall("item")
    stats["items"] = len(items)
    done = sum(1 for t in ctx["tasks"].values()
               if t["user_id"] == feed_row["user_id"] and t["status"] == "DONE")
    expect = min(done, limit)
    if len(items) != expect:
        problems.append(f"E-COUNT: item 条数 {len(items)}，期望 min(DONE={done}, limit={limit})={expect}")

    # ---- 4. 逐条核对 -------------------------------------------------------
    seen: set[str] = set()
    for i, item in enumerate(items, 1):
        guid = _txt(item.find("guid"))
        enc = item.find("enclosure")

        if not guid:
            problems.append(f"E-GUID-ORPHAN: item {i} 没有 guid")
        elif guid in seen:
            problems.append(f"E-GUID-DUP: item {i} 的 guid {guid} 在 feed 内重复")
        else:
            seen.add(guid)

        ep = ctx["episodes"].get(guid) if guid else None
        if ep is None:
            problems.append(f"E-GUID-ORPHAN: item {i} 的 guid {guid} 在 episodes 表中不存在")
        else:
            task = ctx["tasks"].get(ep["task_id"])
            if task is None:
                problems.append(f"E-OWNER: item {i} 的任务 {ep['task_id']} 不存在")
            else:
                if task["user_id"] != feed_row["user_id"]:
                    problems.append(
                        f"E-OWNER: item {i} 的任务属主 {task['user_id']} != feed 属主 {feed_row['user_id']}")
                if task["status"] != "DONE":
                    problems.append(f"E-STATUS: item {i} 的任务状态为 {task['status']}（应 DONE）")

        if enc is None:
            problems.append(f"E-ENC-URL: item {i} 缺 <enclosure>")
        else:
            url = enc.get("url") or ""
            want_tail = f"/feed/{token_from_name}/{guid}.mp3"
            if not url.endswith(want_tail):
                problems.append(f"E-ENC-URL: item {i} enclosure url {url!r} 应以 {want_tail!r} 结尾")
            if enc.get("type") != "audio/mpeg":
                problems.append(f"E-ENC-TYPE: item {i} type={enc.get('type')!r}，应为 audio/mpeg")

            # --- 三方字节数核对（本脚本存在的核心理由）---
            try:
                declared = int(enc.get("length") or 0)
            except ValueError:
                declared = -1
            disk = None
            if ep is not None:
                mp3 = Path(str(ep["mp3_path"]))
                if not mp3.is_file():
                    problems.append(f"E-LEN-3WAY: item {i} 成片文件不存在：{mp3}")
                else:
                    disk = mp3.stat().st_size
                    if int(ep["file_size"] or 0) != disk:
                        problems.append(
                            f"E-LEN-3WAY: item {i} episodes.file_size={ep['file_size']} "
                            f"!= 磁盘 {disk}")
                    if declared != disk:
                        problems.append(
                            f"E-LEN-3WAY: item {i} enclosure@length={declared} != 磁盘 {disk}"
                            "（4.9：必须是字节数）")
            else:
                if declared <= 0:
                    problems.append(f"E-LEN-3WAY: item {i} enclosure@length={declared} 非法")

        # --- 时长 ---
        dur_txt = _txt(item.find("itunes:duration", NS))
        if ep is not None:
            want = _fmt_duration(float(ep["duration_sec"] or 0))
            if dur_txt != want:
                problems.append(
                    f"E-DUR-MISMATCH: item {i} itunes:duration={dur_txt!r} != duration_sec {want!r}")

        # --- pubDate ---
        pub = _txt(item.find("pubDate"))
        try:
            dt = parsedate_to_datetime(pub)
        except Exception:
            dt = None
        if dt is None:
            problems.append(f"E-PUBDATE: item {i} pubDate 不可解析：{pub!r}")
        elif dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt is not None and dt > datetime.now(timezone.utc) + timedelta(days=1):
            problems.append(f"E-PUBDATE: item {i} pubDate 在未来：{pub!r}")

    # ---- 5. 独立第三方解析器复核 -------------------------------------------
    if use_feedparser:
        if feedparser is None:
            stats["feedparser"] = "skipped(未安装)"
        else:
            d = feedparser.parse(xml.encode("utf-8"))
            stats["feedparser"] = f"bozo={int(bool(d.bozo))}"
            if d.bozo:
                problems.append(f"E-FP-BOZO: feedparser 解析异常：{d.bozo_exception!r}")
            if len(d.entries) != len(items):
                problems.append(
                    f"E-FP-COUNT: feedparser 读到 {len(d.entries)} 条，XML 里 {len(items)} 条")
            if d.entries:
                e0 = d.entries[0]
                if not e0.get("title") or not e0.get("enclosures"):
                    problems.append("E-FP-COUNT: feedparser 未能读出首条的 title / enclosure")
    return {"problems": problems, "stats": stats}


# --------------------------------------------------------------------------- #
# 变异自证：证明这套判据不是摆设
# --------------------------------------------------------------------------- #

MUTATIONS = (
    # (代号, 说明, 期望出现的检查码, 变换函数)
    ("M1", "enclosure@length 改成真实字节数 +1", "E-LEN-3WAY",
     lambda xml: re.sub(r'length="(\d+)"', lambda m: f'length="{int(m.group(1)) + 1}"', xml, count=1)),
    ("M2", "enclosure@length 改成 0（典型误用：只写类型不写大小）", "E-LEN-3WAY",
     lambda xml: re.sub(r'length="\d+"', 'length="0"', xml, count=1)),
    ("M3", "把第 2 条的 guid 改成与第 1 条相同", "E-GUID-DUP",
     lambda xml: _dup_guid(xml)),
    ("M4", "itunes:duration 改成 00:00:01", "E-DUR-MISMATCH",
     lambda xml: re.sub(r"<itunes:duration>[^<]*</itunes:duration>",
                        "<itunes:duration>00:00:01</itunes:duration>", xml, count=1)),
)


def _dup_guid(xml: str) -> str:
    guids = re.findall(r"<guid[^>]*>([^<]+)</guid>", xml)
    if len(guids) < 2:
        return xml
    return xml.replace(f"<guid isPermaLink=\"false\">{guids[1]}</guid>",
                       f"<guid isPermaLink=\"false\">{guids[0]}</guid>", 1)


def self_test(ctx: dict, feeds: list[Path], base_url: str, limit: int) -> int:
    """对同一份真实 feed 注入 4 类缺陷，每类都必须让判据变红。"""
    target = next((p for p in sorted(feeds, key=lambda q: -q.stat().st_size)
                   if "<item>" in p.read_text(encoding="utf-8")), None)
    if target is None:
        print("[SELF-TEST] 找不到含 item 的 feed，无法自证", file=sys.stderr)
        return 3
    print(f"[SELF-TEST] 基准样本：{target.name}")
    original = target.read_text(encoding="utf-8")

    bad = 0
    for code, desc, want, mutate in MUTATIONS:
        mutated = mutate(original)
        if mutated == original:
            print(f"  {code} 变异未生效（变换没命中任何文本）—— 自证无效", file=sys.stderr)
            bad += 1
            continue
        # 原地替换文件内容后走真实核对路径（避免另写一条「影子判据」）
        target.write_text(mutated, encoding="utf-8")
        try:
            got = check_feed(target, ctx, base_url=base_url, limit=limit)["problems"]
        finally:
            target.write_text(original, encoding="utf-8")
        hit = any(p.startswith(want) for p in got)
        print(f"  {code} {desc} -> {'RED (OK)' if hit else 'GREEN (摆设!)'}  期望 {want}")
        if not hit:
            print(f"      实际 problems={got}", file=sys.stderr)
            bad += 1

    # 复原后必须回到 GREEN，否则说明自证过程污染了样本
    after = check_feed(target, ctx, base_url=base_url, limit=limit)["problems"]
    if after:
        print(f"  [RESTORE] 复原后仍为红：{after}", file=sys.stderr)
        bad += 1
    else:
        print("  [RESTORE] 复原后回到 GREEN (OK)")
    print(f"[SELF-TEST] {'FAIL' if bad else 'PASS'}（{len(MUTATIONS) - bad}/{len(MUTATIONS)} 如期变红）")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="RSS 订阅源跨产物验收自检")
    ap.add_argument("--dir", default=str(ROOT / "data" / "podcast"), help="feed xml 目录")
    ap.add_argument("--db", default=str(ROOT / "data" / "podcast.db"), help="SQLite 路径")
    ap.add_argument("--env", default=str(ROOT / ".env"), help="用于读 PUBLIC_BASE_URL")
    ap.add_argument("--limit", type=int, default=100, help="FEED_EPISODE_LIMIT")
    ap.add_argument("--json", default="", help="证据落盘路径")
    ap.add_argument("--self-test", action="store_true", help="变异自证：4 类缺陷必须全部变红")
    ap.add_argument("--no-feedparser", action="store_true", help="跳过第三方解析器复核")
    args = ap.parse_args(argv)

    feeds = sorted(Path(args.dir).glob("*.xml"))
    if not feeds:
        print(f"未找到任何 feed：{args.dir}", file=sys.stderr)
        return 2

    ctx = load_context(Path(args.db))
    base_url = env_base_url(Path(args.env))
    use_fp = not args.no_feedparser

    if args.self_test:
        return self_test(ctx, feeds, base_url, args.limit)

    results, n_err, n_warn = [], 0, 0
    for p in feeds:
        r = check_feed(p, ctx, base_url=base_url, limit=args.limit, use_feedparser=use_fp)
        errs = [x for x in r["problems"] if x.startswith("E-")]
        warns = [x for x in r["problems"] if x.startswith("W-")]
        n_err += len(errs)
        n_warn += len(warns)
        r["errors"], r["warnings"] = errs, warns
        results.append(r)

    total_items = sum(r["stats"]["items"] for r in results)
    with_items = sum(1 for r in results if r["stats"]["items"])
    print(f"feed 文件 {len(feeds)} 份；含单集的 {with_items} 份；item 合计 {total_items} 条")
    print(f"feedparser: {'未安装' if feedparser is None else feedparser.__version__}")
    print(f"E- 错误 {n_err} 条，W- 警告 {n_warn} 条")
    for r in results:
        if r["errors"]:
            print(f"  [FAIL] {r['stats']['file']}")
            for e in r["errors"]:
                print(f"      {e}")
    for r in results:
        for w in r["warnings"]:
            print(f"  [WARN] {r['stats']['file']}: {w}")

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "feed_dir": str(Path(args.dir)),
            "db": str(Path(args.db)),
            "public_base_url": base_url,
            "feedparser_version": None if feedparser is None else feedparser.__version__,
            "summary": {"files": len(feeds), "files_with_items": with_items,
                        "items": total_items, "errors": n_err, "warnings": n_warn},
            "results": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"证据落盘：{out}")

    return 1 if n_err else 0


if __name__ == "__main__":
    raise SystemExit(main())
