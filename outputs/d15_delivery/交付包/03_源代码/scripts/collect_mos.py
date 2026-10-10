#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D11 MOS 评分回收与汇总（M4 指标之一：MOS 自然度 ≥ 3.5）。

立场声明（先读）
----------------
**本脚本不生成、不推测、不填补任何评分。** 它只做两件事：

  1. **校验**回收到的评分表是否**有资格参与判定**（人数 / 完整性 / 取值范围）；
  2. 在合格时算出均分与离散度，并给出达标判定。

读不到有效评分表时，它**明确报「未回收」并以退出码 2 结束**，绝不给一个
「N/A → 通过」的假绿。MOS 是 M4 的五项指标之一，**假绿比没有更糟**：
它会让「五项达标」的结论整体失真，而且没人会再去补做。

原始打分表一律保留（计划书 R5 要求），汇总只读不写。

用法
----
    # 默认从 MOS 包目录回收（文件名形如 mos_scoresheet_张三.csv）
    python scripts/collect_mos.py --dir outputs/eval/20260918_0920/mos

    # 也可逐个指定
    python scripts/collect_mos.py --sheet a.csv --sheet b.csv --sheet c.csv

退出码：0 = 已达判定条件（含达标/不达标两种结论都算「判完了」）；
        2 = 未回收或有效表不足，**判不了**。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

SCORE_COLS = ("自然度", "停顿节奏", "音色区分度")
PRIMARY_COL = "自然度"          # 验收线只看它
POLYPHONE_COL = "多音字问题"
SHEET_ID = "序号"

MIN_RATERS = 3                  # 计划书：3~5 人取均值，少于 3 人判不了
TARGET_MEAN = 3.5               # 验收线
TEMPLATE_HINT = "（模板，未填写）"


# --------------------------------------------------------------------------- #
# 读取与校验
# --------------------------------------------------------------------------- #

@dataclass
class Sheet:
    path: Path
    rows: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    blank: bool = False          # 完全是未填写的模板
    polyphone_flagged: list[str] = field(default_factory=list)


def _col(row: dict, prefix: str) -> str | None:
    """按前缀取列。列名带「(1-5)」这类后缀，且 BOM 会粘在第一列上，故不能用等值匹配。"""
    for k in row:
        if k and k.lstrip("\ufeff").startswith(prefix):
            return k
    return None


def _is_blank_row(row: dict) -> bool:
    for c in SCORE_COLS:
        k = _col(row, c)
        if k and str(row.get(k) or "").strip():
            return False
    return True


def load_sheet(path: Path) -> Sheet:
    """读一份评分表。**任何格式问题都记进 errors，不抛异常**（回收件质量参差）。"""
    sh = Sheet(path=path)
    try:
        # utf-8-sig：make_mos_pack 写出的表带 BOM，用 utf-8 读会把 BOM 粘进首列名
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if any(str(v or "").strip()
                                                         for v in r.values())]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        sh.errors.append(f"读取失败：{exc}")
        return sh

    if not rows:
        sh.blank = True
        return sh
    if all(_is_blank_row(r) for r in rows):
        sh.blank = True
        return sh

    for r in rows:
        seq = str(r.get(_col(r, SHEET_ID) or "", "") or "").strip() or "?"
        for c in SCORE_COLS:
            k = _col(r, c)
            raw = str(r.get(k) or "").strip() if k else ""
            if not raw:
                sh.errors.append(f"第 {seq} 项「{c}」为空")
                continue
            try:
                f = float(raw)
            except ValueError:
                sh.errors.append(f"第 {seq} 项「{c}」不是数字：{raw!r}")
                continue
            # 档位定义是 1~5 的**整数**（见 mos_README 的档位表）。
            # 这里必须显式拒绝 3.5 这类半整数，**不能 int() 截断** ——
            # 截断会把「填错」静默变成一个差一档的合法分，等于悄悄改分。
            if f != int(f):
                sh.errors.append(f"第 {seq} 项「{c}」应为整数档位（1~5），实际 {raw!r}")
                continue
            v = int(f)
            if not 1 <= v <= 5:
                sh.errors.append(f"第 {seq} 项「{c}」越界（应为 1~5）：{v}")

        pk = _col(r, POLYPHONE_COL)
        pv = str(r.get(pk) or "").strip() if pk else ""
        if not pv:
            sh.errors.append(f"第 {seq} 项「{POLYPHONE_COL}」未填（应填「有」或「无」）——"
                             "这一列是 bad case 修复的唯一输入")
        elif pv not in ("无", "没有", "否", "none", "no"):
            sh.polyphone_flagged.append(
                f"第 {seq} 项：{pv}｜备注：{str(r.get(_col(r, '备注') or '') or '').strip()}")

    if not sh.errors:
        sh.rows = rows
    return sh


def _scores(sheet: Sheet, col: str) -> list[int]:
    out = []
    for r in sheet.rows:
        k = _col(r, col)
        if k and str(r.get(k) or "").strip():
            out.append(int(float(str(r[k]).strip())))
    return out


# --------------------------------------------------------------------------- #
# 汇总与判定（纯函数，便于表驱动单测）
# --------------------------------------------------------------------------- #

def summarize(sheets: list[Sheet]) -> dict:
    """只汇总**完全合格**的表。有错的表不参与算分（错的分数比没有更危险）。"""
    valid = [s for s in sheets if s.rows and not s.errors]
    blank = [s for s in sheets if s.blank]
    invalid = [s for s in sheets if not s.blank and s.errors]

    per_dim: dict[str, dict] = {}
    for col in SCORE_COLS:
        vals = [v for s in valid for v in _scores(s, col)]
        per_dim[col] = {
            "n": len(vals),
            "mean": round(statistics.fmean(vals), 3) if vals else None,
            "sd": round(statistics.pstdev(vals), 3) if len(vals) > 1 else 0.0,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
        }

    # 评分人之间的一致性：按「人」算自然度均分，再看这批均分的离散度
    per_rater = [round(statistics.fmean(_scores(s, PRIMARY_COL)), 3)
                 for s in valid if _scores(s, PRIMARY_COL)]
    flagged = [f for s in valid for f in s.polyphone_flagged]

    return {
        "sheets_total": len(sheets),
        "sheets_valid": len(valid),
        "sheets_blank": len(blank),
        "sheets_invalid": len(invalid),
        "raters": [
            {"file": s.path.name,
             "lines": len(s.rows),
             "mean_primary": per_rater[i] if i < len(per_rater) else None}
            for i, s in enumerate(valid)
        ],
        "per_dim": per_dim,
        "rater_means": per_rater,
        "rater_spread": (round(max(per_rater) - min(per_rater), 3)
                         if len(per_rater) > 1 else 0.0),
        "polyphone_flagged": flagged,
        "errors": [{"file": s.path.name, "errors": s.errors} for s in invalid],
        "blank_files": [s.path.name for s in blank],
    }


def verdict(summary: dict) -> tuple[str, str]:
    """返回 (结论码, 人话)。结论码 ∈ {RECYCLED_PASS, RECYCLED_FAIL, NOT_RECYCLED}。

    **只有 RECYCLED_* 才是「判完了」。** 资料不足时一律 NOT_RECYCLED，
    并且**不给出任何均分结论** —— 缺数据时的「通过」是最危险的一种结论。
    """
    n = summary["sheets_valid"]
    if n == 0:
        msg = "**未回收**：没有读到任何有效评分表。"
        if summary["sheets_blank"]:
            msg += f"（发现 {summary['sheets_blank']} 份未填写的模板：{', '.join(summary['blank_files'])}）"
        if summary["sheets_invalid"]:
            msg += f"（另有 {summary['sheets_invalid']} 份填写不合格，见错误清单）"
        msg += " MOS 无法判定，M4 该指标待人工回收。"
        return "NOT_RECYCLED", msg
    if n < MIN_RATERS:
        return "NOT_RECYCLED", (f"**有效评分人不足**：{n} 人 < 计划书要求的 {MIN_RATERS} 人。"
                                "按 R5 的处置须凑满 ≥3 人后再判，本轮不给结论。")

    mean = summary["per_dim"][PRIMARY_COL]["mean"]
    if mean is None:
        return "NOT_RECYCLED", "有效表里没有任何「自然度」分值，无法判定。"
    if mean >= TARGET_MEAN:
        return "RECYCLED_PASS", (f"自然度均分 **{mean} ≥ {TARGET_MEAN}**（{n} 人），"
                                 f"评分人间极差 {summary['rater_spread']}。**达标**。")
    return "RECYCLED_FAIL", (f"自然度均分 **{mean} < {TARGET_MEAN}**（{n} 人）。"
                            "按 R5 处置：优先修 bad case（多音字词典 → 停顿策略 → 句长 → 语速）。")


# --------------------------------------------------------------------------- #
# 书面记录（D11 的 Done 要求「MOS ≥ 3.5 书面记录」）
# --------------------------------------------------------------------------- #

def render_markdown(summary: dict, code: str, note: str, source: str) -> str:
    out: list[str] = ["# D11 MOS 评分书面记录\n",
                      f"- 结论：**{code}**",
                      f"- 说明：{note}",
                      f"- 评分表来源：{source}",
                      f"- 回收情况：共 {summary['sheets_total']} 份文件 ｜ "
                      f"有效 {summary['sheets_valid']} ｜ 空白模板 {summary['sheets_blank']} ｜ "
                      f"不合格 {summary['sheets_invalid']}",
                      ""]
    out.append("## 一、各维度结果\n")
    if summary["sheets_valid"] == 0:
        out.append("**无有效数据，不给出任何均分。**\n")
    else:
        out.append("| 维度 | 样本数 | 均分 | 标准差 | 最小 | 最大 |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for col, d in summary["per_dim"].items():
            tag = "（主指标，验收线 ≥ 3.5）" if col == PRIMARY_COL else ""
            out.append(f"| {col}{tag} | {d['n']} | {d['mean']} | {d['sd']} | "
                       f"{d['min']} | {d['max']} |")
        out.append("")

    out.append("## 二、评分人明细\n")
    if not summary["raters"]:
        out.append("无。\n")
    else:
        out.append("| 文件 | 项数 | 自然度均分 |")
        out.append("| --- | --- | --- |")
        for r in summary["raters"]:
            out.append(f"| {r['file']} | {r['lines']} | {r['mean_primary']} |")
        out.append(f"\n评分人间极差：**{summary['rater_spread']}**"
                   "（过大说明评分标准未对齐，需回看评分说明再判）\n")

    out.append("## 三、多音字问题（bad case 的唯一输入）\n")
    if not summary["polyphone_flagged"]:
        out.append("未报出多音字问题。" if summary["sheets_valid"] else "无数据。")
        out.append("")
    else:
        for f in summary["polyphone_flagged"]:
            out.append(f"- {f}")
        out.append("")

    out.append("## 四、填写不合格的表（未参与算分）\n")
    if not summary["errors"]:
        out.append("无。")
    else:
        for e in summary["errors"]:
            out.append(f"- `{e['file']}`：{len(e['errors'])} 处问题")
            for msg in e["errors"][:10]:
                out.append(f"    - {msg}")
            if len(e["errors"]) > 10:
                out.append(f"    - …另有 {len(e['errors']) - 10} 处")
    out.append("")

    out.append("## 五、口径声明\n")
    out.append("1. **本记录不含任何推测分**。所有数字都来自回收到的原始评分表，原始表按要求保留。")
    out.append(f"2. 结论码 `{code}`：只有 `RECYCLED_PASS` / `RECYCLED_FAIL` 表示"
               "「已判定」；`NOT_RECYCLED` 表示资料不足、**判不了**，"
               "此时**不给出均分**，也不得据此声称 M4 该项达标。")
    out.append(f"3. 验收线：自然度均分 ≥ {TARGET_MEAN}；人数 ≥ {MIN_RATERS}（计划书 7.2 / R5）。")
    out.append("4. 本脚本只读评分表、只写汇总产物，**不回写任何原始评分**。")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def collect_sheets(dir_path: Path | None, files: list[str]) -> list[Path]:
    """收集评分表：显式指定的 + 目录下的 `mos_scoresheet*.csv`（模板自身也会被读到，
    由 `load_sheet` 判为 blank 后单独列出）。"""
    found: list[Path] = [Path(f) for f in files]
    if dir_path is not None:
        found += sorted(p for p in dir_path.glob("mos_scoresheet*.csv"))
    # 去重且保持顺序
    seen, uniq = set(), []
    for p in found:
        key = str(p.resolve()).lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return uniq


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="D11 MOS 评分回收与汇总（不生成任何评分）")
    ap.add_argument("--dir", default=None, help="MOS 包目录（自动找 mos_scoresheet*.csv）")
    ap.add_argument("--sheet", action="append", default=[], help="显式指定评分表，可重复")
    ap.add_argument("--out", default=None, help="书面记录输出路径（默认 <dir>/mos_summary.md）")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    args = build_argparser().parse_args(argv)
    if not args.dir and not args.sheet:
        # 零输入是最常见的调用事故：不给路径就什么也读不到，必须明确报错而不是「通过」
        print("必须给出 --dir 或至少一个 --sheet。")
        return 2

    paths = collect_sheets(Path(args.dir) if args.dir else None, args.sheet)
    if not paths:
        print(f"在 {args.dir} 下没找到任何 mos_scoresheet*.csv —— 未回收。")
        return 2

    sheets = [load_sheet(p) for p in paths]
    summary = summarize(sheets)
    code, note = verdict(summary)
    source = args.dir or "、".join(p.name for p in paths)
    md = render_markdown(summary, code, note, source)

    out_dir = Path(args.dir) if args.dir else Path("outputs/mos")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / "mos_summary.md"
    out_path.write_text(md, encoding="utf-8")
    (out_dir / "mos_summary.json").write_text(
        json.dumps({"code": code, "note": note, "summary": summary},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"评分表 {summary['sheets_total']} 份：有效 {summary['sheets_valid']}｜"
          f"空白模板 {summary['sheets_blank']}｜不合格 {summary['sheets_invalid']}")
    print(f"结论：{code}")
    print(note)
    print(f"书面记录：{out_path}")
    return 0 if code.startswith("RECYCLED") else 2


if __name__ == "__main__":
    raise SystemExit(main())
