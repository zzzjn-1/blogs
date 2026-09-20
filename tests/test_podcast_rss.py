# -*- coding: utf-8 -*-
"""RSS 生成模块单元测试（计划书 4.9 订阅文件规范）。

覆盖：离线结构自检 `validate_feed_xml` 对「合法 / 缺字段 / enclosure 非字节数」的判定，
以及 `new_user_token` 产出不可猜测 token。
"""
from __future__ import annotations

from api.config import get_settings
from api.services.podcast_rss import new_user_token, validate_feed_xml

_ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_ATOM = "http://www.w3.org/2005/Atom"

VALID_RSS = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:itunes="{_ITUNES}" xmlns:atom="{_ATOM}">
  <channel>
    <title>测试频道</title>
    <description>描述</description>
    <link>http://example.com</link>
    <language>zh-cn</language>
    <itunes:author>alice</itunes:author>
    <itunes:category text="Technology"/>
    <itunes:explicit>no</itunes:explicit>
    <atom:link rel="self" href="http://example.com/feed/t.xml"/>
    <item>
      <title>单集一</title>
      <description>单集描述</description>
      <pubDate>Wed, 16 Sep 2026 01:00:00 +0000</pubDate>
      <guid>guid-1</guid>
      <enclosure url="http://example.com/feed/t/guid-1.mp3" length="12345" type="audio/mpeg"/>
      <itunes:duration>00:01:30</itunes:duration>
      <itunes:explicit>no</itunes:explicit>
    </item>
  </channel>
</rss>
"""

BROKEN_RSS = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:itunes="{_ITUNES}" xmlns:atom="{_ATOM}">
  <channel>
    <description>描述</description>
    <link>http://example.com</link>
    <language>zh-cn</language>
    <itunes:author>alice</itunes:author>
    <itunes:category text="Technology"/>
    <itunes:explicit>no</itunes:explicit>
    <item>
      <title>单集一</title>
      <description>单集描述</description>
      <pubDate>Wed, 16 Sep 2026 01:00:00 +0000</pubDate>
      <guid>guid-1</guid>
      <enclosure url="http://example.com/feed/t/guid-1.mp3" length="0" type="audio/mpeg"/>
      <itunes:duration>00:01:30</itunes:duration>
      <itunes:explicit>no</itunes:explicit>
    </item>
  </channel>
</rss>
"""


def test_valid_rss_passes_selfcheck():
    problems = validate_feed_xml(VALID_RSS)
    assert problems == [], problems


def test_broken_rss_flags_missing_title_and_bad_enclosure():
    problems = validate_feed_xml(BROKEN_RSS)
    joined = "\n".join(problems)
    assert "title" in joined, problems
    # enclosure length=0 → 应判为非法（计划书 4.9：必须是字节数）
    assert "length" in joined, problems


def test_missing_atom_self_link_flagged():
    # 去掉 atom:link 后应有对应问题
    no_atom = VALID_RSS.replace(
        '    <atom:link rel="self" href="http://example.com/feed/t.xml"/>\n', "")
    problems = validate_feed_xml(no_atom)
    assert any("atom:link" in p for p in problems), problems


def test_new_user_token_is_unguessable():
    tok = new_user_token(get_settings())
    assert isinstance(tok, str) and len(tok) >= 8
    # 两次调用应不同
    assert new_user_token(get_settings()) != tok
