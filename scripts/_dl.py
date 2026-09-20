#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
通用断点续传下载器（本机网络环境下实测最稳的实现）。

设计要点（都是踩坑后确定的）：
1. **只用单流**。本机到 GitHub / ModelScope 的分片下载不可靠——codeload 会忽略
   Range 头返回 200 全量，导致多线程各自下载完整文件并越界写入（实测日志出现
   600% 进度且 zip 损坏）。单流 + 续传是稳定方案。
2. **严格校验服务端是否真的支持续传**。请求 Range 后若返回 200（而非 206），
   必须从头重下，不能把全量数据接到已有偏移后面。
3. **落盘用 .part 临时文件 + 校验后原子替换**，避免半成品被误用。
"""
from __future__ import annotations

import os
import time
import urllib.request
import urllib.error

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
TIMEOUT = 60
RETRY = 6


def head_size(url: str) -> int:
    """取文件总字节数；失败返回 -1。"""
    req = urllib.request.Request(url, headers={**HEADERS, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                return int(cr.rsplit("/", 1)[-1])
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else -1
    except Exception:
        return -1


def download(url: str, dest: str, total: int | None = None,
             verbose: bool = True) -> tuple[bool, int, str]:
    """
    下载 url 到 dest（自动续传）。

    返回 (是否成功, 字节数, 错误信息)
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"

    if total is None or total <= 0:
        total = head_size(url)
    if total <= 0:
        total = 0  # 未知长度也允许下（靠 EOF 判断）

    have = os.path.getsize(part) if os.path.exists(part) else 0
    if total and have == total:
        os.replace(part, dest)
        if verbose:
            print(f"    已完成（{total} 字节），跳过")
        return True, total, ""
    if total and have > total:
        os.remove(part)
        have = 0

    for attempt in range(1, RETRY + 1):
        try:
            hdrs = dict(HEADERS)
            if have:
                hdrs["Range"] = f"bytes={have}-"
            req = urllib.request.Request(url, headers=hdrs)
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                # 关键：服务端未按 Range 响应时必须重头下
                if have and r.status != 206:
                    if verbose:
                        print("    服务端不支持续传，从头开始")
                    have = 0
                    if os.path.exists(part):
                        os.remove(part)
                    # 重新发起普通请求
                    req2 = urllib.request.Request(url, headers=dict(HEADERS))
                    with urllib.request.urlopen(req2, timeout=TIMEOUT) as r2:
                        _write(r2, part, have, total, verbose, t0)
                else:
                    _write(r, part, have, total, verbose, t0)
            got = os.path.getsize(part)
            if total and got != total:
                raise IOError(f"长度不符 {got}/{total}")
            os.replace(part, dest)
            return True, got, ""
        except Exception as e:  # noqa: BLE001 - 任何异常都重试并保留 .part
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if verbose:
                print(f"    第 {attempt} 次中断（已存 {have} 字节）: {type(e).__name__}: {e}")
            if attempt < RETRY:
                time.sleep(min(2 * attempt, 8))
    return False, os.path.getsize(part) if os.path.exists(part) else 0, "重试耗尽"


def _write(resp, part: str, have: int, total: int, verbose: bool, t0: float) -> None:
    mode = "ab" if have else "wb"
    done = have
    last = time.time()
    with open(part, mode) as f:
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if verbose and time.time() - last > 5:
                last = time.time()
                dt = max(time.time() - t0, 0.001)
                if total:
                    print(f"    {100.0 * done / total:5.1f}%  "
                          f"{done / 1e6:7.2f}/{total / 1e6:.2f} MB  "
                          f"{chunk and (done - have) / 1024 / dt:7.0f} KB/s")
                else:
                    print(f"    {done / 1e6:7.2f} MB  {(done - have) / 1024 / dt:7.0f} KB/s")
