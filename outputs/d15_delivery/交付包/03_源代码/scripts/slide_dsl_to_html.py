#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 slidep 的 .slide DSL 机械转译成 HTML，用于「肉眼版式核查」。

## 为什么需要这个脚本

`slidep screenshot`（依赖本地 editor_sdk）在本机会稳定报错：

    render failed: The argument 'filename' must be a file URL object, ...
    Received 'file:///workspace/node_modules/.pnpm/@tencent+docs-slide-view-sdk-node@...'

这是 SDK 在 `/workspace` 下的路径解析缺陷，与本项目 pptx 无关（换任意页、任意路径都一样）。
而 `slidep lint` 只能判「有没有溢出」，判不了「好不好看」——白底、层级、留白这些
视觉主张必须真的看见才算验证过。

所以补一条**独立**的观察通路：把 DSL 按语法机械映射成 HTML/CSS，用本机 Edge 渲染截图。
它不是 PowerPoint 的忠实还原（字体回退、行高度量会有差异），但足够回答
「构图是否平衡、层级是否清楚、有没有明显错位」。

## 映射规则（机械，不做设计决定）

    <Slide>  ->  div.slide（1280x720, box-sizing:border-box, display:flex, 默认 column）
    <Box>    ->  div（display:flex，默认 column）
    <Text>   ->  div（block，保留 width/height/字号/行高等）
    <Image>  ->  <img>（object-fit: cover）
    <br />   ->  <br/>
    <span>   ->  <span>

style={{...}} 的 JS 对象字面量按「顶层逗号切分 + 首个冒号切分」解析，
camelCase 转 kebab-case，无单位数字补 px（opacity/flex/lineHeight/fontWeight/zIndex 除外）。

## 用法

    python scripts/slide_dsl_to_html.py <slides目录> <输出.html> [--assets <assets目录>]
    python scripts/slide_dsl_to_html.py --self-test
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys

NOTES_RE = re.compile(r"notes=\{`.*?`\}", re.S)
COMMENT_RE = re.compile(r"\{/\*.*?\*/\}", re.S)
TAG_RE = re.compile(r"<(/?)([A-Za-z][\w]*)((?:(?:[^<>\"']|\"[^\"]*\"|'[^']*')*?))(/?)>", re.S)
STYLE_RE = re.compile(r"style=\{\{(.*?)\}\}", re.S)
SRC_RE = re.compile(r"src=(?:'([^']*)'|\"([^\"]*)\")")

# 不加 px 的数值属性
UNITLESS = {"opacity", "flex", "flexgrow", "flexshrink", "zindex", "lineheight", "fontweight"}


def split_top(s: str) -> list[str]:
    """按顶层逗号切分（跳过引号内的逗号与括号内的逗号）。"""
    parts, buf, quote, depth = [], "", None, 0
    for ch in s:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            buf += ch
        elif ch in "([":
            depth += 1
            buf += ch
        elif ch in ")]":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    return parts


def parse_style(inner: str) -> dict[str, str]:
    """JS 对象字面量 -> {css属性: 值}。"""
    out: dict[str, str] = {}
    for item in split_top(inner):
        item = item.strip()
        if not item or ":" not in item:
            continue
        key, _, val = item.partition(":")
        key, val = key.strip(), val.strip()
        if val[:1] in ("'", '"') and val[-1:] == val[:1] and len(val) >= 2:
            val = val[1:-1]
        css = re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), key)
        if re.fullmatch(r"-?\d+(\.\d+)?", val) and key.lower() not in UNITLESS:
            val = val + "px"
        out[css] = val
    return out


def css_text(style: dict[str, str], extra: dict[str, str] | None = None) -> str:
    merged = dict(style)
    if extra:
        merged.update(extra)
    # 关键：CSS 值（如 font-family 里的 \"Source Han Sans SC\"）含双引号时，
    # 塞进双引号包裹的 HTML 属性会提前闭合，导致后面的声明被当成垃圾属性丢掉。
    # CSS 两者等价，统一转成单引号。
    return ";".join("%s:%s" % (k, v.replace('"', "'")) for k, v in merged.items())


class Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: str = "") -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node | str] = []

    def style(self) -> dict[str, str]:
        m = STYLE_RE.search(self.attrs)
        return parse_style(m.group(1)) if m else {}


def parse(s: str) -> Node:
    """极简 JSX 解析：只认自闭合标签 / 开标签 / 闭标签 / 文本。"""
    root = Node("#root")
    stack = [root]
    i = 0
    while i < len(s):
        if s[i] == "<":
            m = TAG_RE.match(s, i)
            if m:
                closing, name, attrs, sc = m.group(1), m.group(2), m.group(3), m.group(4)
                if closing:
                    if len(stack) > 1:
                        stack.pop()
                else:
                    node = Node(name, attrs)
                    stack[-1].children.append(node)
                    if not sc:
                        stack.append(node)
                i = m.end()
                continue
        j = s.find("<", i)
        if j == -1:
            j = len(s)
        text = s[i:j]
        if text.strip():
            stack[-1].children.append(text)
        i = j
    return root


def render(node: Node, assets_rel: str) -> str:
    if isinstance(node, str):
        return html.escape(node)
    tag = node.tag
    if tag == "#root":
        return "".join(render(c, assets_rel) for c in node.children)
    if tag in ("br", "BR"):
        return "<br/>"

    st = node.style()
    inner = "".join(render(c, assets_rel) for c in node.children)

    if tag == "Slide":
        extra = {"display": "flex", "box-sizing": "border-box"}
        extra.setdefault("flex-direction", "column")
        st.setdefault("flex-direction", "column")
        st.setdefault("background", "#FFFFFF")
        return '<div class="slide" style="%s">%s</div>' % (css_text(st, extra), inner)

    if tag == "Box":
        st.setdefault("flex-direction", "column")
        return '<div style="%s">%s</div>' % (css_text(st, {"display": "flex"}), inner)

    if tag == "Text":
        return '<div style="%s">%s</div>' % (css_text(st, {"display": "block"}), inner)

    if tag == "span":
        return '<span style="%s">%s</span>' % (css_text(st), inner)

    if tag == "Image":
        m = SRC_RE.search(node.attrs)
        src = (m.group(1) or m.group(2)) if m else ""
        if src and not re.match(r"^(https?:|data:|/)", src):
            src = os.path.join(assets_rel, src.split("/")[-1]) if assets_rel else src
        return '<img src="%s" style="%s"/>' % (
            html.escape(src),
            css_text(st, {"display": "block", "object-fit": "cover"}),
        )

    if tag in ("svg", "path", "rect", "circle", "line", "text", "g"):
        # 结构元素原样透传（本项目未用，留个兜底）
        return "<%s%s>%s</%s>" % (tag, node.attrs, inner, tag)

    return '<div style="%s">%s</div>' % (css_text(st), inner)


HTML_SHELL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%s</title>
<style>
  html,body{margin:0;padding:0;background:#E7E9EC;}
  .slide{width:1280px;height:720px;margin:0 auto 24px auto;overflow:hidden;
         font-family:"Source Han Sans SC","Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;}
  img{max-width:none;}
</style></head>
<body>
%s
</body></html>
"""


def convert(slides_dir: str, out_html: str, assets_rel: str = "") -> int:
    files = sorted(f for f in os.listdir(slides_dir) if f.endswith(".slide"))
    if not files:
        raise SystemExit("目录里没有 .slide：%s" % slides_dir)
    pages = []
    for f in files:
        s = open(os.path.join(slides_dir, f), encoding="utf-8").read()
        s = NOTES_RE.sub("", s)
        s = COMMENT_RE.sub("", s)
        tree = parse(s)
        pages.append(render(tree, assets_rel))
    title = os.path.basename(out_html)
    with open(out_html, "w", encoding="utf-8") as fh:
        fh.write(HTML_SHELL % (title, "\n".join(pages)))
    return len(pages)


def self_test() -> int:
    ok, bad = 0, []

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        if cond:
            ok += 1
            print("  PASS  %s" % name)
        else:
            bad.append(name)
            print("  FAIL  %s  %s" % (name, extra))

    print("=== slide_dsl_to_html --self-test ===")
    check("T1 split_top 不切引号内逗号",
          split_top("a:1, b:'x,y', c:2") == ["a:1", " b:'x,y'", " c:2"],
          split_top("a:1, b:'x,y', c:2"))
    check("T2 split_top 不切括号内逗号",
          split_top("a:rgba(0,0,0,.5), b:1") == ["a:rgba(0,0,0,.5)", " b:1"],
          split_top("a:rgba(0,0,0,.5), b:1"))
    st = parse_style("width: 368, flexDirection: 'row', opacity: 0.12, lineHeight: 1.6")
    check("T3 数值补 px、无单位属性不补",
          st == {"width": "368px", "flex-direction": "row", "opacity": "0.12", "line-height": "1.6"},
          st)
    check("T4 值内含逗号的字体串不被切碎",
          parse_style("fontFamily: '\"A\",\"B\",sans-serif'").get("font-family") == '"A","B",sans-serif',
          parse_style("fontFamily: '\"A\",\"B\",sans-serif'"))
    n = parse("<Slide style={{ width: 10 }}><Box style={{ height: 2 }} /></Slide>")
    check("T5 解析层级正确", len(n.children) == 1 and n.children[0].tag == "Slide")
    h = render(parse("<Text style={{ fontSize: 13 }}>你好</Text>"), "")
    check("T6 Text 渲染出文本", "你好" in h and "font-size:13px" in h, h)
    h2 = render(parse("<span style={{ fontWeight: 'bold' }}>&lt;x&gt;</span>"), "")
    check("T7 span 样式生效", "font-weight:bold" in h2, h2)
    # T8 notes 与注释不得出现在产物里
    s = NOTES_RE.sub("", COMMENT_RE.sub("", "<Slide notes={`讲稿正文`}>{/*注释*/}<Text>真内容</Text></Slide>"))
    h3 = render(parse(s), "")
    check("T8 notes/注释不进产物", "讲稿正文" not in h3 and "注释" not in h3 and "真内容" in h3, h3[:120])
    # T9 CSS 值里不得残留双引号（否则会把 HTML 属性提前闭合，丢掉后续声明）
    cx = css_text(parse_style("""fontFamily: '"A","B",sans-serif', boxSizing: 'x'"""),
                  {"box-sizing": "border-box"})
    check("T9 属性值无双引号且末位声明保留",
          '"' not in cx and "box-sizing:border-box" in cx, cx)

    print()
    print("结果：%d 通过 / %d 失败" % (ok, len(bad)))
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=".slide DSL -> HTML（版式肉眼核查）")
    ap.add_argument("slides_dir", nargs="?")
    ap.add_argument("out_html", nargs="?")
    ap.add_argument("--assets", default="", help="图片目录相对输出的路径前缀")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        return self_test()
    if not args.slides_dir or not args.out_html:
        ap.print_usage()
        return 2
    n = convert(args.slides_dir, args.out_html, args.assets)
    print("已转译 %d 页 -> %s" % (n, args.out_html))
    return 0


if __name__ == "__main__":
    sys.exit(main())
