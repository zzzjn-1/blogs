#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""D11 逐句一致率离线全量普查（对应 M3「合成文本与脚本文本逐句一致率 = 100%」）。

与 D10 `eval_batch.py --check-segments` 的关系
---------------------------------------------
D10 的回验跑在压测链路里，有两个客观限制：
  1. 只覆盖当批 12 条（491 行）；
  2. 第 3 层判据只能通过 HTTP 试听端点拿「该行首段」音频。

本脚本改为**离线只读**，覆盖库里全部**已成片**的任务（含历次冒烟 / 压测 / 手工成片），
直接读磁盘缓存做实测。**不触发任何合成推理，不占用 GPU。**

判据（四层行级 + 两条期级）
---------------------------
  L1  `read_text` 非空                             规范化读文已落库
  L2  `text_hash` 非空                             音频指纹已落库
  L3  `duration_ms > 0` 且 `seg_status = DONE`
  L4  `text_hash` 能定位到缓存 wav（文件在盘、时长可解析）
  L4b 物理不变量：首段时长 ≤ 该行 `duration_ms` + `WAV_TOL_MS`
  E1  期级：成片音频存在且时长可读
  E2  期级不变量：Σ行时长 ≤ 成片时长（成片 = 片头 + 语音 + 停顿 + 片尾，必大于语音总和）

`text_hash` / `duration_ms` 的粒度（**易踩，先读这段**）
-----------------------------------------------------
`task_runner._stage_synthesize` 落库时：

    ln.duration_ms = sum(r.duration_ms for r in segs)   # 行级：该行**所有段**之和
    ln.text_hash   = segs[0].text_hash                  # 段级：**只有首段**

两个字段**粒度不同**。因此「用 `text_hash` 取音轨、再拿 `duration_ms` 判它的时长」
对多句行必然得到 `wav < duration_ms`。这**不是数据缺陷**：多句行的每一段都已合成
（`task_runner` 拼接阶段缺任一段即抛 `TaskRunnerError`，而这些任务全是 DONE），
只是 `text_hash` 的语义是「该行的试听入口（首段）」，不是「整行音轨指纹」。

本脚本初版判据误把 `text_hash` 当整行指纹、写成等式，实测 **14/1120 行失败，且
14 行全是「一行多句」的行** —— 定位到粒度后改用物理不变量重写。**这不是放松判据**：
换上去的是一条在多句行上依然会红的不变量（若 `duration_ms` 被误按首段写入，
首段就会「长于整行」，`L4b` 立刻变红）。实测粒度分布见报告「二·补」节。

分母口径
--------
**只有已成片任务的行进入一致率分母**。中途放弃、从未成片的任务（`seg_status=PENDING`）
单列为「未成片任务」，不计入一致率 —— 否则会把「没做完」误报成「不一致」。
若某**已成片**任务里出现非 DONE 行，那是真实缺陷，照常计入失败。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WAV_TOL_MS = 20       # wav 时长与落库时长的取整容差
MAX_FAIL_ROWS = 200   # 报告里失败明细最多列出的行数


# --------------------------------------------------------------------------- #
# 音频时长解析（自解析 RIFF，不依赖 torchaudio / wave 模块）
# --------------------------------------------------------------------------- #

def wav_duration_ms(path: str | Path) -> int | None:
    """解析 RIFF/WAVE 头得到时长（毫秒）。失败返回 None（由调用方判为不可解析）。

    必须自解析而不用标准库 `wave`：本项目的缓存 wav 是 **IEEE float32**
    （fmt tag = 3），`wave` 模块只支持 PCM，会直接抛 `unknown format: 3`。
    同时支持 WAVE_FORMAT_EXTENSIBLE（tag = 0xFFFE），其真实格式在 SubFormat 前两字节。
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(65536)
    except OSError:
        return None
    if len(raw) < 44 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return None

    fmt: bytes | None = None
    data_len: int | None = None
    pos = 12
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        (size,) = struct.unpack_from("<I", raw, pos + 4)
        body = pos + 8
        if cid == b"fmt ":
            fmt = raw[body:body + size]
        elif cid == b"data":
            data_len = size
            break
        pos = body + size + (size & 1)   # chunk 按偶数字节对齐
    if fmt is None or data_len is None or len(fmt) < 16:
        return None

    tag, channels, rate, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", fmt, 0)
    if tag == 0xFFFE and len(fmt) >= 40:
        (tag,) = struct.unpack_from("<H", fmt, 24)
    if tag not in (1, 3) or channels <= 0 or rate <= 0 or bits <= 0:
        return None

    frames = data_len / (channels * (bits // 8))
    return int(round(frames / rate * 1000))


def probe_audio_ms(path: str | Path) -> int | None:
    """用 ffprobe 实测任意音频文件时长（毫秒）；ffprobe 不可用则返回 None。"""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return int(round(float(out.stdout.strip()) * 1000))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 判定（纯函数，便于表驱动单测）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Verdict:
    ok: bool
    layer: str              # 首个未通过的层；全过则为 ""
    reason: str = ""


def evaluate_line(line: dict, cache_row: dict | None,
                  wav_dur_ms: int | None, *, tol_ms: int = WAV_TOL_MS) -> Verdict:
    """单行判定。**按层短路**：只报首个未过的层，避免级联噪音掩盖根因。"""
    if not (line.get("read_text") or "").strip():
        return Verdict(False, "L1", "read_text 为空（规范化读文未落库）")
    if not (line.get("text_hash") or "").strip():
        return Verdict(False, "L2", "text_hash 为空（音频指纹未落库）")

    dur = int(line.get("duration_ms") or 0)
    status = line.get("seg_status")
    if status != "DONE" or dur <= 0:
        return Verdict(False, "L3", f"seg_status={status} / duration_ms={dur}（未落 DONE 或时长缺失）")

    if cache_row is None:
        return Verdict(False, "L4", f"audio_cache 查不到指纹 {str(line['text_hash'])[:12]}…（音轨无法定位）")
    if wav_dur_ms is None:
        return Verdict(False, "L4", f"缓存 wav 不存在或不可解析：{cache_row.get('wav_path')}")

    # L4b：text_hash 指向**首段**，首段时长不可能超过整行合计。
    # 超出即说明两个字段指向了不同的音频（例如 duration_ms 被误按首段写入）。
    if wav_dur_ms > dur + tol_ms:
        return Verdict(False, "L4b",
                       f"首段音频 {wav_dur_ms}ms 长于该行合计 {dur}ms"
                       f"（超出 {wav_dur_ms - dur}ms > 容差 {tol_ms}ms）"
                       "—— duration_ms 与实际音轨不是同一粒度")
    return Verdict(True, "")


def evaluate_episode(sum_line_ms: int, mp3_ms: int | None) -> Verdict:
    """期级判定：成片可读 + 「Σ行时长 ≤ 成片时长」这一硬不变量。"""
    if mp3_ms is None:
        return Verdict(False, "E1", "成片音频不存在或时长不可读")
    if mp3_ms <= 0:
        return Verdict(False, "E1", f"成片时长为 {mp3_ms}ms（不可信）")
    if sum_line_ms > mp3_ms:
        return Verdict(False, "E2",
                       f"行时长合计 {sum_line_ms}ms > 成片 {mp3_ms}ms"
                       "（成片含片头/停顿/片尾，理应更长）")
    return Verdict(True, "")


# --------------------------------------------------------------------------- #
# 数据装配
# --------------------------------------------------------------------------- #

def connect_readonly(db: str | Path) -> sqlite3.Connection:
    """只读连接。用 `query_only` 而非 `mode=ro`：后者在 WAL 库上可能读不到 -wal 内容。"""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def load_frames(conn: sqlite3.Connection, *, task_ids: list[str] | None = None) -> dict:
    """把库、缓存与成片信息装配成普查输入。"""
    eps = {r["task_id"]: dict(r) for r in conn.execute("select * from episodes")}
    cache = {r["text_hash"]: dict(r) for r in conn.execute("select * from audio_cache")}
    lines = [dict(r) for r in conn.execute(
        "select * from script_lines order by task_id, seq")]

    groups: dict[str, list[dict]] = {}
    for ln in lines:
        if task_ids and ln["task_id"] not in task_ids:
            continue
        groups.setdefault(ln["task_id"], []).append(ln)
    return {"episodes": eps, "cache": cache, "groups": groups}


def run(db: str | Path, *, task_ids: list[str] | None = None,
        use_ffprobe: bool = True, tol_ms: int = WAV_TOL_MS) -> dict:
    """执行普查，返回机器可读结果（也是 render_report 的唯一输入）。"""
    conn = connect_readonly(db)
    try:
        data = load_frames(conn, task_ids=task_ids)
    finally:
        conn.close()

    wav_cache: dict[str, int | None] = {}     # 同一 wav 多行共用，避免重复读盘
    episodes: list[dict] = []
    total_lines = passed_lines = 0
    failures: list[dict] = []
    idle_tasks: list[dict] = []
    # 粒度实测：用来**用数据证明** duration_ms 是「整行合计」而非「首段时长」
    granularity = {"exact": 0, "first_seg_shorter": 0, "max_gap_ms": 0}

    for tid, rows in sorted(data["groups"].items()):
        ep = data["episodes"].get(tid)
        if ep is None:
            idle_tasks.append({
                "task_id": tid,
                "lines": len(rows),
                "statuses": sorted({r["seg_status"] for r in rows}),
            })
            continue

        line_ok = 0
        sum_ms = 0
        for ln in rows:
            cr = data["cache"].get(ln["text_hash"]) if ln["text_hash"] else None
            wav_path = (cr or {}).get("wav_path")
            if wav_path:
                if wav_path not in wav_cache:
                    wav_cache[wav_path] = wav_duration_ms(wav_path)
                wav_ms = wav_cache[wav_path]
            else:
                wav_ms = None

            v = evaluate_line(ln, cr, wav_ms, tol_ms=tol_ms)
            total_lines += 1
            if v.ok:
                passed_lines += 1
                line_ok += 1
                dur_int = int(ln["duration_ms"] or 0)
                sum_ms += dur_int
                gap = dur_int - int(wav_ms or 0)
                if gap <= tol_ms:
                    granularity["exact"] += 1
                else:
                    granularity["first_seg_shorter"] += 1
                    granularity["max_gap_ms"] = max(granularity["max_gap_ms"], gap)
            else:
                failures.append({
                    "task_id": tid, "seq": ln["seq"], "layer": v.layer,
                    "speaker": ln.get("speaker"), "text": (ln.get("text") or "")[:60],
                    "reason": v.reason,
                })

        mp3_path = ep.get("mp3_path")
        mp3_ms = None
        mp3_src = "缺失"
        if mp3_path and Path(mp3_path).is_file():
            if use_ffprobe:
                mp3_ms = probe_audio_ms(mp3_path)
                mp3_src = "ffprobe 实测"
            if mp3_ms is None:                      # 降级：用落库整秒数
                sec = ep.get("duration_sec")
                if sec:
                    mp3_ms = int(sec) * 1000
                    mp3_src = "落库 duration_sec（ffprobe 不可用）"
        ev = evaluate_episode(sum_ms, mp3_ms)
        if not ev.ok:
            failures.append({
                "task_id": tid, "seq": 0, "layer": ev.layer,
                "speaker": "-", "text": ep.get("title") or "", "reason": ev.reason,
            })

        episodes.append({
            "task_id": tid, "title": ep.get("title") or "",
            "lines": len(rows), "lines_ok": line_ok,
            "sum_line_ms": sum_ms, "mp3_ms": mp3_ms, "mp3_src": mp3_src,
            "episode_ok": ev.ok, "episode_layer": ev.layer, "episode_reason": ev.reason,
        })

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": str(db), "tol_ms": tol_ms, "use_ffprobe": use_ffprobe,
        "totals": {
            "episodes": len(episodes),
            "lines": total_lines,
            "lines_passed": passed_lines,
            "consistency_rate": round(passed_lines / total_lines, 6) if total_lines else None,
            "failures": len(failures),
            "idle_tasks": len(idle_tasks),
            "idle_lines": sum(t["lines"] for t in idle_tasks),
        },
        "granularity": granularity,
        "episodes": episodes,
        "failures": failures,
        "idle_tasks": idle_tasks,
    }


# --------------------------------------------------------------------------- #
# 报告渲染（纯函数）
# --------------------------------------------------------------------------- #

def _md_table(head: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |",
           "| " + " | ".join("---" for _ in head) + " |"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def render_report(res: dict) -> str:
    t = res["totals"]
    g = res.get("granularity") or {}
    rate = t["consistency_rate"]
    out: list[str] = []
    out.append("# D11 逐句一致率离线全量普查\n")
    out.append(f"- 生成时间：{res['generated_at']}")
    out.append(f"- 数据库：`{res['db']}`（只读打开，`PRAGMA query_only=ON`）")
    out.append(f"- wav 时长容差：±{res['tol_ms']} ms")
    out.append(f"- 成片时长来源：{'ffprobe 实测' if res['use_ffprobe'] else '落库 duration_sec'}")
    out.append("")

    out.append("## 一、判据\n")
    out.append(_md_table(["层", "判据"], [
        ["L1", "`read_text` 非空"],
        ["L2", "`text_hash` 非空"],
        ["L3", "`duration_ms > 0` 且 `seg_status = DONE`"],
        ["L4", "`text_hash` 能定位到缓存 wav（文件在盘、时长可解析）"],
        ["L4b", f"首段时长 ≤ 该行 `duration_ms` + {res['tol_ms']}ms（物理不变量）"],
        ["E1", "成片音频存在且时长可读"],
        ["E2", "Σ行时长 ≤ 成片时长（成片含片头 / 停顿 / 片尾，理应更长）"],
    ]))
    out.append("")

    out.append("## 二、总览\n")
    out.append(_md_table(["指标", "实测", "目标", "判定"], [
        ["已成片期数", t["episodes"], "—", "—"],
        ["进入分母的行数", t["lines"], "—", "—"],
        ["**逐句一致率**",
         f"{t['lines_passed']}/{t['lines']} = {rate:.2%}" if rate is not None else "—",
         "= 100%", "**达标**" if rate == 1.0 else "**不达标**"],
        ["失败行数", t["failures"], "0", "达标" if t["failures"] == 0 else "**不达标**"],
        ["未成片任务（不计入一致率）", f"{t['idle_tasks']} 个 / {t['idle_lines']} 行", "—", "—"],
    ]))
    out.append("")

    out.append("## 二·补、`text_hash` / `duration_ms` 粒度实测\n")
    out.append("`task_runner._stage_synthesize` 里两个字段的写入粒度不同：")
    out.append("")
    out.append("```python")
    out.append("ln.duration_ms = sum(r.duration_ms for r in segs)   # 行级：所有段之和")
    out.append("ln.text_hash   = segs[0].text_hash                  # 段级：只有首段")
    out.append("```")
    out.append("")
    out.append("下表是**实测**（不是从代码推断）：首段 wav 实测时长与 `duration_ms` 的比较结果。")
    out.append("")
    out.append(_md_table(["情形", "行数", "含义"], [
        ["两者相等（≤ 容差）", g.get("exact", 0), "该行只有一段，或首段恰好等于整行"],
        ["首段更短", g.get("first_seg_shorter", 0), "该行被切成多段 —— `duration_ms` 是**行级合计**"],
    ]))
    out.append("")
    out.append(f"首段短于整行的最大差值为 **{g.get('max_gap_ms', 0)} ms**。"
               "数据支持「`duration_ms` = 整行合计、`text_hash` = 首段」这一粒度声明；"
               "L4b 正是按这个物理关系写的，**若将来有人把 `duration_ms` 按首段写入，L4b 会立刻变红**。")
    out.append("")

    out.append("## 三、逐期明细\n")
    rows = []
    for e in res["episodes"]:
        rows.append([
            e["task_id"], (e["title"] or "")[:22],
            f"{e['lines_ok']}/{e['lines']}",
            f"{e['sum_line_ms'] / 1000:.1f}", f"{e['mp3_ms'] / 1000:.1f}" if e["mp3_ms"] else "—",
            f"{e['sum_line_ms'] / e['mp3_ms'] * 100:.1f}%" if e["mp3_ms"] else "—",
            "OK" if e["episode_ok"] else f"**{e['episode_layer']}**",
        ])
    out.append(_md_table(
        ["task_id", "标题", "行通过", "Σ行时长 s", "成片 s", "Σ/成片", "期级"], rows))
    out.append("")

    out.append(f"## 四、失败明细（{len(res['failures'])} 条）\n")
    if not res["failures"]:
        out.append("**无失败项。**")
    else:
        for f in res["failures"][:MAX_FAIL_ROWS]:
            out.append(f"- `{f['task_id']}` seq={f['seq']} [{f['layer']}] "
                       f"{f['reason']}｜原文「{f['text']}」")
        if len(res["failures"]) > MAX_FAIL_ROWS:
            out.append(f"- …另有 {len(res['failures']) - MAX_FAIL_ROWS} 条（见 result.json）")
    out.append("")

    out.append("## 五、未成片任务（不计入一致率）\n")
    if not res["idle_tasks"]:
        out.append("无。")
    else:
        out.append(_md_table(["task_id", "行数", "段状态"],
                             [[x["task_id"], x["lines"], ",".join(x["statuses"])]
                              for x in res["idle_tasks"]]))
    out.append("")

    out.append("## 六、口径边界\n")
    out.append("1. 本表与 D10 `eval_batch.py` 的**一致率不是同一份样本**：D10 是当批 12 条的在线回验"
               "（12 条恰好都是单句行，所以其等式判据没有暴露粒度问题）；本表是库里全部已成片的离线普查。"
               "两者可互为交叉验证，**不可逐位对齐比较**。")
    out.append("2. **只有已成片任务的行进入分母**。中途放弃的任务（全 PENDING）单列于第五章；"
               "若已成片任务里出现非 DONE 行，属真实缺陷，照常计入失败。")
    out.append("3. 第 4 层只能证明「落库指纹能定位到音轨、且粒度自洽」，**不能证明读音正确** —— "
               "多音字错读在本口径下不可见，须靠 D11 的 MOS 人工听测发现。")
    out.append("4. 「每行首段在盘」不等于「每行所有段在盘」。后者由生产链路保证："
               "拼接阶段缺任一段即抛 `TaskRunnerError`，而进入本表的任务全部是 `DONE`。"
               "本脚本无法脱离当时的配置重建段级指纹（`cache_key` 含 model_version / seed / ratio），"
               "故不以事后重建的方式来证伪它。")
    out.append("5. 本脚本只读库与磁盘，**不触发任何合成推理**。")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="D11 逐句一致率离线全量普查（只读，不占 GPU）")
    ap.add_argument("--db", default="data/podcast.db", help="SQLite 库路径")
    ap.add_argument("--out", default="outputs/consistency",
                    help="产物目录（写 report.md 与 result.json）")
    ap.add_argument("--task-id", action="append", default=None,
                    help="只查指定任务，可重复；默认全部")
    ap.add_argument("--tol-ms", type=int, default=WAV_TOL_MS, help="wav 时长容差（毫秒）")
    ap.add_argument("--no-ffprobe", action="store_true",
                    help="不用 ffprobe 实测成片时长，改用落库 duration_sec")
    return ap


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    args = build_argparser().parse_args(argv)
    res = run(args.db, task_ids=args.task_id,
              use_ffprobe=not args.no_ffprobe, tol_ms=args.tol_ms)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(render_report(res), encoding="utf-8")

    t = res["totals"]
    g = res["granularity"]
    rate = t["consistency_rate"]
    print(f"已成片 {t['episodes']} 期 / {t['lines']} 行；"
          f"一致率 {t['lines_passed']}/{t['lines']}"
          + (f" = {rate:.2%}" if rate is not None else ""))
    print(f"失败 {t['failures']} 条；未成片任务 {t['idle_tasks']} 个（{t['idle_lines']} 行，不计入）")
    print(f"粒度实测：相等 {g['exact']} 行 / 首段更短 {g['first_seg_shorter']} 行"
          f"（最大差 {g['max_gap_ms']}ms）")
    print(f"产物：{out_dir}/report.md、{out_dir}/result.json")
    return 0 if t["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
