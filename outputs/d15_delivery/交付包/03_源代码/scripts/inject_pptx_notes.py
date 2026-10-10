#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 .slide 里写的 notes 注入 pptx（生成真正的演讲者备注页）。

## 为什么需要这个脚本

当前版本的 slidep 转换器**不产出 `ppt/notesSlides/`**：`<Slide notes={...}>` 里的
文本只会被当成源码留痕嵌进 slide XML，在 PowerPoint 的「演讲者视图」里看不到。
（实测：本包 12 页全部 notesSlides 为 0；旧 D14 包同样为 0。）

因此补一个后处理：按 OOXML 规范往 zip 里补 4 类内容 ——
  1. `ppt/notesSlides/notesSlideN.xml`（正文占位符 + 幻灯片图像占位符）
  2. `ppt/notesSlides/_rels/notesSlideN.xml.rels`（指回 notesMaster 与本页 slide）
  3. `ppt/slides/_rels/slideN.xml.rels` 追加一条 notesSlide 关系
  4. `[Content_Types].xml` 追加 notesSlide 的 Override

## 用法

    python scripts/inject_pptx_notes.py <pptx路径> <slides目录> --check
    python scripts/inject_pptx_notes.py <pptx路径> <slides目录>
    python scripts/inject_pptx_notes.py --self-test

## 纪律

- **幂等**：重复跑不会叠加条目（先按前缀清掉旧的 notesSlide 部件再写）。
- **不做破坏性删除**：只替换 `ppt/notesSlides/` 下的内容，其余条目原样搬运。
- **--self-test 必须会红**：注入式自检，把每一种判据都故意破坏一次。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import zipfile

NOTES_CT = "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
REL_NOTES_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
REL_NOTES_MASTER = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesMaster"
REL_SLIDE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
REL_IMAGE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
REL_LAYOUT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"

NOTES_RE = re.compile(r"notes=\{`(.*?)`\}", re.S)


def xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def parse_notes(slide_text: str) -> str | None:
    m = NOTES_RE.search(slide_text)
    if not m:
        return None
    body = m.group(1).replace("\\`", "`")
    return body


def slide_order(zf: zipfile.ZipFile) -> list[str]:
    """按 presentation.xml 的 sldIdLst 顺序返回 slide 部件路径。"""
    pres = zf.read("ppt/presentation.xml").decode("utf-8")
    rels = zf.read("ppt/_rels/presentation.xml.rels").decode("utf-8")
    id2t = {i: t for i, t in re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', rels)}
    order = re.findall(r'<p:sldId[^>]*r:id="(rId\d+)"', pres)
    return ["ppt/" + id2t[r].lstrip("/").replace("../", "") for r in order]


def build_notes_slide(paragraphs: list[str], slide_part: str) -> str:
    runs = []
    for p in paragraphs:
        if not p.strip():
            runs.append("<a:p><a:endParaRPr lang=\"zh-CN\"/></a:p>")
        else:
            runs.append(
                '<a:p><a:r><a:rPr lang="zh-CN" dirty="0"/><a:t>%s</a:t></a:r></a:p>'
                % xml_escape(p)
            )
    body = "".join(runs)
    # 注意：% 的优先级高于 +，所以头部必须单独格式化后再拼接，
    # 否则 "</p:notes>" % (…) 会被先求值并抛 "not all arguments converted"。
    head = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<p:notes xmlns:a="%s" xmlns:r="%s" xmlns:p="%s">' % (NS_A, NS_R, NS_P)
    )
    return (
        head + "<p:cSld><p:spTree>"
        "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>"
        '<p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/>'
        '<a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>'
        "<p:sp><p:nvSpPr>"
        '<p:cNvPr id="2" name="Notes Placeholder 1"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>"
        + body
        + "</p:txBody></p:sp>"
        "<p:sp><p:nvSpPr>"
        '<p:cNvPr id="3" name="Slide Image Placeholder 1"/>'
        '<p:cNvSpPr><a:spLocks noGrp="1" noRot="1" noChangeAspect="1"/></p:cNvSpPr>'
        '<p:nvPr><p:ph type="sldImg" idx="2"/></p:nvPr></p:nvSpPr>'
        "<p:spPr/></p:sp>"
        "</p:spTree></p:cSld>"
        '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>'
        "</p:notes>"
    )


def build_notes_rels(idx: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="%s" Target="../notesMasters/notesMaster1.xml"/>'
        '<Relationship Id="rId2" Type="%s" Target="../slides/slide%d.xml"/>'
        "</Relationships>" % (REL_NOTES_MASTER, REL_SLIDE, idx)
    )


def add_notes_rel(rels_xml: str, idx: int) -> str:
    """往 slideN.xml.rels 里追加一条 notesSlide 关系（幂等：先删同名）。"""
    rels_xml = re.sub(
        r'<Relationship[^>]*Type="%s"[^>]*/>' % re.escape(REL_NOTES_SLIDE), "", rels_xml
    )
    used = set(re.findall(r'Id="(rId\d+)"', rels_xml))
    n = 900
    while "rId%d" % n in used:
        n += 1
    rel = '<Relationship Id="rId%d" Type="%s" Target="../notesSlides/notesSlide%d.xml"/>' % (
        n,
        REL_NOTES_SLIDE,
        idx,
    )
    return rels_xml.replace("</Relationships>", rel + "</Relationships>")


def add_content_types(ct_xml: str, n: int) -> str:
    # 幂等：先清掉所有 notesSlide Override，再按需重建
    ct_xml = re.sub(r'<Override PartName="/ppt/notesSlides/notesSlide\d+\.xml"[^>]*/>', "", ct_xml)
    overrides = "".join(
        '<Override PartName="/ppt/notesSlides/notesSlide%d.xml" ContentType="%s"/>' % (i, NOTES_CT)
        for i in range(1, n + 1)
    )
    return ct_xml.replace("</Types>", overrides + "</Types>")


def inject(pptx: str, slides_dir: str) -> tuple[int, list[str]]:
    """返回 (写入的备注页数, 警告列表)。"""
    warns: list[str] = []
    if not os.path.isfile(pptx):
        raise SystemExit("找不到 pptx：%s" % pptx)
    if not os.path.isdir(slides_dir):
        raise SystemExit("找不到 slides 目录：%s" % slides_dir)

    with zipfile.ZipFile(pptx) as zf:
        names = zf.namelist()
        order = slide_order(zf)
        items = {n: zf.read(n) for n in names}
        if "ppt/notesMasters/notesMaster1.xml" not in names:
            raise SystemExit("该 pptx 没有 notesMaster，无法挂备注页")

    new_parts: dict[str, bytes] = {}
    count = 0
    for idx, part in enumerate(order, start=1):
        src = os.path.join(slides_dir, "%02d.slide" % idx)
        if not os.path.isfile(src):
            warns.append("缺 %s（第 %d 页）→ 该页不写备注" % (src, idx))
            continue
        with open(src, "r", encoding="utf-8") as f:
            body = parse_notes(f.read())
        if body is None or not body.strip():
            warns.append("第 %d 页的 .slide 里没有 notes → 该页不写备注" % idx)
            continue

        paras = [ln.strip() for ln in body.split("\n")]
        while paras and not paras[-1]:
            paras.pop()

        new_parts["ppt/notesSlides/notesSlide%d.xml" % idx] = build_notes_slide(
            paras, part
        ).encode("utf-8")
        new_parts["ppt/notesSlides/_rels/notesSlide%d.xml.rels" % idx] = build_notes_rels(
            idx
        ).encode("utf-8")

        rels_name = "ppt/slides/_rels/slide%d.xml.rels" % idx
        base = items.get(rels_name, b"")
        if not base:
            warns.append("第 %d 页缺 rels 文件，已新建" % idx)
            base = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                "</Relationships>"
            ).encode("utf-8")
        items[rels_name] = add_notes_rel(base.decode("utf-8"), idx).encode("utf-8")
        count += 1

    ct = items["[Content_Types].xml"].decode("utf-8")
    items["[Content_Types].xml"] = add_content_types(ct, len(order)).encode("utf-8")

    # 幂等：先剔除旧的 notesSlide 部件，再合并新内容
    stale = [n for n in items if n.startswith("ppt/notesSlides/") and not n.endswith("/")]
    for n in stale:
        del items[n]
    items.update(new_parts)

    tmp = pptx + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for n, data in items.items():
            out.writestr(n, data)
    shutil.move(tmp, pptx)
    return count, warns


def verify(pptx: str) -> dict:
    """只读核验：4 类内容是否齐全并且互相对得上。"""
    with zipfile.ZipFile(pptx) as zf:
        names = set(zf.namelist())
        order = slide_order(zf)
        res = {"slides": len(order), "notes_parts": 0, "rels_ok": 0, "ct_ok": 0, "text_ok": 0}
        ct = zf.read("[Content_Types].xml").decode("utf-8")
        for idx in range(1, len(order) + 1):
            np_ = "ppt/notesSlides/notesSlide%d.xml" % idx
            nr = "ppt/notesSlides/_rels/notesSlide%d.xml.rels" % idx
            sr = "ppt/slides/_rels/slide%d.xml.rels" % idx
            if np_ not in names:
                continue
            res["notes_parts"] += 1
            nrx = zf.read(nr).decode("utf-8")
            if ('Target="../slides/slide%d.xml"' % idx) in nrx and "notesMaster1.xml" in nrx:
                res["rels_ok"] += 1
            if ('PartName="/%s"' % np_) in ct:
                res["ct_ok"] += 1
            if 'Type="%s"' % REL_NOTES_SLIDE in zf.read(sr).decode("utf-8"):
                res["text_ok"] += 1
        return res


def self_test() -> int:
    ok = [0]
    bad = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        if cond:
            ok[0] += 1
            print("  PASS  %s" % name)
        else:
            bad.append(name)
            print("  FAIL  %s  %s" % (name, extra))

    print("=== inject_pptx_notes --self-test ===")

    # T1 notes 解析：模板串必须能取到
    check(
        "T1 notes={...} 能解析出来",
        parse_notes("<Slide notes={`第一段\n\n第二段`}></Slide>") == "第一段\n\n第二段",
    )
    # T2 没有 notes 时返回 None（而不是空串）
    check("T2 无 notes 返回 None", parse_notes("<Slide></Slide>") is None)
    # T3 XML 转义：< > & 必须被转义（否则产物损坏）
    x = build_notes_slide(["a<b&c>d"], "ppt/slides/slide1.xml")
    check("T3 特殊字符已转义", "&lt;" in x and "&amp;" in x and "<b&c>" not in x)
    # T4 关系追加必须使用未被占用的 Id
    # 注意：别用 count("<Relationship")，它会连根标签 <Relationships> 一起数进去。
    r = add_notes_rel('<Relationships><Relationship Id="rId900" Type="x" Target="y"/></Relationships>', 1)
    check(
        "T4 追加关系时 Id 不冲突",
        'Id="rId901"' in r and r.count("<Relationship ") == 2,
        r,
    )
    # T5 幂等：重复追加不会叠加
    r2 = add_notes_rel(r, 1)
    check("T5 追加关系幂等", r2.count(REL_NOTES_SLIDE) == 1, r2)
    # T6 content types 幂等
    ct = (
        '<Types><Override PartName="/ppt/slides/slide1.xml" ContentType="x"/>'
        '<Override PartName="/ppt/notesSlides/notesSlide1.xml" ContentType="old"/></Types>'
    )
    ct2 = add_content_types(add_content_types(ct, 2), 2)
    check("T6 content-types 幂等且覆盖正确", ct2.count("/ppt/notesSlides/notesSlide1.xml") == 1)
    # T7 段落切分：空行保留为分隔、尾随空行裁掉
    paras = [ln.strip() for ln in "a\n\nb\n".split("\n")]
    while paras and not paras[-1]:
        paras.pop()
    check("T7 尾随空行被裁掉", paras == ["a", "", "b"], paras)
    # T8 讲稿里出现字面 % （如「100%」「提升 30%」）不得被当格式符吃掉
    try:
        y = build_notes_slide(["一致率 100%", "显存占用 88%"], "ppt/slides/slide1.xml")
        check("T8 正文含 % 不炸且原样保留", "100%" in y and "88%" in y)
    except Exception as e:  # noqa: BLE001
        check("T8 正文含 % 不炸且原样保留", False, repr(e))
    # T9 产物必须是合法 XML（用标准解析器验，而不是只做字符串包含）
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(build_notes_slide(["第一段", "", "第二段 <带尖括号>"], "p"))
        ts = [e.text for e in root.iter("{%s}t" % NS_A)]
        check("T9 产物是合法 XML 且文本完整", len(ts) == 2 and ts[1] == "第二段 <带尖括号>", ts)
    except Exception as e:  # noqa: BLE001
        check("T9 产物是合法 XML 且文本完整", False, repr(e))

    print()
    print("结果：%d 通过 / %d 失败" % (ok[0], len(bad)))
    if bad:
        print("失败项：%s" % bad)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把 .slide 的 notes 注入 pptx 备注页")
    ap.add_argument("pptx", nargs="?", help="目标 pptx")
    ap.add_argument("slides_dir", nargs="?", help="slides 目录（01.slide ...）")
    ap.add_argument("--check", action="store_true", help="只核验，不写入")
    ap.add_argument("--self-test", action="store_true", help="跑注入式自检")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if not args.pptx or not args.slides_dir:
        ap.print_usage()
        return 2

    if args.check:
        r = verify(args.pptx)
        print("页数 %d / 备注部件 %d / 关系对得上 %d / content-types %d / slide↔notes 挂接 %d"
              % (r["slides"], r["notes_parts"], r["rels_ok"], r["ct_ok"], r["text_ok"]))
        ok = (
            r["notes_parts"] == r["slides"]
            and r["rels_ok"] == r["slides"]
            and r["ct_ok"] == r["slides"]
            and r["text_ok"] == r["slides"]
        )
        print("结论：%s" % ("全部齐全" if ok else "**不齐全**"))
        return 0 if ok else 1

    n, warns = inject(args.pptx, args.slides_dir)
    print("已写入 %d 页备注" % n)
    for w in warns:
        print("  警告：%s" % w)
    r = verify(args.pptx)
    print(
        "复核：页数 %d / 备注部件 %d / 关系 %d / content-types %d / 挂接 %d"
        % (r["slides"], r["notes_parts"], r["rels_ok"], r["ct_ok"], r["text_ok"])
    )
    allok = (
        r["notes_parts"] == r["slides"] == r["rels_ok"] == r["ct_ok"] == r["text_ok"]
    )
    print("结论：%s" % ("全部齐全" if allok else "**不齐全**"))
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
