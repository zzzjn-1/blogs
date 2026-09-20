# -*- coding: utf-8 -*-
"""文本规范化 + 切句 + 原文读文映射。

对应开发计划书 4.4「S2 文本规范化」「S3 标点切句 单句 ≤ 40 字」。

设计约束（来自 4.4 可逆性约束）：
    规范化必须保留「原文 → 读文」的显式映射，仅允许可逆或语义等价的替换。
    任何不可逆改写必须打标并告警 —— 这是逐句一致率达到 100% 的前提。

实现方式：把文本表示成 `(字符, 原文下标)` 的配对序列，所有变换都作用在配对序列上，
因此每个读文字符天然带一个指回原文的下标（替换/插入字符继承锚点下标）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

_CN_DIGIT = "零一二三四五六七八九"
_CN_UNIT4 = ("", "十", "百", "千")
_CN_BIG = ("", "万", "亿", "万亿")

# 数字 2 在这些量词前读「两」
_LIANG_MEASURES = (
    "个", "小时", "分钟", "天", "年", "周", "次", "位", "名", "种", "条", "期", "倍",
    "分", "秒", "岁", "台", "本", "部", "页", "步", "款", "家", "座", "只",
    "双", "对", "句", "首", "段", "张", "场", "届", "轮", "组", "批", "成",
    "层", "件", "点", "碗", "杯", "盒", "包", "箱", "套", "支", "根",
)
# 注意：刻意不含「月」——「2月」应读「二月」而非「两月」

_CJK_RANGE = r"\u4e00-\u9fff\u3400-\u4dbf"
_LATIN_RANGE = r"A-Za-z"
# 句末边界（保留在切出的句子里）
_SENT_END = "。！？!?…"
# 次级切分边界
_SENT_COMMA = "，,；;：:、"

# 需要剥离的 Markdown / 装饰标记
_MD_PATTERNS = (
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),          # 图片
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),      # 链接保留文字
    (re.compile(r"\*\*\*(.+?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),
    (re.compile(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", re.S), r"\1"),
    (re.compile(r"___(.+?)___", re.S), r"\1"),
    (re.compile(r"__(.+?)__", re.S), r"\1"),
    (re.compile(r"`{1,3}([^`]*)`{1,3}"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s*", re.M), ""),       # 标题号
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),            # 引用号
    (re.compile(r"^\s{0,3}[-*+]\s+", re.M), ""),        # 无序列表
    (re.compile(r"^\s{0,3}\d+[.)]\s+", re.M), ""),      # 有序列表
)

# 旁白括号：短、且命中旁白关键词，才剥离（保守策略，避免误删正常括号内容）
_ASIDE_PATTERN = re.compile(r"[（(]\s*([^（()）]{1,10}?)\s*[)）]")
_ASIDE_KEYWORDS = ("笑", "叹气", "停顿", "喘", "咳", "掌声", "音乐", "背景", "画外音",
                   "laugh", "sigh", "pause", "cough", "applause", "music", "bgm")

# CosyVoice 认识的行内标签，必须原样保留
_INLINE_TAGS = ("laughter", "breath", "sigh", "confirmation", "quick")
_TAG_SENTINEL = "\ue000"   # 私用区占位符，不匹配任何裁剪规则

_EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\u2190-\u21FF\u2B00-\u2BFF\uFE0F\u200D]+"
)
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\ufeff\u2060]+")
_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")

_NUM_PATTERN = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


class NormalizeError(ValueError):
    """规范化失败（例如全文被清空 / 词典格式错误）。"""


@dataclass
class Edit:
    """一次替换：原文 [start, end) 被替换为 read。"""

    orig_start: int
    orig_end: int
    orig: str
    read: str
    rule: str
    reversible: bool = True

    @property
    def is_insert(self) -> bool:
        return self.orig_start == self.orig_end


@dataclass
class NormResult:
    original: str
    read_text: str
    offsets: list[int | None]
    edits: list[Edit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.read_text

    @property
    def reversible(self) -> bool:
        return all(e.reversible for e in self.edits)

    def to_dict(self) -> dict:
        return {
            "original": self.original,
            "read_text": self.read_text,
            "changed": self.changed,
            "reversible": self.reversible,
            "warnings": self.warnings,
            "edits": [
                {"start": e.orig_start, "end": e.orig_end, "orig": e.orig,
                 "read": e.read, "rule": e.rule, "reversible": e.reversible}
                for e in self.edits
            ],
        }


# --------------------------------------------------------------------------- #
# 数字 → 中文读法
# --------------------------------------------------------------------------- #

def _four_to_cn(group: int) -> str:
    """0 < group < 10000 → 中文，保留内部「零」。"""
    out, zero_pending, unit = "", False, 0
    while group > 0:
        d = group % 10
        if d == 0:
            zero_pending = True
        else:
            if zero_pending and out:
                out = "零" + out
            zero_pending = False
            out = _CN_DIGIT[d] + _CN_UNIT4[unit] + out
        group //= 10
        unit += 1
    return out


def int_to_cn(n: int) -> str:
    """整数转中文读法。

    2026 → 两千零二十六；128 → 一百二十八；10000 → 一万；10001 → 一万零一。
    十位段读「两」（两千、两万）是口语惯例。
    """
    if n == 0:
        return "零"
    neg, n = n < 0, abs(n)
    groups, big = [], 0
    while n > 0:
        groups.append(n % 10000)
        n //= 10000
        big += 1
    parts: list[str] = []
    for gi in range(len(groups) - 1, -1, -1):
        g = groups[gi]
        if g == 0:
            if parts and not parts[-1].endswith("零"):
                parts.append("零")
            continue
        # 非最高位且该组不足四位 → 需补「零」（10001 → 一万零一）
        if parts and g < 1000:
            parts.append("零")
        parts.append(_four_to_cn(g) + _CN_BIG[gi])
    out = "".join(parts).rstrip("零")
    if out.startswith("一十"):                 # 十~十九：说「十五」而非「一十五」
        out = out[1:]
    out = re.sub(r"^二千", "两千", out)
    out = re.sub(r"^二万", "两万", out)
    out = re.sub(r"^二百", "两百", out)
    out = re.sub(r"^二亿", "两亿", out)
    return ("负" + out) if neg else out


def _digits_to_cn(s: str) -> str:
    """逐位读：2026 → 二零二六（年份、编号、小数部分用）。"""
    return "".join(_CN_DIGIT[int(c)] for c in s)


def _read_number(token: str, lookahead: str) -> str:
    """把一段阿拉伯数字读法化。

    token     只含 0-9、逗号与小数点
    lookahead 紧随其后的若干原文（用于判定年份 / 量词「两」）
    """
    token = token.replace(",", "")
    if "." in token:
        head, _, frac = token.partition(".")
        head_cn = int_to_cn(int(head)) if head else "零"
        return f"{head_cn}点{_digits_to_cn(frac)}"
    n = int(token)
    # 四位年份 + 「年」→ 逐位读（2026年 → 二零二六年）
    if lookahead.startswith("年") and len(token) == 4 and 1000 <= n <= 2999:
        return _digits_to_cn(token)
    if n == 2 and any(lookahead.startswith(m) for m in _LIANG_MEASURES):
        return "两"
    return int_to_cn(n)


# --------------------------------------------------------------------------- #
# 核心：配对序列变换
# --------------------------------------------------------------------------- #

class _Doc:
    """字符 + 原文下标的配对文档，所有变换的载体。"""

    __slots__ = ("chars", "offs", "edits", "warnings")

    def __init__(self, text: str) -> None:
        self.chars: list[str] = list(text)
        self.offs: list[int | None] = list(range(len(text)))
        self.edits: list[Edit] = []
        self.warnings: list[str] = []

    @property
    def text(self) -> str:
        return "".join(self.chars)

    def _note(self, start: int, end: int, read: str, rule: str, reversible: bool = True) -> None:
        orig = "".join(self.chars[start:end])
        idx = [o for o in self.offs[start:end] if o is not None]
        self.edits.append(Edit(
            orig_start=min(idx) if idx else -1,
            orig_end=(max(idx) + 1) if idx else -1,
            orig=orig, read=read, rule=rule, reversible=reversible,
        ))
        if not reversible:
            self.warnings.append(f"不可逆改写[{rule}]：「{orig}」→「{read}」")

    def rewrite(self, matches: list[tuple[int, int, str]], rule: str,
                reversible: bool = True) -> None:
        """按「长串优先、从后往前」替换，避免下标漂移。"""
        for start, end, read in sorted(matches, key=lambda m: m[0], reverse=True):
            anchor = self.offs[start]
            self._note(start, end, read, rule, reversible)
            self.chars[start:end] = list(read) if read else []
            self.offs[start:end] = [anchor] * len(read) if read else []

    def regex_sub(self, pattern: re.Pattern, repl: str, rule: str,
                  reversible: bool = True) -> None:
        matches = [(m.start(), m.end(), m.expand(repl)) for m in pattern.finditer(self.text)]
        if matches:
            self.rewrite(matches, rule, reversible)


def _cjk(ch: str) -> bool:
    return bool(ch) and bool(re.match(f"[{_CJK_RANGE}]", ch))


def normalize_text(text: str, *, strip_aside: bool = True,
                   polyphone: dict[str, str] | None = None,
                   keep_inline_tags: bool = True) -> NormResult:
    """文本规范化主入口。返回读文与「原文→读文」映射。"""
    if text is None:
        raise NormalizeError("text 不能为 None")
    original = text
    doc = _Doc(text)

    # 0) 行内标签保护：[laughter] 等必须原样保留（CosyVoice 认识它们）
    if keep_inline_tags:
        prot = []
        for m in re.finditer(r"\[(" + "|".join(_INLINE_TAGS) + r")\]", doc.text, re.I):
            prot.append((m.start(), m.end(), f"{_TAG_SENTINEL}{m.group(0)}{_TAG_SENTINEL}"))
        if prot:
            doc.rewrite(prot, "protect_inline_tag")

    # 1) 控制字符 / 零宽字符 / emoji
    doc.regex_sub(_CTRL, "", "strip_ctrl", reversible=False)
    doc.regex_sub(_ZERO_WIDTH, "", "strip_zero_width", reversible=False)
    doc.regex_sub(_EMOJI_PATTERN, "", "strip_emoji", reversible=False)

    # 2) Markdown 标记
    if doc.text.count("**") % 2 or doc.text.count("__") % 2:
        doc.warnings.append("存在未配对的 Markdown 加粗标记，可能残留 * 或 _")
    for pat, repl in _MD_PATTERNS:
        doc.regex_sub(pat, repl, "strip_markdown", reversible=False)

    # 3) 括号旁白
    if strip_aside:
        hits = [(m.start(), m.end(), "") for m in _ASIDE_PATTERN.finditer(doc.text)
                if any(k in m.group(1).lower() for k in _ASIDE_KEYWORDS)]
        if hits:
            doc.rewrite(hits, "strip_aside", reversible=False)

    # 4) 全角字母数字 → 半角（\uff01-\uff5e 与 ASCII 有 0xFEE0 偏移）
    fw = [(m.start(), m.end(), chr(ord(m.group(0)) - 0xFEE0))
          for m in re.finditer(r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a\uff0e\uff0c]", doc.text)]
    if fw:
        doc.rewrite(fw, "fullwidth_to_halfwidth")

    # 5) 百分号统一为全角（作为「百分之」的触发符），其余半角标点按邻近中日韩字符转全角
    punct = []
    t = doc.text
    for i, ch in enumerate(t):
        if ch in "%％":
            punct.append((i, i + 1, "％"))
            continue
        if ch in ",!?;:":
            prev_cjk = _cjk(t[i - 1]) if i > 0 else False
            next_cjk = _cjk(t[i + 1]) if i + 1 < len(t) else False
            if prev_cjk or next_cjk:
                punct.append((i, i + 1, {",": "，", "!": "！", "?": "？",
                                         ";": "；", ":": "："}[ch]))
    if punct:
        doc.rewrite(punct, "halfwidth_to_fullwidth")

    # 6) 数字读法（含百分比）
    #    注意 lookahead 必须 lstrip：原文「2026 年」「2 小时」中间有空格，
    #    不剥离会让年份规则与「两」量词规则双双失效（2026 年 → 两千零二十六年）。
    nums = []
    t = doc.text
    for m in _NUM_PATTERN.finditer(t):
        end = m.end()
        if end < len(t) and t[end] == "％":
            nums.append((m.start(), end + 1, "百分之" + _read_number(m.group(0), "")))
        else:
            nums.append((m.start(), end,
                         _read_number(m.group(0), t[end:end + 4].lstrip(" \t\u3000"))))
    if nums:
        doc.rewrite(nums, "number_to_cn")

    # 7) 中英之间加空格（RSS订阅 → RSS 订阅）
    t = doc.text
    sp = []
    for i in range(len(t) - 1):
        a, b = t[i], t[i + 1]
        a_lat = bool(re.match(f"[{_LATIN_RANGE}0-9]", a))
        b_lat = bool(re.match(f"[{_LATIN_RANGE}0-9]", b))
        if a_lat and _cjk(b):
            sp.append((i + 1, i + 1, " "))
        elif _cjk(a) and b_lat:
            sp.append((i + 1, i + 1, " "))
    if sp:
        doc.rewrite(sp, "space_between_cjk_latin")

    # 8) 去掉中日韩字符之间的空格（原文 "2026 年" 数字转写后会留下孤立空格）
    t = doc.text
    cjk_sp = [(i, i + 1, "") for i, ch in enumerate(t)
              if ch == " " and i > 0 and i + 1 < len(t) and _cjk(t[i - 1]) and _cjk(t[i + 1])]
    if cjk_sp:
        doc.rewrite(cjk_sp, "strip_cjk_space")

    # 9) 多音字 / 专有名词词典（默认空表；见 backend/dicts/polyphone.json 说明）
    if polyphone:
        hits = []
        for word, read in sorted(polyphone.items(), key=lambda kv: -len(kv[0])):
            for m in re.finditer(re.escape(word), doc.text):
                if m.group(0) != read:
                    hits.append((m.start(), m.end(), read))
        if hits:
            doc.rewrite(hits, "polyphone_dict", reversible=False)

    # 10) 还原行内标签
    if keep_inline_tags:
        rest = [(m.start(), m.end(), m.group(1))
                for m in re.finditer(_TAG_SENTINEL + r"(\[.+?\])" + _TAG_SENTINEL, doc.text)]
        if rest:
            doc.rewrite(rest, "restore_inline_tag")

    # 11) 收尾：空白折叠、首尾标点清理
    doc.regex_sub(re.compile(r"[ \t\u3000]+"), " ", "collapse_space")
    doc.regex_sub(re.compile(r"\s*\n\s*"), "", "strip_newline")
    doc.regex_sub(re.compile(r"^[\s,，、;；:：!！?？·\-—]+"), "", "strip_lead_punct",
                  reversible=False)
    doc.regex_sub(re.compile(r"[\s,，、;；:：]+$"), "", "strip_trail_punct", reversible=False)

    read_text = doc.text.strip()
    if read_text and read_text != doc.text:
        shift = doc.text.index(read_text)
        doc.offs = doc.offs[shift: shift + len(read_text)]
        doc.chars = list(read_text)
    if not read_text and original.strip():
        doc.warnings.append("规范化后文本为空，请检查原文是否只含标记/emoji")
        doc.offs = []
    if not all(e.reversible for e in doc.edits):
        doc.warnings.append("本次规范化包含不可逆改写，逐句一致率核对时需按映射比对")

    return NormResult(original=original, read_text=read_text,
                      offsets=doc.offs, edits=doc.edits, warnings=doc.warnings)


# --------------------------------------------------------------------------- #
# 切句
# --------------------------------------------------------------------------- #

def split_sentences(text: str, max_chars: int = 40) -> list[str]:
    """标点切句 + 逗号二次切分，保证每段 ≤ max_chars。

    单句上限 40 字是净可用 4.32 GB 显存下的安全档（计划书 4.4）。
    """
    if max_chars < 1:
        raise ValueError("max_chars 必须 ≥ 1")
    text = (text or "").strip()
    if not text:
        return []

    chunks: list[str] = []
    buf = ""
    for ch in text:
        buf += ch
        if ch in _SENT_END:
            chunks.append(buf.strip())
            buf = ""
    if buf.strip():
        chunks.append(buf.strip())

    out: list[str] = []
    for c in chunks:
        out.extend(_split_soft(c, max_chars) if len(c) > max_chars else [c])
    return [s for s in out if s]


def _split_soft(chunk: str, max_chars: int) -> list[str]:
    """按逗号/顿号切分；仍超长则硬切（优先在标点处断开）。"""
    parts: list[str] = []
    buf = ""
    for ch in chunk:
        buf += ch
        if ch in _SENT_COMMA and len(buf) >= max_chars // 2:
            parts.append(buf)
            buf = ""
    if buf:
        parts.append(buf)

    out: list[str] = []
    for p in parts:
        p = p.strip()
        while len(p) > max_chars:
            cut = max_chars
            for k in range(max_chars, max_chars // 2, -1):
                if p[k - 1] in _SENT_COMMA + _SENT_END:
                    cut = k
                    break
            out.append(p[:cut].strip())
            p = p[cut:]
        if p.strip():
            out.append(p.strip())
    return out


# --------------------------------------------------------------------------- #
# 脚本行 → 合成分段
# --------------------------------------------------------------------------- #

@dataclass
class Segment:
    """一次推理单元：一行脚本切出的一个句子。"""

    seq: int
    line_seq: int
    speaker: str
    text: str
    read_text: str
    char_count: int


@dataclass
class ScriptLine:
    seq: int
    speaker: str
    text: str
    read_text: str
    warnings: list[str] = field(default_factory=list)


def build_script_lines(turns: list[dict], *, max_chars: int = 40,
                       polyphone: dict[str, str] | None = None) -> list[ScriptLine]:
    """把脚本轮次 [{speaker, text}] 规范化成行。

    text 保留原文，read_text 是送 TTS 的读文（对应 4.6 script_lines 的两个字段）。
    """
    lines: list[ScriptLine] = []
    for i, t in enumerate(turns):
        speaker = (str(t.get("speaker", "A")).strip().upper()[:1]) or "A"
        res = normalize_text(str(t.get("text", "")), polyphone=polyphone)
        lines.append(ScriptLine(seq=i + 1, speaker=speaker,
                                text=res.original, read_text=res.read_text,
                                warnings=list(res.warnings)))
    return lines


def build_segments(lines: list[ScriptLine], *, max_chars: int = 40) -> list[Segment]:
    """把规范化后的行切成推理分段（单句 ≤ max_chars）。"""
    segs: list[Segment] = []
    n = 0
    for line in lines:
        for s in split_sentences(line.read_text, max_chars=max_chars):
            n += 1
            segs.append(Segment(seq=n, line_seq=line.seq, speaker=line.speaker,
                                text=line.text, read_text=s, char_count=len(s)))
    return segs


def load_polyphone(path: str) -> dict[str, str]:
    """读取多音字/专有名词词典。以 _ 开头的键是说明项，运行时忽略。"""
    import json
    import os
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise NormalizeError(f"词典格式错误，应为 JSON 对象：{path}")
    return {str(k): str(v) for k, v in data.items()
            if k and v and not str(k).startswith("_")}
