# -*- coding: utf-8 -*-
"""Pydantic 出入参模型（对应开发计划书 4.8 工程目录结构）。

D4 落地的是「LLM 脚本输出」这一组模型，它是计划书 4.10 ③「三重保险」的第 2 层：
    response_format=json_object（第 1 层，由 script_gen 发起）
    -> Pydantic 结构校验 + check_policy 业务校验（本模块，第 2 层）
    -> 带具体错误信息纠错重试 / 正则降级（第 3 层，在 script_gen）

## 约束分级（V1.0.0 定稿，依据 D4 实测）

`check_policy()` 把业务约束分成三档，**这是本模块最重要的设计决定**：

| 档位 | 内容 | 是否判「不可用」 | 是否触发重试 |
| --- | --- | --- | --- |
| `errors` | 说话人非法 / `seq` 不连续 / 单行超上限 / 只有一个说话人 / 连续同人 | 是 | 是 |
| `quota` | 总字数偏离配额 ±10% | **否**（默认，见 `SCRIPT_WORD_QUOTA_ENFORCE`） | 是 |
| `warnings` | title / summary 长度、LLM 多余字段 | 否 | 否 |

为什么把总字数单列一档：D4 三轮实测（共 30 次生成、最多 3 轮纠错）显示
**模型每行长度稳定落在 12~21 字符且与提示词无关**，总字数系统性偏低，
把配额当硬性失败判据会让「可用率」恒为 0~70%，且重试只能小幅收敛。
因此配额继续**驱动重试**（尽力补足），但不判可用性；未达标时如实上报
`word_quota_ok=false` 与偏差百分比，由调用方按「时长是否敏感」自行决定处置。

命名约定：本模块的 `ScriptTurn` 是「LLM 返回的一行台词」，
与 `api.services.normalize.ScriptLine`（规范化后的脚本行，含 read_text）不是同一个东西 ——
前者经 `as_turns()` 转成 `[{speaker, text}]` 后交给 `build_script_lines()` 落地为后者。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 标题 / 摘要的期望长度区间。**必须与 backend/prompts/script_system.md 的声明一致**，
# 由 script_gen.verify_prompt_consistency() 在启动期核对。
TITLE_LEN = (12, 24)
SUMMARY_LEN = (30, 60)

# 同一类别的问题最多回报几条（错误清单要回喂给模型纠错，过长反而降低纠错质量）
_MAX_REPORTED = 5

_WS = re.compile(r"\s+")


def count_chars(text: str) -> int:
    """字数口径：**含标点、不含空白字符**。

    该口径同时写在系统提示词与用户消息里（「含标点、不含空格」），三处必须一致 ——
    否则会出现「模型按提示词写够了，校验器却判它超配额」的假失败。
    """
    return len(_WS.sub("", text or ""))


class ScriptTurn(BaseModel):
    """LLM 返回的一行台词。"""

    model_config = ConfigDict(extra="ignore")

    seq: int = Field(ge=1, description="行序号，从 1 连续递增")
    speaker: Literal["A", "B"] = Field(description="说话人，只能是 A 或 B")
    text: str = Field(min_length=1, description="台词原文，不含换行与前缀")

    @field_validator("text")
    @classmethod
    def _clean_text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("台词不能为空")
        if "\n" in v or "\r" in v:
            raise ValueError("台词不得包含换行")
        return v

    @property
    def char_count(self) -> int:
        return count_chars(self.text)


@dataclass
class PolicyReport:
    """业务规则校验结果，见模块 docstring 的约束分级表。"""

    errors: list[str] = field(default_factory=list)
    quota: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """**可用性判据**：只看硬约束。"""
        return not self.errors

    @property
    def needs_retry(self) -> bool:
        """**是否值得再试一轮**：硬约束未过，或字数配额未达（尽力补足）。"""
        return bool(self.errors or self.quota)

    @property
    def all_issues(self) -> list[str]:
        return list(self.errors) + list(self.quota)

    def to_dict(self) -> dict[str, list[str]]:
        return {"errors": list(self.errors), "quota": list(self.quota),
                "warnings": list(self.warnings)}


class PodcastScript(BaseModel):
    """一份完整的双人对话脚本（LLM 结构化输出的顶层对象）。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    summary: str = ""
    lines: list[ScriptTurn] = Field(min_length=1)

    # ---------------- 结构校验 ----------------

    @model_validator(mode="after")
    def _seq_continuous(self) -> "PodcastScript":
        for i, line in enumerate(self.lines, 1):
            if line.seq != i:
                raise ValueError(
                    f"seq 必须从 1 连续递增：第 {i} 行是 {line.seq}，应为 {i}")
        return self

    # ---------------- 派生信息 ----------------

    @property
    def total_chars(self) -> int:
        return sum(line.char_count for line in self.lines)

    @property
    def speakers(self) -> set[str]:
        return {line.speaker for line in self.lines}

    def as_turns(self) -> list[dict[str, Any]]:
        """转成 `normalize.build_script_lines()` 需要的 `[{speaker, text}]`。"""
        return [{"speaker": line.speaker, "text": line.text} for line in self.lines]

    def as_dialogue(self) -> str:
        """渲染成 `A：xxx` 形式的纯文本（合规自检与人工核对用）。"""
        return "\n".join(f"{line.speaker}：{line.text}" for line in self.lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "summary": self.summary,
            "total_chars": self.total_chars,
            "line_count": len(self.lines),
            "lines": [line.model_dump() for line in self.lines],
        }

    # ---------------- 业务规则校验（口径来自 Settings） ----------------

    def check_policy(self, *, max_chars: int, target_words: int | None = None,
                     tolerance: float = 0.10,
                     require_alternating: bool = True,
                     enforce_word_quota: bool = False) -> PolicyReport:
        """按计划书 4.10 ③ 的校验项做业务规则检查。

        与上游提示词的口径一致性由 `script_gen.verify_prompt_consistency()` 在启动期保证，
        本函数只按传入的阈值执行 —— 阈值绝不硬编码，避免「提示词说 40、这里按 50 判」。
        """
        rep = PolicyReport()

        # 1) 单行字符数上限（下游 TTS 单句上限，超了会被切句，破坏「一行=一口气」的节奏设计）
        over = [ln for ln in self.lines if ln.char_count > max_chars]
        for line in over[:_MAX_REPORTED]:
            rep.errors.append(
                f"第 {line.seq} 行 {line.char_count} 个字符，超过单行上限 {max_chars}，"
                f"请拆成多行：「{_ellipsis(line.text)}」")
        if len(over) > _MAX_REPORTED:
            rep.errors.append(f"…另有 {len(over) - _MAX_REPORTED} 行同样超过单行上限 {max_chars}")

        # 2) 双人对话必须两个说话人都出现
        if len(self.speakers) < 2:
            only = next(iter(self.speakers)) if self.speakers else "（无）"
            rep.errors.append(f"全篇只出现说话人 {only}，双人对话必须 A 与 B 都出现")

        # 3) 强制交替
        if require_alternating:
            # 同类问题只报前 N 条 —— 错误清单要回喂给模型做纠错，太长反而降低纠错质量
            same = [(prev, cur) for prev, cur in zip(self.lines, self.lines[1:])
                    if prev.speaker == cur.speaker]
            for prev, cur in same[:_MAX_REPORTED]:
                rep.errors.append(
                    f"第 {cur.seq} 行与第 {prev.seq} 行说话人相同（{cur.speaker}），"
                    f"必须 A/B 交替")
            if len(same) > _MAX_REPORTED:
                rep.errors.append(f"…另有 {len(same) - _MAX_REPORTED} 处连续同人")

        # 4) 总字数配额（默认只入 quota 档：驱动重试但不判可用性，见模块 docstring）
        if target_words:
            lo = int(target_words * (1 - tolerance))
            hi = int(target_words * (1 + tolerance))
            if not (lo <= self.total_chars <= hi):
                msg = (f"总字数 {self.total_chars} 不在配额区间 {lo}~{hi}"
                       f"（目标 {target_words} 字，允许上下浮动 {tolerance:.0%}）")
                (rep.errors if enforce_word_quota else rep.quota).append(msg)

        # 5) 标题 / 摘要长度 —— 只告警，不影响链路
        tlen = count_chars(self.title)
        if not (TITLE_LEN[0] <= tlen <= TITLE_LEN[1]):
            rep.warnings.append(
                f"title 长度 {tlen} 不在建议区间 {TITLE_LEN[0]}~{TITLE_LEN[1]}：{self.title}")
        if self.summary:
            slen = count_chars(self.summary)
            if not (SUMMARY_LEN[0] <= slen <= SUMMARY_LEN[1]):
                rep.warnings.append(
                    f"summary 长度 {slen} 不在建议区间 {SUMMARY_LEN[0]}~{SUMMARY_LEN[1]}")

        return rep

    def extra_fields(self, raw: dict[str, Any]) -> dict[str, Any]:
        """原始 dict 中未被模型采纳的字段（LLM 常自作主张加字段，记录但不拦）。"""
        known = {"title", "summary", "lines"}
        return {k: v for k, v in raw.items() if k not in known}


def _ellipsis(text: str, limit: int = 16) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


# =========================================================================== #
# 以下为 D6 服务化新增：HTTP 接口出入参（计划书 4.7 API 契约表）
#
# 与上面「LLM 脚本输出」那组模型的区别：
#   - `ScriptTurn`（上）= LLM 返回的一行台词，带 seq，用于结构化校验
#   - `ScriptLineIn/Out`（下）= HTTP 接口的一行台词，seq 由服务端按列表顺序派生
# 两者不可混用：接口层不能让客户端指定 seq，否则「行序」这一不变量交给外部输入去保证。
# =========================================================================== #

# ---------------- 鉴权 ----------------

class RegisterIn(BaseModel):
    """注册入参。用户名限 ASCII：它要进 RSS 标题与日志，限定字符集可避免编码歧义。"""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=32,
                          pattern=r"^[A-Za-z0-9_.-]+$",
                          description="3~32 位，仅限字母数字与 _ . -")
    password: str = Field(min_length=6, max_length=128, description="6~128 位")


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    created_at: datetime | None = None


class TokenOut(BaseModel):
    """登录返回。令牌同时写入 HttpOnly Cookie（计划书 4.7.1），此处回给脚本调用方。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="有效期（秒）")
    user: UserOut


# ---------------- 音色 ----------------

class VoiceOut(BaseModel):
    id: str
    name: str
    gender: str = "unknown"
    style: str = "neutral"
    description: str = ""
    prompt_text: str = ""
    wav: str = ""
    fingerprint: str = ""


# ---------------- 任务 ----------------

class TaskCreateIn(BaseModel):
    """创建任务。时长两种写法二选一，`duration_min` 优先。"""

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=200)
    target_duration_sec: int = Field(default=300, ge=30, le=3600,
                                     description="目标时长（秒），默认 300")
    duration_min: float | None = Field(default=None, ge=0.5, le=60.0,
                                       description="目标时长（分钟）；给定时覆盖 target_duration_sec")
    style: str = Field(default="", max_length=100, description="语言风格提示")
    voice_a: str | None = Field(default=None, max_length=64,
                                description="留空取配置 SPEAKER_A_VOICE")
    voice_b: str | None = Field(default=None, max_length=64)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    tone: str = Field(default="", max_length=50, description="情绪指令，留空不用")


class TaskOut(BaseModel):
    id: str
    topic: str
    status: str
    progress: int = Field(ge=0, le=100)
    stage: str = ""
    error_msg: str = ""
    target_duration_sec: int
    target_word_count: int
    style: str = ""
    voice_a: str = ""
    voice_b: str = ""
    speed: float = 1.0
    tone: str = ""
    content_flagged: bool = False
    script_title: str = ""
    script_summary: str = ""
    line_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    # --- D12 可观测性 ---
    #: 队列位置：0 = 正在合成，1 = 下一个，2 = 再下一个……不在队列（终态/等待确认）为 None。
    queue_position: int | None = None
    #: 合成阶段最近一次的句级缓存命中读数（续跑会整体重算，见 models.Task 注释）
    cache_hit_count: int = 0
    cache_seg_count: int = 0
    cache_hit_rate: float | None = None


class TaskPageOut(BaseModel):
    total: int
    page: int
    page_size: int
    pages: int
    items: list[TaskOut]


# ---------------- 脚本 ----------------

class ScriptLineOut(BaseModel):
    seq: int
    speaker: str
    text: str
    read_text: str = ""
    text_hash: str | None = None
    duration_ms: int = 0
    seg_status: str = "PENDING"


class ScriptOut(BaseModel):
    task_id: str
    title: str = ""
    summary: str = ""
    status: str
    line_count: int
    lines: list[ScriptLineOut]


class ScriptLineIn(BaseModel):
    """人工编辑后的一行。`seq` 由服务端按列表顺序派生，故这里不接收该字段。"""

    model_config = ConfigDict(extra="forbid")

    speaker: Literal["A", "B"]
    #: 300 只是防御性硬顶；真正生效的是 SCRIPT_MAX_CHARS_PER_LINE（默认 40），由路由校验
    text: str = Field(min_length=1, max_length=300)


class ScriptSaveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=500)
    lines: list[ScriptLineIn] = Field(min_length=2, description="整份替换；顺序即行序")


# ---------------- 频道与单集 ----------------

class FeedOut(BaseModel):
    title: str = ""
    description: str = ""
    cover_url: str = ""
    category: str = "Technology"
    explicit: bool = False
    user_token: str = ""
    feed_url: str | None = None
    updated_at: datetime | None = None


class FeedUpdateIn(BaseModel):
    """字段全部可选；只更新显式传入的项（`None` = 不改，空串 = 清空）。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    cover_url: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=64)
    explicit: bool | None = None
    reset_token: bool = Field(default=False,
                              description="置 true 重新生成订阅 token，旧 RSS 地址立即失效（计划书 4.7.1）")


class EpisodeOut(BaseModel):
    id: str
    task_id: str
    title: str = ""
    duration_sec: int = 0
    file_size: int = 0
    feed_guid: str = ""
    audio_url: str | None = None
    pub_date: datetime | None = None
