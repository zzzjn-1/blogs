# -*- coding: utf-8 -*-
"""D14 演示数据集的**跨产物核验**（演示/答辩前的证据化自检）。

## 这份脚本要解决的问题

演示之前必须回答一个问题：**「我准备的这几期，真的完整、能播、且正确地对外发布了吗？」**
本项目已经用血换来过教训——`[FIX-FEED-LATEST-01]` 活了 4 个阶段，只因为原来的用例写的是
`assert path.exists()`：**「文件存在」不等于「内容正确」**。

因此这里的每一条判据都遵守两条纪律：

1. **期望值不得与被测产物同源**。字节数要跟**磁盘**比、时长要跟 **ffprobe 实测**比，
   不允许拿 `episodes.file_size` 去比由它生成的 XML `length`（那是「拿产物跟自己比」，
   D11/D12/D13 已各栽过一次）。
2. **每条判据都要能被打红**。`--self-test` 注入 12 类缺陷，每一类都必须让对应检查码变红；
   注入不了红的判据=摆设，等于没检查。**变异必须打在演示集条目上**——打在其他期上，
   判据看不见，会得到「假绿」的假自证。

## 检查码

| 码 | 内容 |
| --- | --- |
| `E-COUNT` | 演示集条目数 < 3（计划书要求 ≥ 3 期成片） |
| `E-ACCOUNT` | 条目属主与 manifest 声明的演示账号不一致 |
| `E-TASK-MISSING` | task 不在库中 |
| `E-STATUS` | task.status ≠ DONE |
| `E-TOPIC-GARBLED` | topic 含 `?` 占位或整串 ASCII（中文经 shell 失真的现场特征） |
| `E-TITLE-MISSING` | `script_title` 为空 |
| `E-EPISODE-MISSING` | 没有 episodes 行（未发布） |
| `E-FILE-MISSING` | 成片 mp3 不在磁盘上 |
| `E-SIZE-3WAY` | `episodes.file_size` ≠ 磁盘字节数 |
| `E-PROBE` | ffprobe 读不出音频流（文件损坏 / 不是音频） |
| `E-DURATION` | ffprobe 实测时长与 `episodes.duration_sec` 偏差超容差 |
| `E-DUR-COVER` | **成片实测时长 < 脚本各行时长之和**（成片被截断，只合成了一部分） |
| `E-SCRIPT-EMPTY` | 没有任何 script_lines |
| `E-SEG-NOT-DONE` | 存在 `seg_status` ≠ DONE 的行 |
| `E-SPEAKER` | 说话人不是 A/B |
| `E-CACHE-ROW` | DONE 行的 `text_hash` 在 `audio_cache` 中不存在 |
| `E-CACHE-PATH` | `audio_cache.wav_path` 不符合 `<cache>/<hash[:2]>/<hash>.wav` 寻址规则 |
| `E-CACHE-WAV` | 缓存 wav **不在磁盘上**或为空（跨表↔磁盘） |
| `E-FEED-MISSING` | 该期 guid 不在属主的 `feed.xml` 中（未对外发布） |
| `E-ENC-LEN` | feed `enclosure@length` ≠ 磁盘字节数（三方核对第三边） |
| `E-FEED-DUP` | 同一 guid 在同一份 feed 里出现多次 |
| `E-INTRO-TONE` | **片头合成调用点不再传空 `tone`**（AST 静态检查，防 instruct2 回显回归） |
| `E-INTRO-PRE-FIX` | 演示条目生成于片头修复生效时间之前（成片内嵌的片头可能带参考音频回显） |

## 片头为什么要单独两条判据

`inference_instruct2` 在 `zero_shot_spk_id` 非空时会把 `prompt_wav`（`voice_a.wav`，内容为
「生活就像海洋…」）**当声学前缀回显到输出开头**。2026-09-17 起片头/片尾一律走
`inference_zero_shot`（`tone=""`）——**修法是「换通道」，不是「换参数」**，
因此它极易被后来的重构改回去，而症状只在听到成片开头时才暴露。

两条判据各守一半，缺一不可：

- `E-INTRO-TONE` 守**将来**：AST 解析 `task_runner._build_dynamic_intro` 与
  `make_intro_outro` 里的 `engine.synthesize(...)`，要求 `tone` 必须是字面量空串。
  用 AST 而不是正则，是因为 `task_runner` 里还有一处**正当**的 `tone=tone`（正文分段），
  按文本搜索会把那一处也算进来，导致判据自相矛盾。
- `E-INTRO-PRE-FIX` 守**眼前**：演示集只收修复生效之后生成的成片。
  时间下界由 manifest 的 `intro_fix_cutoff` 声明，并**必须写明取证依据**——
  本机无版本库，代码修复时刻无法从提交历史取证，故取「本轮演示数据生成日」这一
  **保守可得**的下界；取不到证的旧成片一律不进演示集。

## 为什么不「重算 text_hash」

`text_hash = sha1(voice.id|fingerprint|model_version|seed|ratio|read_text|speed|tone)`，
而 `read_text` 是 `build_script_lines()` 归一化后的产物。在核验脚本里**再实现一遍**这个
公式，等于自己造一条「影子判据」：公式一旦漂移，核验红了但生产是对的（或反之），
而两者都由我自己维护，谁对谁错无从判定。故这里退一步做**可判定**的跨产物核对：
hash → `audio_cache` 行 → 磁盘上那个 wav 文件，三边必须同时存在且寻址自洽。

用法::

    python scripts/verify_demo_pack.py                    # 核对 manifest 里的演示集
    python scripts/verify_demo_pack.py --json outputs/x.json
    python scripts/verify_demo_pack.py --self-test        # 注入 12 类缺陷，必须全部变红
    python scripts/verify_demo_pack.py --auto             # 不用 manifest，按规则现选（复现用）
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "data" / "demo_pack.json"

# 时长比对容差（秒）。ffprobe 与落库值的来源不同（前者逐帧、后者按采样数），
# 且 intro/outro 的淡入淡出会带来零点几秒差；1.5s 足够严，又不会因噪声误红。
DUR_TOL = 1.5
# 成片必须不比「各行时长之和」短这么多（片头/片尾已计入，行时长是净语音）。
DUR_COVER_SLACK = -2.0

# 片头合成**必须**传空 tone（`instruct2` 会把参考音频回显到输出开头）。
# 用 AST 检查调用点而非全文搜索 `tone=""`：`task_runner` 里另有一处**正当**的
# `tone=tone`（正文分段），按文本搜索会把它一并命中，判据立刻自相矛盾。
# 元组 = (相对路径, 限定函数名（None=整文件）, 人类标签)
INTRO_TONE_SITES = (
    ("api/services/task_runner.py", "_build_dynamic_intro", "主链路动态片头"),
    ("scripts/make_intro_outro.py", None, "离线素材脚本"),
)
# 片头修复生效时间的**保守**下界。
#
# ⚠️ 必须带时区偏移：库里的 `tasks.created_at` / `episodes.pub_date` 存的是 **UTC 朴素串**
#    （实测：本地 11:06 生成的一期，落库为 03:06），而人在读文档时说的「9-20」是本地时间。
#    早先版本把下界写成不带偏移的 "2026-09-20T00:00:00" 直接按字符串比大小，
#    结果候选期全部被判「早于下界」——判据恒红，`--self-test` 的 RESTORE 一步当场抓到。
# 本机无版本库，代码修复时刻无法从提交历史取证，故取「本轮演示数据生成日（本地）」。
# manifest 的 `intro_fix_cutoff` 可覆盖，格式为带偏移的 ISO 8601。
DEFAULT_INTRO_FIX_CUTOFF = "2026-09-20T00:00:00+08:00"

_FFPROBE = shutil.which("ffprobe")


def as_utc(stamp: str) -> datetime | None:
    """把「库里的 naive UTC 串」或「带偏移的 ISO 串」都归一到 **UTC aware**。

    解析不了一律返回 None，由调用方决定是报错还是跳过 —— 不做「猜一个时区」这种事。
    """
    s = (stamp or "").strip().replace(" ", "T")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# 独立测量
# --------------------------------------------------------------------------- #

def measure_duration(path: Path) -> tuple[float | None, str]:
    """用 **ffprobe**（独立于本项目与数据库）实测音频时长。

    ffprobe 缺失时退回纯 Python 的 CBR 估算（本项目成片是 128 kbps / 44.1 kHz），
    **并在说明里注明来源**，以免把估算值当实测值引用。
    """
    if _FFPROBE:
        try:
            p = subprocess.run(
                [_FFPROBE, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=60)
            if p.returncode == 0 and p.stdout.strip():
                return float(p.stdout.strip()), "ffprobe"
            return None, "ffprobe-failed:%s" % (p.stderr.strip()[:120] or p.returncode)
        except Exception as exc:  # noqa: BLE001
            return None, "ffprobe-error:%s" % exc
    try:
        raw = path.read_bytes()
        n = len(raw)
        off = 0
        if raw[:3] == b"ID3":
            off = 10 + (raw[6] << 21 | raw[7] << 14 | raw[8] << 7 | raw[9])
        BR = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
        i = off
        while i < min(len(raw) - 4, off + 65536):
            if raw[i] == 0xFF and (raw[i + 1] & 0xE0) == 0xE0:
                br_idx = (raw[i + 2] >> 4) & 0x0F
                if br_idx < len(BR) and BR[br_idx]:
                    return (n - off) * 8 / (BR[br_idx] * 1000), "cbr-estimate(no ffprobe)"
                break
            i += 1
    except Exception:  # noqa: BLE001
        pass
    return None, "unmeasurable"


# --------------------------------------------------------------------------- #
# 上下文装载
# --------------------------------------------------------------------------- #

def load_ctx(db_path: Path, feed_dir: Path) -> dict:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        users = {r[0]: r[1] for r in con.execute("SELECT username,id FROM users")}
        tasks = {r["id"]: dict(r) for r in con.execute(
            "SELECT id,user_id,topic,status,script_title,script_summary,"
            "voice_a,voice_b,speed,tone,created_at FROM tasks")}
        episodes = {r["task_id"]: dict(r) for r in con.execute("SELECT * FROM episodes")}
        lines: dict[str, list[dict]] = {}
        for r in con.execute("SELECT task_id,seq,speaker,text,read_text,text_hash,"
                             "duration_ms,seg_status FROM script_lines ORDER BY task_id,seq"):
            lines.setdefault(r["task_id"], []).append(dict(r))
        cache = {r["text_hash"]: dict(r) for r in con.execute(
            "SELECT text_hash,speaker,wav_path,duration_ms FROM audio_cache")}
        feeds = {r["user_token"]: dict(r) for r in con.execute(
            "SELECT user_id,user_token FROM feeds")}
    finally:
        con.close()

    feed_items: dict[str, dict[str, tuple[int, int]]] = {}
    for token in feeds:
        f = feed_dir / f"{token}.xml"
        items: dict[str, tuple[int, int]] = {}
        if f.is_file():
            try:
                root = ET.fromstring(f.read_text(encoding="utf-8"))
                for it in root.iter("item"):
                    g = (it.findtext("guid") or "").strip()
                    enc = it.find("enclosure")
                    ln = int(enc.get("length") or 0) if enc is not None else 0
                    prev = items.get(g, (ln, 0))
                    items[g] = (ln, prev[1] + 1)
            except ET.ParseError:
                pass
        feed_items[token] = items

    tok_by_user = {str(v["user_id"]): k for k, v in feeds.items()}
    return {"users": users, "tasks": tasks, "episodes": episodes, "lines": lines,
            "cache": cache, "feeds": feeds, "feed_items": feed_items,
            "tok_by_user": tok_by_user}


def resolve_token(ctx: dict, entry: dict) -> str:
    t = ctx["tasks"].get(entry["task_id"])
    return ctx["tok_by_user"].get(str(t["user_id"]) if t else "", "")


# --------------------------------------------------------------------------- #
# 片头通道与时间线
# --------------------------------------------------------------------------- #

def load_intro_sources() -> dict[str, str]:
    """把片头合成相关源码读进内存。

    读成**文本**而不是让判据自己去 open 文件，是为了让 `--self-test` 能只改
    内存副本就打到这条判据 —— 自证过程绝不去改仓库里的真实源码。
    """
    out: dict[str, str] = {}
    for rel, _fn, _label in INTRO_TONE_SITES:
        try:
            out[rel] = (ROOT / rel).read_text(encoding="utf-8")
        except OSError:
            pass
    return out


def check_intro_tone(sources: dict[str, str]) -> list[str]:
    """AST 静态判据：片头合成调用点的 `tone` 必须是字面量空串。

    这是**面向将来**的判据。片头回显缺陷的修法是「换通道」（instruct2 → zero_shot），
    不是改参数，因此后来任何一次「顺手把 tone 接回配置」的重构都会让它复活，
    而症状要等到有人听成片开头才会被发现。
    """
    import ast

    problems: list[str] = []
    for rel, func_name, label in INTRO_TONE_SITES:
        src = sources.get(rel)
        if src is None:
            problems.append(f"E-INTRO-TONE: 读不到源码 {rel}（{label}），无法确认片头通道")
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            problems.append(f"E-INTRO-TONE: {rel} 解析失败（{exc}）")
            continue
        if func_name is None:
            scopes: list = [tree]
        else:
            scopes = [n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == func_name]
            if not scopes:
                problems.append(f"E-INTRO-TONE: {rel} 里找不到函数 {func_name}（{label}）——"
                                "调用点被改名/搬走，判据已失去目标")
                continue
        hits = 0
        for sc in scopes:
            for node in ast.walk(sc):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "synthesize"):
                    continue
                hits += 1
                kws = [k for k in node.keywords if k.arg == "tone"]
                if not kws:
                    problems.append(f"E-INTRO-TONE: {rel}:{node.lineno}（{label}）synthesize "
                                    "未显式传 tone —— 一旦默认值非空即回显参考音频")
                elif not (isinstance(kws[0].value, ast.Constant)
                          and kws[0].value.value == ""):
                    problems.append(f"E-INTRO-TONE: {rel}:{node.lineno}（{label}）tone "
                                    "不是字面量空串 —— instruct2 会把参考音频回显到素材开头")
        if hits == 0:
            problems.append(f"E-INTRO-TONE: {rel}（{label}）内找不到 synthesize 调用，"
                            "判据已失去目标")
    return problems


# --------------------------------------------------------------------------- #
# 单条核验
# --------------------------------------------------------------------------- #

def check_entry(entry: dict, ctx: dict) -> list[str]:
    """核验一期演示成片；返回 problem 列表（每条为 `CODE: 说明`）。"""
    problems: list[str] = []
    tid = entry["task_id"]
    label = entry.get("role") or tid[:8]

    task = ctx["tasks"].get(tid)
    if task is None:
        return [f"E-TASK-MISSING: [{label}] task {tid} 不在 tasks 表中"]

    if task["status"] != "DONE":
        problems.append(f"E-STATUS: [{label}] status={task['status']}，应为 DONE")

    topic = str(task.get("topic") or "")
    if not topic.strip() or "?" in topic or all(ord(c) < 128 for c in topic if c.strip()):
        problems.append(f"E-TOPIC-GARBLED: [{label}] topic={topic!r} 不可读"
                        "（中文参数经 shell 失真的特征）")

    if not str(task.get("script_title") or "").strip():
        problems.append(f"E-TITLE-MISSING: [{label}] script_title 为空")

    # ---- 片头通道的时间线（面向眼前：这批成片本身干净吗） ----
    # 口径：created_at 是 UTC 朴素串，下界是带偏移的 ISO 串，两边都归一到 UTC aware 再比。
    cutoff = as_utc(str(ctx.get("intro_fix_cutoff") or ""))
    created = as_utc(str(task.get("created_at") or ""))
    if created is None:
        problems.append(f"E-INTRO-PRE-FIX: [{label}] task.created_at 不可解析"
                        f"（{task.get('created_at')!r}），无法判定片头通道")
    elif cutoff is not None and created < cutoff:
        problems.append(f"E-INTRO-PRE-FIX: [{label}] 生成于 {created:%Y-%m-%d %H:%M} UTC，"
                        f"早于片头修复下界 {cutoff:%Y-%m-%d %H:%M} UTC —— "
                        "该期片头可能带参考音频回显，不得进演示集")

    ep = ctx["episodes"].get(tid)
    if ep is None:
        problems.append(f"E-EPISODE-MISSING: [{label}] 无 episodes 行（未发布）")
    mp3 = Path(str(ep["mp3_path"])) if ep else None

    # ---- 脚本完整性 + 缓存三方 ----
    lines = ctx["lines"].get(tid) or []
    if not lines:
        problems.append(f"E-SCRIPT-EMPTY: [{label}] 没有任何 script_lines")
    else:
        bad_seg = [ln["seq"] for ln in lines if str(ln["seg_status"] or "") != "DONE"]
        if bad_seg:
            problems.append(f"E-SEG-NOT-DONE: [{label}] {len(bad_seg)} 行未完成，"
                            f"如 seq={bad_seg[:5]}")
        bad_spk = sorted({str(ln["speaker"]) for ln in lines
                          if str(ln["speaker"] or "") not in ("A", "B")})
        if bad_spk:
            problems.append(f"E-SPEAKER: [{label}] 非 A/B 说话人：{bad_spk}")
        for ln in lines:
            if str(ln["seg_status"]) != "DONE":
                continue
            h = str(ln["text_hash"] or "")
            if not h:
                problems.append(f"E-CACHE-ROW: [{label}] seq={ln['seq']} 已 DONE 但 text_hash 为空")
                continue
            crow = ctx["cache"].get(h)
            if crow is None:
                problems.append(f"E-CACHE-ROW: [{label}] seq={ln['seq']} 的 text_hash={h[:12]}… "
                                "不在 audio_cache 中")
                continue
            wp = Path(str(crow["wav_path"] or ""))
            if wp.parent.name != h[:2] or wp.stem != h:
                problems.append(f"E-CACHE-PATH: [{label}] seq={ln['seq']} wav_path={wp} "
                                f"不符合 <cache>/{h[:2]}/{h}.wav 寻址规则")
            if not wp.is_file():
                problems.append(f"E-CACHE-WAV: [{label}] seq={ln['seq']} 缓存 wav 不在磁盘：{wp}")
            elif wp.stat().st_size == 0:
                problems.append(f"E-CACHE-WAV: [{label}] seq={ln['seq']} 缓存 wav 为空文件：{wp}")

    # ---- 成片 ----
    if ep is not None:
        if mp3 is None or not mp3.is_file():
            problems.append(f"E-FILE-MISSING: [{label}] 成片不存在：{mp3}")
        else:
            disk = mp3.stat().st_size
            if int(ep["file_size"] or 0) != disk:
                problems.append(f"E-SIZE-3WAY: [{label}] episodes.file_size={ep['file_size']} "
                                f"≠ 磁盘 {disk}")
            dur, how = measure_duration(mp3)
            if dur is None:
                problems.append(f"E-PROBE: [{label}] 无法测出音频时长（{how}）")
            else:
                db_dur = float(ep["duration_sec"] or 0)
                if abs(dur - db_dur) > DUR_TOL:
                    problems.append(f"E-DURATION: [{label}] ffprobe 实测 {dur:.2f}s "
                                    f"与 duration_sec {db_dur:.0f}s 偏差 > {DUR_TOL}s")
                speech = sum(int(ln["duration_ms"] or 0) for ln in lines) / 1000.0
                if speech and dur < speech + DUR_COVER_SLACK:
                    problems.append(f"E-DUR-COVER: [{label}] 成片 {dur:.2f}s 短于脚本各行之和 "
                                    f"{speech:.2f}s —— 成片被截断")
                entry["_measured"] = {"duration_sec": round(dur, 2), "source": how,
                                      "disk_bytes": disk, "speech_sec": round(speech, 2)}

        # ---- 订阅源三方 ----
        token = entry.get("_token") or resolve_token(ctx, entry)
        items = ctx["feed_items"].get(token, {})
        guid = str(ep["feed_guid"] or "")
        if guid not in items:
            problems.append(f"E-FEED-MISSING: [{label}] guid {guid[:12]}… 不在 feed {token} 中")
        else:
            len_xml, times = items[guid]
            if times > 1:
                problems.append(f"E-FEED-DUP: [{label}] guid 在 feed 里出现 {times} 次")
            if mp3 is not None and mp3.is_file() and len_xml != mp3.stat().st_size:
                problems.append(f"E-ENC-LEN: [{label}] feed enclosure@length={len_xml} "
                                f"≠ 磁盘 {mp3.stat().st_size}")
    return problems


# --------------------------------------------------------------------------- #
# 变异自证
#
# ⚠️ 每个变异体都**必须打在演示集条目上**（`entries[0]`）。
#    早先版本用 `next(iter(ctx["episodes"]))` 取第一条，很可能落在库里的
#    其他 36 期上 —— 判据根本看不见，于是「注入后仍全绿」，把摆设断言伪装成通过。
# --------------------------------------------------------------------------- #

def _lines_of(c: dict, entries: list[dict]) -> list[dict]:
    for e in entries:
        ls = c["lines"].get(e["task_id"])
        if ls:
            return ls
    return []


def _mut_size(c, entries):
    t = entries[0]["task_id"]
    c["episodes"][t]["file_size"] = int(c["episodes"][t]["file_size"] or 0) + 1


def _mut_duration(c, entries):
    t = entries[0]["task_id"]
    c["episodes"][t]["duration_sec"] = float(c["episodes"][t]["duration_sec"] or 0) + 600


def _mut_seg(c, entries):
    for ln in _lines_of(c, entries):
        if str(ln["seg_status"]) == "DONE":
            ln["seg_status"] = "PENDING"
            return


def _mut_cache_row(c, entries):
    for ln in _lines_of(c, entries):
        if ln["text_hash"]:
            ln["text_hash"] = None
            return


def _mut_cache_wav(c, entries):
    for ln in _lines_of(c, entries):
        h = str(ln["text_hash"] or "")
        if h and h in c["cache"]:
            c["cache"][h]["wav_path"] = str(ROOT / "outputs" / "__no_such_cache__" / f"{h}.wav")
            return


def _mut_feed_missing(c, entries):
    e = entries[0]
    tok = e.get("_token") or ""
    items = c["feed_items"].get(tok) or {}
    tid = e["task_id"]
    ep = c["episodes"].get(tid) or {}
    g = str(ep.get("feed_guid") or "")
    if g in items:
        items.pop(g)


def _mut_feed_len(c, entries):
    e = entries[0]
    tok = e.get("_token") or ""
    items = c["feed_items"].get(tok) or {}
    tid = e["task_id"]
    ep = c["episodes"].get(tid) or {}
    g = str(ep.get("feed_guid") or "")
    if g in items:
        ln, n = items[g]
        items[g] = (ln + 1, n)


def _mut_status(c, entries):
    c["tasks"][entries[0]["task_id"]]["status"] = "FAILED"


def _mut_topic(c, entries):
    c["tasks"][entries[0]["task_id"]]["topic"] = "?"


def _mut_dur_cover(c, entries):
    for ln in _lines_of(c, entries):
        ln["duration_ms"] = int(ln["duration_ms"] or 0) * 10


def _mut_intro_tone(c, entries):
    """把素材脚本里的 `tone=` 由空串改成非空 —— 复现 instruct2 回显通道。

    ⚠️ 必须 replace **全部**出现（count=-1）：该文件**文档字符串里也含 `tone=""`**
    （「本脚本素材一律 tone=""」那句说明），只换第一处会打在注释上、代码纹丝不动，
    于是判据照旧全绿 —— 这正是「变异打偏 = 假自证」那一族坑。
    只改**内存副本**，不碰仓库里的真实源码。
    """
    rel = "scripts/make_intro_outro.py"
    src = c["intro_sources"].get(rel, "")
    c["intro_sources"][rel] = src.replace('tone=""', 'tone="用开心的语气说这句话"')


def _mut_intro_cutoff(c, entries):
    """把演示条的生成时间挪到片头修复下界之前（模拟混入修复前的旧成片）。"""
    c["tasks"][entries[0]["task_id"]]["created_at"] = "2026-09-17 03:00:00.000000"


MUTATIONS = (
    ("M1", "episodes.file_size 改成磁盘字节数 +1", "E-SIZE-3WAY", _mut_size),
    ("M2", "duration_sec 加 600s", "E-DURATION", _mut_duration),
    ("M3", "一行 seg_status 改成 PENDING", "E-SEG-NOT-DONE", _mut_seg),
    ("M4", "一行的 text_hash 清空", "E-CACHE-ROW", _mut_cache_row),
    ("M5", "audio_cache.wav_path 指向不存在的文件", "E-CACHE-WAV", _mut_cache_wav),
    ("M6", "从 feed 里删掉演示集的 guid", "E-FEED-MISSING", _mut_feed_missing),
    ("M7", "feed enclosure@length 改成 +1", "E-ENC-LEN", _mut_feed_len),
    ("M8", "task.status 改成 FAILED", "E-STATUS", _mut_status),
    ("M9", "topic 改成 '?'（shell 失真现场）", "E-TOPIC-GARBLED", _mut_topic),
    ("M10", "演示集各行 duration_ms ×10（模拟成片被截断）", "E-DUR-COVER", _mut_dur_cover),
    ("M11", "把素材脚本的 tone 由空串改成非空", "E-INTRO-TONE", _mut_intro_tone),
    ("M12", "演示条生成时间挪到片头修复下界之前", "E-INTRO-PRE-FIX", _mut_intro_cutoff),
)


def run_checks(ctx: dict, entries: list[dict]) -> list[str]:
    """跑**全部**判据（条目级 + 全局级）。

    `--self-test` 与正式核验共用这一个入口。两边各写一套的话，就会出现
    「自证跑的是 A、正式跑的是 B」——判据漂移而自证仍然全绿。

    注意**不传拷贝**：`check_entry` 会把实测值写回 `entry["_measured"]` 供汇总打印，
    传 `dict(e)` 会让这些值落在副本上，症状是汇总表里全是 `?s / 0 B` ——
    判据照样全绿，但**人看到的是空的**。这类「结论对、证据不显示」同样算缺陷。
    """
    problems: list[str] = []
    for e in entries:
        problems += check_entry(e, ctx)
    problems += check_intro_tone(ctx.get("intro_sources") or {})
    return problems


def self_test(ctx: dict, entries: list[dict]) -> int:
    print("[SELF-TEST] 基准样本：%d 期演示成片（%s）"
          % (len(entries), ", ".join(e["task_id"][:8] for e in entries)))
    bad = 0
    for code, desc, want, mutate in MUTATIONS:
        c = copy.deepcopy(ctx)
        mutate(c, entries)
        got = run_checks(c, entries)
        hit = any(p.startswith(want) for p in got)
        print(f"  {code} {desc} -> {'RED (OK)' if hit else 'GREEN (摆设!)'}  期望 {want}")
        if not hit:
            print(f"      实际 problems={got[:6]}", file=sys.stderr)
            bad += 1
    after = run_checks(ctx, entries)
    if after:
        print(f"  [RESTORE] 未注入时本应全绿，实际 {after[:6]}", file=sys.stderr)
        bad += 1
    else:
        print("  [RESTORE] 未注入时全绿 (OK)")
    n = len(MUTATIONS)
    print(f"[SELF-TEST] {'FAIL' if bad else 'PASS'}（{n - bad}/{n} 如期变红）")
    return 1 if bad else 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def pick_auto(ctx: dict, account: str, minimum_sec: int = 200) -> list[dict]:
    """按规则现选演示集：该账号下 DONE、**生成于片头修复下界之后**、成片时长 ≥ minimum_sec，新的在前。

    片头那条约束不能只在 manifest 里手工遵守 —— 否则 `--auto` 会照旧把
    09-17 那批「片头带参考音频回显」的旧成片选进来，两条判据当场打架。
    """
    uid = ctx["users"].get(account)
    cutoff = as_utc(str(ctx.get("intro_fix_cutoff") or ""))
    out = []
    for tid, ep in ctx["episodes"].items():
        t = ctx["tasks"].get(tid)
        if not t or str(t["user_id"]) != str(uid) or t["status"] != "DONE":
            continue
        created = as_utc(str(t.get("created_at") or ""))
        if cutoff is not None and (created is None or created < cutoff):
            continue        # 取不到生成时刻的也排除：宁可少选，不放可能带回显的成片进演示集
        if float(ep["duration_sec"] or 0) >= minimum_sec:
            out.append({"task_id": tid, "role": "auto", "note": "按规则现选"})
    out.sort(key=lambda e: ctx["episodes"][e["task_id"]]["pub_date"], reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D14 演示数据集跨产物核验")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--db", default=str(ROOT / "data" / "podcast.db"))
    ap.add_argument("--feeds", default=str(ROOT / "data" / "podcast"))
    ap.add_argument("--json", default="")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--auto", action="store_true", help="忽略 manifest，按规则现选演示集")
    args = ap.parse_args(argv)

    ctx = load_ctx(Path(args.db), Path(args.feeds))
    mpath = Path(args.manifest)
    manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.is_file() else {}
    account = manifest.get("account") or "local"

    # 这两项必须在 pick_auto **之前**装好：pick_auto 也吃片头下界，
    # 而两条判据都吃源码副本（供 --self-test 打变异用）。
    ctx["intro_fix_cutoff"] = str(manifest.get("intro_fix_cutoff")
                                  or DEFAULT_INTRO_FIX_CUTOFF)
    ctx["intro_sources"] = load_intro_sources()

    if args.auto:
        entries = pick_auto(ctx, account)
    else:
        if not mpath.is_file():
            print(f"未找到 manifest：{mpath}（可用 --auto 按规则现选）", file=sys.stderr)
            return 2
        entries = [dict(e) for e in (manifest.get("entries") or [])]

    for e in entries:                      # 统一解析 feed token，后续各检查共用
        e["_token"] = resolve_token(ctx, e)

    if args.self_test:
        if not entries:
            print("[SELF-TEST] 演示集为空，无法自证", file=sys.stderr)
            return 3
        return self_test(ctx, entries)

    problems: list[str] = []
    uid = ctx["users"].get(account)
    if len(entries) < 3:
        problems.append(f"E-COUNT: 演示集只有 {len(entries)} 期，计划书要求 ≥ 3 期")

    for e in entries:
        t = ctx["tasks"].get(e["task_id"])
        if t is not None and uid is not None and str(t["user_id"]) != str(uid):
            problems.append(f"E-ACCOUNT: [{e['task_id'][:8]}] 属主 user_id={t['user_id']} "
                            f"≠ 演示账号 {account}(id={uid})")
    problems += run_checks(ctx, entries)

    n = len(problems)
    print("=" * 72)
    print(f"演示集：{len(entries)} 期　账号：{account}(id={uid})　"
          f"测量工具：{'ffprobe' if _FFPROBE else 'CBR 估算（无 ffprobe）'}")
    print(f"片头通道：AST 检查 {len(INTRO_TONE_SITES)} 处调用点　"
          f"片头修复下界：{ctx['intro_fix_cutoff']}")
    for e in entries:
        m = e.get("_measured") or {}
        print(f"  · [{str(e.get('role') or ''):<6}] {e['task_id'][:8]}  "
              f"{str(m.get('duration_sec', '?')):>7}s  {m.get('disk_bytes', 0):>9} B  "
              f"语音 {m.get('speech_sec', '?')}s  {e.get('note', '')}")
    print(f"问题 {n} 条")
    for p in problems:
        print("  " + p)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "account": account, "count": len(entries),
            "intro_fix_cutoff": ctx["intro_fix_cutoff"],
            "measured_by": "ffprobe" if _FFPROBE else "cbr-estimate",
            "problems": problems,
            "entries": [{k: v for k, v in e.items() if not k.startswith("_") or k == "_measured"}
                        for e in entries],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"证据落盘：{out}")

    return 1 if n else 0


if __name__ == "__main__":
    raise SystemExit(main())
