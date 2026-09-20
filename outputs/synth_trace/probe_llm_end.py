# -*- coding: utf-8 -*-
"""诊断：为什么真实链路里 tracer 抓不到 `yield speech len`（llm_end 全 null）。

分三步把链路拆开，每一步都打印可证伪的观测量：
  A. 纯 tracer 自测：装上钩子后手动 `logging.info('yield speech len ...')`，
     看 sink 是否收到；
  B. 真实引擎自测：load() 之后合成一句**全局唯一**的短文本，看 jsonl 里
     llm_end 是否非空、以及 root 的 handlers/level 当时是什么；
  C. 若 A/B 都正常，则问题只在「服务端 import 顺序」，回去改启动器。

用法（cwd=仓库根）：<cosyvoice python> outputs/synth_trace/probe_llm_end.py
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

from api.config import Settings, get_settings          # noqa: E402
from api.services.synth_trace import make_tracer       # noqa: E402

OUT = ROOT / "outputs" / "synth_trace" / "probe_llm_end.jsonl"


def _root_state(tag: str) -> None:
    r = logging.getLogger()
    print(f"[{tag}] root.level={logging.getLevelName(r.level)} "
          f"enabled_for_INFO={r.isEnabledFor(logging.INFO)} "
          f"handlers={[type(h).__name__ for h in r.handlers]}")


def main() -> int:
    if OUT.exists():
        OUT.unlink()

    # ---------------- A. 纯 tracer 自测 ----------------
    print("=== A. 纯 tracer 自测 ===")
    s = get_settings()
    s.synth_trace = True
    s.synth_trace_path = str(OUT)
    t = make_tracer(s)
    print("  tracer.enabled =", t.enabled, " path =", t.path)
    _root_state("A root")
    t.begin(seg=999)
    logging.getLogger().info("yield speech len 1.23, rtf 1.0")
    print("  sink 收到 =", t._yield_events)
    t.end()
    rec = json.loads(OUT.read_text(encoding="utf-8").splitlines()[-1])
    print("  A 记录 llm_end =", rec.get("llm_end"))
    a_ok = rec.get("llm_end") is not None

    # ---------------- B. 真实引擎自测 ----------------
    print("\n=== B. 真实引擎自测 ===")
    from api.services.tts import TTSEngine
    eng = TTSEngine.instance(s)
    eng.load()
    _root_state("B root after load")
    print("  engine tracer is 同上？", eng.tracer is t, " tracer.enabled =", eng.tracer.enabled)
    text = "诊断探针甲乙丙丁戊己庚辛壬癸用"   # 全局唯一，避免命中缓存
    r = eng.synthesize(text, "voice_a", speed=1.0, tone="")
    print(f"  合成完成 cached={r.cached} dur={r.duration_ms}ms")
    recs = [json.loads(x) for x in OUT.read_text(encoding="utf-8").splitlines() if x.strip()]
    last = recs[-1]
    print("  B 记录 llm_end =", last.get("llm_end"), " seg =", last.get("seg"))
    print("  marks =", last.get("marks"))
    b_ok = last.get("llm_end") is not None

    print(f"\n结论：A={a_ok}  B={b_ok}")
    return 0 if (a_ok and b_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
