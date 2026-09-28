#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PPTX 交付终检：OPC 包完整性 + 演讲者备注逐页对撞。

## 检查项

| 项 | 判据 |
| --- | --- |
| A 压缩包 | `zipfile.testzip()` 返回 None（无 CRC 错误） |
| B XML | 每个 `.xml` / `.rels` 都能被 ElementTree 解析 |
| C 内容类型 | 每个部件都被 `[Content_Types].xml` 覆盖（Default 后缀命中或 Override 命中） |
| D 关系 | 每条非 External 关系的 Target 都存在（按 rels 所在目录拼接后归一化） |
| E 幻灯片 | `presentation.xml` 的 sldIdLst 顺序 = 实际放映顺序，页数 > 0 |
| F 备注 | 每页 slide 的 rels 都指向一个**存在**的 notesSlide，且其 `a:t` 文本与 `.slide` 源里 `notes={...}` 逐页一致 |

E/F 是这套工具链最容易出问题的地方：slidep **不产出 notesSlides**，且每次
`upsert-dsl` 重写 pptx 后**被替换的那一页会丢讲稿**（其余页保住）。所以改完任何一页，
都必须重跑备注注入，然后跑本脚本确认 F 项回到 N/N。

## 用法

    python scripts/verify_pptx_delivery.py <pptx> <slides目录>
    python scripts/verify_pptx_delivery.py <pptx> <slides目录> --self-test

`--self-test` 会在临时副本上故意破坏 4 处（删备注部件 / 删备注关系 / 删 Content-Type /
塞一个悬空关系），要求对应检查项**分别变红**——即证明这些判据真的在参与判定，
而不是摆设。
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NOTES_RE = re.compile(r"notes=\{`(.*?)`\}", re.S)

_PML = "application/vnd.openxmlformats-officedocument.presentationml."
# 这些部件必须有专门的 Override，靠 Default Extension="xml" 兜底不算数
EXPECTED_CT: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ppt/presentation\.xml"), _PML + "presentation.main+xml"),
    (re.compile(r"ppt/slides/slide\d+\.xml"), _PML + "slide+xml"),
    (re.compile(r"ppt/notesSlides/notesSlide\d+\.xml"), _PML + "notesSlide+xml"),
    (re.compile(r"ppt/notesMasters/notesMaster\d+\.xml"), _PML + "notesMaster+xml"),
    (re.compile(r"ppt/slideLayouts/slideLayout\d+\.xml"), _PML + "slideLayout+xml"),
    (re.compile(r"ppt/slideMasters/slideMaster\d+\.xml"), _PML + "slideMaster+xml"),
]


# ---------------- 单项检查（都接受“(名字 -> 字节) 映射”，方便自检时喂坏数据） ----------------


def _parts(zf: zipfile.ZipFile) -> dict[str, bytes]:
    return {n: zf.read(n) for n in zf.namelist() if not n.endswith("/")}


def slide_order(parts: dict[str, bytes]) -> list[str]:
    pres = parts["ppt/presentation.xml"].decode("utf-8")
    rels = parts["ppt/_rels/presentation.xml.rels"].decode("utf-8")
    id2t = dict(re.findall(r'Id="(rId\d+)"[^>]*Target="([^"]+)"', rels))
    return [id2t[r] for r in re.findall(r'<p:sldId[^>]*r:id="(rId\d+)"', pres)]


def check_xml_parse(parts: dict[str, bytes]) -> list[str]:
    bad = []
    for n, data in parts.items():
        if n.endswith((".xml", ".rels")):
            try:
                ET.fromstring(data)
            except Exception as e:  # noqa: BLE001
                bad.append("%s: %s" % (n, str(e)[:50]))
    return bad


def check_content_types(parts: dict[str, bytes]) -> list[str]:
    """两个层次都要查：

    1. **有没有覆盖**：没有任何 Default 后缀命中、也没有 Override 的部件 = 裸件。
    2. **覆盖得对不对**：OOXML 里这些部件**必须**用 Override 指定专门的内容类型；
       只被 `Default Extension="xml"`（application/xml）兜住是不够的 —— 包能打开，
       但 PowerPoint 会按裸 XML 处理，备注页/幻灯片都可能失效。

    第 2 层是自检 T3 逼出来的：只看第 1 层时，删掉 notesSlide 的 Override 也判"通过"，
    属于摆设判据。
    """
    ct = parts["[Content_Types].xml"].decode("utf-8")
    defaults = {k.lower() for k in re.findall(r'<Default Extension="([^"]+)"', ct)}
    overrides = dict(
        re.findall(r'<Override PartName="([^"]+)" ContentType="([^"]+)"', ct)
    )
    bad: list[str] = []

    for n in parts:
        if n == "[Content_Types].xml":
            continue
        if ("/" + n) not in overrides and n.rsplit(".", 1)[-1].lower() not in defaults:
            bad.append("裸件（无任何内容类型）：%s" % n)

    for pattern, want in EXPECTED_CT:
        for n in parts:
            if pattern.fullmatch(n):
                got = overrides.get("/" + n)
                if got != want:
                    bad.append(
                        "%s 的 Override 缺失或类型不对（期望 %s，实际 %s）" % (n, want, got)
                    )
    return bad


def check_rels(parts: dict[str, bytes]) -> list[str]:
    bad = []
    for n in [x for x in parts if x.endswith(".rels")]:
        base = posixpath.dirname(posixpath.dirname(n))
        for t, mode in re.findall(
            r'Target="([^"]+)"(?:\s+TargetMode="(\w+)")?', parts[n].decode("utf-8")
        ):
            if mode == "External":
                continue
            p = posixpath.normpath(posixpath.join(base, t))
            if p not in parts:
                bad.append("%s -> %s" % (n, t))
    return bad


def check_notes(parts: dict[str, bytes], slides_dir: str) -> tuple[int, list[str]]:
    """返回 (对上的页数, 问题列表)。"""
    problems: list[str] = []
    order = slide_order(parts)
    ok = 0
    for i in range(1, len(order) + 1):
        src = os.path.join(slides_dir, "%02d.slide" % i)
        if not os.path.isfile(src):
            problems.append("P%02d 缺源文件 %s" % (i, src))
            continue
        m = NOTES_RE.search(open(src, encoding="utf-8").read())
        if not m:
            problems.append("P%02d .slide 里没有 notes" % i)
            continue
        exp = [ln.strip() for ln in m.group(1).split("\n")]
        while exp and not exp[-1]:
            exp.pop()
        exp = [p for p in exp if p.strip()]

        rels_name = "ppt/slides/_rels/slide%d.xml.rels" % i
        if rels_name not in parts:
            problems.append("P%02d 缺 %s" % (i, rels_name))
            continue
        mm = re.search(
            r'Type="[^"]*/notesSlide"[^>]*Target="([^"]+)"', parts[rels_name].decode("utf-8")
        )
        if not mm:
            problems.append("P%02d slide rels 里没有 notesSlide 关系（讲稿丢了）" % i)
            continue
        npart = "ppt/" + mm.group(1).replace("../", "")
        if npart not in parts:
            problems.append("P%02d 关系指向的备注部件不存在：%s" % (i, npart))
            continue
        try:
            root = ET.fromstring(parts[npart])
        except Exception as e:  # noqa: BLE001
            # 坏 XML 必须被报成问题，而不是把校验器本身炸掉
            problems.append("P%02d 备注部件 XML 无法解析：%s" % (i, str(e)[:60]))
            continue
        got = [(e.text or "") for e in root.iter("{%s}t" % NS_A)]
        if got == exp:
            ok += 1
        else:
            problems.append("P%02d 讲稿文本不符（部件 %d 段 / 源 %d 段）" % (i, len(got), len(exp)))
    return ok, problems


# ---------------- 汇总 ----------------


def verify(pptx: str, slides_dir: str, quiet: bool = False) -> tuple[bool, dict]:
    zf = zipfile.ZipFile(pptx)
    parts = _parts(zf)
    crc = zf.testzip()
    zf.close()

    res: dict = {"file": os.path.basename(pptx),
                 "size_kb": round(os.path.getsize(pptx) / 1024, 1),
                 "parts": len(parts), "crc": crc is None}
    res["xml_bad"] = check_xml_parse(parts)
    res["ct_missing"] = check_content_types(parts)
    res["rels_dangling"] = check_rels(parts)
    try:
        res["slides"] = len(slide_order(parts))
    except Exception as e:  # noqa: BLE001
        res["slides"] = 0
        res["slide_order_error"] = str(e)[:80]
    res["notes_ok"], res["notes_problems"] = check_notes(parts, slides_dir)
    res["media"] = len(
        [n for n in parts if n.startswith("ppt/slides/media/") and n.lower().endswith(".png")]
    )
    ok = (
        res["crc"]
        and not res["xml_bad"]
        and not res["ct_missing"]
        and not res["rels_dangling"]
        and res["slides"] > 0
        and res["notes_ok"] == res["slides"]
    )
    res["pass"] = ok
    if not quiet:
        print("文件      : %s（%.1f KB，%d 部件）" % (res["file"], res["size_kb"], res["parts"]))
        print("A 压缩包  : %s" % ("OK" if res["crc"] else "**CRC 损坏于 %s**" % crc))
        print("B XML 解析: %s" % ("OK" if not res["xml_bad"] else res["xml_bad"][:3]))
        print("C 内容类型: %s" % ("OK" if not res["ct_missing"] else res["ct_missing"][:3]))
        print("D 关系    : %s" % ("OK" if not res["rels_dangling"] else res["rels_dangling"][:3]))
        print("E 幻灯片  : %d 页   配图 %d 张" % (res["slides"], res["media"]))
        print("F 备注    : %d/%d 页讲稿对得上" % (res["notes_ok"], res["slides"]))
        for p in res["notes_problems"]:
            print("            ✗ %s" % p)
        print()
        print("结论：%s" % ("全部通过，可交付" if ok else "**有问题，不可交付**"))
    return ok, res


def self_test(pptx: str, slides_dir: str) -> int:
    """在临时副本上故意破坏，要求对应检查项变红。"""
    ok, bad = 0, []

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal ok
        if cond:
            ok += 1
            print("  PASS  %s" % name)
        else:
            bad.append(name)
            print("  FAIL  %s  %s" % (name, extra))

    print("=== verify_pptx_delivery --self-test ===")
    base_ok, _ = verify(pptx, slides_dir, quiet=True)
    check("T0 基线（未破坏）应通过", base_ok)

    def mutate(name: str, fn, key: str, expect: str) -> None:
        with tempfile.TemporaryDirectory() as d:
            zf = zipfile.ZipFile(pptx)
            parts = _parts(zf)
            zf.close()
            fn(parts)
            out = os.path.join(d, "m.pptx")
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for n, data in parts.items():
                    z.writestr(n, data)
            okx, res = verify(out, slides_dir, quiet=True)
            check(name, (not okx) and bool(res.get(key)), res.get(key))

    def drop_notes_part(parts: dict) -> None:
        for n in [x for x in parts if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", x)][:1]:
            del parts[n]

    mutate("T1 删掉一个备注部件 → F 报警", drop_notes_part, "notes_problems", "notes_problems")

    def drop_notes_rel(parts: dict) -> None:
        n = "ppt/slides/_rels/slide1.xml.rels"
        parts[n] = re.sub(r'<Relationship[^>]*/notesSlide"[^>]*/>', "", parts[n].decode()).encode()

    mutate("T2 删掉一页的 notesSlide 关系 → F 报警", drop_notes_rel, "notes_problems", "notes_problems")

    def drop_ct(parts: dict) -> None:
        n = "[Content_Types].xml"
        parts[n] = re.sub(
            r'<Override PartName="/ppt/notesSlides/notesSlide1\.xml"[^>]*/>', "", parts[n].decode()
        ).encode()

    mutate("T3 删掉一个 Content-Type Override → C 报警", drop_ct, "ct_missing", "ct_missing")

    def add_dangling(parts: dict) -> None:
        n = "ppt/slides/_rels/slide1.xml.rels"
        parts[n] = parts[n].decode().replace(
            "</Relationships>",
            '<Relationship Id="rId999" Type="%s" Target="../media/nope.png"/></Relationships>'
            % "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
        ).encode()

    mutate("T4 塞一条悬空关系 → D 报警", add_dangling, "rels_dangling", "rels_dangling")

    def break_crc(parts: dict) -> None:
        # 改坏 XML 内容（合法 zip，但解析会炸）
        parts["ppt/notesSlides/notesSlide1.xml"] = b"<?xml version='1.0'?><p:notes"

    mutate("T5 把 XML 截断 → B 报警", break_crc, "xml_bad", "xml_bad")

    print()
    print("结果：%d 通过 / %d 失败" % (ok, len(bad)))
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PPTX 交付终检（OPC + 备注对撞）")
    ap.add_argument("pptx", nargs="?")
    ap.add_argument("slides_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        if not args.pptx or not args.slides_dir:
            ap.print_usage()
            return 2
        return self_test(args.pptx, args.slides_dir)
    if not args.pptx or not args.slides_dir:
        ap.print_usage()
        return 2
    ok, _ = verify(args.pptx, args.slides_dir)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
