#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查 .slide DSL 里文字与背景的对比度（WCAG 2.1），专抓「文字被容器压暗」。

## 为什么需要它

`slidep lint` 只判语法与溢出，判不了「看不看得清」。曾经踩过：

    <Box style={{ background: '#15171B', opacity: 0.06 }}>   // 想做个淡灰底
        <Text style={{ color: '#15171B' }}>写脚本</Text>       // 结果自己也变成 6% 不透明
    </Box>

`opacity` 是**组不透明度**，作用在整棵子树上：容器和里面的文字一起被压向背景色，
于是「深灰字 + 淡灰底」双双变成接近白色 —— 白底上的白字。溢出检查全过，
只有人眼能看出来。这条判据就是把它变成机器能判的。

## 合成模型（近似，且偏保守）

沿根到文字的路径累乘 `opacity` 得 A，取最近祖先的背景色 B（缺省纸白 `#FFFFFF`）：

    文字最终色 = A·T + (1−A)·P
    文字处底色 = A·B + (1−A)·P        （P = 页面背景，缺省 #FFFFFF）

这忽略了多层嵌套组的精确逐层合成，但对「底色都是近白、组不深」的情形足够准确，
且误差方向是**更容易报警**（宁可多报）。真要精确合成需按层从内到外复利，本工具箱用不到。

## 判据

WCAG 2.1 AA：普通文字 ≥ 4.5:1；大号文字（≥ 24px，或 ≥ 18.66px 且加粗）≥ 3:1。

- `FAIL` = 低于大号门槛（< 3.0）→ 一定是看不见，必须改。
- `WARN` = 未达普通门槛但过了大号门槛（3.0 ≤ r < 4.5，且是小字）→ 投影仍可能吃力。
- 退出码：有 FAIL → 1；只有 WARN → 0（但打印出来）。

## 用法

    python scripts/check_slide_contrast.py <slides目录> [--min-ratio 4.5]
    python scripts/check_slide_contrast.py --self-test
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from slide_dsl_to_html import COMMENT_RE, NOTES_RE, Node, parse  # noqa: E402

PAGE_BG = (255.0, 255.0, 255.0)
LARGE_RATIO = 3.0


def parse_color(v: str) -> tuple[float, float, float] | None:
    v = (v or "").strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        h = m.group(1)
        return (float(int(h[0:2], 16)), float(int(h[2:4], 16)), float(int(h[4:6], 16)))
    m = re.fullmatch(r"#([0-9a-fA-F]{3})", v)
    if m:
        h = m.group(1)
        return tuple(float(int(c * 2, 16)) for c in h)  # type: ignore[return-value]
    return None


def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(c: tuple[float, float, float]) -> float:
    r, g, b = (_lin(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def composite(alpha: float, top: tuple[float, float, float], bottom: tuple[float, float, float]):
    return tuple(alpha * top[i] + (1 - alpha) * bottom[i] for i in range(3))


def is_large(font_size: float, bold: bool) -> bool:
    return font_size >= 24 or (bold and font_size >= 18.66)


def text_of(node: Node) -> str:
    out = []
    for c in node.children:
        if isinstance(c, str):
            out.append(c)
    return "".join(out).strip()


def scan_file(path: str, min_ratio: float) -> list[dict]:
    src = NOTES_RE.sub("", COMMENT_RE.sub("", open(path, encoding="utf-8").read()))
    root = parse(src)
    issues: list[dict] = []

    def walk(node: Node, alpha: float, bg: tuple[float, float, float]) -> None:
        st = node.style()
        a = alpha * float(st.get("opacity", 1) or 1)
        c = parse_color(st.get("background", ""))
        inner_bg = c if c is not None else bg

        if node.tag == "Text" and text_of(node):
            t = parse_color(st.get("color", ""))
            if t is not None:
                fs = st.get("font-size", "14px").replace("px", "")
                try:
                    fs_v = float(fs)
                except ValueError:
                    fs_v = 14.0
                bold = st.get("font-weight", "") in ("bold", "700", "600")
                t_eff = composite(a, t, PAGE_BG)
                b_eff = composite(a, inner_bg, PAGE_BG)
                ratio = contrast(t_eff, b_eff)
                thr = LARGE_RATIO if is_large(fs_v, bold) else min_ratio
                if ratio < thr:
                    issues.append({
                        "file": os.path.basename(path),
                        "text": text_of(node)[:26],
                        "font": fs_v,
                        "bold": bold,
                        "color": st.get("color"),
                        "bg": st.get("background", "（继承）"),
                        "alpha": round(a, 3),
                        "ratio": round(ratio, 2),
                        "need": thr,
                        "level": "FAIL" if ratio < LARGE_RATIO else "WARN",
                    })

        for c2 in node.children:
            if isinstance(c2, Node):
                walk(c2, a, inner_bg)

    walk(root, 1.0, PAGE_BG)
    return issues


def run(slides_dir: str, min_ratio: float) -> int:
    files = sorted(glob.glob(os.path.join(slides_dir, "*.slide")))
    if not files:
        print("没有 .slide：%s" % slides_dir)
        return 2
    allissues: list[dict] = []
    for f in files:
        allissues += scan_file(f, min_ratio)

    if not allissues:
        print("对比度：%d 页全部通过（普通文字 ≥ %.1f:1，大号 ≥ %.1f:1）"
              % (len(files), min_ratio, LARGE_RATIO))
        return 0

    fails = [i for i in allissues if i["level"] == "FAIL"]
    warns = [i for i in allissues if i["level"] == "WARN"]
    for i in fails + warns:
        print("%-4s %-9s %-26s %gpx%s  色 %s / 底 %s  有效不透明 %.2f  对比度 %.2f (需 %.1f)"
              % (i["level"], i["file"], i["text"], i["font"], " 粗" if i["bold"] else "",
                 i["color"], i["bg"], i["alpha"], i["ratio"], i["need"]))
    print()
    print("FAIL %d 项 / WARN %d 项（共 %d 页）" % (len(fails), len(warns), len(files)))
    return 1 if fails else 0


def self_test() -> int:
    """注入式自检：把每种判据都故意破坏一次，必须变红。"""
    import tempfile

    ok, bad = 0, []

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        if cond:
            ok += 1
            print("  PASS  %s" % name)
        else:
            bad.append(name)
            print("  FAIL  %s  %s" % (name, extra))

    def scan(snippet: str, min_ratio: float = 4.5) -> list[dict]:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.slide")
            with open(p, "w", encoding="utf-8") as f:
                f.write(snippet)
            return scan_file(p, min_ratio)

    print("=== check_slide_contrast --self-test ===")

    buggy = ("<Slide style={{ background: '#FFFFFF' }}>"
             "<Box style={{ background: '#15171B', opacity: 0.06 }}>"
             "<Text style={{ fontSize: 15, color: '#15171B' }}>写脚本</Text>"
             "</Box></Slide>")
    r = scan(buggy)
    check("T1 容器 opacity 压暗文字被抓（本次真实缺陷）", len(r) == 1 and r[0]["level"] == "FAIL",
          r)

    good = ("<Slide style={{ background: '#FFFFFF' }}>"
            "<Box style={{ background: '#EEF0F3' }}>"
            "<Text style={{ fontSize: 15, color: '#15171B' }}>写脚本</Text>"
            "</Box></Slide>")
    check("T2 实色淡底 + 深字 通过", scan(good) == [], scan(good))

    tiny = ("<Slide style={{ background: '#FFFFFF' }}>"
            "<Text style={{ fontSize: 12, color: '#6E7480' }}>x</Text></Slide>")
    check("T3 小字 4.0:1 报 WARN（未达 4.5）",
          len(scan(tiny, 4.9)) == 1 and scan(tiny, 4.9)[0]["level"] == "WARN", scan(tiny, 4.9))

    # 同一个颜色（约 3.2:1）在两档字号下必须给出不同结论——这才证明门槛真的在起作用
    sw_big = ("<Slide style={{ background: '#FFFFFF' }}>"
              "<Text style={{ fontSize: 34, color: '#8A9098' }}>标题</Text></Slide>")
    sw_small = ("<Slide style={{ background: '#FFFFFF' }}>"
                "<Text style={{ fontSize: 12, color: '#8A9098' }}>脚注</Text></Slide>")
    r_big, r_small = scan(sw_big), scan(sw_small)
    check("T4 大号字门槛放宽（同一 3.2:1 色：34px 放行 / 12px 报警）",
          r_big == [] and len(r_small) == 1 and r_small[0]["level"] == "WARN",
          (r_big, r_small))

    check("T5 对比度算法正确：黑白 = 21:1",
          abs(contrast((0, 0, 0), (255, 255, 255)) - 21.0) < 0.01,
          contrast((0, 0, 0), (255, 255, 255)))
    check("T6 六位/三位十六进制都能解析",
          parse_color("#fff") == (255.0, 255.0, 255.0) and parse_color("#15171B") == (21.0, 23.0, 27.0))
    check("T7 非颜色值不误判", parse_color("rgba(0,0,0,.5)") is None and parse_color("") is None)

    print()
    print("结果：%d 通过 / %d 失败" % (ok, len(bad)))
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=".slide 文字对比度检查（WCAG 2.1）")
    ap.add_argument("slides_dir", nargs="?")
    ap.add_argument("--min-ratio", type=float, default=4.5)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.slides_dir:
        ap.print_usage()
        return 2
    return run(args.slides_dir, args.min_ratio)


if __name__ == "__main__":
    sys.exit(main())
