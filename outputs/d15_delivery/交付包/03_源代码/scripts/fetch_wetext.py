#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
抓取 wetext 文本规范化所需的 FST 模型（pengzhendong/wetext）

背景：CosyVoice 的 text_normalize 依赖 ttsfrd 或 wetext。本机无 ttsfrd，
      wetext==0.0.4 已装，但其 Normalizer.__init__ 会
      `snapshot_download("pengzhendong/wetext")` 拉取 tagger/verbalizer.fst。
      实测匿名请求 /revisions 会被 ModelScope 限流：
          403 {"Code":10013201001,"Message":"操作过于频繁，请稍后再试!"}
      而 /api/v1/models/pengzhendong/wetext 本身返回 200（仓库公开存在）。
      → 属**限流**而非权限问题，带退避重试即可。

失败也不阻塞主链路（仅文本规范化降级），但会使「合成文本与脚本逐句一致率 100%」
失去保障（数字/英文不会转写为中文读法），必须在上线前解决。
"""
from __future__ import annotations

import os
import sys
import time
import traceback

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(WS, "logs", "fetch_wetext.log")
REPO = "pengzhendong/wetext"
MAX_TRY = 8
SLEEP = 25

_lines: list[str] = []


def log(msg: str = "") -> None:
    _lines.append(str(msg))
    try:
        print(msg, flush=True)
    except Exception:  # noqa: BLE001
        pass


def flush() -> None:
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_lines))


def main() -> int:
    log("=" * 70)
    log(f"抓取 wetext FST 模型：{REPO}（最多 {MAX_TRY} 次，间隔 {SLEEP}s）")
    log(f"MODELSCOPE_API_TOKEN = {'已设置' if os.environ.get('MODELSCOPE_API_TOKEN') else '未设置（匿名）'}")
    log("=" * 70)

    # 已就绪？
    try:
        from wetext import Normalizer
        n = Normalizer(lang="zh", operator="tn")
        log("[SKIP] wetext 已可用")
        log("  '2026年' -> " + n.normalize("2026年"))
        return 0
    except Exception as e:  # noqa: BLE001
        log(f"[STEP] 当前不可用：{type(e).__name__}: {str(e)[:200]}")
        log("")

    from modelscope import snapshot_download

    last = None
    for i in range(1, MAX_TRY + 1):
        log(f"--- 第 {i}/{MAX_TRY} 次尝试 ---")
        try:
            d = snapshot_download(REPO)
            log(f"  [OK] 已下载到 {d}")
            for root, dirs, files in os.walk(d):
                rel = os.path.relpath(root, d)
                if rel == ".":
                    rel = ""
                for f in sorted(files):
                    p = os.path.join(root, f)
                    log(f"    {os.path.join(rel, f) if rel else f:44s} {os.path.getsize(p)} B")
            break
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:300]}"
            log(f"  [FAIL] {last}")
            if i < MAX_TRY:
                log(f"  等待 {SLEEP}s 后重试（限流退避）...")
                time.sleep(SLEEP)

    log("")
    log("=" * 70)
    log("验证")
    log("=" * 70)
    ok = False
    try:
        from wetext import Normalizer
        n = Normalizer(lang="zh", operator="tn")
        log("  [OK] wetext.Normalizer 可用")
        log("  '2026年'        -> " + n.normalize("2026年"))
        log("  '45%的人'       -> " + n.normalize("45%的人"))
        log("  '每周听2小时'    -> " + n.normalize("每周听2小时"))
        ok = True
    except Exception:  # noqa: BLE001
        log("  [FAIL] " + traceback.format_exc()[-900:])
        if last:
            log("  最后一次抓取错误：" + last)

    log("")
    log(f"WETEXT_EXIT={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())
        rc = 99
    finally:
        flush()
    sys.exit(rc)
