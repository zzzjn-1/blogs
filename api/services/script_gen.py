# -*- coding: utf-8 -*-
"""LLM 脚本生成（DeepSeek）+ 结构化校验 + 敏感词过滤。

对应开发计划书 4.10「DeepSeek API 接入规范」与 6.2 节 D4 行。

## 分层与职责边界

    ScriptGenerator.generate() 只负责「产出一份可用脚本」并**如实上报判定结果**：
    不写库、不改任务状态、不抛「合规异常」。命中敏感词或自检判不安全时，
    只置 `flagged=True` 并给出命中明细，由任务层按计划书 R9 执行阻断策略
    （置 tasks.content_flagged=true + 提示用户重新生成）。

## 三重保险（计划书 4.10 ③）

    ① `response_format={"type": "json_object"}` + 系统提示词含 json 字样与 Schema；
    ② Pydantic 结构校验（`api.schemas.PodcastScript`）+ `check_policy()` 业务校验；
    ③ 校验失败 -> 携**具体错误信息**纠错重试（默认 2 次）-> 仍失败则降级为
       「纯文本 + 正则解析」并置 `degraded=True`，提示人工修正（难点 8 兜底）。

## 两条纪律（本项目已踩过同类坑，勿删）

- **模型名纪律（4.10 ①）**：每次响应都读响应体 `model` 字段。网关会静默改写模型名，
  所以「本次到底用了哪个模型」不能以请求参数为准，不一致即 WARNING 并记录实际值。
- **阈值一致性纪律（同 `[PATCH-GUARD-02]`）**：单行字数上限、交替规则同时写在
  系统提示词与 `Settings` 里。`verify_prompt_consistency()` 在构造期核对，不一致即
  fail-fast —— 防止「提示词说 40 字、校验器却按别的阈值判」这类阈值脱节。
"""
from __future__ import annotations

import json
import logging
import os
import re
import string
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

import openai
from pydantic import ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from api.config import Settings, get_settings
from api.schemas import PodcastScript, ScriptTurn

log = logging.getLogger(__name__)

# 提示词文件名（相对 Settings.prompts_dir）
SYSTEM_PROMPT_FILE = "script_system.md"
USER_PROMPT_FILE = "script_user.md"
COMPLIANCE_PROMPT_FILE = "compliance_system.md"

DEFAULT_OUTLINE = "开场引入 → 主体讨论（多轮问答与补充）→ 收束总结"
DEFAULT_STYLE = "轻松自然、信息密度适中"

# 每行的目标字符数 —— 用于把「目标总字数」折算成「大概写多少行」下发给模型。
#
# ⚠️ 这个值只能减少偏差，**不能消除偏差**。D4 三轮实测（10 主题 × 最多 3 轮，共 30 次生成）：
#   ① 用 22 做除数：首轮总字数 10/10 低于配额；
#   ② 改用 25 并显式声明「每行 20~32 字符」：首轮仍 10/10 偏低；
#   ③ 改用 20 并显式声明「每行 18~22 字符」：首轮仍 10/10 偏低，且行数变多、每行更短。
#   三轮共同结论：**模型每行实际长度落在 12~21 字符，且基本不随提示词变化**，
#   总字数 ≈ 行数 × 15~19，与「行数提示」强相关、与「每行字数提示」弱相关。
#   因此提示词只能把偏差从 ~40% 压到 ~15%，无法稳定落进 ±10%。
#   真正的兜底是：字数配额**驱动重试但不判可用性**（见 api/schemas.py 的约束分级表），
#   并把达标率单独作为指标上报（见 scripts/verify_d4.py）。
_AVG_CHARS_PER_LINE = 22.0
# 纠错时回显给模型的上一次输出上限（防止上下文膨胀）
_ECHO_LIMIT = 6000
# 长脚本走 LLM_TIMEOUT_LONG 的字数门槛（计划书 4.10 ⑤）
_LONG_SCRIPT_WORDS = 2000

# 系统提示词里必须出现的两条声明（与 Settings 对账用）
_SYS_LINE_LIMIT_RE = re.compile(r"单行字符数上限\s*=\s*(\d+)")
_SYS_ALTERNATE_MARK = "说话人必须交替出现"


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #

class ScriptGenError(RuntimeError):
    """脚本生成链路基类。消息一律写成**可直接落 tasks.error_msg 的中文**。"""


class LLMError(ScriptGenError):
    """大模型调用失败。"""


class LLMRetryable(LLMError):
    """可重试的传输层错误（超时 / 连接失败 / 5xx）。"""


class LLMRateLimited(LLMRetryable):
    """限流或额度不足（429 / 402）。重试有意义，但耗尽后必须明确提示用户。"""


class LLMAuthError(LLMError):
    """Key 无效或权限不足（401 / 403）。重试无意义。"""


class LLMBadRequest(LLMError):
    """请求不合法（400）。重试无意义，通常是提示词或参数问题。"""


class ScriptSchemaError(ScriptGenError):
    """结构不可用：纠错重试与正则降级都未能产出可用脚本。"""


class PromptInconsistent(ScriptGenError):
    """提示词声明与运行时配置不一致 —— 启动期 fail-fast，不带病运行。"""


# --------------------------------------------------------------------------- #
# 敏感词库
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SensitiveWord:
    word: str
    group: str


@dataclass(frozen=True)
class SensitiveHit:
    word: str
    group: str

    def to_dict(self) -> dict[str, str]:
        return {"word": self.word, "group": self.group}


_WS_RE = re.compile(r"\s+")


def _squash(text: str) -> str:
    """去掉全部空白，使「代 开 发 票」这类插空写法也能被命中。"""
    return _WS_RE.sub("", text or "")


def load_sensitive_words(path: str | Path) -> list[SensitiveWord]:
    """读取敏感词库：`#` 注释、`[组名]` 分组标题、其余为词条。

    词库缺失只告警不报错 —— 过滤是**兜底**而非唯一防线（另有 LLM 合规自检），
    不应该因为一个可选文件缺失就让整个服务起不来。
    """
    p = Path(path)
    if not p.is_file():
        log.warning("[D4] 敏感词库不存在，跳过词库过滤：%s", p)
        return []

    words: list[SensitiveWord] = []
    seen: set[str] = set()
    group = "未分组"
    for raw in p.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("[") and s.endswith("]"):
            group = s[1:-1].strip() or group
            continue
        key = _squash(s).lower()
        if key in seen:
            continue
        seen.add(key)
        words.append(SensitiveWord(word=s, group=group))
    return words


def scan_sensitive(text: str, words: Sequence[SensitiveWord]) -> list[SensitiveHit]:
    """子串包含匹配（大小写不敏感、忽略空白）。"""
    hay = _squash(text).lower()
    if not hay:
        return []
    return [SensitiveHit(word=w.word, group=w.group)
            for w in words if _squash(w.word).lower() in hay]


# --------------------------------------------------------------------------- #
# 提示词装载与一致性自检
# --------------------------------------------------------------------------- #

@dataclass
class Prompts:
    system: str
    user_template: string.Template
    compliance_system: str
    dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {"dir": str(self.dir), "system_bytes": len(self.system.encode("utf-8")),
                "user_template_bytes": len(self.user_template.template.encode("utf-8")),
                "compliance_bytes": len(self.compliance_system.encode("utf-8"))}


def load_prompts(settings: Settings | None = None) -> Prompts:
    s = settings or get_settings()
    d = s.prompts_path

    def _read(name: str, required: bool = True) -> str:
        p = d / name
        if not p.is_file():
            if required:
                raise ScriptGenError(f"提示词模板缺失：{p}（可用 PROMPTS_DIR 指定目录）")
            log.warning("[D4] 可选提示词缺失，已跳过：%s", p)
            return ""
        return p.read_text(encoding="utf-8")

    return Prompts(
        system=_read(SYSTEM_PROMPT_FILE),
        user_template=string.Template(_read(USER_PROMPT_FILE)),
        compliance_system=_read(COMPLIANCE_PROMPT_FILE, required=False),
        dir=d,
    )


def verify_prompt_consistency(settings: Settings | None = None,
                              prompts: Prompts | None = None) -> list[str]:
    """核对「提示词里声明的约束」与「校验器实际使用的阈值」。

    返回问题清单（空 = 一致）。**这是启动期 fail-fast 的依据**，不要降级为日志：
    阈值脱节会让系统「看起来在守规矩、实际没守」，比直接报错危险得多。
    """
    s = settings or get_settings()
    p = prompts or load_prompts(s)
    problems: list[str] = []

    # JSON 模式要求提示词里必须出现 "json" 字样，否则 API 会拒绝 response_format
    if "json" not in p.system.lower():
        problems.append("系统提示词未出现 'json' 字样 —— DeepSeek 的 "
                        "response_format=json_object 要求提示词中包含 json")

    # 单行字数上限
    m = _SYS_LINE_LIMIT_RE.search(p.system)
    if not m:
        problems.append("系统提示词缺少可解析的「单行字符数上限 = N」声明，"
                        "无法与 SCRIPT_MAX_CHARS_PER_LINE 对账")
    elif int(m.group(1)) != s.script_max_chars_per_line:
        problems.append(
            f"单行上限不一致：系统提示词声明 {m.group(1)}，"
            f"而 Settings.script_max_chars_per_line = {s.script_max_chars_per_line}")

    # 交替规则
    declared = _SYS_ALTERNATE_MARK in p.system
    if s.script_require_alternating and not declared:
        problems.append("Settings 要求强制交替（SCRIPT_REQUIRE_ALTERNATING=true），"
                        "但系统提示词未声明「说话人必须交替出现」")
    if declared and not s.script_require_alternating:
        log.warning("[D4] 系统提示词要求交替，但 SCRIPT_REQUIRE_ALTERNATING=false，"
                    "校验器不会拦截 —— 模型被无谓约束")

    # 用户模板占位符
    tpl = p.user_template.template
    for token in ("$topic", "$target_words", "$style"):
        if token not in tpl:
            problems.append(f"用户提示词模板缺少占位符 {token}，该信息将不会下发给模型")

    return problems


def build_user_prompt(prompts: Prompts, settings: Settings, *, topic: str,
                      target_words: int, style: str = "",
                      duration_min: float | None = None, outline: str = "") -> str:
    """渲染用户消息。**所有用户变量只出现在这里**，系统提示词保持逐字节稳定。"""
    tol = settings.script_word_tolerance
    return prompts.user_template.safe_substitute(
        topic=topic,
        duration_min=("%g" % duration_min) if duration_min else "由主题与字数配额决定",
        target_words=target_words,
        tol_pct=int(round(tol * 100)),
        lo=int(target_words * (1 - tol)),
        hi=int(target_words * (1 + tol)),
        style=style or DEFAULT_STYLE,
        outline=outline or DEFAULT_OUTLINE,
        lines_hint=max(6, int(round(target_words / _AVG_CHARS_PER_LINE))),
    )


# --------------------------------------------------------------------------- #
# 输出解析
# --------------------------------------------------------------------------- #

def _strip_fence(text: str) -> str:
    """剥掉模型自作主张加的 Markdown 代码块围栏。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[A-Za-z0-9_+-]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    return t.strip()


def _unwrap(data: dict) -> dict:
    """兼容模型把脚本套在 script/data/result 等单层包装里。"""
    if "lines" in data:
        return data
    for key in ("script", "data", "result", "podcast", "output"):
        inner = data.get(key)
        if isinstance(inner, dict) and "lines" in inner:
            return inner
    for value in data.values():
        if isinstance(value, dict) and "lines" in value:
            return value
    return data


def _format_validation(exc: ValidationError, limit: int = 6) -> str:
    items = []
    for e in exc.errors()[:limit]:
        loc = ".".join(str(x) for x in e.get("loc", ())) or "(根)"
        items.append(f"{loc}：{e.get('msg')}")
    if len(exc.errors()) > limit:
        items.append(f"…还有 {len(exc.errors()) - limit} 处")
    return "；".join(items)


def parse_script(raw: str) -> tuple[PodcastScript, dict]:
    """把 LLM 输出解析成 `PodcastScript`；失败抛 `ScriptSchemaError`（原因可读、可回喂）。"""
    text = _strip_fence(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptSchemaError(
            f"输出不是合法 JSON（{exc.msg}，位置 {exc.lineno}:{exc.colno}）") from exc
    if not isinstance(data, dict):
        raise ScriptSchemaError(f"输出 JSON 顶层应为对象，实际为 {type(data).__name__}")
    data = _unwrap(data)
    try:
        script = PodcastScript.model_validate(data)
    except ValidationError as exc:
        raise ScriptSchemaError("结构校验失败：" + _format_validation(exc)) from exc
    return script, data


_LOOSE_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*|__)?[\"'「]?(?P<spk>A|B)[\"'」]?(?:\*\*|__)?\s*[：:]\s*(?P<text>.+?)\s*$",
    re.IGNORECASE)
_LOOSE_TITLE_RE = re.compile(r"^\s*(?:#+\s*)?(?:标题|主题|title)\s*[：:]\s*(?P<t>.+?)\s*$",
                             re.IGNORECASE)
_LOOSE_HEADING_RE = re.compile(r"^\s*#{1,3}\s+(?P<t>.+?)\s*$")


def parse_loose(raw: str) -> PodcastScript | None:
    """正则降级解析（计划书 4.10 ③ 兜底）：从纯文本里抠出 `A：xxx` / `B：xxx`。

    只在结构重试全部失败后使用。返回的方案**无法保证**字数配额与单行上限，
    因此调用方必须置 `degraded=True` 并提示人工修正。
    """
    turns: list[tuple[str, str]] = []
    title = ""
    for line in (raw or "").splitlines():
        m = _LOOSE_LINE_RE.match(line)
        if m:
            turns.append((m.group("spk").upper(), m.group("text").strip()))
            continue
        if not title:
            tm = _LOOSE_TITLE_RE.match(line) or _LOOSE_HEADING_RE.match(line)
            if tm:
                title = tm.group("t").strip()
    if len(turns) < 2:
        return None
    seq = {"A": 0, "B": 0}
    for spk, _ in turns:
        seq[spk] += 1
    if not seq["A"] or not seq["B"]:
        return None
    try:
        return PodcastScript(
            title=title or "未命名对话",
            summary="",
            lines=[ScriptTurn(seq=i, speaker=spk, text=txt)
                   for i, (spk, txt) in enumerate(turns, 1)],
        )
    except ValidationError:
        return None


def build_correction_message(errors: Sequence[str], *, max_chars: int,
                             target_words: int | None, tolerance: float,
                             actual_words: int | None = None) -> str:
    """纠错指令：把**具体错误 + 差值**回喂给模型。

    实测：只说「总字数不在 468~572 之间」时，模型每轮只补几十字（452 → 459），
    三轮仍不达标；把「你上一次 452 字，还差 16 字，请整份重写并补足」写清楚，
    模型才知道该做多大改动。
    """
    lines = [
        "你上一次的输出不满足约束，请重新输出一份完整的 JSON 对象"
        "（整份重写，不是只改几行；不要解释文字，不要 Markdown 标记）。",
        "需要修正的问题：",
    ]
    lines += [f"{i}) {e}" for i, e in enumerate(errors, 1)]
    lines += [
        "",
        "重申约束：",
        "- seq 从 1 连续递增；speaker 只能是 A 或 B，且必须 A/B 交替；",
        f"- 每行不超过 {max_chars} 个字符（含标点、不含空格）；",
    ]
    if target_words:
        lo = int(target_words * (1 - tolerance))
        hi = int(target_words * (1 + tolerance))
        detail = ""
        if actual_words is not None:
            if actual_words < lo:
                detail = f"（你上一次只有 {actual_words} 字，还差 {lo - actual_words} 字）"
            elif actual_words > hi:
                detail = f"（你上一次有 {actual_words} 字，超出 {actual_words - hi} 字）"
            else:
                detail = f"（你上一次 {actual_words} 字）"
        lines.append(f"- 所有 text 的字符数之和必须落在 {lo} 至 {hi} 字之间{detail}；"
                     f"不足就继续补充讨论轮次，不要提前收尾。")
    lines.append("- 只输出 JSON 对象本身。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 调用层：异常归类 + 指数退避
# --------------------------------------------------------------------------- #

def _brief(exc: Exception, limit: int = 200) -> str:
    return str(exc).replace("\n", " ")[:limit]


def _proxy_hint() -> str:
    """连接类失败时补一句代理提示——本机默认挂着 HTTP(S)_PROXY，代理挂掉会伪装成「无法连接」。

    实测：代理进程返回 502 时，SDK 把它包成 APIConnectionError("Connection error.")，
    只看这句话会误判成「网线断了」而反复重试，实际上退出应用代理或加 NO_PROXY 即可。
    """
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        val = os.environ.get(key)
        if val:
            # 代理 URL 可能带 user:pass，日志与错误信息里一律脱敏
            safe = re.sub(r"//[^/@]*@", "//***@", val)
            return ("（检测到 %s=%s：请确认该代理可用；若本机直连正常，"
                    "可设 NO_PROXY=api.deepseek.com 绕过）" % (key, safe))
    return ""


def _classify(exc: Exception, timeout: float) -> ScriptGenError:
    """把 SDK 异常翻译成**用户可读**的中文错误，并区分「可重试 / 不可重试」。"""
    if isinstance(exc, ScriptGenError):
        return exc
    if isinstance(exc, openai.AuthenticationError):
        return LLMAuthError(
            "DeepSeek 鉴权失败（401）：API Key 无效、已吊销或复制不全。"
            "请在 platform.deepseek.com 重新签发，并更新 .env 的 LLM_API_KEY")
    if isinstance(exc, openai.PermissionDeniedError):
        return LLMAuthError("DeepSeek 拒绝访问（403）：Key 无对应模型权限或账户受限")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited("DeepSeek 限流（429）：请求过于频繁或额度不足，已退避重试")
    if isinstance(exc, openai.APITimeoutError):
        return LLMRetryable(f"DeepSeek 请求超时（>{timeout:.0f} s），已退避重试")
    if isinstance(exc, openai.APIConnectionError):
        return LLMRetryable(f"无法连接 DeepSeek，已退避重试：{_brief(exc)}" + _proxy_hint())
    if isinstance(exc, openai.APIStatusError):
        code = getattr(exc, "status_code", None)
        if code == 402:
            return LLMRateLimited("DeepSeek 返回 402：账户余额不足，请充值后重试")
        if code == 400:
            return LLMBadRequest(f"DeepSeek 返回 400（请求不合法，多为提示词或参数问题）："
                                 f"{_brief(exc)}")
        if isinstance(code, int) and code >= 500:
            return LLMRetryable(f"DeepSeek 服务端错误（{code}），已退避重试")
        return LLMError(f"DeepSeek 返回 {code}：{_brief(exc)}")
    if isinstance(exc, openai.APIError):
        return LLMError(f"DeepSeek 调用失败：{_brief(exc)}")
    return LLMError(f"DeepSeek 调用异常：{type(exc).__name__}: {_brief(exc)}")


def _extract(resp: Any) -> tuple[str, str, dict[str, int]]:
    """从响应体取 (content, 响应体 model 字段, usage)。"""
    returned_model = str(getattr(resp, "model", "") or "")
    usage: dict[str, int] = {}
    usage_obj = getattr(resp, "usage", None)
    if usage_obj is not None:
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage_obj, key, None)
            if isinstance(value, int):
                usage[key] = value

    choices = list(getattr(resp, "choices", None) or [])
    if not choices:
        raise LLMError("DeepSeek 返回体不含 choices，无法取用内容")
    choice = choices[0]
    finish = getattr(choice, "finish_reason", None)
    if finish == "length":
        raise LLMError(
            "DeepSeek 输出被 max_tokens 截断（finish_reason=length）："
            "请提高 LLM_MAX_TOKENS 或降低目标字数后重试")
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if not content or not str(content).strip():
        raise LLMError(f"DeepSeek 返回内容为空（finish_reason={finish}）")
    return str(content), returned_model, usage


def _accumulate(dst: dict[str, int], src: dict[str, int]) -> None:
    for key, value in src.items():
        dst[key] = dst.get(key, 0) + value


# --------------------------------------------------------------------------- #
# 结果对象
# --------------------------------------------------------------------------- #

@dataclass
class ScriptGenResult:
    """一次脚本生成的完整结果（可直接序列化进证据 JSON / 落库）。"""

    script: PodcastScript
    topic: str
    style: str
    target_words: int
    word_range: tuple[int, int]
    model_requested: str
    model_returned: str
    model_mapped: bool
    calls: int                      # 实际发起的 HTTP 请求数（含退避重试与纠错轮）
    correction_rounds: int
    degraded: bool
    flagged: bool
    sensitive_hits: list[SensitiveHit] = field(default_factory=list)
    self_check: dict[str, Any] | None = None
    self_check_error: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def word_quota_ok(self) -> bool:
        """总字数是否落在 ±容差区间内。

        ⚠️ 这是**独立上报的指标，不参与 `usable`**：D4 实测证明模型无法稳定控制总字数
        （每行 12~21 字符，三轮提示词标定均 10/10 偏低），把它当可用性判据会让
        「可用」恒为假。见 api/schemas.py 的约束分级表与 config.script_word_quota_enforce。
        """
        return self.word_range[0] <= self.script.total_chars <= self.word_range[1]

    @property
    def word_deviation(self) -> float:
        """实际字数相对目标字数的**比值**偏差（负值 = 偏短）。例：-0.1135 表示短 11.35%。

        ⚠️ 这是比值，不是百分数。此前这个属性叫 `word_deviation_pct` 却返回比值，
        在 D5 的 CLI 里被直接按 `%+.1f%%` 打印，输出「偏差 -0.1%」（真实为 -11.3%），
        差了 100 倍。需要展示时请用 `word_deviation_pct`。
        """
        if not self.target_words:
            return 0.0
        return round(self.script.total_chars / self.target_words - 1.0, 4)

    @property
    def word_deviation_pct(self) -> float:
        """与目标字数的偏差**百分数**（负值 = 偏短）。例：-11.35 表示短 11.35%。"""
        return round(self.word_deviation * 100.0, 2)

    @property
    def usable(self) -> bool:
        """**可用**：结构完好、未降级、无合规风险、硬约束全过。

        对应 D4 的「一次生成即可用」分子 —— 不含字数配额（见 `word_quota_ok`）。
        """
        return (not self.degraded) and (not self.flagged) and not self.errors

    @property
    def first_pass(self) -> bool:
        """**一次通过**：连内部纠错都没用上（更严的口径，单独统计以便定位退化）。"""
        return self.usable and self.calls == 1 and self.correction_rounds == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "style": self.style,
            "target_words": self.target_words,
            "word_range": list(self.word_range),
            "actual_words": self.script.total_chars,
            "word_quota_ok": self.word_quota_ok,
            "word_deviation": self.word_deviation,          # 比值，如 -0.1135
            "word_deviation_pct": self.word_deviation_pct,   # 百分数，如 -11.35
            "line_count": len(self.script.lines),
            "avg_chars_per_line": (round(self.script.total_chars / len(self.script.lines), 1)
                                   if self.script.lines else 0.0),
            "model_requested": self.model_requested,
            "model_returned": self.model_returned,
            "model_mapped": self.model_mapped,
            "calls": self.calls,
            "correction_rounds": self.correction_rounds,
            "degraded": self.degraded,
            "flagged": self.flagged,
            "usable": self.usable,
            "first_pass": self.first_pass,
            "sensitive_hits": [h.to_dict() for h in self.sensitive_hits],
            "self_check": self.self_check,
            "self_check_error": self.self_check_error,
            "usage": self.usage,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "elapsed_s": round(self.elapsed_s, 2),
        }


# --------------------------------------------------------------------------- #
# 生成器
# --------------------------------------------------------------------------- #

class ScriptGenerator:
    """DeepSeek 脚本生成器。

    依赖注入点：`client`（假客户端供离线单测）、`prompts`、`sensitive_words`。
    """

    def __init__(self, settings: Settings | None = None, *, client: Any = None,
                 prompts: Prompts | None = None,
                 sensitive_words: Sequence[SensitiveWord] | None = None) -> None:
        self.settings = settings or get_settings()
        self.prompts = prompts if prompts is not None else load_prompts(self.settings)
        problems = verify_prompt_consistency(self.settings, self.prompts)
        if problems:
            raise PromptInconsistent(
                "提示词与配置不一致，已拒绝启动（详见 script_gen.verify_prompt_consistency）：\n"
                + "\n".join("  - " + p for p in problems))
        self.sensitive_words = (list(sensitive_words)
                                if sensitive_words is not None
                                else load_sensitive_words(self.settings.sensitive_dict_path))
        self._client = client
        self._mapping_warned: set[tuple[str, str]] = set()

    # ---------------- 客户端 ----------------

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self.settings.llm_api_key:
                raise LLMAuthError("未配置 LLM_API_KEY（.env），无法调用 DeepSeek API")
            self._client = openai.OpenAI(
                api_key=self.settings.llm_api_key,
                base_url=self.settings.llm_base_url,
                timeout=self.settings.llm_timeout,
                max_retries=0,   # 重试由 tenacity 控制，避免双层重试放大（4.10 ②）
            )
        return self._client

    # ---------------- 单次调用（含退避） ----------------

    def _request_kwargs(self, messages: list[dict[str, str]], model: str,
                        *, temperature: float | None = None,
                        max_tokens: int | None = None) -> dict[str, Any]:
        s = self.settings
        return {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": s.llm_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or s.llm_max_tokens,
        }

    def _invoke(self, kwargs: dict[str, Any], timeout: float) -> tuple[Any, int]:
        """发一次请求（含 tenacity 指数退避）。返回 (响应, 实际请求次数)。"""
        s = self.settings
        counter = {"n": 0}

        def _once() -> Any:
            counter["n"] += 1
            try:
                return self.client.with_options(timeout=timeout).chat.completions.create(**kwargs)
            except Exception as exc:                      # noqa: BLE001 —— 统一归类后再抛
                raise _classify(exc, timeout) from exc

        def _before_sleep(state: Any) -> None:
            log.warning("[D4] 第 %d 次调用失败，%.1fs 后重试：%s",
                        counter["n"], state.next_action.sleep,
                        _brief(state.outcome.exception() if state.outcome else Exception("?")))

        retryer = Retrying(
            reraise=True,
            stop=stop_after_attempt(max(1, s.llm_max_retry) + 1),   # 1 次首发 + N 次重试
            wait=wait_exponential(multiplier=1, min=1, max=4),      # 1s -> 2s -> 4s
            retry=retry_if_exception_type(LLMRetryable),
            before_sleep=_before_sleep,
        )
        return retryer(_once), counter["n"]

    def _note_model_mapping(self, requested: str, returned: str) -> bool:
        """模型名纪律：网关静默映射时告警一次，并报告实际落到的模型。"""
        if not returned or returned == requested:
            return False
        key = (requested, returned)
        if key not in self._mapping_warned:
            self._mapping_warned.add(key)
            log.warning("[D4] 网关静默映射模型名：请求 %s → 实际 %s。"
                        "凡涉及「本次用了哪个模型」的结论，一律以响应体 model 字段为准"
                        "（计划书 4.10 ①）", requested, returned)
        return True

    # ---------------- 合规自检 ----------------

    def _compliance_check(self, script: PodcastScript, *, timeout: float,
                          model: str) -> tuple[dict[str, Any] | None, str | None, dict[str, int]]:
        if not self.prompts.compliance_system:
            return None, "未提供 compliance_system.md，已跳过 LLM 合规自检", {}
        payload = (f"请审查以下播客脚本。\n标题：{script.title}\n摘要：{script.summary}\n\n"
                   f"正文：\n{script.as_dialogue()}")
        kwargs = self._request_kwargs(
            [{"role": "system", "content": self.prompts.compliance_system},
             {"role": "user", "content": payload}],
            model, temperature=0.0, max_tokens=256)
        try:
            resp, _ = self._invoke(kwargs, timeout)
        except ScriptGenError as exc:
            log.warning("[D4] 合规自检调用失败（不影响主流程）：%s", exc)
            return None, str(exc), {}

        try:
            raw, returned, usage = _extract(resp)
        except ScriptGenError as exc:
            return None, str(exc), {}
        self._note_model_mapping(model, returned)

        try:
            data = json.loads(_strip_fence(raw))
            if not isinstance(data, dict):
                raise ValueError("顶层不是对象")
            raw_safe = data.get("safe")
            if isinstance(raw_safe, str):
                safe = raw_safe.strip().lower() in ("true", "1", "yes", "y", "是")
            else:
                safe = bool(raw_safe)
            risk = str(data.get("risk") or ("none" if safe else "unknown")).strip().lower()
            reason = str(data.get("reason") or "").strip()
        except Exception as exc:                          # noqa: BLE001
            return None, f"合规自检返回不可解析：{_brief(exc)}", usage
        return {"safe": safe, "risk": risk, "reason": reason}, None, usage

    # ---------------- 主入口 ----------------

    def generate(self, *, topic: str, target_words: int | None = None,
                 duration_min: float | None = None, style: str = "",
                 outline: str = "", use_reasoner: bool = False) -> ScriptGenResult:
        """生成一份双人对话脚本。

        `target_words` 与 `duration_min` 二选一（都缺则按 5 分钟折算）；
        给出 `duration_min` 时按 `WORDS_PER_MINUTE` 反推字数（计划书 4.6 tasks.target_word_count）。
        """
        s = self.settings
        topic = (topic or "").strip()
        if not topic:
            raise ScriptGenError("主题不能为空")

        if target_words is None:
            minutes = duration_min if duration_min else 5.0
            target_words = max(60, int(round(minutes * s.words_per_minute)))
        tol = s.script_word_tolerance
        word_range = (int(target_words * (1 - tol)), int(target_words * (1 + tol)))

        model = s.llm_reasoner_model if use_reasoner else s.llm_model
        timeout = float(s.llm_timeout_long if target_words >= _LONG_SCRIPT_WORDS
                        else s.llm_timeout)

        user_prompt = build_user_prompt(
            self.prompts, s, topic=topic, target_words=target_words,
            style=style, duration_min=duration_min, outline=outline)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.prompts.system},   # 稳定前缀，勿插入变量
            {"role": "user", "content": user_prompt},
        ]

        t0 = time.perf_counter()
        calls = 0
        correction_rounds = 0
        usage_total: dict[str, int] = {}
        script: PodcastScript | None = None
        # 留一份「结构可用、仅业务规则未过」的最佳候选：丢弃它去做正则降级是**倒退**。
        # 取「错误条数最少、同条数时总字数最接近配额」的那一轮 ——
        # 不能简单取最后一轮：纠错有时会修好字数却引入新的超长行，一进一退反而更差。
        best_script: PodcastScript | None = None
        best_errors: list[str] = []
        best_quota: list[str] = []
        best_rank: tuple[int, int, int] | None = None
        last_words: int | None = None
        degraded = False
        returned_model = ""
        model_mapped = False
        last_raw = ""
        errors: list[str] = []
        warnings: list[str] = []

        for round_idx in range(max(0, s.script_correction_retry) + 1):
            resp, n = self._invoke(
                self._request_kwargs(messages, model), timeout)
            calls += n
            raw, returned_model, usage = _extract(resp)
            _accumulate(usage_total, usage)
            model_mapped = self._note_model_mapping(model, returned_model) or model_mapped
            last_raw = raw

            try:
                candidate, data = parse_script(raw)
                report = candidate.check_policy(
                    max_chars=s.script_max_chars_per_line,
                    target_words=target_words,
                    tolerance=tol,
                    require_alternating=s.script_require_alternating,
                    enforce_word_quota=s.script_word_quota_enforce)
                warnings = list(report.warnings)
                extra = candidate.extra_fields(data)
                if extra:
                    warnings.append("LLM 输出了多余字段（已忽略）：" + ", ".join(sorted(extra)))
                rank = (len(report.errors), len(report.quota),
                        abs(candidate.total_chars - target_words))
                if best_rank is None or rank < best_rank:
                    best_script, best_errors, best_quota, best_rank = (
                        candidate, list(report.errors), list(report.quota), rank)
                last_words = candidate.total_chars
                if report.ok and not report.quota:
                    script, errors = candidate, []
                    break
                errors = list(report.all_issues)
            except ScriptSchemaError as exc:
                errors = [str(exc)]
                last_words = None

            log.warning("[D4] 第 %d 轮输出未通过校验（共 %d 轮）：%s",
                        round_idx + 1, s.script_correction_retry + 1, "；".join(errors[:4]))

            if round_idx >= s.script_correction_retry:
                break
            correction_rounds += 1
            messages.append({"role": "assistant", "content": last_raw[:_ECHO_LIMIT]})
            messages.append({"role": "user", "content": build_correction_message(
                errors, max_chars=s.script_max_chars_per_line,
                target_words=target_words, tolerance=tol, actual_words=last_words)})

        if script is None:
            if best_script is not None:
                # 结构完好，只是业务规则没全过（典型：总字数偏离配额、个别行超长）。
                # 原样交付并如实记录 —— 这比把一份能用的脚本扔进正则降级更合理，也不会丢信息。
                script, errors = best_script, best_errors
                warnings += best_quota
                log.warning("[D4] 纠错 %d 轮后仍有未通过的约束：%s",
                            correction_rounds, "；".join((best_errors + best_quota)[:4]))
            else:
                script = parse_loose(last_raw)
                if script is None:
                    raise ScriptSchemaError(
                        "脚本结构化输出不可用：纠错 %d 轮后仍不满足约束（%s），"
                        "正则降级也未能提取出对话行。原始输出前 200 字：%s"
                        % (correction_rounds, "；".join(errors[:3]) or "无具体错误",
                           last_raw[:200]))
                degraded = True
                warnings.append("已降级为正则解析结果：字数配额与单行上限不保证，需人工修正")
                log.error("[D4] 结构重试全部失败，已降级为正则解析（%d 行）", len(script.lines))

        # 合规：词库过滤（确定性） + LLM 自检（语义）
        haystack = "\n".join([script.title, script.summary, script.as_dialogue()])
        hits = scan_sensitive(haystack, self.sensitive_words)
        self_check: dict[str, Any] | None = None
        self_check_error: str | None = None
        if hits:
            # 词库已判风险，省掉一次自检调用（阻断结论不变）
            warnings.append("敏感词命中 %d 处：%s（按计划书 R9 交任务层阻断）"
                            % (len(hits), "、".join(h.word for h in hits[:6])))
        elif s.script_self_check:
            self_check, self_check_error, usage = self._compliance_check(
                script, timeout=timeout, model=model)
            _accumulate(usage_total, usage)
            if self_check and not self_check["safe"]:
                warnings.append("LLM 合规自检判为有风险（%s）：%s"
                                % (self_check.get("risk"), self_check.get("reason")))

        flagged = bool(hits) or bool(self_check and not self_check["safe"])
        elapsed = time.perf_counter() - t0

        result = ScriptGenResult(
            script=script, topic=topic, style=style or DEFAULT_STYLE,
            target_words=target_words, word_range=word_range,
            model_requested=model, model_returned=returned_model or model,
            model_mapped=model_mapped, calls=calls,
            correction_rounds=correction_rounds, degraded=degraded, flagged=flagged,
            sensitive_hits=hits, self_check=self_check,
            self_check_error=self_check_error, usage=usage_total,
            errors=errors, warnings=warnings, elapsed_s=elapsed)

        log.info("[D4] 脚本生成完成：主题=%s | %d 行 / %d 字（配额 %d）| 模型 %s→%s | "
                 "请求 %d 次 / 纠错 %d 轮 | 降级=%s 命中=%s | %.1fs | tokens=%s",
                 topic, len(script.lines), script.total_chars, target_words,
                 model, returned_model or model, calls, correction_rounds,
                 degraded, flagged, elapsed, usage_total or "-")
        return result


@lru_cache(maxsize=1)
def get_generator() -> ScriptGenerator:
    """进程内单例（与 tts 的 `TTSEngine.instance()` 同一取向）。"""
    return ScriptGenerator()
