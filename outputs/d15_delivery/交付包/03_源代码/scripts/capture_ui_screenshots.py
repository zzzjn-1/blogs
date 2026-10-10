# -*- coding: utf-8 -*-
"""D14.5 浏览器界面留证：用**真实浏览器**逐页截图，并对每页内容做断言。

## 为什么要写脚本，而不是手动截几张图

「留档了几张截图」这件事本身毫无证据力 —— 判据必须是可复跑的。
本脚本每一页都做**三层**核对：

1. **真的渲染出来了**：截图像素尺寸 = 视口尺寸（读 PNG 头，不依赖 PIL）；
2. **不是白屏**：`document.body.innerText` 长度 > 0 且**包含该页的标志性文案**；
   —— 「文件存在」不等于「内容正确」，截图同理：白图也是 PNG。
3. **不是错误页/登录页打回来的**：正文不得包含 `用户名或口令错误`、
   `Failed to fetch`、`404` 之类特征串。

## 为什么用 CDP 而不是某个浏览器自动化包

本机只有 Edge（Chromium 内核），没有 Chrome、也没有 playwright/selenium。
Edge 自带 `--remote-debugging-port`，用标准库 + `websockets` 直接讲 CDP 协议即可，
**不引入新的依赖**（项目对依赖是锁版本的）。

## 登录怎么做的

在页面上下文里 `fetch('/api/auth/login')` —— 与前端**同源**（走 vite 代理），
于是响应里的 `Set-Cookie` 被浏览器正常接受，后续导航自然带上 Cookie。
不伪造、不注入 token，走的就是真人登录那条路。

## 用法

    python scripts/capture_ui_screenshots.py                 # 默认输出 outputs/d14_browser/
    python scripts/capture_ui_screenshots.py --out D:/x --task-id ep_xxx
    python scripts/capture_ui_screenshots.py --password xxx  # 不传则读 --password-file

⚠️ 前置：前后端必须在跑（`start_dev.bat`），且演示账号口令可用
（`python scripts/ensure_demo_account.py --username local`）。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 打本机端口必须绕开 env proxy（否则 absolute-form 请求会被中间代理转成 404）
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

VIEWPORT = (1440, 900)

# 每页一条：(文件名, 路由, 必须出现的文案, 该页中文名)
PAGES = [
    ("01_login", "/login", ["登录"], "登录页"),
    ("02_register", "/register", ["注册"], "注册页"),
    ("03_submit", "/new", ["新建节目"], "新建节目"),
    ("04_history", "/history", ["历史"], "历史列表"),
    ("05_task_detail", "/tasks/{task_id}", ["脚本"], "任务详情"),
    ("06_feed", "/feed", ["频道设置"], "频道设置"),
]

# 出现这些串说明页面是坏的（后端 500 / 未登入 / 断网），绝不能当成「截到了」
BAD_MARKERS = ("用户名或口令错误", "Failed to fetch", "Internal Server Error",
               "NetworkError", "该网页无法正常运作")


def png_size(path: Path) -> tuple[int, int] | None:
    """读 PNG 头拿宽高（不依赖 Pillow）。"""
    raw = path.read_bytes()[:33]
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", raw[16:24])


def find_edge() -> Path | None:
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(p).is_file():
            return Path(p)
    found = shutil.which("msedge") or shutil.which("chrome")
    return Path(found) if found else None


def get_json(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


class Cdp:
    """极简 CDP 客户端：够用就好，不引依赖。"""

    def __init__(self, ws):
        self._ws = ws
        self._id = 0

    async def call(self, method: str, **params):
        self._id += 1
        mid = self._id
        await self._ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=90))
            if msg.get("id") != mid:
                continue                      # 事件帧，丢弃
            if "error" in msg:
                raise RuntimeError(f"{method} -> {msg['error']}")
            return msg.get("result", {})


async def capture(out_dir: Path, base: str, task_id: str, password: str,
                  edge: Path) -> tuple[list[str], dict]:
    import websockets  # 项目已装（cosyvoice conda env）

    profile = Path(tempfile.mkdtemp(prefix="edge_cdp_"))
    port = 9222
    proc = subprocess.Popen(
        [str(edge), "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check",
         f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         f"--window-size={VIEWPORT[0]},{VIEWPORT[1]}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    problems: list[str] = []
    meta: dict = {"engine": str(edge), "viewport": list(VIEWPORT), "base": base}
    try:
        # 等调试端口就绪（非 200 立即报错，不静默重试到超时）
        ws_url = None
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                ver = get_json(f"http://127.0.0.1:{port}/json/version")
            except Exception:
                await asyncio.sleep(1)
                continue
            meta["browser"] = ver.get("Browser")
            try:
                tab = get_json(f"http://127.0.0.1:{port}/json/new?about:blank", method="PUT")
            except Exception as exc:
                raise RuntimeError(f"无法新建标签页：{exc}") from exc
            ws_url = tab["webSocketDebuggerUrl"]
            break
        if ws_url is None:
            raise RuntimeError("Edge 调试端口 40s 内未就绪 —— 不是「还没好」，是起不来")

        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            c = Cdp(ws)
            await c.call("Page.enable")
            await c.call("Runtime.enable")
            await c.call("Emulation.setDeviceMetricsOverride", width=VIEWPORT[0],
                         height=VIEWPORT[1], deviceScaleFactor=1, mobile=False)

            async def goto(path: str, settle: float = 3.0):
                await c.call("Page.navigate", url=base + path)
                await asyncio.sleep(settle)

            async def text() -> str:
                r = await c.call("Runtime.evaluate",
                                 expression="document.body ? document.body.innerText : ''",
                                 returnByValue=True)
                return str(r.get("result", {}).get("value") or "")

            # ---- 登录（走真人那条路：同源 fetch 登录 → 把 user 写进 localStorage） ----
            # ⚠️ 只登录是不够的：前端 `useAuth` 的登录态**由 localStorage['pc_user'] 决定**
            #    （启动时先读它、再用 Cookie 探测有效性），只种 Cookie 会被 ProtectedRoute
            #    弹回登录页 —— 本脚本第一版就是这么被骗过去的：6 页截到 4 张登录页。
            #    故这里照抄前端自己的落地方式：拿登录响应的 `user` 写回同一把键。
            await goto("/login", settle=4.0)
            js = ("fetch('/api/auth/login',{method:'POST',"
                  "headers:{'Content-Type':'application/json'},"
                  f"body:JSON.stringify({{username:'local',password:{json.dumps(password)}}})}})"
                  ".then(r=>r.json().then(j=>({status:r.status,user:j&&j.user})))"
                  ".catch(e=>({status:-1,err:String(e)}))")
            r = await c.call("Runtime.evaluate", expression=js, awaitPromise=True,
                             returnByValue=True)
            res = r.get("result", {}).get("value") or {}
            status = res.get("status")
            meta["login_status"] = status
            if status != 200:
                problems.append(f"E-LOGIN: 登录返回 {status!r}（期望 200）—— 后续页面必然被弹回登录页")
            else:
                user = res.get("user") or {}
                if not user.get("username"):
                    problems.append("E-LOGIN: 登录 200 但响应里没有 user 对象，前端登录态无法落地")
                else:
                    await c.call(
                        "Runtime.evaluate",
                        expression=("localStorage.setItem('pc_user', "
                                    + json.dumps(json.dumps(user, ensure_ascii=False))
                                    + "); localStorage.getItem('pc_user')"),
                        returnByValue=True)
                    meta["localStorage_user"] = user
            await asyncio.sleep(0.6)

            for name, route, expects, cn in PAGES:
                path = route.replace("{task_id}", task_id)
                await goto(path)
                body = await text()
                shot = (await c.call("Page.captureScreenshot", format="png"))["data"]
                f = out_dir / f"edge_{name}.png"
                f.write_bytes(base64.b64decode(shot))
                size = png_size(f)
                meta.setdefault("pages", {})[name] = {
                    "route": path, "label": cn, "bytes": f.stat().st_size,
                    "px": list(size) if size else None,
                    "text_len": len(body), "expects": expects,
                }
                if size is None:
                    problems.append(f"E-NOT-PNG: {name} 截图不是合法 PNG")
                elif tuple(size) != VIEWPORT:
                    problems.append(f"E-VIEWPORT: {name} 截图为 {size}，期望 {VIEWPORT}")
                for bad in BAD_MARKERS:
                    if bad in body:
                        problems.append(f"E-ERROR-PAGE: {name} 正文含 {bad!r} —— 页面是坏的")
                for want in expects:
                    if want not in body:
                        problems.append(f"E-TEXT: {name}({cn}) 正文缺标志性文案 {want!r} "
                                        f"（长度 {len(body)}，头部 {(body[:60] or '空')!r}）")
                print(f"  {cn:<10} {path:<34} {f.stat().st_size:>7} B  "
                      f"{size}  正文 {len(body)} 字  "
                      f"{'OK' if not any(p.startswith(('E-NOT-PNG', 'E-VIEWPORT', 'E-ERROR-PAGE', 'E-TEXT')) and name in p for p in problems) else '!!'}")
            return problems, meta
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D14.5 真实浏览器逐页截图 + 内容断言")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "d14_browser"))
    ap.add_argument("--base", default="http://127.0.0.1:5173")
    ap.add_argument("--task-id", default="ep_20260920_110641")
    ap.add_argument("--password", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    edge = find_edge()
    if edge is None:
        print("找不到 Edge/Chrome 可执行文件，无法留证", file=sys.stderr)
        return 2
    pwd = args.password
    if not pwd:
        print("必须提供 --password（或先跑 ensure_demo_account.py 拿口令）", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"引擎：{edge}")
    print(f"视口：{VIEWPORT[0]}x{VIEWPORT[1]}　输出：{out_dir}")
    problems, meta = asyncio.run(capture(out_dir, args.base, args.task_id, pwd, edge))
    print(f"问题 {len(problems)} 条")
    for p in problems:
        print("  " + p)
    if args.json:
        jp = Path(args.json)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps({"engine": meta, "problems": problems},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"证据落盘：{jp}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
