# -*- coding: utf-8 -*-
"""`scripts/collect_mos.py` 的单元测试。

MOS 是 M4 的五项指标之一，而它有**两种截然不同的失败方式**，本文件各钉一半：

  1. **假绿**：没有有效数据时给出「通过」—— 比没有结论更糟，因为没人会再补做。
     所以「未回收」「人数不足」「表填写不合格」三种情形都必须给出 `NOT_RECYCLED`，
     且书面记录里**不得出现任何均分**。
  2. **悄悄改分**：把 `3.5` 截断成 `3`、把越界值当合法值、把不合格表的分数混进均值。
     这些都必须在读取阶段就报错，而不是「尽量算出一个数」。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import collect_mos as cm  # noqa: E402

HEAD = ["序号", "编号", "类别", "主题", "片段位置",
        "自然度(1-5)", "停顿节奏(1-5)", "音色区分度(1-5)", "多音字问题(有/无)", "备注"]


def _sheet(path: Path, rows: list[list], *, bom: bool = True,
           header: list[str] | None = None) -> Path:
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header or HEAD)
        w.writerows(rows)
    return path


def _rows(*, nat=4, pause=4, timbre=4, poly="无", note="", n=12, blank=False) -> list[list]:
    out = []
    for i in range(1, n + 1):
        if blank:
            out.append([i, f"K{i}", "知识科普", "主题", "20–65s", "", "", "", "", ""])
        else:
            out.append([i, f"K{i}", "知识科普", "主题", "20–65s", nat, pause, timbre, poly, note])
    return out


def _sheets(tmp_path: Path, *specs) -> list[cm.Sheet]:
    made = []
    for i, spec in enumerate(specs):
        p = _sheet(tmp_path / f"mos_scoresheet_r{i}.csv", spec)
        made.append(cm.load_sheet(p))
    return made


# --------------------------------------------------------------------------- #
# 1. 假绿防线
# --------------------------------------------------------------------------- #

def test_blank_template_is_not_recycled(tmp_path: Path) -> None:
    """未填写的模板必须被识别为空白，**绝不能被当成一份全 0 分的有效表**。"""
    p = _sheet(tmp_path / "mos_scoresheet.csv", _rows(blank=True))
    sh = cm.load_sheet(p)
    assert sh.blank, "空模板应判为 blank"
    assert sh.errors == [], "空模板不该被报「填写错误」，它是没填，不是填错"

    summary = cm.summarize([sh])
    assert summary["sheets_valid"] == 0
    code, note = cm.verdict(summary)
    assert code == "NOT_RECYCLED"
    assert "未回收" in note


def test_not_recycled_report_contains_no_mean(tmp_path: Path) -> None:
    """**核心**：判不了的时候，书面记录里不许出现均分 —— 有数字就会被引用。"""
    p = _sheet(tmp_path / "mos_scoresheet.csv", _rows(blank=True))
    summary = cm.summarize([cm.load_sheet(p)])
    code, note = cm.verdict(summary)
    md = cm.render_markdown(summary, code, note, "测试")
    assert "**无有效数据，不给出任何均分。**" in md
    assert "均分 **" not in md          # 唯独不许出现「均分 **X**」这种可直接引用的结论


def test_fewer_than_three_raters_is_not_recycled(tmp_path: Path) -> None:
    for n in (1, 2):
        summary = cm.summarize(_sheets(tmp_path, *[_rows()] * n)[:n])
        code, note = cm.verdict(summary)
        assert code == "NOT_RECYCLED", f"{n} 人也应判不了"
        assert "不足" in note
        assert "3" in note


def test_invalid_sheet_does_not_count_toward_raters(tmp_path: Path) -> None:
    """不合格的表**不参与算分、也不算人头** —— 否则一份乱填的表就能凑够 3 人。"""
    bad = _rows()
    bad[3][5] = 9                      # 自然度越界
    summary = cm.summarize(_sheets(tmp_path, _rows(), _rows(), bad))
    assert summary["sheets_valid"] == 2
    assert summary["sheets_invalid"] == 1
    code, _ = cm.verdict(summary)
    assert code == "NOT_RECYCLED"


def test_main_returns_2_when_nothing_to_read(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cm.main(["--dir", str(empty)]) == 2


def test_main_requires_some_input() -> None:
    """零参数调用是最常见的操作事故：必须报错，而不是「扫了个空目录 → 通过」。"""
    assert cm.main([]) == 2


# --------------------------------------------------------------------------- #
# 2. 合格数据的判定
# --------------------------------------------------------------------------- #

def test_three_raters_at_4_passes(tmp_path: Path) -> None:
    summary = cm.summarize(_sheets(tmp_path, _rows(nat=4), _rows(nat=4), _rows(nat=4)))
    code, note = cm.verdict(summary)
    assert code == "RECYCLED_PASS"
    assert summary["per_dim"]["自然度"]["mean"] == 4.0
    assert "达标" in note


def test_mean_of_3_and_4_is_3_5_and_passes(tmp_path: Path) -> None:
    """均分可以是半整数（跨人平均），验收线是闭区间下界。"""
    summary = cm.summarize(_sheets(tmp_path, _rows(nat=3), _rows(nat=4), _rows(nat=4)))
    assert summary["per_dim"]["自然度"]["mean"] == round((3 + 4 + 4) / 3, 3)
    assert cm.verdict(summary)[0] == "RECYCLED_PASS"


def test_three_raters_at_3_fails(tmp_path: Path) -> None:
    summary = cm.summarize(_sheets(tmp_path, _rows(nat=3), _rows(nat=3), _rows(nat=3)))
    code, note = cm.verdict(summary)
    assert code == "RECYCLED_FAIL"
    assert "R5" in note, "不达标时必须指向计划书 R5 的处置路径"


def test_primary_metric_ignores_other_dimensions(tmp_path: Path) -> None:
    """验收线只看「自然度」；其余维度是诊断用的，不得左右判定。"""
    rows = _rows(nat=5, pause=1, timbre=1)
    summary = cm.summarize(_sheets(tmp_path, rows, rows, rows))
    assert summary["per_dim"]["自然度"]["mean"] == 5.0
    assert summary["per_dim"]["停顿节奏"]["mean"] == 1.0
    assert cm.verdict(summary)[0] == "RECYCLED_PASS"


def test_bom_and_parenthesised_headers_are_handled(tmp_path: Path) -> None:
    """真实表是 `utf-8-sig` 写出、列名带「(1-5)」后缀，匹配必须按前缀。"""
    p = _sheet(tmp_path / "s.csv", _rows(nat=5), bom=True)
    sh = cm.load_sheet(p)
    assert not sh.errors, sh.errors
    assert cm._scores(sh, "自然度") == [5] * 12


# --------------------------------------------------------------------------- #
# 3. 不许悄悄改分
# --------------------------------------------------------------------------- #

def test_fractional_score_is_rejected_not_truncated(tmp_path: Path) -> None:
    """`3.5` 不是合法档位。**必须报错，不许截断成 3** —— 截断等于悄悄改分。"""
    bad = _rows()
    bad[0][5] = 3.5
    sh = cm.load_sheet(_sheet(tmp_path / "s.csv", bad))
    assert not sh.rows, "有错就不该参与算分"
    assert any("整数档位" in e for e in sh.errors), sh.errors


def test_out_of_range_score_is_rejected(tmp_path: Path) -> None:
    for v in (0, 6, -1):
        bad = _rows()
        bad[0][5] = v
        sh = cm.load_sheet(_sheet(tmp_path / f"s{v}.csv", bad))
        assert any("越界" in e or "整数档位" in e for e in sh.errors), (v, sh.errors)


def test_missing_score_is_rejected(tmp_path: Path) -> None:
    bad = _rows()
    bad[2][6] = ""                     # 停顿节奏 留空
    sh = cm.load_sheet(_sheet(tmp_path / "s.csv", bad))
    assert any("停顿节奏」为空" in e for e in sh.errors), sh.errors


def test_polyphone_column_is_mandatory(tmp_path: Path) -> None:
    """多音字列是 D11「bad case 修复」的唯一输入，空着就等于这次评测白做。"""
    bad = _rows()
    bad[5][8] = ""
    sh = cm.load_sheet(_sheet(tmp_path / "s.csv", bad))
    assert any("多音字问题」未填" in e for e in sh.errors), sh.errors


def test_non_numeric_score_is_rejected(tmp_path: Path) -> None:
    bad = _rows()
    bad[0][5] = "不错"
    sh = cm.load_sheet(_sheet(tmp_path / "s.csv", bad))
    assert any("不是数字" in e for e in sh.errors), sh.errors


# --------------------------------------------------------------------------- #
# 4. bad case 收集
# --------------------------------------------------------------------------- #

def test_polyphone_flags_are_collected_with_note(tmp_path: Path) -> None:
    rows = _rows()
    rows[2][8] = "有"
    rows[2][9] = "第 3 声后 12 秒，「重庆」读成重(zhòng)庆"
    summary = cm.summarize(_sheets(tmp_path, rows, _rows(), _rows()))
    flagged = summary["polyphone_flagged"]
    assert len(flagged) == 1
    assert "重庆" in flagged[0]

    md = cm.render_markdown(summary, "RECYCLED_PASS", "说明", "测试")
    assert "重庆" in md


def test_verdict_codes_are_exhaustive(tmp_path: Path) -> None:
    """三个结论码必须覆盖「判完了」与「判不了」两类，且不许有第四种含糊态。"""
    blank = cm.summarize([cm.load_sheet(_sheet(tmp_path / "b.csv", _rows(blank=True)))])
    under = cm.summarize(_sheets(tmp_path, _rows()))
    good = cm.summarize(_sheets(tmp_path, _rows(nat=5), _rows(nat=5), _rows(nat=5)))
    low = cm.summarize(_sheets(tmp_path, _rows(nat=2), _rows(nat=2), _rows(nat=2)))
    assert cm.verdict(blank)[0] == "NOT_RECYCLED"
    assert cm.verdict(under)[0] == "NOT_RECYCLED"
    assert cm.verdict(good)[0] == "RECYCLED_PASS"
    assert cm.verdict(low)[0] == "RECYCLED_FAIL"
