"""片头 / 片尾文案渲染与素材解析（主链路与生成脚本共用）。

背景
----
用户 2026-09-16 指定片头文案为**动态模板**：

    大家好，今天是（日期），今天的主题是（主题）。

带占位符 ⇒ 片头**不能再是固定 mp3**，必须按当期日期与主题在运行时合成。
本模块把「模板渲染」与「片头尾来源解析」集中一处，供
`api/services/task_runner.py`（运行时）与 `scripts/make_intro_outro.py`（离线生成）共用，
避免两处逻辑漂移。

注意（易踩坑）
------------
- 渲染用 `str.replace` 而非 `str.format`：文案是用户自由文本，可能含 `{}`，
  用 format 会抛 `KeyError`。
- 主题可能过长导致渲染后超出 `max_chars_per_seg`，故 `render_template` 支持
  `max_chars` 自动截断主题（优先保住模板骨架）。
"""
from __future__ import annotations

import time
from pathlib import Path

# 占位符同时支持全角括号 / 半角括号 / format 风格，用户随手写哪种都能命中
DATE_PLACEHOLDERS = ("（日期）", "(日期)", "{date}")
TOPIC_PLACEHOLDERS = ("（主题）", "(主题)", "{topic}")

DEFAULT_INTRO_TEMPLATE = "大家好，今天是（日期），今天的主题是（主题）。"


def today_cn(now: time.struct_time | None = None) -> str:
    """中文日期读法，如「2026年9月16日」。

    不用 `%-m`：那是 glibc 扩展，Windows 的 strftime 不支持，会直接抛 ValueError。
    """
    n = now or time.localtime()
    return "%d年%d月%d日" % (n.tm_year, n.tm_mon, n.tm_mday)


def render_template(tpl: str, date: str, topic: str,
                    max_chars: int = 0) -> str:
    """渲染模板中的日期 / 主题占位符。

    `max_chars > 0` 时，若渲染结果超长，会**从尾部截断 topic** 以保住模板骨架
    （片头开场白骨架比完整主题更重要），并用「…」标记截断。
    """
    def _render(t: str) -> str:
        out = tpl
        for ph in DATE_PLACEHOLDERS:
            out = out.replace(ph, date)
        for ph in TOPIC_PLACEHOLDERS:
            out = out.replace(ph, t)
        return out

    text = _render(topic)
    if max_chars and len(text) > max_chars:
        # 超长时逐字收缩主题（留 1 字符给省略号）
        over = len(text) - max_chars
        keep = max(len(topic) - over - 1, 1)
        text = _render(topic[:keep] + "…")
        # 极端情况（日期本身超长）仍超限时硬截
        if len(text) > max_chars:
            text = text[:max_chars]
    return text


def has_placeholder(tpl: str) -> bool:
    """模板是否含任一占位符（不含则视为固定文案，可安全复用缓存素材）。"""
    return any(p in (tpl or "") for p in DATE_PLACEHOLDERS + TOPIC_PLACEHOLDERS)


def _as_path(value) -> Path:
    """兼容 property 与方法两种形态。

    ⚠️ 踩过的坑：`Settings.intro_file` / `outro_file` 是 **property**，不是方法。
    早期实现写成 `settings.intro_file()` → 真实链路抛
    `TypeError: 'WindowsPath' object is not callable`（单测用假 settings 定义成
    方法反而测不出来，是典型的「假对象掩盖真接口」）。
    """
    return value() if callable(value) else value


def resolve_outro_file(settings) -> Path | None:
    """片尾素材：固定文件；不存在则返回 None（后期会跳过并告警）。"""
    p = _as_path(getattr(settings, "outro_file", None))
    return p if p is not None and Path(p).is_file() else None


def resolve_intro_file(settings) -> Path | None:
    """片头**固定**素材：仅当未启用动态模板时才有意义。"""
    p = _as_path(getattr(settings, "intro_file", None))
    return p if p is not None and Path(p).is_file() else None
