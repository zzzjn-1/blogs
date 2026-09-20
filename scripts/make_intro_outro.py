"""生成片头 / 片尾**女声（voice_a）**素材，替换合成音占位素材（R18 收口）。

背景（为什么要单独做脚本，而不是随便丢一个 mp3 进去）
--------------------------------------------------------
`api/services/postprocess.py` 对片头尾**只做 `unify()` + `apply_fade()`，不做 loudnorm**
（见 `postprocess()` 中 intro/outro 分支）。这意味着**素材自身响度就是成片响度**，
片头若比正片轻/响，听感上会非常突兀。所以本脚本在落盘前先走 `normalize_loudness()`，
把片头尾归一到与正片相同的目标（settings.audio_loudness_i，约 −16 LUFS）。

规格保持 `backend/assets/README.md` 约定 → **零改动替换**（无需改代码/配置）：
mp3 / 44100 Hz / 单声道 / 128 kbps。

用法
----
    python scripts/make_intro_outro.py                 # 合成并替换
    python scripts/make_intro_outro.py --dry-run       # 只打印计划，不加载模型
    python scripts/make_intro_outro.py --text-intro "…" --text-outro "…"
    # 动态片头（文案含日期 / 主题占位符，按当期渲染）
    python scripts/make_intro_outro.py --only intro --use-template \
        --date "2026年9月16日" --topic "人工智能"

语气说明
--------
⚠️ 素材必须用 `inference_zero_shot`（tone 空），**绝不能走 instruct2**。
实测：`inference_instruct2` 在 `zero_shot_spk_id` 非空时会把 `prompt_wav`
（voice_a.wav，内容正是「生活就像海洋…」）作为声学前缀回显到输出开头，
导致素材开头先朗读一遍参考音频旧句（旧版 intro.mp3 / outro.mp3 即带此句）。
`inference_zero_shot` 改用预计算 spk2info，只输出目标文本、不回显，音色仍由
零样本克隆（voice_a）决定，听感无差异。故本脚本素材一律 tone=""（纯克隆）。

输出
----
- `backend/assets/{intro,outro}.mp3`（新素材）
- 旧素材备份至 `backend/assets/_archive/<时间戳>/`
- 控制台打印电平台账（时长 / 响度 / 真峰 / 是否削波）
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# 通用文案（与具体话题无关，任何一期都可复用）
# 用户要求：「片头片尾由女生发言，一般情况下使用博客通用的开场白」
# --------------------------------------------------------------------------- #
DEFAULT_INTRO = "大家好，欢迎收听本期播客！今天，我们用一段轻松的对话，好好聊聊这个话题。"
DEFAULT_OUTRO = "好啦，以上就是今天的全部内容。感谢你的陪伴和收听，我们下期再见！"

# --------------------------------------------------------------------------- #
# 动态文案模板（用户 2026-09-16 指定）
# 片头需按当期信息渲染，占位符支持全角 / 半角 / format 三种写法：
#   （日期）(日期) {date}      （主题）(主题) {topic}
# 渲染逻辑与运行时共用 api/services/intro_outro.py，避免脚本与主链路漂移。
# --------------------------------------------------------------------------- #
from api.services.intro_outro import (  # noqa: E402
    DEFAULT_INTRO_TEMPLATE,
    render_template,
    today_cn,
)

# --------------------------------------------------------------------------- #
# 语气（语调层，与文案无关）
# ⚠️ 素材必须用 zero_shot（tone 空）：instruct2 会回显参考音频旧句（见上方语气说明）。
# 故 DEFAULT_TONE 设为空串，synthesize 走 inference_zero_shot 纯克隆路径。
# --------------------------------------------------------------------------- #
DEFAULT_TONE = ""  # 强制 zero_shot；instruct2 回显 prompt_wav 旧句，禁用
DEFAULT_SPEED = 1.05       # 片尾语速（用户认可的手感，勿轻改）
DEFAULT_SPEED_INTRO = 1.05  # 片头语速：用户 2026-09-16 拍板「就按 outro 的感觉」→ 与片尾同档

# 历史语气指令（曾因「充满活力」用力过猛，听感偏僵硬，已弃用）：
#   "用轻松愉快、语调上扬、充满活力的语气说这句话，让人感到放松和开心"

SPEAKER = "voice_a"  # 女声（用户本人 life.m4a，统一自相关 F0 175.8 Hz）


def _probe_mp3(path: Path, s) -> dict:
    """对最终 mp3 做体检：时长 / 响度 / 真峰 / 削波。"""
    from api.services.postprocess import measure_loudness, probe

    info = probe(path, settings=s)
    out = {"duration_s": round(info.duration_s, 2) if getattr(info, "duration_s", None) else None}
    try:
        m = measure_loudness(path, settings=s)
        out["loudness_i"] = m.get("input_i")
        out["true_peak"] = m.get("input_tp")
    except Exception as exc:  # pragma: no cover - 体检失败不应中断主流程
        out["loudness_err"] = str(exc)[:120]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生成片头/片尾女声素材（替换占位）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不加载模型/不写文件")
    ap.add_argument("--text-intro", default=DEFAULT_INTRO, help="开场白文本")
    ap.add_argument("--text-outro", default=DEFAULT_OUTRO, help="结束语文本")
    ap.add_argument("--use-template", action="store_true",
                    help="片头改用动态模板 DEFAULT_INTRO_TEMPLATE（需配合 --date / --topic）")
    ap.add_argument("--date", default=None, help="（日期）占位符取值，默认今天（中文读法）")
    ap.add_argument("--topic", default=None, help="（主题）占位符取值，默认「本期话题」")
    ap.add_argument("--speaker", default=SPEAKER, help="音色 id（默认 voice_a 女声）")
    ap.add_argument("--tone", default=DEFAULT_TONE,
                    help="（保留参数，素材固定走 zero_shot）传空串=纯克隆；"
                         "instruct2 会回显参考音频旧句，故脚本实际忽略非空的 tone")
    ap.add_argument("--speed", type=float, default=DEFAULT_SPEED, help="语速倍率（默认 1.05）")
    ap.add_argument("--speed-intro", type=float, default=None, help="单独覆盖片头语速")
    ap.add_argument("--speed-outro", type=float, default=None, help="单独覆盖片尾语速")
    ap.add_argument("--only", choices=["intro", "outro", "both"], default="both",
                    help="只处理片头 / 片尾 / 两者（默认 both）")
    ap.add_argument("--no-replace", action="store_true",
                    help="不替换 assets 现役素材，仅生成到 work 目录（做候选对比时用）")
    ap.add_argument("--tag", default="", help="--no-replace 时的文件名后缀，避免多档互相覆盖")
    ap.add_argument("--no-backup", action="store_true", help="不备份旧素材")
    args = ap.parse_args()

    # 动态模板渲染（片头按当期日期 / 主题生成）
    date_s = args.date or today_cn()
    topic_s = args.topic or "本期话题"
    if args.use_template:
        args.text_intro = render_template(DEFAULT_INTRO_TEMPLATE, date_s, topic_s)

    # 片头 / 片尾语速分开取默认：两者文案长度与听感需求不同
    sp_intro = args.speed_intro if args.speed_intro is not None else DEFAULT_SPEED_INTRO
    sp_outro = args.speed_outro if args.speed_outro is not None else args.speed

    assets = ROOT / "backend" / "assets"
    work = ROOT / "data" / "work" / "assets_voice"

    plan = [("intro", args.text_intro, sp_intro), ("outro", args.text_outro, sp_outro)]
    if args.only != "both":
        plan = [p for p in plan if p[0] == args.only]

    print("=" * 68)
    print("片头 / 片尾女声素材生成")
    print("=" * 68)
    print("音色    : %s（女声）" % args.speaker)
    print("语气    : %s" % (args.tone if args.tone else "（关闭，纯克隆路径）"))
    if args.use_template:
        print("模板    : %s" % DEFAULT_INTRO_TEMPLATE)
        print("  渲染  : 日期=%s / 主题=%s" % (date_s, topic_s))
    for kind, text, sp in plan:
        print("  %-5s : 语速 %s | %s（%d 字）" % (kind, sp, text, len(text)))
    print("目标    : mp3 / 44100 Hz / 单声道 / 128 kbps + loudnorm 至正片口径")
    print("输出    : %s" % ("work（候选，不替换现役）" if args.no_replace else assets))
    if args.dry_run:
        print("\n[dry-run] 未加载模型，未写入任何文件。")
        return 0

    from api.config import get_settings
    from api.services import postprocess as pp
    from api.services.tts import TTSEngine

    s = get_settings()
    ffmpeg, ffprobe = pp.find_ffmpeg(s)
    print("\nffmpeg : %s" % ffmpeg)
    print("ffprobe: %s" % ffprobe)

    work.mkdir(parents=True, exist_ok=True)

    # ---------------- 加载引擎（冷启动 ~23 s）----------------
    t0 = time.time()
    engine = TTSEngine(s).load()
    print("引擎已加载：%.1f s" % (time.time() - t0))

    results = {}
    for kind, text, sp in plan:
        print("\n--- %s（语速 %s）---" % (kind, sp))
        # 素材必须走 zero_shot（tone 强制空）：instruct2 会把 prompt_wav
        # （voice_a.wav 内容=「生活就像海洋…」）回显到输出开头（已实测旧素材带此句）。
        # zero_shot + zero_shot_spk_id=voice_a 仍克隆女声，听感无差异、且无回显。
        res = engine.synthesize(text, speaker=args.speaker,
                                speed=sp, tone="")
        print("  合成: %.2f s / %d ms / cached=%s" %
              (res.elapsed_s, res.duration_ms, getattr(res, "cached", False)))

        src = Path(res.wav_path)

        # 响度归一：后期对片头尾不做 loudnorm，必须在此对齐正片口径
        m = pp.measure_loudness(src, settings=s)
        ok, reason = pp.loudness_usable(m)
        if not ok:
            print("  ⚠️ 响度测量不可用（%s）→ 跳过归一，直接导出" % reason)
            normed = src
        else:
            normed, _ = pp.normalize_loudness(
                src, work / ("%s_norm.wav" % kind), measured=m, settings=s)
            print("  响度归一: input_i=%s LUFS → 目标 %s LUFS" %
                  (m.get("input_i"), s.audio_loudness_i))

        if args.no_replace:
            # 候选模式：文件名带语速，避免多档互相覆盖
            fname = "%s_sp%s%s.mp3" % (kind, "%g" % sp,
                                       ("_" + args.tag) if args.tag else "")
        else:
            fname = "%s.mp3" % kind
        mp3 = pp.export_mp3(normed, work / fname,
                            sample_rate=44100, channels=1, bitrate="128k", settings=s)
        info = _probe_mp3(mp3, s)
        results[kind] = {"mp3": mp3, "info": info}
        print("  导出: %s" % mp3.name)
        print("  体检: 时长=%s s / 响度=%s LUFS / 真峰=%s dBTP" %
              (info.get("duration_s"), info.get("loudness_i"), info.get("true_peak")))

    # ---------------- 候选模式：到此为止，不碰现役素材 ----------------
    if args.no_replace:
        print("\n" + "=" * 68)
        print("候选已生成（未替换现役素材）")
        print("=" * 68)
        for kind, _, sp in plan:
            src = results[kind]["mp3"]
            info = results[kind]["info"]
            secs = info.get("duration_s") or 0
            n_chars = len(args.text_intro if kind == "intro" else args.text_outro)
            print("  %s  %s" % (kind, src))
            print("       %7d B / %s s / %s LUFS / 真峰 %s / 语速 %.2f 字每秒" %
                  (src.stat().st_size, secs, info.get("loudness_i"),
                   info.get("true_peak"), (n_chars / secs) if secs else 0))
        print("\n选中某档后，用对应 --speed 去掉 --no-replace 重跑即可落盘。")
        return 0

    # ---------------- 备份 + 替换 ----------------
    if not args.no_backup:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        bak = assets / "_archive" / stamp
        bak.mkdir(parents=True, exist_ok=True)
        for kind, _, _sp in plan:
            old = assets / ("%s.mp3" % kind)
            if old.is_file():
                shutil.copy2(old, bak / ("%s.mp3" % kind))
                print("  已备份 %s → %s" % (old.name, bak))

    print("\n" + "=" * 68)
    print("替换结果")
    print("=" * 68)
    for kind, _, sp in plan:
        src = results[kind]["mp3"]
        dst = assets / ("%s.mp3" % kind)
        shutil.copy2(src, dst)
        info = results[kind]["info"]
        print("  %-5s : %7d B / %s s / %s LUFS / 真峰 %s（语速 %s）" %
              (kind, dst.stat().st_size, info.get("duration_s"),
               info.get("loudness_i"), info.get("true_peak"), sp))

    print("\n提示：重跑端到端（如 verify_d5）确认片头尾已生效；")
    print("      如需换文案，重跑本脚本并传 --text-intro / --text-outro。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
