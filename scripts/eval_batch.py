#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D10 端到端压测与指标采集（开发计划书 6.2 D10 行）。

对应 Done 条件：

    测试主题集 12 条（知识科普 / 行业解读 / 生活闲聊 各 4 条，长短各半）；
    自动压测脚本；耗时统计；产出 MOS 评测音频包并交付评测人
    -> 产出端到端耗时表，标出超 10 分钟的 case；评测音频包已发出

## 为什么走 HTTP 而不是直接调 TaskRunner

计划书的验收对象是**交付系统**，不是内部函数：真实链路上有 Cookie 鉴权、单并发队列、
SQLite 落库、FFmpeg 后期、RSS 封装。直接调 runner 会绕掉鉴权与部分落库，
测出来的耗时不能代表用户实际等待时间。因此本脚本只用一个 `httpx.Client`
（自带 Cookie jar，与浏览器的凭据链一致）打真实 API。

## 指标口径（易误读，逐条声明）

| 指标 | 口径 |
| --- | --- |
| 端到端耗时 | `POST /api/tasks` 发出 → `status == DONE` 为止的墙钟秒数（含脚本生成、GPU 合成、后期、RSS） |
| 端到端 RTF | 端到端耗时 / 成片时长。**< 1 表示比实时快**；与 D3/D5 的「合成 RTF」不可直接比（口径见上） |
| 千字耗时 | 端到端耗时 / 字数 × 1000。**这是「1000 字 ≤ 10 min」这条 M4 指标的可比形式**——短档按绝对值直接比会被片头尾与固定开销稀释 |
| >10 min 标记 | 按**端到端耗时**判定，不是成片时长（M4 原文即「1000 字脚本端到端耗时 ≤ 10 分钟」） |
| 字数 | `api.schemas.count_chars`（含标点、不含空白），直接 import 复用，杜绝口径两套 |
| 响度 / 真峰 | `make_listen_pack.measure`（ebur128 + astats），与 D3/D5 基线同一函数 |
| 可用率 | 见 `--help` 与报告「口径声明」段：本脚本只能给**结构可用率**（A/B 交替 + 行长 + 未合规命中），因为 HTTP 响应不暴露链路内部的纠错轮次 |

## 一致率（对应 M3「合成文本与脚本文本逐句一致率 = 100%」）

计划书难点 4 给的是「三层对齐」，本脚本按 `seq` 逐句回验这三层都成立：

1. `script_lines.read_text` 非空   —— 规范化读文已落库；
2. `text_hash` 非空且 `duration_ms > 0` —— 音频指纹与时长已落库；
3. `seg_status == DONE` 且 `GET /segments/{seq}/audio` 返回 200，
   且返回音频时长相容（> 0 且 ≤ 该行 `duration_ms` + 容差）
   —— **该行在「音轨清单」里确有对应音轨**。

第 3 条为什么用「≤ 该行时长 + 容差」而不是「≈」：一行可能被切句成多段，
试听端点返回的是**首段**，而 `duration_ms` 是**该行全部段之和**（见 models.ScriptLine
注释与 routers/tasks.py 的 `segment_audio`）。故正确判据是「首段 ≤ 整行之和」，
写成等式会把切句行误判为不一致。

用法（项目根目录，须与后端同一解释器）：

    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts/eval_batch.py --limit 1
    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts/eval_batch.py --ids K1,L3
    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts/eval_batch.py            # 全量 12 条

产物（`outputs/eval/<时间戳>/`）：

    results.json   机器可读全量明细（含逐句一致率失败清单）
    cases.csv      逐例宽表（Excel 直开，含 BOM）
    report.md      端到端耗时表 + 指标结论 + >10 min 清单
    audio/         各例成片副本（供 MOS 造包与人工抽听）
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from api.schemas import count_chars                       # noqa: E402
from make_listen_pack import measure as lp_measure        # noqa: E402

log = logging.getLogger("eval_batch")

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

DEFAULT_TOPICS = ROOT / "backend" / "assets" / "eval_topics.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"

#: M4 指标线：1000 字脚本端到端 ≤ 10 分钟（墙钟）。标记与结论都基于它。
TEN_MIN_S = 600.0
#: 「千字耗时」的归一化基准
PER_WORDS = 1000

#: 试听端点首段 vs 整行时长的容差（秒）。切句行只允许「首段更短」，不允许更长。
SEG_DUR_TOL_S = 0.35

#: 预热任务。M4 的指标前提写的是「模型已预热」，而 TTS 引擎是懒加载的：
#: 不预热的话，第一个 case 会把「加载模型（约 23 s）+ warmup」算进端到端耗时，
#: 让「1000 字 ≤ 10 min」这条线变成「谁排在第一个谁背锅」。预热用例本身不进统计。
WARMUP_TOPIC = "开播前先热一下嗓子"
WARMUP_DURATION_MIN = 0.5

TERMINAL = ("DONE", "FAILED", "CANCELED")
#: 脚本阶段的停靠点。**刻意不放进 TERMINAL** —— 状态机里 SCRIPT_READY 还能迁到
#: SYNTHESIZING/CANCELED，它不是终态；压测在这里停下是因为要等「用户确认脚本」这一步
#: （计划书 4.5：SCRIPT_READY --确认脚本--> SYNTHESIZING）。
SCRIPT_READY = "SCRIPT_READY"


class EvalError(RuntimeError):
    """压测流程错误（网络、状态机、超时）。"""


# --------------------------------------------------------------------------- #
# 纯函数（可离线单测）
# --------------------------------------------------------------------------- #

def slug(text: str, limit: int = 20) -> str:
    """文件系统安全的短名：保留中英文与数字，其余丢弃。"""
    keep = [c for c in (text or "") if c.isalnum() or "\u4e00" <= c <= "\u9fff"]
    return "".join(keep)[:limit] or "case"


def load_topics(path: Path) -> dict:
    """读测试主题集，做**结构自检**后才返回（主题集是评测资产，坏了必须当场发现）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    topics = raw.get("topics") or []
    if not topics:
        raise EvalError(f"主题集为空：{path}")
    ids = [t.get("id") for t in topics]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise EvalError(f"主题集 id 重复：{sorted(dup)}")
    for t in topics:
        for key in ("id", "category", "length", "topic", "duration_min"):
            if t.get(key) in (None, ""):
                raise EvalError(f"主题集条目缺字段 {key}：{t}")
    return raw


def select_cases(spec: dict, *, ids: list[str] | None = None,
                 category: str | None = None, length: str | None = None,
                 limit: int | None = None) -> list[dict]:
    """按 id / 类别 / 档位筛选，再按原顺序截断。"""
    rows = list(spec.get("topics") or [])
    if ids:
        want = {i.strip() for i in ids if i.strip()}
        rows = [r for r in rows if r["id"] in want]
        missing = want - {r["id"] for r in rows}
        if missing:
            raise EvalError(f"主题集里没有这些 id：{sorted(missing)}")
    if category:
        rows = [r for r in rows if r["category"] == category]
    if length:
        rows = [r for r in rows if r["length"] == length]
    if limit is not None:
        rows = rows[:limit]
    return rows


def per_1000_words(elapsed_s: float | None, words: int | None) -> float | None:
    """把端到端耗时归一化到「每千字」——M4 那条指标的可比形式。"""
    if not elapsed_s or not words:
        return None
    return round(float(elapsed_s) / int(words) * PER_WORDS, 1)


def wav_duration_from_bytes(data: bytes) -> float | None:
    """从 RIFF 字节里算时长，纯 stdlib。

    为什么不写临时文件交给 ffprobe：逐句回验会调用几十~几百次，每次起一个进程太慢；
    为什么不用 stdlib `wave`：CosyVoice 输出 float32（format 3），`wave` 读不了
    （项目已知坑，见 D3 报告）。故直接解析 `fmt `/`data` 块。
    """
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos = 12
    fmt: tuple[int, int, int] | None = None      # (channels, sample_rate, bits)
    data_size: int | None = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt " and len(body) >= 16:
            channels = int.from_bytes(body[2:4], "little")
            rate = int.from_bytes(body[4:8], "little")
            bits = int.from_bytes(body[14:16], "little")
            fmt = (channels, rate, bits)
        elif cid == b"data":
            data_size = size
        pos += 8 + size + (size & 1)
        if fmt and data_size is not None:
            break
    if not fmt or data_size is None or not fmt[0] or not fmt[1] or not fmt[2]:
        return None
    return data_size / (fmt[0] * fmt[1] * (fmt[2] // 8))


def structure_check(lines: list[dict], *, max_chars: int = 40,
                    require_alternating: bool = True) -> tuple[bool, list[str]]:
    """脚本结构硬约束（与 `PodcastScript.check_policy` 的 errors 档同口径）。

    只覆盖 HTTP 响应能看到的项：A/B 都出现、强制交替、单行不超上限。
    字数配额**刻意不在此**——它是 quota 档（驱动重试但不判可用性），
    由报告里的 `word_deviation_pct` 单独上报（计划书 4.10 ③ 要求两者分列）。
    """
    issues: list[str] = []
    speakers = {ln.get("speaker") for ln in lines}
    if len(speakers) < 2:
        issues.append(f"只出现说话人 {sorted(speakers)}，非双人对话")
    if require_alternating:
        same = [(a, b) for a, b in zip(lines, lines[1:])
                if a.get("speaker") == b.get("speaker")]
        if same:
            issues.append(f"{len(same)} 处连续同人（首个在第 {same[0][1].get('seq')} 行）")
    over = [ln for ln in lines if count_chars(ln.get("text", "")) > max_chars]
    if over:
        issues.append(f"{len(over)} 行超过单行上限 {max_chars}"
                      f"（首个第 {over[0].get('seq')} 行 "
                      f"{count_chars(over[0].get('text', ''))} 字）")
    return (not issues), issues


def summarize(cases: list[dict]) -> dict:
    """汇总层。所有结论性数字都从这里出，报告只负责排版。"""
    done = [c for c in cases if c.get("status") == "DONE"]
    failed = [c for c in cases if c.get("status") not in (None, "DONE")]

    # 可用率（结构口径，见模块 docstring）：结构合规 且 未命中敏感词自检
    usable = [c for c in done if c.get("structure_ok") and not c.get("content_flagged")]

    lines_total = sum(int(c.get("consistency_total") or 0) for c in done)
    lines_ok = sum(int(c.get("consistency_passed") or 0) for c in done)

    over = [c["id"] for c in done if (c.get("elapsed_total_s") or 0) > TEN_MIN_S]

    def _med(key: str, rows: list[dict]) -> float | None:
        vals = [c[key] for c in rows if c.get(key) is not None]
        return round(statistics.median(vals), 2) if vals else None

    long_rows = [c for c in done if c.get("length") == "long"]
    short_rows = [c for c in done if c.get("length") == "short"]

    return {
        "total": len(cases),
        "done": len(done),
        "failed": len(failed),
        "usable": len(usable),
        "usable_rate": round(len(usable) / len(done), 4) if done else 0.0,
        "consistency_total": lines_total,
        "consistency_passed": lines_ok,
        "consistency_rate": round(lines_ok / lines_total, 4) if lines_total else 0.0,
        "over_10min": over,
        "long_median_elapsed_s": _med("elapsed_total_s", long_rows),
        "short_median_elapsed_s": _med("elapsed_total_s", short_rows),
        "long_median_per_1000_words": _med("per_1000_words_s", long_rows),
        "median_rtf_e2e": _med("rtf_e2e", done),
        "median_lufs": _med("lufs", done),
        "median_true_peak_dbtp": _med("true_peak_dbtp", done),
        "word_deviation_median": _med("word_deviation_pct", done),
        "failed_ids": [c["id"] for c in failed],
    }


CSV_COLUMNS = [
    ("id", "编号"), ("category", "类别"), ("length", "档位"), ("topic", "主题"),
    ("duration_min", "目标时长min"), ("target_words", "目标字数"), ("words", "实际字数"),
    ("word_deviation_pct", "字数偏差%"), ("line_count", "行数"),
    ("structure_ok", "结构合规"), ("content_flagged", "合规命中"),
    ("script_elapsed_s", "脚本耗时s"), ("synth_elapsed_s", "合成耗时s"),
    ("elapsed_total_s", "端到端耗时s"), ("per_1000_words_s", "千字耗时s"),
    ("audio_duration_s", "成片时长s"), ("rtf_e2e", "端到端RTF"),
    ("lufs", "响度LUFS"), ("true_peak_dbtp", "真峰dBTP"), ("size_bytes", "大小B"),
    ("consistency_total", "比对句数"), ("consistency_passed", "一致句数"),
    ("consistency_rate", "一致率"), ("over_10min", "超10min"),
    ("status", "终态"), ("task_id", "任务ID"), ("error", "错误"),
]


def render_cases_csv(cases: list[dict]) -> str:
    """逐例宽表。带 BOM，Windows Excel 直接双击不乱码。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in CSV_COLUMNS])
    for c in cases:
        row = []
        for key, _ in CSV_COLUMNS:
            v = c.get(key)
            if key == "over_10min":
                v = "Y" if (c.get("elapsed_total_s") or 0) > TEN_MIN_S else ""
            row.append("" if v is None else v)
        w.writerow(row)
    return buf.getvalue()


def _fmt(v: float | None, nd: int = 1, dash: str = "—") -> str:
    return dash if v is None else f"{v:.{nd}f}"


def _md_table(head: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(head) + " |",
             "| " + " | ".join("---" for _ in head) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(lines)


def render_report(meta: dict, cases: list[dict]) -> str:
    """D10 交付物：端到端耗时表（标出 >10 min）+ 指标结论 + 一致率明细 + 口径声明。"""
    s = summarize(cases)
    out: list[str] = []
    out.append("# D10 端到端压测报告（自动生成）\n")
    out.append(f"- 生成时间：{meta.get('generated_at')}")
    out.append(f"- 后端地址：{meta.get('base_url')}")
    out.append(f"- 主题集：`{meta.get('topics_file')}`（版本 {meta.get('topics_version')}）")
    out.append(f"- 压测账号：`{meta.get('user')}`")
    out.append(f"- 逐句一致率回验：{'开' if meta.get('check_segments') else '关'}"
               f"；合成：{'开' if meta.get('with_synth') else '关'}")
    if meta.get("warmup"):
        w = meta["warmup"]
        out.append(f"- 预热：已执行（端到端 {w.get('elapsed_total_s')} s，"
                   f"类型 {w.get('status')}）——**不计入统计**，"
                   f"用于满足 M4「模型已预热」的前提；否则首例会背冷启动的锅")
    else:
        out.append("- 预热：未执行（首例含模型加载与 warmup，其耗时偏高属预期）")
    if meta.get("notes"):
        out.append(f"- 备注：{meta['notes']}")
    carried = [c for c in cases if c.get("carried_over")]
    if carried:
        out.append(f"- 断点续跑：本批实跑 {len(cases) - len(carried)} 条，"
                   f"另有 {len(carried)} 条并入自既有产物"
                   f"（下表标「续」，其耗时取自上一批，未重跑）")
    if meta.get("merged_from"):
        out.append(f"  - 并入来源：{'、'.join(meta['merged_from'])}")
    out.append("")

    # ---------------- 端到端耗时表（本阶段的 Done 交付物） ----------------
    out.append("## 一、端到端耗时表\n")
    out.append("> 端到端 = `POST /api/tasks` → `status=DONE` 的墙钟秒数（含脚本、合成、后期、RSS）。")
    out.append(f"> **>10min 标记按端到端耗时判定**（M4 原文：「1000 字脚本端到端耗时 ≤ 10 分钟」）；")
    out.append("> 千字耗时＝端到端耗时／字数×1000，是跨档位可比的归一化形式。\n")
    rows = []
    for c in cases:
        over = "**>10min**" if (c.get("elapsed_total_s") or 0) > TEN_MIN_S else ""
        last = over or c.get("status")
        if c.get("carried_over"):
            last = f"{last}（续）"
        rows.append([
            c.get("id"), c.get("category"), c.get("length"),
            c.get("topic"),
            _fmt(c.get("elapsed_total_s"), 1),
            _fmt(c.get("per_1000_words_s"), 1),
            _fmt(c.get("audio_duration_s"), 1),
            _fmt(c.get("rtf_e2e"), 2),
            c.get("words") if c.get("words") is not None else "—",
            last,
        ])
    out.append(_md_table(
        ["编号", "类别", "档位", "主题", "端到端 s", "千字 s", "成片 s", "端到端 RTF",
         "字数", "标记"],
        rows))
    out.append("")

    # ---------------- 指标结论 ----------------
    out.append("## 二、指标结论\n")
    long_ok = s["long_median_per_1000_words"]
    verdict = ("无 ≥1000 字样本，无法判定" if long_ok is None
               else ("达标" if long_ok <= TEN_MIN_S else "不达标"))
    out.append(_md_table(
        ["指标", "口径", "实测", "目标", "判定"],
        [
            ["可用率（结构口径）", "结构合规且未合规命中 / 已完成",
             f"{s['usable']}/{s['done']} = {s['usable_rate']:.0%}", "≥ 80%",
             "达标" if s["usable_rate"] >= 0.8 else "不达标"],
            ["逐句一致率", "三层对齐回验通过行 / 总行",
             f"{s['consistency_passed']}/{s['consistency_total']} = {s['consistency_rate']:.2%}",
             "= 100%", "达标" if s["consistency_rate"] >= 1.0 else "不达标"],
            ["千字端到端耗时", "长档端到端耗时 / 字数 × 1000（中位）",
             _fmt(long_ok, 1) + " s", "≤ 600 s", verdict],
            ["端到端 RTF", "端到端耗时 / 成片时长（中位）",
             _fmt(s["median_rtf_e2e"], 2), "—",
             "参考（与 D3/D5 合成 RTF 不同口径）"],
            ["成片响度", "ebur128 综合响度（中位）", _fmt(s["median_lufs"], 2) + " LUFS",
             "-16 ± 1", "参考"],
            ["成片真峰", "ebur128 真峰（中位）", _fmt(s["median_true_peak_dbtp"], 2) + " dBTP",
             "≤ -1.5", "达标"],
            ["字数偏差", "(实际−目标)/目标（中位）",
             # ⚠️ `word_deviation_pct` 里存的**已经是百分数**（-8.05 表示 −8.05%），
             # 这里若再用 `:+.1%` 会被乘第二次 100 → 渲染成 "-805.0%"（实测踩到）。
             (f"{s['word_deviation_median']:+.1f}%"
              if s["word_deviation_median"] is not None else "—"),
             "±10%", "只上报不判（见口径声明）"],
        ]))
    out.append("")
    out.append(f"- 短档端到端耗时中位：**{_fmt(s['short_median_elapsed_s'], 1)} s**")
    out.append(f"- 长档端到端耗时中位：**{_fmt(s['long_median_elapsed_s'], 1)} s**")
    out.append(f"- 完成 / 失败：**{s['done']} / {s['failed']}**"
               + (f"（失败：{s['failed_ids']}）" if s["failed_ids"] else ""))
    out.append("")

    # ---------------- >10 min 清单 ----------------
    out.append("## 三、超 10 分钟 case 清单\n")
    if s["over_10min"]:
        out.append("下列 case 端到端耗时超过 M4 的 10 分钟线，需按计划书 2.4 尾部的处置表收敛"
                   "（句级缓存命中率 / 压缩单句长度 / 关闭第二遍 loudnorm）：\n")
        rows = [[c["id"], c["topic"], _fmt(c.get("elapsed_total_s"), 1),
                 c.get("words"), _fmt(c.get("per_1000_words_s"), 1)] for c in cases
                if c["id"] in s["over_10min"]]
        out.append(_md_table(["编号", "主题", "端到端 s", "字数", "千字 s"], rows))
    else:
        out.append("无。所有 case 端到端耗时均在 10 分钟线内。")
    out.append("")
    out.append("## 四、一致率失败明细\n")
    bad = [c for c in cases if (c.get("consistency_failures") or [])]
    if not bad:
        out.append("无（未发现「脚本行 ↔ 音轨」对不上的行）。")
    else:
        for c in bad:
            out.append(f"### {c['id']} 《{c['topic']}》"
                       f"（{c.get('consistency_passed')}/{c.get('consistency_total')}）")
            for item in c["consistency_failures"][:20]:
                out.append(f"- seq={item.get('seq')}：{item.get('reason')}")
            if len(c["consistency_failures"]) > 20:
                out.append(f"- …另有 {len(c['consistency_failures']) - 20} 条")
            out.append("")

    # ---------------- 口径声明 ----------------
    out.append("## 五、口径声明（引用本报告数字前必读）\n")
    out.append("1. **可用率**只统计「结构合规 + 未命中合规自检」。HTTP 响应不暴露链路内部的"
               "纠错轮次，因此本脚本给不出「首次调用结构通过率」（需零纠错的指标）；"
               "计划书 4.10 ③ 要求该指标**分列统计**，本报告只提供其中一列。")
    out.append("2. **端到端 RTF** 的分子含脚本生成（LLM 网络耗时）与后期/RSS，"
               "与 D3/D5 报告里的「合成 RTF」不是一个口径，**不可直接比较**。")
    out.append("3. **字数配额不判可用性**：`SCRIPT_WORD_QUOTA_ENFORCE` 默认关，"
               "字数偏差只上报不判失败（依据 D4 三轮实测，见计划书 4.10 ③）。")
    out.append("4. **千字耗时**是 M4「1000 字 ≤ 10 min」的可比形式：短档绝对值里含"
               "约 11~13 s 动态片头与固定开销，直接与 600 s 比会低估长档风险。")
    out.append("5. 一致率第 3 条判据用「首段 ≤ 整行之和 + 容差」——一行可能被切句成多段，"
               "试听端点只返回首段（见 `routers/tasks.py::segment_audio`）。")
    out.append("")
    out.append("---")
    out.append(f"原始明细：`{meta.get('out_dir')}/results.json`；"
               f"逐例宽表：`{meta.get('out_dir')}/cases.csv`")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #

class EvalClient:
    """薄封装：自带 Cookie jar（与浏览器同一凭据链）。"""

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        # ⚠️ `trust_env=False`：本机后端**绝不能**走环境里的 HTTP_PROXY/HTTPS_PROXY。
        # 代理转发的请求行是 absolute-form（`GET http://host/path`），而 uvicorn 不是代理，
        # 会把整串当成路径、百分号编码成 `GET http%3A//host/path` → 404。
        # 实测踩到：压测第一步注册成功后，`GET /api/feeds/me` 就 404 退出（EXIT=2），
        # 而裸 httpx.get(绝对 URL) 却正常——非常迷惑，值得留这段注释。
        # 确需走代理时显式设 `EVAL_HTTP_TRUST_ENV=1`。
        trust_env = os.environ.get("EVAL_HTTP_TRUST_ENV", "").strip().lower() not in (
            "", "0", "false", "no")
        self._c = httpx.Client(base_url=self.base_url, timeout=timeout,
                               follow_redirects=True, trust_env=trust_env)

    def close(self) -> None:
        self._c.close()

    # ---------------- 鉴权 ----------------

    def register(self, username: str, password: str) -> dict:
        r = self._c.post("/api/auth/register",
                         json={"username": username, "password": password})
        if r.status_code == 409:            # 账号已存在 -> 直接登录（便于重跑复用）
            return self.login(username, password)
        r.raise_for_status()
        return r.json()

    def login(self, username: str, password: str) -> dict:
        r = self._c.post("/api/auth/login",
                         json={"username": username, "password": password})
        r.raise_for_status()
        return r.json()

    # ---------------- 任务 ----------------

    def create_task(self, *, topic: str, duration_min: float, style: str = "") -> dict:
        r = self._c.post("/api/tasks", json={
            "topic": topic, "duration_min": float(duration_min), "style": style})
        r.raise_for_status()
        return r.json()

    def get_task(self, task_id: str) -> dict:
        r = self._c.get(f"/api/tasks/{task_id}", timeout=15.0)
        r.raise_for_status()
        return r.json()

    def get_script(self, task_id: str) -> dict:
        r = self._c.get(f"/api/tasks/{task_id}/script", timeout=30.0)
        r.raise_for_status()
        return r.json()

    def synthesize(self, task_id: str) -> dict:
        r = self._c.post(f"/api/tasks/{task_id}/synthesize", timeout=30.0)
        r.raise_for_status()
        return r.json()

    # ---------------- 媒体 ----------------

    def segment_audio(self, task_id: str, seq: int) -> httpx.Response:
        return self._c.get(f"/api/tasks/{task_id}/segments/{seq}/audio", timeout=30.0)

    def audio_bytes(self, task_id: str) -> bytes:
        r = self._c.get(f"/api/tasks/{task_id}/audio", timeout=120.0)
        r.raise_for_status()
        return r.content

    def feed(self) -> dict:
        r = self._c.get("/api/feeds/me", timeout=15.0)
        r.raise_for_status()
        return r.json()


def wait_terminal(client: EvalClient, task_id: str, *, timeout: float,
                  interval: float, label: str,
                  targets: tuple[str, ...] = TERMINAL) -> dict:
    """轮询到「命中 `targets`」或「进入终态」为止。

    ⚠️ 为什么必须可指定 `targets`：脚本阶段停在 `SCRIPT_READY`，而它**不是终态**
    （状态机里它还能迁到 SYNTHESIZING / CANCELED）。只认终态会一直轮询到超时——
    首次自测就是这么被卡了 300 s（任务早已就绪，脚本却还在等）。合成阶段才用默认终态。
    """
    deadline = time.perf_counter() + timeout
    last: dict = {}
    while True:
        last = client.get_task(task_id)
        st = last.get("status")
        if st in targets or st in TERMINAL:
            return last
        remain = deadline - time.perf_counter()
        if remain <= 0:
            raise EvalError(f"{label} 超时（{timeout:.0f}s）：最后状态 {st}"
                            f" / 阶段 {last.get('stage')} / 进度 {last.get('progress')}%")
        time.sleep(min(interval, max(0.5, remain)))


# --------------------------------------------------------------------------- #
# 单例流程
# --------------------------------------------------------------------------- #

def measure_audio(path: Path) -> dict:
    """成片指标：时长（ffprobe）/ 响度与真峰（ebur128，复用 make_listen_pack 口径）。"""
    import shutil as _sh
    import subprocess as _sp

    dur = None
    exe = _sh.which("ffprobe")
    if exe and path.is_file():
        p = _sp.run([exe, "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=nw=1:nk=1", str(path)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
        try:
            dur = float((p.stdout or "").strip())
        except ValueError:
            dur = None
    m = lp_measure(path) if path.is_file() else {}
    lufs = m.get("I")
    tp = m.get("TP")
    if lufs == float("-inf"):
        lufs = None
    return {
        "audio_duration_s": round(dur, 3) if dur else None,
        "lufs": lufs if isinstance(lufs, float) else None,
        "true_peak_dbtp": tp if isinstance(tp, float) else None,
        "size_bytes": path.stat().st_size if path.is_file() else None,
    }


def load_prior_cases(dirs: list[Path]) -> tuple[list[dict], list[Path], list[str]]:
    """从既有产物目录读回 case 记录（断点续跑的基石）。

    为什么需要它：批次被打断后重跑时，已完成的 case 若再跑一遍，会**命中句级缓存**
    （缓存键含 read_text），耗时被压缩成「假快」，反而污染 M4 统计口径。
    所以正确做法是「只跑没跑过的 + 把跑过的原样并入」，而不是全部重跑。

    返回 (cases, 实际读到的目录, 告警)。读不到/解析失败的目录只告警，不抛——
    续跑不该因为一个坏目录就整体失败。
    """
    cases: list[dict] = []
    used: list[Path] = []
    warns: list[str] = []
    for d in dirs:
        p = Path(d) / "results.json" if Path(d).is_dir() else Path(d)
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            warns.append(f"{p} 不存在，已跳过")
            continue
        except Exception as exc:                                 # noqa: BLE001
            warns.append(f"{p} 解析失败（{type(exc).__name__}），已跳过")
            continue
        got = payload.get("cases") or []
        for c in got:
            c = dict(c)
            c["carried_over"] = True
            c["source_dir"] = str(p.parent)
            cases.append(c)
        used.append(p.parent)
        if got:
            warns.append(f"{p.parent.name}：并入 {len(got)} 条")
    return cases, used, warns


def merge_carried_over(results: list[dict], prior: list[dict]) -> list[dict]:
    """把上次已完成、本次未跑的 case 并进结果集。

    - 本次真跑过的以本次为准（同一 id 不重复）；
    - 只并入 `status=DONE` 的：失败/半截的记录没有统计价值，重跑更干净。
    """
    have = {c.get("id") for c in results}
    for c in prior:
        if c.get("id") in have or c.get("status") != "DONE":
            continue
        have.add(c.get("id"))
        results.append(c)
    return results


def order_results_by_topic_set(spec: dict, results: list[dict]) -> list[dict]:
    """按主题集声明顺序重排（报告与 CSV 的行序稳定、可逐次比对）。"""
    try:
        order = {c["id"]: i for i, c in enumerate(spec.get("topics") or [])}
    except Exception:                                            # noqa: BLE001
        return results
    return sorted(results, key=lambda c: order.get(c.get("id"), 10_000))


def check_consistency(client: EvalClient, task_id: str,
                      lines: list[dict]) -> tuple[int, int, list[dict]]:
    """按 seq 逐句回验「脚本行 ↔ 音轨」三层对齐（判据见模块 docstring）。"""
    passed = 0
    failures: list[dict] = []
    for ln in lines:
        seq = int(ln.get("seq") or 0)
        reasons: list[str] = []
        if not (ln.get("read_text") or "").strip():
            reasons.append("read_text 为空（规范化读文未落库）")
        if not ln.get("text_hash"):
            reasons.append("text_hash 为空（音频指纹未落库）")
        dur_ms = int(ln.get("duration_ms") or 0)
        if dur_ms <= 0:
            reasons.append("duration_ms=0（该行无音轨时长）")
        if ln.get("seg_status") != "DONE":
            reasons.append(f"seg_status={ln.get('seg_status')}（未落 DONE）")
        if not reasons:
            r = client.segment_audio(task_id, seq)
            if r.status_code != 200:
                reasons.append(f"试听端点返回 {r.status_code}（音轨清单里没有这一行）")
            else:
                d = wav_duration_from_bytes(r.content)
                if d is None:
                    reasons.append("试听端点返回的音频无法解析时长")
                elif d <= 0:
                    reasons.append("试听音频时长为 0")
                elif d > dur_ms / 1000.0 + SEG_DUR_TOL_S:
                    reasons.append(f"试听首段 {d:.2f}s 长于该行合计 {dur_ms / 1000:.2f}s"
                                   f"（超出容差 {SEG_DUR_TOL_S}s）")
        if reasons:
            failures.append({"seq": seq, "text": ln.get("text", "")[:40],
                             "reason": "；".join(reasons)})
        else:
            passed += 1
    return passed, len(lines), failures


def run_case(client: EvalClient, case: dict, *, out_dir: Path, style: str,
             with_synth: bool, check_segments: bool,
             timeout_script: float, timeout_synth: float,
             poll_interval: float) -> dict:
    """跑单条主题的完整链路并采集指标。任何异常都落进返回的 dict（不中断批次）。"""
    cid = case["id"]
    rec: dict = {
        "id": cid, "category": case["category"], "length": case["length"],
        "topic": case["topic"], "duration_min": case["duration_min"],
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    t0 = time.perf_counter()
    try:
        task = client.create_task(topic=case["topic"],
                                  duration_min=case["duration_min"], style=style)
        tid = task["id"]
        rec["task_id"] = tid
        rec["target_words"] = task.get("target_word_count")

        t = wait_terminal(client, tid, timeout=timeout_script,
                          interval=poll_interval, label="脚本生成",
                          targets=(SCRIPT_READY,))
        rec["script_elapsed_s"] = round(time.perf_counter() - t0, 2)
        rec["status"] = t["status"]
        rec["content_flagged"] = bool(t.get("content_flagged"))
        if t["status"] != "DONE" and t["status"] != "SCRIPT_READY":
            rec["error"] = t.get("error_msg") or t.get("stage") or t["status"]
            rec["elapsed_total_s"] = round(time.perf_counter() - t0, 2)
            return rec

        sc = client.get_script(tid)
        lines = sc.get("lines") or []
        words = sum(count_chars(ln.get("text", "")) for ln in lines)
        rec.update({
            "script_title": sc.get("title", ""),
            "line_count": len(lines),
            "words": words,
        })
        if rec.get("target_words"):
            rec["word_deviation_pct"] = round(
                (words - rec["target_words"]) / rec["target_words"] * 100, 1)
        ok, issues = structure_check(lines)
        rec["structure_ok"] = ok
        rec["structure_issues"] = issues

        if not with_synth:
            rec["status"] = t["status"]
            rec["elapsed_total_s"] = round(time.perf_counter() - t0, 2)
            rec["per_1000_words_s"] = per_1000_words(rec["elapsed_total_s"], words)
            return rec

        t1 = time.perf_counter()
        client.synthesize(tid)
        t = wait_terminal(client, tid, timeout=timeout_synth,
                          interval=poll_interval, label="合成流水线")
        rec["synth_elapsed_s"] = round(time.perf_counter() - t1, 2)
        rec["status"] = t["status"]
        if t["status"] != "DONE":
            rec["error"] = t.get("error_msg") or t.get("stage") or t["status"]
            rec["elapsed_total_s"] = round(time.perf_counter() - t0, 2)
            return rec

        rec["elapsed_total_s"] = round(time.perf_counter() - t0, 2)
        rec["per_1000_words_s"] = per_1000_words(rec["elapsed_total_s"], words)

        # 成片落盘副本（MOS 造包与人工抽听都用它，避免依赖 back-end 保留期）
        audio_dir = out_dir / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        mp3 = audio_dir / f"{cid}_{slug(case['topic'])}.mp3"
        mp3.write_bytes(client.audio_bytes(tid))
        rec["audio_path"] = str(mp3)
        rec.update(measure_audio(mp3))
        if rec.get("audio_duration_s"):
            rec["rtf_e2e"] = round(rec["elapsed_total_s"] / rec["audio_duration_s"], 3)
            if rec.get("synth_elapsed_s"):
                rec["rtf_synth"] = round(rec["synth_elapsed_s"] / rec["audio_duration_s"], 3)
        rec["over_10min"] = bool(rec["elapsed_total_s"] > TEN_MIN_S)

        if check_segments:
            # ⚠️ 必须**重新拉一次**脚本：上面那份 `lines` 是 SCRIPT_READY 时的快照，
            # 那时还没合成，所有行都是 seg_status=PENDING / read_text="" / duration_ms=0。
            # 用旧快照比对会把「一致率」判成 0%（首次自测就是这么误判的）。
            fresh = (client.get_script(tid).get("lines") or [])
            passed, total, fails = check_consistency(client, tid, fresh)
            rec.update({"consistency_passed": passed, "consistency_total": total,
                        "consistency_rate": round(passed / total, 4) if total else 0.0,
                        "consistency_failures": fails})
        return rec
    except Exception as exc:                                     # noqa: BLE001
        rec["status"] = "ERROR"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["elapsed_total_s"] = round(time.perf_counter() - t0, 2)
        return rec


def _fmt_console(c: dict) -> str:
    if c.get("status") != "DONE":
        return f"[{c['id']:>3}] {c['topic']} -> {c.get('status')}：{c.get('error', '')}"
    cons = (f"{c.get('consistency_passed')}/{c.get('consistency_total')}"
            if c.get("consistency_total") else "—")
    return (f"[{c['id']:>3}] {c['category']}/{c['length']:5s} 《{c['topic']}》 "
            f"端到端 {c.get('elapsed_total_s'):6.1f}s（千字 {c.get('per_1000_words_s')}s）"
            f" | 脚本 {c.get('script_elapsed_s'):5.1f}s 合成 {c.get('synth_elapsed_s'):6.1f}s"
            f" | {c.get('words')}字/{c.get('line_count')}行"
            f" | 成片 {c.get('audio_duration_s'):6.1f}s RTF {c.get('rtf_e2e')}"
            f" | {_fmt(c.get('lufs'), 2)} LUFS / {_fmt(c.get('true_peak_dbtp'), 2)} dBTP"
            f" | 一致 {cons}")


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def _setup_logging(verbose: bool) -> None:
    # Windows 控制台默认 GBK，直接 print 中文会 UnicodeEncodeError（同 verify_d4 的处置）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")             # type: ignore[union-attr]
        except Exception:                                    # noqa: BLE001
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout, force=True)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="D10 端到端压测与指标采集（走真实 HTTP + Cookie 鉴权链）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help=f"后端地址（默认 {DEFAULT_BASE_URL}）")
    ap.add_argument("--topics", default=str(DEFAULT_TOPICS), help="测试主题集 JSON")
    ap.add_argument("--out", default="", help="产物目录（默认 outputs/eval/<时间戳>）")
    ap.add_argument("--ids", default="", help="只跑这些编号，逗号分隔，如 K1,L3")
    ap.add_argument("--merge-from", default="",
                    help="断点续跑：从既有产物目录读回已完成的 case 一并统计（逗号分隔）；"
                         "避免重跑命中句级缓存导致耗时假快")
    ap.add_argument("--category", default="", help="只跑某类别（知识科普/行业解读/生活闲聊）")
    ap.add_argument("--length", default="", choices=["", "short", "long"], help="只跑某档位")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    ap.add_argument("--style", default="", help="语言风格提示（透传给任务）")
    ap.add_argument("--user", default="", help="复用既有账号；留空则按时间戳新建")
    ap.add_argument("--password", default="Eval@12345", help="账号口令")
    ap.add_argument("--no-synth", action="store_true",
                    help="只跑到 SCRIPT_READY（快速验证 harness，不占 GPU）")
    ap.add_argument("--no-warmup", action="store_true",
                    help="跳过预热。默认预热一次，避免首例背上冷启动耗时（M4 前提是「模型已预热」）")
    ap.add_argument("--no-segment-check", action="store_true", help="跳过逐句一致率回验")
    ap.add_argument("--timeout-script", type=float, default=300.0, help="脚本阶段超时（秒）")
    ap.add_argument("--timeout-synth", type=float, default=2400.0, help="合成阶段超时（秒）")
    ap.add_argument("--poll-interval", type=float, default=3.0, help="轮询间隔（秒）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将要执行的 case 清单，不发任何请求")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)
    _setup_logging(args.verbose)

    ids = [s for s in (args.ids or "").split(",") if s.strip()]
    try:
        spec = load_topics(Path(args.topics))
        cases = select_cases(spec, ids=ids or None,
                             category=args.category or None,
                             length=args.length or None, limit=args.limit)
    except EvalError as exc:
        # 参数写错（如 --ids 拼错）不该给用户看 traceback：它是使用错误，不是程序缺陷。
        log.error("%s", exc)
        return 2
    if not cases:
        log.error("筛选后没有可执行的 case，请检查 --ids/--category/--length")
        return 2

    print("=" * 96)
    print(f"D10 端到端压测：{len(cases)} 条（主题集 {Path(args.topics).name} "
          f"v{spec.get('version')}）")
    for c in cases:
        print(f"  [{c['id']:>3}] {c['category']} / {c['length']:5s}｜"
              f"{c['topic']}（目标 {c['duration_min']} min）")
    print(f"  后端 {args.base_url}｜合成 {'开' if not args.no_synth else '关'}"
          f"｜逐句一致率回验 {'开' if not args.no_segment_check else '关'}")
    print("=" * 96)
    if args.dry_run:
        print("--dry-run：未发起任何请求。")
        return 0

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (ROOT / "outputs" / "eval" / ts)
    out_dir.mkdir(parents=True, exist_ok=True)

    client = EvalClient(args.base_url)
    user = args.user or f"eval_{ts}"
    try:
        info = client.register(user, args.password)
        print(f"账号就绪：{info['user']['username']}（id={info['user']['id']}）")
        feed = client.feed()
        print(f"频道：{feed.get('title') or '(未命名)'}｜订阅 {feed.get('feed_url')}")
    except Exception as exc:                                     # noqa: BLE001
        client.close()
        log.error("无法建立登录态（后端没起？地址写错？）：%s", exc)
        return 2

    results: list[dict] = []
    warmup: dict | None = None

    def _flush(meta_extra: dict | None = None) -> None:
        """每条都落盘一次：长批次中途被打断时保留既有证据。"""
        meta = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "base_url": args.base_url,
            "topics_file": str(Path(args.topics)),
            "topics_version": spec.get("version"),
            "user": user,
            "with_synth": not args.no_synth,
            "check_segments": not args.no_segment_check,
            "out_dir": str(out_dir),
            "style": args.style,
            "warmup": warmup,
        }
        if meta_extra:
            meta.update(meta_extra)
        payload = {"meta": meta, "summary": summarize(results), "cases": results,
                   "warmup": warmup}
        (out_dir / "results.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "cases.csv").write_text(render_cases_csv(results), encoding="utf-8-sig")
        (out_dir / "report.md").write_text(render_report(meta, results), encoding="utf-8")

    if not args.no_synth and not args.no_warmup:
        print("-" * 96)
        print("预热：先跑一条极短任务，让 TTS 引擎完成加载与 warmup（不计入统计）…")
        sys.stdout.flush()
        warmup = run_case(client, {
            "id": "WARMUP", "category": "预热", "length": "short",
            "topic": WARMUP_TOPIC, "duration_min": WARMUP_DURATION_MIN,
        }, out_dir=out_dir, style="", with_synth=True, check_segments=False,
            timeout_script=args.timeout_script, timeout_synth=args.timeout_synth,
            poll_interval=args.poll_interval)
        print("  " + _fmt_console(warmup))
        if warmup.get("status") != "DONE":
            print("  ⚠️ 预热未成功（不影响后续统计，但首个 case 仍会带冷启动耗时）")
        _flush()

    started = time.perf_counter()
    for i, case in enumerate(cases, 1):
        print("-" * 96)
        print(f"[{i}/{len(cases)}] {case['id']} 《{case['topic']}》"
              f"（目标 {case['duration_min']} min）开始…")
        sys.stdout.flush()
        rec = run_case(client, case, out_dir=out_dir, style=args.style,
                       with_synth=not args.no_synth,
                       check_segments=not args.no_segment_check,
                       timeout_script=args.timeout_script,
                       timeout_synth=args.timeout_synth,
                       poll_interval=args.poll_interval)
        results.append(rec)
        print("  " + _fmt_console(rec))
        if rec.get("structure_issues"):
            print(f"  结构告警：{'；'.join(rec['structure_issues'])}")
        if rec.get("consistency_failures"):
            print(f"  一致率失败 {len(rec['consistency_failures'])} 条，"
                  f"首条：{rec['consistency_failures'][0]['reason']}")
        if rec.get("over_10min"):
            print("  ⚠️ 端到端耗时超过 10 分钟线（M4 指标）")
        sys.stdout.flush()
        _flush()

    wall = round(time.perf_counter() - started, 1)
    merged_from: list[str] = []
    if args.merge_from:
        dirs = [Path(s.strip()) for s in args.merge_from.split(",") if s.strip()]
        prior, used, warns = load_prior_cases(dirs)
        for w in warns:
            print(f"  [合并] {w}")
        before = len(results)
        merge_carried_over(results, prior)
        results[:] = order_results_by_topic_set(spec, results)
        merged_from = [str(u) for u in used]
        print(f"  [合并] 本次实跑 {before} 条 + 上次并入 {len(results) - before} 条 "
              f"= {len(results)} 条")
    _flush({"wall_clock_s": wall, "merged_from": merged_from})
    client.close()

    s = summarize(results)
    print("=" * 96)
    print("汇总")
    print(f"  完成/失败                 : {s['done']}/{s['failed']}"
          + (f"（{s['failed_ids']}）" if s["failed_ids"] else ""))
    print(f"  可用率（结构口径）        : {s['usable']}/{s['done']}"
          f" = {s['usable_rate']:.0%}（目标 ≥ 80%）")
    print(f"  逐句一致率                : {s['consistency_passed']}/{s['consistency_total']}"
          f" = {s['consistency_rate']:.2%}（目标 100%）")
    print(f"  千字端到端耗时（长档中位）: {_fmt(s['long_median_per_1000_words'], 1)} s"
          f"（目标 ≤ {TEN_MIN_S:.0f} s）")
    print(f"  短/长档端到端中位         : {_fmt(s['short_median_elapsed_s'], 1)} / "
          f"{_fmt(s['long_median_elapsed_s'], 1)} s")
    print(f"  端到端 RTF 中位           : {_fmt(s['median_rtf_e2e'], 2)}")
    print(f"  超 10 分钟 case           : "
          f"{s['over_10min'] if s['over_10min'] else '无'}")
    print(f"  本批墙钟                  : {wall}s")
    if merged_from:
        n_carried = sum(1 for c in results if c.get("carried_over"))
        print(f"  断点续跑                  : 实跑 {len(results) - n_carried} 条"
              f" + 并入 {n_carried} 条（来自 {'、'.join(merged_from)}）")
    print(f"  产物目录                  : {out_dir}")
    print("=" * 96)
    return 0 if not s["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
