# -*- coding: utf-8 -*-
"""诊断 MOS 包自检的两处 FAIL（item4 数出 5、item6 数出 7）。

思路：解码 mos_pack.mp3，按 self-check 同样的窗口取包络，
把「被判为 burst」的连通区位置打印出来——看第 5/7 个到底是
真多出来的提示音，还是窗口边界外溢进来的正文语音（假阳性）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(r"D:\podcast-ai")
sys.path.insert(0, str(ROOT / "scripts"))

import make_listen_pack as lp  # noqa: E402

EVAL_DIR = ROOT / "outputs" / "eval" / "20260918_0920"
PACK = EVAL_DIR / "mos" / "mos_pack.mp3"
MANIFEST = EVAL_DIR / "mos" / "mos_manifest.json"


def bursts_detail(y: np.ndarray, rel: float = 0.3, min_frames: int = 5):
    """返回 [(起始样本, 持续帧数)]，与 lp.count_bursts 同判据。"""
    hop = int(0.010 * lp.SR)
    n = int(0.020 * lp.SR)
    frames = np.lib.stride_tricks.sliding_window_view(y, n)[::hop]
    env = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    out, run, start = [], 0, 0
    for k, v in enumerate(env > env.max() * rel):
        if v:
            if run == 0:
                start = k
            run += 1
        else:
            if run >= min_frames:
                out.append((start * hop, run))
            run = 0
    if run >= min_frames:
        out.append((start * hop, run))
    return out, env


def main() -> int:
    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    items = man["items"] if isinstance(man, dict) and "items" in man else man
    full = lp.decode(PACK)
    print(f"包总样本 {len(full)}（{len(full)/lp.SR:.1f}s）  SR={lp.SR}")
    print(f"BEEP_DUR={lp.BEEP_DUR} BEEP_GAP={lp.BEEP_GAP} GAP_AFTER_BEEPS={lp.GAP_AFTER_BEEPS}")

    for it in items:
        i = it.get("i")
        if i not in (3, 4, 5, 6, 7):
            continue
        span = i * (lp.BEEP_DUR + lp.BEEP_GAP) + 0.05
        bs = int(it["beep_start"])
        is_ = int(it["item_start"])
        w = full[bs:bs + int(span * lp.SR)]
        found, env = bursts_detail(w)
        print(f"\n--- item{i}: beep_start={bs}（{bs/lp.SR:.3f}s） item_start={is_}（{is_/lp.SR:.3f}s）"
              f" 窗口 {span:.3f}s ---")
        print(f"    判定 burst 数 = {len(found)}（期望 {i}）")
        for st, ln in found:
            print(f"      burst @ +{st/lp.SR:.3f}s  持续 {ln*0.010:.2f}s  "
                  f"峰 {env[st//int(0.010*lp.SR)]:.4f}（窗口 max {env.max():.4f}）")
        # 窗口尾部之后的正文起点相对位置
        print(f"    item 正文起点距窗口结束 {(is_ - bs)/lp.SR - span:+.3f}s")
        # 最后 0.2s 包络（看是否在窗口尾巴上混入正文）
        tail = env[-20:]
        print(f"    窗口最后 0.2s 包络: {np.round(tail/env.max(), 2).tolist()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
