# -*- coding: utf-8 -*-
"""把 `.slide` 的页脚页码与文件编号对齐，并统一总页数。

为什么需要它
------------
每页 C 区页脚写着 `<Text …>07 / 12</Text>` —— 分子是**手写死的**，不是算出来的。
一旦增删页面（本次在第 02 章末插入一页），后面所有页的分子要顺延、**全部页的分母**要改，
13 个文件 26 处数字靠手改必错；而且错了没有任何测试会红 —— 页脚不是内容，
lint 只查语法与溢出。

更阴的一层：正文里也会出现 `NN / MM` 形态的数据（指标页的**可用率 `12 / 12`**）。
按「全篇只有一处」去找页脚，会把这条**指标数据**当成页码改掉。所以本脚本以
`{/* C · 页脚条 */}` 注释为锚点，**只在锚点之后**定位与改写。

用法
----
    python scripts/renumber_slides.py <slides_dir> --check          # 只读
    python scripts/renumber_slides.py <slides_dir> --renumber --total 13
    python scripts/renumber_slides.py --self-test                   # 注入式自检

约定
----
* 页脚形如 `NN / MM`（两位数），位于 C 区锚点之后；有锚点的页必须**恰好一处**。
* 分子必须等于文件名编号（`09.slide` → `09`）。
* 封面 / 结束页**结构上就没有页脚条**（无锚点）—— 不算缺陷，但此时也不允许残留
  页脚文案「双人对话播客自动生成系统 · 项目答辩」，否则说明页脚条被删了一半。
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

ANCHOR = "{/* C · 页脚条 */}"
FOOT_RE = re.compile(r">(\d{2}) / (\d{2})</Text>")
FOOT_MARK = "双人对话播客自动生成系统 · 项目答辩"


def slide_files(d: str) -> list[str]:
    return sorted(glob.glob(os.path.join(d, "[0-9][0-9].slide")))


def split_footer(text: str) -> tuple[str | None, str]:
    """返回 (页脚区, 其余)。无锚点时页脚区为 None。"""
    i = text.find(ANCHOR)
    if i < 0:
        return None, text
    return text[i:], text[:i]


def check(d: str) -> int:
    """分子 == 文件名编号；全篇分母一致；无锚点页不得残留页脚文案。"""
    bad: list[str] = []
    totals: set[str] = set()
    no_footer: list[str] = []
    files = slide_files(d)
    if not files:
        print("目录里没有 NN.slide：%s" % d)
        return 1
    for p in files:
        name = os.path.basename(p)
        want = name[:2]
        text = open(p, encoding="utf-8").read()
        foot, body = split_footer(text)
        if foot is None:
            no_footer.append(name)
            if FOOT_MARK in body:
                bad.append("%s 无页脚锚点，却残留页脚文案（页脚条被删了一半？）" % name)
            continue
        hits = FOOT_RE.findall(foot)
        if len(hits) != 1:
            bad.append("%s 页脚区出现 %d 处页码（应为 1）" % (name, len(hits)))
            continue
        num, tot = hits[0]
        totals.add(tot)
        if num != want:
            bad.append("%s 页脚分子是 %s，与文件名不符" % (name, num))
    if len(totals) != 1:
        bad.append("分母不一致：%s" % sorted(totals))
    print("检查 %d 个文件；有页脚 %d 页（分母 %s）；无页脚 %d 页 %s"
          % (len(files), len(files) - len(no_footer), sorted(totals), len(no_footer), no_footer))
    if bad:
        for b in bad:
            print("  !! %s" % b)
        return 1
    print("分子 == 文件名编号，逐页一致 ✅")
    return 0


def renumber(d: str, total: int) -> int:
    files = slide_files(d)
    if not files:
        print("目录里没有 NN.slide：%s" % d)
        return 1
    n = 0
    for p in files:
        name = os.path.basename(p)
        want = name[:2]
        text = open(p, encoding="utf-8").read()
        foot, body = split_footer(text)
        if foot is None:
            print("  %s  （本页无页脚，跳过）" % name)
            continue
        hits = FOOT_RE.findall(foot)
        if len(hits) != 1:
            print("  !! %s 页脚区出现 %d 处，跳过" % (name, len(hits)))
            continue
        old = "%s / %s" % hits[0]
        new = "%s / %02d" % (want, total)
        if old == new:
            continue
        text = body + FOOT_RE.sub(lambda m: ">%s</Text>" % new, foot, count=1)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print("  %s  %s → %s" % (name, old, new))
        n += 1
    print("改写了 %d 个文件（共 %d 页）" % (n, len(files)))
    return 0


def self_test() -> int:
    import shutil
    import tempfile

    results: list[bool] = []

    def check_it(name: str, ok: bool, note: str = "") -> None:
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + note) if note else ""))
        results.append(bool(ok))

    with tempfile.TemporaryDirectory() as td:
        def put(i: int, num: str, tot: str, body: str = "", anchor: bool = True) -> None:
            p = os.path.join(td, "%02d.slide" % i)
            foot = (ANCHOR + "<Text>%s / %s</Text>" % (num, tot)) if anchor else ""
            with open(p, "w", encoding="utf-8") as f:
                f.write("<Slide>%s%s</Slide>" % (body, foot))

        def fresh() -> None:
            shutil.rmtree(td, ignore_errors=True)
            os.makedirs(td, exist_ok=True)

        # T1 分子与文件名不符必须判红（这正是加页后最容易犯的错）
        put(1, "01", "12")
        put(2, "03", "12")
        rc = check(td)
        check_it("T1 分子与文件名不符会被判红", rc == 1, "rc=%d" % rc)

        # T2 分母不一致必须判红
        fresh()
        put(1, "01", "12")
        put(2, "02", "13")
        rc = check(td)
        check_it("T2 分母不一致会被判红", rc == 1, "rc=%d" % rc)

        # T3 全对时必须判绿（判据只会红就没用了）
        fresh()
        put(1, "01", "13")
        put(2, "02", "13")
        rc = check(td)
        check_it("T3 完全一致时判绿", rc == 0, "rc=%d" % rc)

        # T4 有锚点但页脚缺失必须判红
        fresh()
        put(1, "01", "13", anchor=False)
        with open(os.path.join(td, "01.slide"), "w", encoding="utf-8") as f:
            f.write("<Slide>%s<Text>没有页码</Text></Slide>" % ANCHOR)
        rc = check(td)
        check_it("T4 有锚点却无页码会被判红", rc == 1, "rc=%d" % rc)

        # T5 改写后分子跟着文件名走、分母统一
        fresh()
        put(1, "01", "12")
        put(10, "09", "12")        # 模拟「原 09 页重命名为 10」
        renumber(td, 13)
        s1 = open(os.path.join(td, "01.slide"), encoding="utf-8").read()
        s10 = open(os.path.join(td, "10.slide"), encoding="utf-8").read()
        check_it("T5 改写后 01→01/13、10→10/13",
                 "01 / 13" in s1 and "10 / 13" in s10 and "09 / 12" not in s10, repr(s10[-40:]))

        # T6 锚点之前的正文 `12 / 12`（指标页可用率）绝不能被当成页码改掉
        fresh()
        put(1, "01", "12", body="<Text>可用率 12 / 12 = 100%</Text>")
        renumber(td, 13)
        s = open(os.path.join(td, "01.slide"), encoding="utf-8").read()
        check_it("T6 锚点前的正文数据 `12 / 12` 不被误改",
                 "可用率 12 / 12 = 100%" in s and "01 / 13" in s, repr(s))

        # T6b 同一文件里正文与页脚同时存在 `NN / MM` 时，只改页脚
        fresh()
        put(1, "01", "12", body="<Text>可用率 12 / 12</Text>")
        put(2, "02", "12")
        renumber(td, 13)
        s = open(os.path.join(td, "01.slide"), encoding="utf-8").read()
        check_it("T6b 同页正文+页脚并存时只改页脚",
                 s.count("12 / 12") == 1 and s.count("01 / 13") == 1, repr(s))

        # T7 无锚点页（封面/结束页）合法：不判红、不计入分母
        fresh()
        put(1, "01", "13", anchor=False)
        put(2, "02", "13")
        rc = check(td)
        check_it("T7 无页脚页（封面/结束页）合法，不判红", rc == 0, "rc=%d" % rc)

        # T8 无锚点却残留页脚文案 → 红（页脚条被删了一半）
        fresh()
        with open(os.path.join(td, "01.slide"), "w", encoding="utf-8") as f:
            f.write("<Slide><Text>%s</Text></Slide>" % FOOT_MARK)
        put(2, "02", "13")
        rc = check(td)
        check_it("T8 无锚点却残留页脚文案会被判红", rc == 1, "rc=%d" % rc)

    n_ok = sum(1 for r in results if r)
    print("\n自检结论：%d/%d 如期通过" % (n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="对齐 .slide 页脚页码与文件编号")
    ap.add_argument("slides_dir", nargs="?", help="存放 NN.slide 的目录")
    ap.add_argument("--check", action="store_true", help="只读校验")
    ap.add_argument("--renumber", action="store_true", help="按文件名重写分子，分母置为 --total")
    ap.add_argument("--total", type=int, default=0, help="总页数（写入分母）")
    ap.add_argument("--self-test", action="store_true", help="注入式自检")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.slides_dir:
        ap.error("需要 slides_dir（或 --self-test）")
    if args.check:
        return check(args.slides_dir)
    if args.renumber:
        if args.total <= 0:
            ap.error("--renumber 需要 --total N")
        rc = renumber(args.slides_dir, args.total)
        if rc != 0:
            return rc
        return check(args.slides_dir)
    ap.error("请指定 --check 或 --renumber")
    return 2


if __name__ == "__main__":
    sys.exit(main())
