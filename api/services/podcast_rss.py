# -*- coding: utf-8 -*-
"""播客 RSS 生成（计划书 4.8 的 `podcast_rss.py`、4.9 订阅文件规范）。

## ⚠️ 计划书 4.9 的一处实现缺陷（V1.10.0 原文有误，本轮实测更正）

4.9 给的实现示例是：

```python
fg.load_extension('podcast')       # 提供 itunes: 命名空间
fg.load_extension('atom')          # 提供 atom:link rel=self
```

**第二行必然抛 `ModuleNotFoundError: No module named 'feedgen.ext.atom'`。**
实测 feedgen 1.0.0（本机环境）的扩展目录只有：
`base / dc / geo / geo_entry / media / podcast / podcast_entry / syndication / torrent`
——没有 `atom`。

而且它**根本不需要**：`xmlns:atom` 与 `<atom:link rel="self">` 由 feedgen **核心**提供，
只要调用 `fg.link(href=..., rel="self", type="application/rss+xml")` 即可生成
（实测输出含 `xmlns:atom` 与 `<atom:link href=... rel="self" type=.../>`）。

也就是说，照 4.9 抄不但多一行错的，还会把「本来能跑」的代码写崩。本模块按实测写法实现，
并保留 `[FIX-RSS-ATOM-01]` 记录（详见 D6 报告）。

## 校验标准（计划书 4.9）

生成后须过 W3C Feed Validator 与 Apple Podcasts 收录规则。本模块自带**离线结构自检**
（`validate_feed_xml`），把 4.9 表里的必填字段逐项断言一遍 —— 离线自检不能代替
在线校验器，但能把「字段漏了」这类错误在上线前全部拦住。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from feedgen.feed import FeedGenerator
from sqlalchemy import or_
from sqlalchemy.orm import Session

from api.config import Settings, get_settings
from api.models import Episode, Feed, Task, TaskStatus, User

log = logging.getLogger(__name__)

NS = {
    "rss": "http://purl.org/rss/1.0/",
    "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "atom": "http://www.w3.org/2005/Atom",
}

#: 计划书 4.9「必填字段」表逐项落地。channel 四条 + itunes 五条，item 三组。
CHANNEL_REQUIRED = ("title", "description", "link", "language",
                    "itunes:author", "itunes:category", "itunes:explicit", "atom:link")
ITEM_REQUIRED = ("title", "description", "pubDate", "guid", "enclosure",
                 "itunes:duration", "itunes:explicit")


class RSSError(Exception):
    """RSS 生成失败。"""


# --------------------------------------------------------------------------- #
# 寻址
# --------------------------------------------------------------------------- #

def new_user_token(settings: Settings | None = None) -> str:
    """生成订阅源公开 token（计划书 4.7.1：路径不可猜测）。"""
    s = settings or get_settings()
    return secrets.token_urlsafe(max(8, int(s.feed_token_bytes)))


def feed_url(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/feed/{token}.xml"


def enclosure_url(settings: Settings, token: str, guid: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/feed/{token}/{guid}.mp3"


def default_feed_title(user: User) -> str:
    return f"{user.username} 的播客"


def _cover_image_url(settings: Settings, token: str) -> str:
    """把配置的 cover_url 解析成对外可访问的完整 URL。

    规则：
    - 空串 -> 空串（表示不设置默认封面）
    - 以 http:// 或 https:// 开头 -> 原样返回
    - 其他（相对路径或 file://） -> 映射到 /feed/{token}/cover.jpg
    """
    raw = (settings.cover_url or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://")):
        return raw
    return f"{settings.public_base_url.rstrip('/')}/feed/{token}/cover.jpg"


def ensure_feed(db: Session, user: User, settings: Settings | None = None) -> Feed:
    """取用户频道配置；不存在则建默认档（计划书 4.6 FEEDS，一用户一频道）。

    为什么要自动建：PACKAGING 阶段要刷新 RSS，而没有频道配置就无法生成合法 feed。
    让「建任务」这条主流程因为「没填过频道设置」而失败，是把可选配置变成了必填前置。
    """
    s = settings or get_settings()
    feed = db.get(Feed, user.id)
    if feed is not None:
        if not feed.user_token:
            # 老数据（或手工插的档）缺 token：补一个，否则 /feed/xxx.xml 无从寻址
            feed.user_token = new_user_token(s)
        # 若频道未配置封面且系统有默认封面，自动回填（首次生效）
        if not feed.cover_url and s.cover_url:
            feed.cover_url = _cover_image_url(s, feed.user_token)
        return feed
    token = new_user_token(s)
    feed = Feed(user_id=user.id, title=default_feed_title(user),
                description=f"{user.username} 使用双人对话播客自动生成系统产出的节目。",
                cover_url=_cover_image_url(s, token),
                category=s.feed_default_category, explicit=False,
                user_token=token)
    db.add(feed)
    db.flush()
    return feed


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

def _fmt_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _owner_email(settings: Settings) -> str:
    """从 public_base_url 推一个 noreply 地址：`itunes:owner` 必填 email，但本项目无邮件服务。"""
    host = settings.public_base_url.split("://")[-1].split("/")[0].split(":")[0]
    return f"noreply@{host or 'localhost'}"


def list_episodes(db: Session, user: User, settings: Settings | None = None, *,
                  include_task_id: str | None = None) -> list[Episode]:
    """该用户已完成的单集，按发布时间倒序（feed 里通常新的在前）。

    `include_task_id` 用于**封装当刻**：PACKAGING 阶段先把 Episode 行落库、再写
    feed、最后才转 DONE，而本函数的过滤条件是 `Task.status == DONE` —— 于是
    「刚做好的这一期」会被自己的过滤条件挡在订阅源之外。

    [FIX-FEED-LATEST-01] 实测后果是**订阅源恒久落后一期**（14/14 份 feed 的 item
    条数都恰为 DONE 单集数 − 1，见 `scripts/verify_feed.py` 的 `E-COUNT`）：
    用户出一期、客户端看不到，要等**再出一期**才会把上一期带进去，而那一期又漏掉
    自己。因为它是 100% 复现的结构性偏差、不是偶发，所以格外容易被认为是「正常」。

    刻意**不**改成「先转 DONE 再写 feed」：那样一旦 RSS 写失败，任务已是终态，
    会留下「状态 = 成功、订阅源却是旧的」这种更坏的组合。正确做法是保持
    「RSS 写成功才转 DONE」，同时把本期显式纳入本次生成。
    """
    s = settings or get_settings()
    q = (db.query(Episode)
         .join(Task, Episode.task_id == Task.id)
         .filter(Task.user_id == user.id))
    if include_task_id:
        q = q.filter(or_(Task.status == TaskStatus.DONE,
                         Task.id == include_task_id))
    else:
        q = q.filter(Task.status == TaskStatus.DONE)
    return (q.order_by(Episode.pub_date.desc())
            .limit(max(1, int(s.feed_episode_limit)))
            .all())


def build_feed_xml(db: Session, *, user: User, settings: Settings | None = None,
                   include_task_id: str | None = None) -> str:
    """生成该用户的 RSS 2.0 + iTunes 订阅 XML（计划书 4.9）。"""
    s = settings or get_settings()
    feed = ensure_feed(db, user, s)
    token = feed.user_token
    self_url = feed_url(s, token)

    fg = FeedGenerator()
    # [FIX-RSS-ATOM-01] 只加载 podcast；不要 load_extension('atom')，feedgen 1.0.0 无此模块
    fg.load_extension("podcast")
    fg.id(self_url)
    fg.title(feed.title or default_feed_title(user))
    # [FIX-RSS-LINK-01] feedgen 把 RSS 主 <link> 设为「最后一条 fg.link() 的 href」
    # （见 feedgen/feed.py L614-616），因此 rel=self 必须先写、主站链接最后写，
    # 否则 plain <link> 会被 self_url 覆盖。rel=self 渲染成 <atom:link rel="self">，
    # 主站链接 rel 默认 alternate，同时成为 plain <link> 与 <atom:link rel="alternate">。
    fg.link(href=self_url, rel="self", type="application/rss+xml")
    fg.link(href=s.public_base_url.rstrip("/"))
    fg.description(feed.description or "")
    fg.language(s.feed_default_language)
    fg.podcast.itunes_author(user.username)
    fg.podcast.itunes_owner(name=user.username, email=_owner_email(s))
    fg.podcast.itunes_category(feed.category or s.feed_default_category)
    fg.podcast.itunes_explicit("yes" if feed.explicit else "no")
    if feed.cover_url:
        fg.podcast.itunes_image(feed.cover_url)
    else:
        # 不阻断：Apple Podcasts 要求 itunes:image，缺失会在收录审核时被拒
        log.warning("频道 %s 未配置封面（COVER_URL），生成的 feed 缺 itunes:image，"
                    "Apple Podcasts 收录会失败", user.username)

    episodes = list_episodes(db, user, s, include_task_id=include_task_id)
    for idx, ep in enumerate(episodes, 1):
        fe = fg.add_entry()
        guid = ep.feed_guid or ep.id
        fe.id(guid)
        fe.title(ep.title or "未命名单集")
        fe.description((ep.task.script_summary if ep.task else "") or ep.title or "")
        # pubDate：feedgen 接受 datetime，但 naive/aware 混用会输出错时区，统一转成 UTC aware
        pub = ep.pub_date or datetime.utcnow()
        fe.pubDate(pub.replace(tzinfo=None).isoformat() + "+00:00")
        fe.enclosure(enclosure_url(s, token, guid), int(ep.file_size or 0), "audio/mpeg")
        fe.podcast.itunes_duration(_fmt_duration(ep.duration_sec))
        fe.podcast.itunes_explicit("yes" if feed.explicit else "no")
        # episodes 按 pub_date 倒序取出，itunes:episode 用「总集数 - 序号 + 1」还原真实集号
        fe.podcast.itunes_episode(max(1, len(episodes) - idx + 1))

    xml = fg.rss_str(pretty=True).decode("utf-8")
    return xml


def write_feed_xml(db: Session, *, user: User,
                   settings: Settings | None = None,
                   include_task_id: str | None = None) -> Path:
    """生成并落盘到 `data/podcast/{user_token}.xml`。

    落盘（而非每次请求现算）的理由：RSS 是**对外公开**的静态资源，
    每次请求都查库+拼 XML 会把公开流量直接压到数据库上；且 PACKAGING 阶段
    「RSS 生成完成」在状态机里是一个可判定的动作，落盘才可判定。
    """
    s = settings or get_settings()
    feed = ensure_feed(db, user, s)
    xml = build_feed_xml(db, user=user, settings=s, include_task_id=include_task_id)
    s.podcast_path.mkdir(parents=True, exist_ok=True)
    out = s.podcast_path / f"{feed.user_token}.xml"
    tmp = out.with_suffix(".xml.tmp")
    # 先写临时文件再改名：避免播客客户端在写入途中抓到半截 XML
    tmp.write_text(xml, encoding="utf-8")
    tmp.replace(out)
    return out


def remove_feed_xml(token: str, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    p = s.podcast_path / f"{token}.xml"
    if p.is_file():
        p.unlink()
        return True
    return False


# --------------------------------------------------------------------------- #
# 离线结构自检（不能替代在线校验器，见模块 docstring）
# --------------------------------------------------------------------------- #

def _find_node(parent: ET.Element, key: str) -> ET.Element | None:
    """按计划书 4.9 的字段名（可带 `itunes:`/`atom:` 前缀）在 parent 下定位节点。

    命名空间判定纪律（实测踩坑）：
    - 无冒号字段（title/description/link/language/pubDate/guid）→ 裸 `find(key)`；
      若误用 `key.partition(":")` 会得到空 tag（`"title".partition(":") == ("title","","")`），
      再 `find("")` 必返回 None，导致「明明有却报缺」。
    - 带前缀且前缀在 `NS` 内 → `find("prefix:tag", NS)`；前缀不在 `NS` 内（如罕见的
      `content:`）→ 退化为裸 `find(tag)`，避免 ElementTree 报 "prefix not found"。
    """
    if ":" in key:
        prefix, _, tag = key.partition(":")
        if prefix in NS:
            return parent.find(f"{prefix}:{tag}", NS)
        return parent.find(tag)
    return parent.find(key)


def validate_feed_xml(xml: str, *, expect_items: int | None = None) -> list[str]:
    """返回问题清单（空列表 = 通过）。

    逐项对应计划书 4.9 的必填字段表；另外校验 enclosure 的 `length` 必须是
    **字节数**（4.9 特意强调「不是时长」——传 0 或时长的 feed 客户端会显示异常大小）。

    ⚠️ `itunes:category` 是自闭合元素，分类名写在 `text` **属性**上（Apple 规范），
    节点 `text` 为空是正常态，故该项特判属性而非文本。
    """
    problems: list[str] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        return [f"XML 解析失败：{exc}"]

    channel = root.find("channel")
    if channel is None:
        return ["缺少 <channel> 元素"]

    for key in CHANNEL_REQUIRED:
        if key == "atom:link":
            found = channel.find("atom:link", NS)
            if found is None:
                problems.append("channel 缺 <atom:link rel=\"self\">（Feed 规范校验要求）")
            elif found.get("rel") != "self":
                problems.append(f"channel 的 atom:link rel 应为 self，实际 {found.get('rel')!r}")
        elif key == "itunes:category":
            node = _find_node(channel, key)
            if node is None or not (node.get("text") or "").strip():
                problems.append("channel 缺 <itunes:category>（分类名写在 text 属性）")
        else:
            node = _find_node(channel, key)
            if node is None or not (node.text or "").strip():
                problems.append(f"channel 缺 <{key}>")

    items = channel.findall("item")
    if expect_items is not None and len(items) != expect_items:
        problems.append(f"item 数量不符：期望 {expect_items}，实际 {len(items)}")
    if not items:
        problems.append("channel 下没有任何 <item>（客户端会认为这是空节目）")

    for i, item in enumerate(items, 1):
        for key in ITEM_REQUIRED:
            if key == "enclosure":
                enc = item.find("enclosure")
                if enc is None:
                    problems.append(f"item {i} 缺 <enclosure>")
                    continue
                if enc.get("type") != "audio/mpeg":
                    problems.append(f"item {i} enclosure type 应为 audio/mpeg")
                try:
                    length = int(enc.get("length") or 0)
                except ValueError:
                    length = 0
                if length <= 0:
                    problems.append(
                        f"item {i} enclosure length 非法（{enc.get('length')!r}）"
                        "—— 计划书 4.9：必须是**字节数**，不是时长")
            else:
                node = _find_node(item, key)
                if node is None or not (node.text or "").strip():
                    problems.append(f"item {i} 缺 <{key}>")

    return problems
