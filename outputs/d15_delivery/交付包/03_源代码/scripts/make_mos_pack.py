#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D10 交付物：MOS 评测音频包 + 评分表 + 说明（开发计划书 6.2 D10 行）。

D10 的 Done 条件里有一条是「产出 MOS 评测音频包并交付评测人」。本脚本把
`scripts/eval_batch.py` 跑出来的成片裁成等长片段、按序号加提示音、拼成**单个 mp3**，
再附上评分表模板与评分说明——评测人拿到一个文件和一张表就能开工，不需要听 12 个独立文件。

## 三条纪律（直接沿用 `make_listen_pack.py` 的实测结论，不重新发明）

1. **只允许静态增益**，禁用 loudnorm / dynaudnorm / speechnorm。MOS 评的是「自然度」，
   动态处理会改动包络与信噪比，把被测对象从「系统产出」换成「后处理产出」。
2. **增益取「响度目标」与「峰值天花板」两约束的较小值**（`feasible_target`），
   天花板按最终 MP3 解码后的实测峰值判定——有损编码会过冲。
3. **上采样一律经 ffmpeg**（`-ar 44100`），并在成包上做无削波自检。

## 为什么默认目标是 −16 LUFS 而不是试听包的 −18

试听包（A/B 比对）要对齐到一个共同电平才有可比性，所以它主动压到 −18；
MOS 包不同：它要评的**就是交付出去的那条成片**。成片本身已被后期归一到 −16 LUFS，
于是这里把目标设成 −16、天花板设成 −1.5（与 `AUDIO_LOUDNESS_I` / `AUDIO_TRUE_PEAK` 同值），
增益≈0 dB —— 评测人听到的就是用户会听到的东西。任何额外增益都会让 MOS 偏离交付物。

## 提示音

第 i 项前播 i 声 1 kHz 短音（1000 Hz / 0.15 s / −20 dBFS，间隔 0.15 s）。
「响几声 = 第几项」使评测人可以在评分表上对号入座，且**不依赖屏幕**——
评审常见场景是一个人戴耳机听完整包再回填表格。

用法（项目根目录）：

    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts/make_mos_pack.py --eval-dir outputs/eval/20260917_180000
    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts/make_mos_pack.py --seg 0     # 整条不截
    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts/make_mos_pack.py --limit 4   # 先出小包试听

产物（默认 `<eval-dir>/mos/`）：

    mos_pack.mp3         单个评测音频包（提示音已内嵌）
    mos_scoresheet.csv   评分表模板（Excel 直开，含 BOM）
    mos_README.md        评分说明：5 分制各档定义、流程、回收方式
    mos_manifest.json    逐项台账（源文件 / SHA1 / 增益 / 成品片段起止），供复核
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (str(ROOT), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 复用试听包已实测过的底层工具：提示音结构、写 wav、解码、响度测量、可达目标推算
import make_listen_pack as lp                                  # noqa: E402

log = logging.getLogger("make_mos_pack")

SR = lp.SR
#: 与成片配置同值（audio_loudness_i / audio_true_peak）：目标就是交付物本身的电平
DEFAULT_TARGET_LUFS = -16.0
DEFAULT_CEILING_DBFS = -1.5
DEFAULT_SEG_S = 45.0
DEFAULT_OFFSET_S = 20.0        # 跳过动态片头（约 11~13 s），直接进正片
#: 项间响度差超过这个值就告警（不判失败：成片电平由后期决定，造包无权改）
SPREAD_WARN_LU = 1.5


# --------------------------------------------------------------------------- #
# 纯函数（可离线单测）
# --------------------------------------------------------------------------- #

def plan_clip(duration_s: float | None, *, offset: float,
              seg: float) -> tuple[float, float | None]:
    """规划单个片段的 `(起点, 时长)`。

    - `seg <= 0` 表示「不截，用整条」（时长返回 None，交给 ffmpeg 读到结尾）；
    - 起点会被夹到「不越过文件末尾」——短成片（< offset+seg）若直接取 offset
      会得到空片段，ffmpeg 产出的静音会被误当成「系统输出没声音」。
    """
    if seg and seg > 0:
        if duration_s is None:
            return round(offset, 3), round(seg, 3)
        start = max(0.0, min(offset, max(0.0, duration_s - seg)))
        length = min(seg, max(0.0, duration_s - start))
        return round(start, 3), round(length, 3)
    if not duration_s or offset <= 0:
        return 0.0, None
    return round(max(0.0, min(offset, duration_s)), 3), None


def pick_cases(results: dict, *, ids: list[str] | None = None,
               limit: int | None = None) -> list[dict]:
    """从 results.json 里挑出「有本地产物且已完成」的 case，保持原顺序。"""
    rows = [c for c in (results.get("cases") or [])
            if c.get("status") == "DONE" and c.get("audio_path")]
    if ids:
        want = {i.strip() for i in ids if i.strip()}
        rows = [c for c in rows if c.get("id") in want]
        missing = want - {c.get("id") for c in rows}
        if missing:
            raise SystemExit(f"results.json 里没有这些可用 case：{sorted(missing)}")
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        raise SystemExit("没有可用的 case（需要 status=DONE 且 audio_path 存在）")
    return rows


SCORESHEET_COLUMNS = [
    ("index", "序号"), ("id", "编号"), ("category", "类别"), ("topic", "主题"),
    ("clip", "片段位置"),
    ("naturalness", "自然度(1-5)"), ("prosody", "停顿节奏(1-5)"),
    ("speaker_distinct", "音色区分度(1-5)"),
    ("multi_syllable", "多音字问题(有/无)"), ("note", "备注"),
]


def render_scoresheet(rows: list[dict]) -> str:
    """评分表模板：只填「评分列」，其余列由脚本预填（避免人工抄错主题）。

    刻意留空而非填 0——空单元格在 Excel 里一眼可见，填 0 会被误当成「已评 0 分」。
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in SCORESHEET_COLUMNS])
    for i, r in enumerate(rows, 1):
        w.writerow([i, r.get("id"), r.get("category"), r.get("topic"),
                    r.get("clip", ""), "", "", "", "", ""])
    return buf.getvalue()


SCALE_ROWS = [
    ("5", "几乎与真人听不出差别：韵律自然、换人处停顿恰当、无错读"),
    ("4", "自然，细听有轻微机械感或个别停顿略生硬，不影响理解"),
    ("3", "可听懂且听感可接受，但明显是合成音（韵律偏平/停顿机械）"),
    ("2", "多处不自然：停顿位置突兀、语调单调，需费力才能听下去"),
    ("1", "严重影响理解：错读、断句错误、语速异常或音质明显损坏"),
]


def render_readme(meta: dict, rows: list[dict]) -> str:
    """评分说明：把「怎么听、怎么打、怎么回」写清楚，减少回收后的返工。"""
    out: list[str] = []
    out.append("# MOS 评测说明\n")
    out.append(f"- 音频包：`{meta.get('pack_name')}`")
    out.append(f"- 生成时间：{meta.get('generated_at')}")
    out.append(f"- 片段策略：{meta.get('clip_policy')}")
    out.append(f"- 材料来源：`{meta.get('eval_dir')}`（D10 端到端压测产物）")
    out.append(f"- 目标电平：{meta.get('target_lufs')} LUFS / 峰值天花板 "
               f"{meta.get('ceiling_dbfs')} dBFS（静态增益，未做动态处理）")
    out.append("")
    out.append("## 一、怎么听\n")
    out.append("1. 用**耳机**在安静环境里完整听一遍整包，不要跳着听（评分比的是整体自然度）。")
    out.append("2. 每段前会先播 **N 声短提示音**，响几声就是第几项——对着评分表的「序号」填即可。")
    out.append(f"3. 每项片段约 {meta.get('seg_desc')}，项间有约 0.9 s 静音分隔。")
    out.append("4. 音量调到「能听清但不刺耳」后**不要再动**：本包各项已用静态增益对齐到同一电平，")
    out.append("   中途改音量会让后面的项听起来不一样。")
    out.append("5. 为保证「各项之间可比」且不削波，整包电平可能比成片本身低几个 LU——")
    out.append("   这是静态增益受峰值天花板约束的结果，**不是音频质量问题**，也不必因此压分。")
    out.append("")
    out.append("## 二、怎么打分（5 分制）\n")
    out.append("| 分数 | 档位定义 |")
    out.append("| --- | --- |")
    for score, desc in SCALE_ROWS:
        out.append(f"| {score} | {desc} |")
    out.append("")
    out.append("- **自然度**：整体听感，主指标（验收线：均分 ≥ 3.5）。")
    out.append("- **停顿节奏**：换人处（话轮）与句间的停顿是否像真人对话。")
    out.append("- **音色区分度**：两位说话人能否听出是两个人。")
    out.append("- **多音字问题**：听到读错的字就写下来（形如「重(zhòng)要」），没有写「无」——")
    out.append("  这是 D11「bad case 修复」的输入，比分数本身更有用。")
    out.append("- **备注**：位置（如「第 3 声后 12 秒」）＋现象，便于复现。")
    out.append("")
    out.append("## 三、怎么回\n")
    out.append("1. 填 `mos_scoresheet.csv`（Excel 打开，只填评分列与备注列），文件名加自己的名字后缀。")
    out.append("2. 连同原始音频包一起回传，**不要转码**（转码会改变听感，破坏可比性）。")
    out.append("3. 建议 3~5 人独立评，取均分；原始打分表全部保留（计划书 R5 要求）。")
    out.append("")
    out.append("## 四、本包包含的项\n")
    out.append("| 序号 | 编号 | 类别 | 主题 | 片段位置 |")
    out.append("| --- | --- | --- | --- | --- |")
    for i, r in enumerate(rows, 1):
        out.append(f"| {i} | {r.get('id')} | {r.get('category')} | "
                   f"{r.get('topic')} | {r.get('clip', '')} |")
    out.append("")
    out.append("> 本包片段来自 D10 压测的**真实系统产出**（未经人工挑选）。")
    out.append("> 若某项整体异常（如明显爆音），请在备注里写明，不要靠压低分数表达——")
    out.append("> 分数用于判定是否达标，异常用于 D11 定位缺陷，两者分开记录才不互相污染。")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# 造包
# --------------------------------------------------------------------------- #

def _count_beeps_selfref(y: np.ndarray) -> int:
    """在**纯提示音区间**内数提示音个数，基准取第一声提示音自身电平。

    与 `lp.count_bursts`（相对整窗最大值）的区别：本函数只把窗口当提示音，
    基准取窗口开头 `BEEP_DUR` 内的包络峰值 ×0.5。这样即使末尾混进一丝正文，
    也不会把基准顶高、把判据带偏。

    帧长 20 ms、跳步 10 ms、连通区至少 5 帧（≥50 ms）才算一声——
    提示音 `BEEP_DUR=0.15 s`，与 `BEEP_GAP=0.15 s` 之间有充分静音可分离。
    """
    hop = int(0.010 * SR)
    n = int(0.020 * SR)
    if len(y) < n:
        return 0
    frames = np.lib.stride_tricks.sliding_window_view(y, n)[::hop]
    env = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    ref_n = max(1, int(lp.BEEP_DUR * SR) // hop)
    ref = float(env[:ref_n].max()) if len(env[:ref_n]) else 0.0
    if ref <= 0:
        return 0
    bursts, run = 0, 0
    for v in env > ref * 0.5:
        if v:
            run += 1
        else:
            if run >= 5:
                bursts += 1
            run = 0
    if run >= 5:
        bursts += 1
    return bursts


def build(eval_dir: Path, *, out_dir: Path, seg: float, offset: float,
          target_lufs: float, ceiling: float, ids: list[str] | None,
          limit: int | None) -> int:
    if not lp.FFMPEG or not lp.FFPROBE:
        log.error("找不到 ffmpeg / ffprobe，无法造包")
        return 2

    results_path = eval_dir / "results.json"
    if not results_path.is_file():
        log.error("缺少压测明细：%s（先跑 scripts/eval_batch.py）", results_path)
        return 2
    results = json.loads(results_path.read_text(encoding="utf-8"))
    raw_rows = pick_cases(results, ids=ids, limit=limit)

    # 只保留源文件仍存在的项（results.json 可能是别处跑出来的）
    rows: list[dict] = []
    for r in raw_rows:
        p = Path(r["audio_path"])
        if not p.is_file():
            log.warning("跳过 %s：源成片不存在 %s", r.get("id"), p)
            continue
        rows.append(dict(r, _path=p))
    if not rows:
        log.error("所有 case 的成片都不存在，无法造包")
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="mospack_"))
    pack_name = "mos_pack.mp3"

    # ---- 第一遍：解码片段并量源，推算可达目标响度 ----
    print("=" * 92)
    print("源片段")
    print("=" * 92)
    items: list[dict] = []
    for r in rows:
        dur = r.get("audio_duration_s")
        ss, t = plan_clip(dur, offset=offset, seg=seg)
        y = lp.decode(r["_path"], ss=ss, dur=t)
        # < 0.4 s 的素材 ebur128 测不出综合响度（input_i = -inf），会让峰均比变成 inf。
        # 这种情况只可能来自成片本身异常，直接跳过并告警，不要把 inf 带进后续算术。
        if len(y) < int(0.4 * SR):
            log.warning("跳过 %s：可用片段仅 %.2f s（< 0.4 s，无法测响度）",
                        r.get("id"), len(y) / SR)
            continue
        i = len(items) + 1
        probe = tmpdir / f"src{i}.wav"
        lp.write_wav(probe, y)
        m0 = lp.measure(probe)
        crest = m0["sample_peak_db"] - m0["I"]
        clip = (f"整条（去前 {ss:.0f}s）" if t is None
                else f"{ss:.0f}–{ss + len(y) / SR:.0f}s（{len(y) / SR:.1f}s）")
        items.append({"row": r, "i": i, "y": y, "m0": m0, "crest": crest, "clip": clip,
                      "ss": ss, "t": t})
        print(f"  [{i:2d}] {r.get('id'):>3} 《{r.get('topic')}》 {clip}")
        print(f"       I={m0['I']:.2f} LUFS / 采样峰值 {m0['sample_peak_db']:.2f} dBFS"
              f" / 峰均比 {crest:.2f} dB / 时长 {len(y) / SR:.3f} s")

    if not items:
        log.error("过滤后没有可造包的片段（成片过短？），放弃")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 2

    crest_max = max(d["crest"] for d in items)
    target = lp.feasible_target(target_lufs, ceiling, crest_max)
    print()
    print(f"目标响度：期望 {target_lufs} LUFS；可达上界 {target:.2f} LUFS"
          f"（= 天花板 {ceiling} − 最大峰均比 {crest_max:.2f} − 0.5 余量）")
    if target < target_lufs - 1e-9:
        print("  注：期望目标不可达，已回落到可达上界 —— 静态增益的物理限制，非缺陷。")
    print("=" * 92)

    # ---- 第二遍：静态增益 + 提示音 + 拼接 ----
    timeline: list[np.ndarray] = [np.zeros(int(lp.LEAD_SILENCE * SR))]
    cursor = len(timeline[0])
    marks: list[dict] = []
    for d in items:
        i, y, m0 = d["i"], d["y"], d["m0"]
        gain = min(target - m0["I"], ceiling - m0["sample_peak_db"])
        limited_by = "峰值" if (ceiling - m0["sample_peak_db"]) < (target - m0["I"]) else "响度"
        y2 = y * (10 ** (gain / 20.0))

        beeps, beep_lead = lp.make_beep_group(i)
        beep_start = cursor + beep_lead
        timeline.append(beeps)
        cursor += len(beeps)
        item_start = cursor
        timeline.append(y2)
        cursor += len(y2)
        timeline.append(np.zeros(int(lp.GAP_AFTER_ITEM * SR)))
        cursor += len(timeline[-1])

        g = tmpdir / f"gain{i}.wav"
        lp.write_wav(g, y2)
        m1 = lp.measure(g)
        d.update({"gain": gain, "limited_by": limited_by, "m1": m1,
                  "beep_start": beep_start, "item_start": item_start,
                  "n": len(y2), "clip": d["clip"]})
        print(f"  [{i:2d}] 增益 {gain:+.2f} dB（{limited_by}约束）"
              f" -> {m1['I']:.2f} LUFS / 采样峰值 {m1['sample_peak_db']:.2f} dBFS")

    timeline.append(np.zeros(int(lp.TAIL_SILENCE * SR)))
    mixed = tmpdir / "mixed.wav"
    lp.write_wav(mixed, np.concatenate(timeline))
    out_path = out_dir / pack_name
    p = lp.run_bin([lp.FFMPEG, "-y", "-v", "error", "-i", str(mixed),
                    "-c:a", "libmp3lame", "-b:a", "192k",
                    "-ar", str(SR), "-ac", "1", str(out_path)])
    if p.returncode != 0:
        log.error("编码失败：%s", p.stderr.decode("utf-8", "replace")[:400])
        return 2

    # ---- 自检 ----
    print()
    print("=" * 92)
    print("自检")
    print("=" * 92)
    m_all = lp.measure(out_path)
    ok = True
    c = m_all["sample_peak_db"] <= ceiling + 0.5 and m_all["TP"] <= ceiling + 0.5
    print(f"  [{'PASS' if c else 'FAIL'}] 编码后无削波：采样峰值 "
          f"{m_all['sample_peak_db']:.2f} dBFS、真峰 {m_all['TP']:.2f} dBTP"
          f"（均须 ≤ {ceiling + 0.5:.1f}）")
    ok &= c

    full = lp.decode(out_path)
    finals: list[float] = []
    for d in items:
        seg_arr = full[d["item_start"]:d["item_start"] + d["n"]]
        w = tmpdir / f"chk{d['i']}.wav"
        lp.write_wav(w, seg_arr)
        ms = lp.measure(w)
        finals.append(ms["I"])
        dev = abs(ms["I"] - target)
        c = dev <= lp.LOUDNESS_TOL
        print(f"  [{'PASS' if c else 'FAIL'}] item{d['i']:2d} 响度落点 {ms['I']:.2f} LUFS"
              f"（目标 {target:.2f}，偏差 {dev:.2f} LU ≤ {lp.LOUDNESS_TOL}）")
        ok &= c

    for d in items:
        # 只在「准确的提示音区间」内计数，并用**第一声提示音自身电平**做基准。
        # 为什么不用「窗口局部最大值 × 相对阈值」：提示音比正文语音**轻**
        # （正文峰值 ~-2 dBFS，提示音按 BEEP_PEAK_DBFS 生成），一旦窗口里混进一点
        # 正文（原先多伸了 0.05 s 正好探进片段开头，而片段从第 20 s 切入、常常切在词中间），
        # 基准就被语音顶上去，判据随即失真——实测 item4/item6 被误判多出一声。
        n_beeps = int(d["i"])
        exact = n_beeps * lp.BEEP_DUR + max(0, n_beeps - 1) * lp.BEEP_GAP + 0.03
        w = full[d["beep_start"]:d["beep_start"] + int(exact * SR)]
        n = _count_beeps_selfref(w)
        c = n == n_beeps
        print(f"  [{'PASS' if c else 'FAIL'}] item{d['i']:2d} 提示音个数 = {n}"
              f"（期望 {n_beeps}）")
        ok &= c

    spread = max(finals) - min(finals)
    warn = spread > SPREAD_WARN_LU
    print(f"  [{'WARN' if warn else 'PASS'}] 项间响度差 {spread:.2f} LU"
          f"（{min(finals):.2f} ~ {max(finals):.2f} LUFS；告警线 {SPREAD_WARN_LU}）"
          + ("　—— 成片电平由后期决定，造包无权改；如差异明显请回查 D5/D6 后期。" if warn else ""))

    # ---- 台账 / 评分表 / 说明 ----
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "eval_dir": str(eval_dir),
        "pack": str(out_path),
        "target_lufs": round(target, 2),
        "ceiling_dbfs": ceiling,
        "clip_policy": f"offset={offset}s, seg={seg}s",
        "final_peak_dbfs": round(m_all["sample_peak_db"], 2),
        "final_true_peak_dbtp": round(m_all["TP"], 2),
        "loudness_spread_lu": round(spread, 2),
        "items": [
            {
                "序号": d["i"], "编号": d["row"].get("id"),
                "主题": d["row"].get("topic"), "类别": d["row"].get("category"),
                "源文件": Path(d["row"]["audio_path"]).name,
                "源SHA1": lp.sha1_of(Path(d["row"]["audio_path"]))[:16],
                "源时长s": d["row"].get("audio_duration_s"),
                "片段起s": d["ss"], "片段长s": round(d["n"] / SR, 3),
                "源响度LUFS": round(d["m0"]["I"], 2),
                "源采样峰值dBFS": round(d["m0"]["sample_peak_db"], 2),
                "施加静态增益dB": round(d["gain"], 2), "受限项": d["limited_by"],
                "最终响度LUFS": round(d["m1"]["I"], 2),
                "最终采样峰值dBFS": round(d["m1"]["sample_peak_db"], 2),
                # 时间点写进台账：自检的提示音计数窗口就依赖它，留证据便于人工复核
                "提示音起点s": round(d["beep_start"] / SR, 3),
                "正文起点s": round(d["item_start"] / SR, 3),
            }
            for d in items
        ],
    }
    (out_dir / "mos_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    sheet_rows = [dict(d["row"], clip=d["clip"]) for d in items]
    (out_dir / "mos_scoresheet.csv").write_text(
        render_scoresheet(sheet_rows), encoding="utf-8-sig")

    meta = {
        "pack_name": pack_name,
        "generated_at": manifest["generated_at"],
        "eval_dir": str(eval_dir),
        "clip_policy": manifest["clip_policy"],
        "target_lufs": round(target, 2),
        "ceiling_dbfs": ceiling,
        "seg_desc": ("整条正片" if seg <= 0 else f"{seg:.0f} 秒"),
    }
    (out_dir / "mos_README.md").write_text(
        render_readme(meta, sheet_rows), encoding="utf-8")

    size_mb = out_path.stat().st_size / 1024 / 1024
    print()
    print("=" * 92)
    print(f"音频包：{out_path}（{size_mb:.2f} MB，真峰 {m_all['TP']:.2f} dBTP，"
          f"总长 {len(full) / SR / 60:.1f} min）")
    print(f"评分表：{out_dir / 'mos_scoresheet.csv'}")
    print(f"说明  ：{out_dir / 'mos_README.md'}")
    print(f"台账  ：{out_dir / 'mos_manifest.json'}")
    print("判定：" + ("PASS" if ok else "FAIL"))
    print("=" * 92)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if ok else 1


def _default_eval_dir() -> Path:
    base = ROOT / "outputs" / "eval"
    if not base.is_dir():
        raise SystemExit(f"找不到压测产物目录 {base}，请先跑 scripts/eval_batch.py")
    dirs = sorted([d for d in base.iterdir() if d.is_dir()], reverse=True)
    if not dirs:
        raise SystemExit(f"{base} 下没有子目录，请先跑 scripts/eval_batch.py")
    return dirs[0]


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")               # type: ignore[union-attr]
        except Exception:                                      # noqa: BLE001
            pass
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout, force=True)

    ap = argparse.ArgumentParser(description="构建 MOS 评测音频包（含评分表与说明）")
    ap.add_argument("--eval-dir", default="",
                    help="eval_batch 产物目录（默认取 outputs/eval 下最新一个）")
    ap.add_argument("--out", default="", help="输出目录（默认 <eval-dir>/mos）")
    ap.add_argument("--seg", type=float, default=DEFAULT_SEG_S,
                    help=f"每项片段时长秒；0 = 整条不截（默认 {DEFAULT_SEG_S:.0f}）")
    ap.add_argument("--offset", type=float, default=DEFAULT_OFFSET_S,
                    help=f"片段起点秒，用于跳过片头（默认 {DEFAULT_OFFSET_S:.0f}）")
    ap.add_argument("--target-lufs", type=float, default=DEFAULT_TARGET_LUFS,
                    help=f"目标响度（默认 {DEFAULT_TARGET_LUFS}，与成片一致）")
    ap.add_argument("--ceiling", type=float, default=DEFAULT_CEILING_DBFS,
                    help=f"峰值天花板 dBFS（默认 {DEFAULT_CEILING_DBFS}）")
    ap.add_argument("--ids", default="", help="只收录这些编号，逗号分隔")
    ap.add_argument("--limit", type=int, default=None, help="只收录前 N 项")
    args = ap.parse_args(argv)

    eval_dir = Path(args.eval_dir) if args.eval_dir else _default_eval_dir()
    out_dir = Path(args.out) if args.out else (eval_dir / "mos")
    ids = [s for s in (args.ids or "").split(",") if s.strip()]
    return build(eval_dir, out_dir=out_dir, seg=args.seg, offset=args.offset,
                 target_lufs=args.target_lufs, ceiling=args.ceiling,
                 ids=ids or None, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
