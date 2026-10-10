#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D3 验收：合成服务封装（normalize + tts）端到端验证。

对应 D3 的完成判定：
    1. 输入 500 字段落，输出按句落盘的 wav 列表
    2. 同句二次调用命中缓存（不重复推理）
    3. 连续 20 句推理显存峰值不增长
    4. 显存双口径达标（allocated ≤ 3.6 GB 主 / reserved ≤ 4.2 GB 辅）

用法：
    python scripts/verify_d3.py                 # 默认 cosyvoice2
    python scripts/verify_d3.py --tone "请非常开心地说一句话。"   # 顺带验 instruct 指令路径
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("COSYVOICE_ONNX_DEVICE", "cpu")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

OUT_DIR = ROOT / "data" / "work" / "d3_verify"
REPORT = ROOT / "outputs" / "d3_verify_result.json"

# 500 字以上的播客口播段落：含阿拉伯数字、英文缩写、百分比、小数、长句与短句
# 「根据 2026 年……2 小时」一句刻意超过 40 字，用于检验 _split_soft 的逗号二次切分
PARAGRAPH = (
    "欢迎收听本期节目，我是主持人小林。今天要聊的是中文播客这三年的爆发式增长。先看一组数字。"
    "根据 2026 年上半年发布的行业报告，国内播客的听众规模已经突破 3 亿，其中大约 45% 的人每周至少听 2 小时。"
    "而在 2023 年，这个数字还不到 1.2 亿。也就是说，短短 3 年时间翻了一倍多。"
    "更值得关注的是收听场景的变化。通勤时段的占比从 62% 下降到了 48%。"
    "运动、做家务这类碎片时间反而上升到了 31%。这说明播客正在从通勤伴侣变成全天候陪伴。"
    "另一个明显趋势是 AI 技术的渗透。越来越多的创作者开始用 TTS 把文章变成音频。"
    "再用 RSS 分发出去，形成可以订阅的节目。制作周期从原来的 5 天压缩到 30 分钟以内。"
    "门槛降低之后，内容质量就成了唯一的护城河。我们在调研中还发现了一个有趣的现象。"
    "听众对节目时长的容忍度其实在下降。12 分钟左右的单集完播率最高，达到 78%。"
    "超过 30 分钟之后，完播率会掉到 40% 以下。所以与其追求宏大叙事，不如把单集做短做透。"
    "接下来的 20 分钟，我们会拆解 4 个关键趋势，也会聊聊普通创作者该怎么抓住这波机会。"
    "顺便说一句，我们也在用这套流水线做自己的节目。从选题到成片，全程只需要一杯咖啡的时间。"
    "希望你听完之后，能立刻动手做第一期节目。"
)


def build_turns() -> list[dict]:
    """把段落拆成 A/B 两个说话人的多轮对话，凑够 ≥20 句以测试显存稳定性。"""
    sentences = [s + "。" for s in PARAGRAPH.split("。") if s.strip()]
    turns = []
    for i, s in enumerate(sentences):
        turns.append({"speaker": "A" if i % 2 == 0 else "B", "text": s})
    return turns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tone", default="", help="非空则同时验证 instruct2 情绪指令路径")
    ap.add_argument("--keep", action="store_true", help="保留上次产物（默认先清缓存与产物）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from api.config import Settings
    from api.services.normalize import build_script_lines, build_segments, normalize_text
    from api.services.tts import TTSEngine

    settings = Settings()
    report: dict = {"ok": False, "steps": {}}

    # ---------------------------------------------------------------- 1) 规范化
    print("=" * 78)
    print("[1] 文本规范化与切句")
    print("=" * 78)
    norm = normalize_text(PARAGRAPH)
    lines = build_script_lines(build_turns())
    segs = build_segments(lines)
    chars_in = sum(len(l.read_text) for l in lines)
    chars_out = sum(s.char_count for s in segs)
    print(f"  原文 {len(PARAGRAPH)} 字 → 读文 {len(norm.read_text)} 字")
    print(f"  脚本行 {len(lines)} → 分段 {len(segs)} 句，最长 {max(s.char_count for s in segs)} 字")
    print(f"  字符守恒：读文 {chars_in} vs 分段 {chars_out} -> "
          f"{'一致' if chars_in == chars_out else '**不一致**'}")
    over = [s.seq for s in segs if s.char_count > settings.max_chars_per_seg]
    assert not over, f"存在超长分段：{over}"
    assert len(segs) >= 20, f"分段数 {len(segs)} < 20，无法验证显存稳定性"
    assert chars_in == chars_out, "切句丢字"
    report["steps"]["normalize"] = {
        "orig_chars": len(PARAGRAPH), "read_chars": len(norm.read_text),
        "lines": len(lines), "segments": len(segs),
        "max_seg_chars": max(s.char_count for s in segs),
        "reversible": norm.reversible, "warnings": norm.warnings[:5],
        "frontend": None,
    }

    # ---------------------------------------------------------------- 2) 加载
    print("\n" + "=" * 78)
    print("[2] 加载模型（单例 + fp16 + onnx 走 CPU）")
    print("=" * 78)
    data_clean = not args.keep
    if data_clean and OUT_DIR.exists():
        import shutil
        shutil.rmtree(OUT_DIR, ignore_errors=True)
    if data_clean and settings.cache_path.exists():
        import shutil
        shutil.rmtree(settings.cache_path, ignore_errors=True)

    eng = TTSEngine.instance(settings)
    eng.load()
    v0 = eng.vram()
    print(f"  模型类 {eng.model_class_name} | 加载 {eng.load_seconds:.1f} s | "
          f"text_frontend={eng.text_frontend}")
    print(f"  常驻 allocated={v0['allocated_mb']} MB / reserved={v0['reserved_mb']} MB "
          f"| 系统级已占 {v0['system_used_mb']}/{v0['system_total_mb']} MB")
    print(f"  音色档案 {[v.id for v in eng.registry.list()]}")
    assert eng.text_frontend, "R14 fail-fast 断言失效"
    report["steps"]["load"] = {"model_class": eng.model_class_name,
                               "load_s": round(eng.load_seconds, 1),
                               "text_frontend": eng.text_frontend,
                               "resident_alloc_mb": v0["allocated_mb"]}
    report["steps"]["normalize"]["frontend"] = eng.text_frontend
    report["steps"]["normalize"]["speaker_voice_map"] = settings.speaker_voice_map

    # ---------------------------------------------------------------- 3) 全量合成
    print("\n" + "=" * 78)
    print(f"[3] 逐句合成（{len(segs)} 句）→ {OUT_DIR}")
    print("=" * 78)
    prog: list[tuple] = []

    def _on_progress(i: int, n: int, r) -> None:
        prog.append((i, n, r.segment.char_count, round(r.elapsed_s, 2)))
        if i % 5 == 0 or i == n:
            print(f"    {i:3d}/{n}  {r.segment.speaker}  {r.segment.char_count:2d}字  "
                  f"{r.elapsed_s:5.2f}s  cached={r.cached}")

    t0 = time.time()
    results = eng.synthesize_lines(lines, out_dir=OUT_DIR, on_progress=_on_progress)
    wall = time.time() - t0
    audio_s = sum(r.duration_ms for r in results) / 1000
    wavs = sorted(OUT_DIR.glob("*.wav"))
    print(f"  完成 {len(results)} 句 / 落盘 {len(wavs)} 个 wav")
    print(f"  合成耗时 {wall:.1f} s / 音频 {audio_s:.1f} s -> RTF {wall / audio_s:.3f}")
    print(f"  单句均值 {wall / len(results):.2f} s（均值 {sum(s.char_count for s in segs) / len(segs):.0f} 字）")
    assert len(wavs) == len(segs), f"落盘 wav 数 {len(wavs)} != 分段数 {len(segs)}"
    assert all(r.duration_ms > 0 for r in results), "存在 0 时长音频"
    report["steps"]["synthesize"] = {
        "segments": len(segs), "wav_files": len(wavs), "wall_s": round(wall, 1),
        "audio_s": round(audio_s, 1), "rtf": round(wall / audio_s, 3),
        "avg_sentence_s": round(wall / len(results), 2),
    }

    # ---------------------------------------------------------------- 4) 缓存命中
    print("\n" + "=" * 78)
    print("[4] 句级缓存命中（同句二次调用不得重新推理）")
    print("=" * 78)
    probe = segs[2]
    key = eng.cache_key(probe.read_text, probe.speaker, 1.0, "")
    t1 = time.time()
    again = eng.synthesize(probe.read_text, probe.speaker, text_hash=key)
    dt = time.time() - t1
    print(f"  句子 #{probe.seq} 「{probe.read_text[:18]}…」 cached={again.cached} "
          f"| 耗时 {dt * 1000:.1f} ms | hash={key[:12]}")
    assert again.cached, "二次调用未命中缓存"
    assert dt < 0.5, f"缓存命中却耗时 {dt:.2f}s，说明仍在推理"
    # 缓存目录必须两级散列
    sample = sorted(settings.cache_path.glob("*/*.wav"))
    print(f"  缓存目录 {settings.cache_path} 共 {len(sample)} 个 wav")
    assert sample, "缓存目录为空"
    report["steps"]["cache"] = {"hit": True, "latency_ms": round(dt * 1000, 1),
                                "cache_files": len(sample)}

    # ---------------------------------------------------------------- 5) 显存稳定性
    print("\n" + "=" * 78)
    print(f"[5] 连续推理显存稳定性（{len(eng.memory_trace)} 句）")
    print("=" * 78)
    alloc = [m["allocated_mb"] for m in eng.memory_trace]
    peak = [m["peak_alloc_mb"] for m in eng.memory_trace]
    tail = alloc[2:]  # 丢掉前两句（含首次分配抖动）
    spread = max(tail) - min(tail)
    drift = alloc[-1] - alloc[2]
    print(f"  当前 allocated 轨迹：首句 {alloc[0]} → 第二句 {alloc[1] if len(alloc) > 1 else '-'} "
          f"→ 末句 {alloc[-1]} MB")
    print(f"  稳态区间波动 {spread:.1f} MB | 末句相对第二句漂移 {drift:+.1f} MB")
    print(f"  全程 peak_allocated {max(peak):.1f} MB")
    leak_ok = spread <= 150 and drift <= 150
    print(f"  -> 显存{'无增长（无泄漏）' if leak_ok else '**持续增长，疑似泄漏**'}")
    assert leak_ok, f"显存随时间增长：波动 {spread} MB / 漂移 {drift} MB"
    report["steps"]["memory_stability"] = {
        "sentences": len(eng.memory_trace), "spread_mb": round(spread, 1),
        "drift_mb": round(drift, 1),
        # 必须浅拷贝：memory_trace 是活对象，后续步骤（如 instruct2）会继续追加，
        # 不拷贝会让报告里的 trace 长度与 sentences 计数不一致
        "trace": list(eng.memory_trace),
    }

    # ---------------------------------------------------------------- 6) 显存门槛
    ok, msg = eng.check_budget()
    print("\n" + "=" * 78)
    print("[6] 显存双口径门槛")
    print("=" * 78)
    print(f"  {msg} -> {'达标' if ok else '超限'}")
    report["steps"]["budget"] = {"ok": ok, "message": msg, "vram": eng.vram()}

    # ---------------------------------------------------------------- 7) 情绪指令（可选）
    if args.tone:
        print("\n" + "=" * 78)
        print(f"[7] instruct2 情绪指令路径：{args.tone}")
        print("=" * 78)
        try:
            r = eng.synthesize("今天真的太开心了，我们拿到了第一名。", "voice_a", tone=args.tone)
            print(f"  OK | 时长 {r.duration_ms} ms | {r.wav_path}")
            report["steps"]["instruct2"] = {"ok": True, "duration_ms": r.duration_ms}
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL | {type(exc).__name__}: {exc}")
            report["steps"]["instruct2"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------------- 收尾
    est_1000 = 1000 / 260 * 60 * (wall / audio_s) / 60  # 1000 字 ≈ 3.85 min 音频
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)
    print(f"  RTF {wall / audio_s:.3f} | 1000 字纯合成推算 {est_1000:.2f} min（不含 LLM 与后期）")
    print(f"  产出目录：{OUT_DIR}")
    print(f"  缓存目录：{settings.cache_path}")
    report["ok"] = True
    report["summary"] = {"rtf": round(wall / audio_s, 3), "est_1000_chars_min": round(est_1000, 2)}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  报告：{REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
