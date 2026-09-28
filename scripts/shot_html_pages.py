#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 CDP 驱动本机 Edge，把 HTML 里每个匹配元素各截一张 PNG，并可选拼一张总览图。

配套 skill: cdp-edge-screenshot（无 playwright 时的取证通路）。

## 用法

    # 先起无头 Edge（后台）
    msedge.exe --headless=new --disable-gpu --hide-scrollbars \
      --remote-debugging-port=9222 --remote-allow-origins=* --window-size=1400,1000 about:blank

    python scripts/shot_html_pages.py <html> <css选择器> <输出目录> [--sheet out.png] [--cols 4]

## 三层断言

- L1：每张 PNG 的 IHDR 宽高 == 目标元素盒模型尺寸（读字节，不信「文件存在」）。
- L2：元素文本非空且含预期标记（--marker，可重复）。
- L3：正文不得含错误特征（--forbid，默认含 Failed to fetch / Not Found / 未定义）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import sys
import time
import urllib.request

import websockets

DEFAULT_FORBID = ["Failed to fetch", "Not Found", "undefined", "NaN"]

# 调试端口：默认 9222，可用 CDP_PORT 覆盖（改了端口记得跟启动 Edge 时一致）
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))


def png_size(path: str) -> tuple[int, int]:
    """读 PNG 字节头解 IHDR，返回 (宽, 高)。"""
    with open(path, "rb") as f:
        head = f.read(33)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG：%s" % path)
    w, h = struct.unpack(">II", head[16:24])
    return w, h


def targets() -> list[dict]:
    with urllib.request.urlopen("http://127.0.0.1:%d/json" % CDP_PORT) as r:
        return json.load(r)


async def run(args) -> int:
    url = "file:///" + os.path.abspath(args.html).replace("\\", "/")
    ws_url = next(
        (t["webSocketDebuggerUrl"] for t in targets() if t["type"] == "page"), None
    )
    if not ws_url:
        print("找不到可用的 page target，Edge 起来了吗？")
        return 2

    os.makedirs(args.outdir, exist_ok=True)
    forbid = args.forbid if args.forbid else DEFAULT_FORBID
    results, bad = [], []

    async with websockets.connect(ws_url, max_size=None) as ws:
        counter = 0

        async def cmd(method: str, **params):
            nonlocal counter
            counter += 1
            mid = counter
            await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError("%s -> %s" % (method, msg["error"]))
                    return msg.get("result", {})

        await cmd("Page.enable")
        await cmd("Runtime.enable")
        await cmd("Emulation.setDeviceMetricsOverride", width=args.width, height=args.height,
                  deviceScaleFactor=1, mobile=False)
        await cmd("Page.navigate", url=url)
        # 等图片与字体落位：轮询元素数量稳定 + document.fonts.ready
        deadline = time.time() + 20
        n_el = -1
        while time.time() < deadline:
            r = await cmd(
                "Runtime.evaluate",
                expression="document.querySelectorAll(%s).length" % json.dumps(args.selector),
                returnByValue=True,
            )
            cur = r.get("result", {}).get("value", 0) or 0
            if cur > 0 and cur == n_el:
                break
            n_el = cur
            time.sleep(0.4)
        await cmd("Runtime.evaluate", expression="document.fonts.ready", awaitPromise=True)
        time.sleep(0.6)

        if n_el <= 0:
            print("选择器 %s 没匹配到元素" % args.selector)
            return 3

        # 把视口撑到整页高，避免滚动：captureBeyondViewport 下 clip 用的是文档坐标，
        # 而 getBoundingClientRect 给的是视口坐标——滚动过就会截错区域（曾导致相邻页重复）。
        doc_h = (
            await cmd(
                "Runtime.evaluate",
                expression="Math.min(document.documentElement.scrollHeight + 40, 20000)",
                returnByValue=True,
            )
        )["result"]["value"]
        await cmd("Emulation.setDeviceMetricsOverride", width=args.width, height=int(doc_h),
                  deviceScaleFactor=1, mobile=False)
        await cmd("Runtime.evaluate", expression="window.scrollTo(0,0)")
        time.sleep(0.4)

        for i in range(n_el):
            box = await cmd(
                "Runtime.evaluate",
                expression=(
                    "(function(){var e=document.querySelectorAll(%s)[%d];"
                    "if(!e)return null;var r=e.getBoundingClientRect();"
                    "return {x:r.x+window.scrollX,y:r.y+window.scrollY,w:r.width,h:r.height,"
                    "t:(e.innerText||'').slice(0,4000)};})()"
                    % (json.dumps(args.selector), i)
                ),
                returnByValue=True,
            )
            b = box.get("result", {}).get("value")
            if not b:
                bad.append("P%02d 取不到盒模型" % (i + 1))
                continue
            shot = await cmd(
                "Page.captureScreenshot",
                format="png",
                clip={"x": b["x"], "y": b["y"], "width": b["w"], "height": b["h"], "scale": 1},
                captureBeyondViewport=True,
            )
            out = os.path.join(args.outdir, "p%02d.png" % (i + 1))
            with open(out, "wb") as f:
                f.write(base64.b64decode(shot["data"]))

            w, h = png_size(out)
            l1 = abs(w - round(b["w"])) <= 1 and abs(h - round(b["h"])) <= 1
            txt = b.get("t", "") or ""
            l2 = len(txt.strip()) > 0
            if args.marker:
                l2 = l2 and all(mk in txt for mk in args.marker)
            l3 = not any(f in txt for f in forbid)
            results.append({"page": i + 1, "png": out, "size": [w, h],
                            "expect": [round(b["w"]), round(b["h"])],
                            "text_len": len(txt.strip()),
                            "L1_pixels": l1, "L2_content": l2, "L3_no_error": l3})
            if not (l1 and l2 and l3):
                bad.append("P%02d L1=%s L2=%s L3=%s" % (i + 1, l1, l2, l3))

    print(json.dumps(results, ensure_ascii=False, indent=2))

    # 各页截图必须互不相同：重复即说明 clip 截错了区域（踩过：相邻页成对重复）
    import hashlib

    seen: dict[str, int] = {}
    for r in results:
        dg = hashlib.md5(open(r["png"], "rb").read()).hexdigest()
        r["md5"] = dg[:12]
        if dg in seen:
            bad.append("P%02d 与 P%02d 截图完全相同（截错区域）" % (r["page"], seen[dg]))
        seen.setdefault(dg, r["page"])

    nfail = len([b for b in bad if "L1=" in b or "截错" in b])
    print()
    print("共 %d 页；通过 %d；失败 %d" % (len(results), len(results) - nfail, nfail))
    for x in bad:
        print("  ✗", x)
    with open(os.path.join(args.outdir, "_shot_report.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    if args.sheet:
        make_sheet(args.outdir, args.sheet, args.cols, args.scale)

    return 1 if bad else 0


def make_sheet(outdir: str, sheet: str, cols: int, scale: float) -> None:
    """用第二个 HTML 把各页 PNG 拼成总览图，再截一次（避免依赖 Pillow）。"""
    pngs = sorted(f for f in os.listdir(outdir) if f.startswith("p") and f.endswith(".png"))
    if not pngs:
        print("没有 PNG 可拼总览")
        return
    w, h = png_size(os.path.join(outdir, pngs[0]))
    tw, th = int(w * scale), int(h * scale)
    cells = "".join(
        '<div class="c"><img src="%s"/><div class="n">%s</div></div>' % (f, f.replace(".png", ""))
        for f in pngs
    )
    gap, pad = 12, 16
    cw, ch = tw, th + 22
    css_w = pad * 2 + cols * cw + (cols - 1) * gap
    rows = (len(pngs) + cols - 1) // cols
    css_h = pad * 2 + rows * ch + (rows - 1) * gap
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        "html,body{margin:0;padding:0;background:#B9BEC4;}"
        ".g{display:flex;flex-wrap:wrap;gap:%dpx;padding:%dpx;width:%dpx;box-sizing:border-box;}"
        ".c{width:%dpx;}.c img{width:%dpx;height:%dpx;display:block;outline:1px solid #8A9098;}"
        ".n{font:11px Consolas,monospace;color:#20242A;padding-top:3px;}"
        "</style></head><body><div class='g'>%s</div></body></html>"
        % (gap, pad, css_w, cw, tw, th, cells)
    )
    sheet_html = os.path.join(outdir, "_sheet.html")
    with open(sheet_html, "w", encoding="utf-8") as f:
        f.write(html)
    print("总览图 HTML: %s (%dx%d)" % (sheet_html, css_w, css_h))


async def shoot_sheet(sheet_html: str, out_png: str, w: int, h: int) -> None:
    ws_url = next(t["webSocketDebuggerUrl"] for t in targets() if t["type"] == "page")
    async with websockets.connect(ws_url, max_size=None) as ws:
        counter = 0

        async def cmd(method, **params):
            nonlocal counter
            counter += 1
            mid = counter
            await ws.send(json.dumps({"id": mid, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    return msg.get("result", {})

        await cmd("Page.enable")
        await cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
                  deviceScaleFactor=1, mobile=False)
        await cmd("Page.navigate", url="file:///" + os.path.abspath(sheet_html).replace("\\", "/"))
        time.sleep(2.0)
        r = await cmd("Page.captureScreenshot", format="png",
                      clip={"x": 0, "y": 0, "width": w, "height": h, "scale": 1},
                      captureBeyondViewport=True)
        with open(out_png, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        print("总览图: %s %s" % (out_png, png_size(out_png)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("html")
    ap.add_argument("selector")
    ap.add_argument("outdir")
    ap.add_argument("--sheet", default="")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--scale", type=float, default=0.5)
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--marker", action="append", default=[])
    ap.add_argument("--forbid", action="append", default=[])
    ap.add_argument("--sheet-only", action="store_true")
    args = ap.parse_args(argv)

    if args.sheet_only:
        make_sheet(args.outdir, args.sheet or "sheet.png", args.cols, args.scale)
        # 需要的话手动再跑一次 shoot_sheet
        return 0

    rc = asyncio.run(run(args))
    if args.sheet:
        pngs = sorted(f for f in os.listdir(args.outdir) if f.startswith("p") and f.endswith(".png"))
        if pngs:
            w, h = png_size(os.path.join(args.outdir, pngs[0]))
            tw, th = int(w * args.scale), int(h * args.scale)
            gap, pad, cols = 12, 16, args.cols
            rows = (len(pngs) + cols - 1) // cols
            css_w = pad * 2 + cols * tw + (cols - 1) * gap
            css_h = pad * 2 + rows * (th + 22) + (rows - 1) * gap
            asyncio.run(shoot_sheet(os.path.join(args.outdir, "_sheet.html"), args.sheet, css_w, css_h))
    return rc


if __name__ == "__main__":
    sys.exit(main())
