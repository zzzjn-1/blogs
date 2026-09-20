# -*- coding: utf-8 -*-
"""诊断 2：走 `TTSEngine.synthesize_lines()`（服务端同一路径）看 llm_end 是否能抓到。

探针 1（probe_llm_end.py）有个**假绿**：B 步合成走的是 `synthesize()` 单点调用，
不经过 `synthesize_lines` 的 begin/end，所以根本没写记录，它读到的「最后一条」
其实是 A 步那条 → 结论 `B=True` 是错的。这里改成断言**本次合成真正写出的记录**。

同时打印：root 上每个 `_YieldLenHandler` 的 sink 是不是当前 tracer 的那个 list ——
`_pop_yield_since()` 曾经用赋值（`self._yield_events = ...`）重建列表，
会让 handler 继续写进**孤儿列表**，从此再也收不到事件。

用法（cwd=仓库根）：<cosyvoice python> outputs/synth_trace/probe_llm_end2.py
"""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

from api.config import get_settings                          # noqa: E402
from api.services.normalize import build_script_lines        # noqa: E402
from api.services.tts import TTSEngine                       # noqa: E402

OUT = ROOT / "outputs" / "synth_trace" / "probe_llm_end2.jsonl"

#: 每次运行都不同 → 保证句级缓存全未命中
NONCE = "甲乙丙丁戊己庚辛壬癸"[:6] + str(int(time.time()))[-4:]


def main() -> int:
    if OUT.exists():
        OUT.unlink()
    s = get_settings()
    s.synth_trace = True
    s.synth_trace_path = str(OUT)

    eng = TTSEngine.instance(s)
    eng.load()
    tr = eng.tracer

    root = logging.getLogger()
    print(f"root.level={logging.getLevelName(root.level)} "
          f"enabled_for_INFO={root.isEnabledFor(logging.INFO)}")
    print("tracer.enabled =", tr.enabled, "tracer.path =", tr.path)
    for h in root.handlers:
        sink = getattr(h, "_sink", None)
        same = "" if sink is None else f"  sink_is_tracer_list={sink is tr._yield_events}"
        print(f"  root handler: {type(h).__name__} level={logging.getLevelName(h.level)}{same}")

    texts = [f"诊断探针{NONCE}甲号这条用于复核阶段标记。",
             f"诊断探针{NONCE}乙号另一句不该命中缓存。"]
    lines = build_script_lines([{"speaker": "A", "text": texts[0]},
                                {"speaker": "B", "text": texts[1]}],
                               max_chars=40)
    work = ROOT / "outputs" / "synth_trace" / "probe_work"
    work.mkdir(parents=True, exist_ok=True)

    print("\n调用 synthesize_lines() …")
    results = eng.synthesize_lines(lines, out_dir=work, speed=1.0, tone="")
    print("段数 =", len(results), " cached =", [r.cached for r in results])
    print("合成后 tracer._yield_events =", tr._yield_events)

    recs = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    print(f"\n本次写出 {len(recs)} 条记录：")
    ok = 0
    for r in recs:
        nonnull = r.get("llm_end") is not None
        ok += nonnull
        print(f"  seg={r.get('seg')} chars={r.get('chars')} "
              f"dur={r.get('dur_ms')} llm_end={'SET' if nonnull else 'NULL'}")

    print(f"\n结论：{ok}/{len(recs)} 条抓到 llm_end")
    return 0 if (recs and ok == len(recs)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
