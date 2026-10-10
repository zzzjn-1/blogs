"""端到端命令行入口：一条命令从主题产出完整 mp3（D5 Done 条件）。

    python scripts/cli.py --topic "为什么天空是蓝色的" --duration 2

链路：主题 →（LLM）对话脚本 →（CosyVoice）逐句合成 →（FFmpeg）后期 → mp3。

也支持用 `--turns-json` 跳过 LLM，直接给一份已有脚本（便于离线复现后期问题）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 与 verify_d3.py 保持一致：ONNX 走 CPU，避免与主模型抢显存
os.environ.setdefault("COSYVOICE_ONNX_DEVICE", "cpu")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def slugify(text: str, limit: int = 40) -> str:
    """把主题变成安全的文件名片段：保留中日韩与字母数字，其余折叠为下划线。"""
    s = _ILLEGAL.sub("_", (text or "").strip())
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_.")
    if len(s) > limit:
        s = s[:limit].rstrip("_.")
    return s or "podcast"


def load_turns_from_json(path: str) -> tuple[list[dict], str]:
    """读取外部脚本：兼容 {script:{lines:[]}} / {lines:[]} / [{speaker,text}] 三种形态。"""
    p = Path(path)
    if not p.is_file():
        raise SystemExit("脚本文件不存在：%s" % p)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit("脚本文件不是合法 JSON：%s（%s）" % (p, exc))

    title = ""
    if isinstance(data, list):
        lines = data
    elif isinstance(data, dict):
        script = data.get("script") if isinstance(data.get("script"), dict) else data
        lines = script.get("lines") or []
        title = str(script.get("title") or "")
    else:
        raise SystemExit("脚本文件结构无法识别：%s" % p)

    # lines 写成字符串时，直接遍历会得到一堆单字符 —— 报「第 1 行不是对象：'n'」纯属误导
    if not isinstance(lines, list):
        raise SystemExit("脚本文件的 lines 应为数组，实际是 %s：%s"
                         % (type(lines).__name__, p))

    turns: list[dict] = []
    for i, item in enumerate(lines, 1):
        if not isinstance(item, dict):
            raise SystemExit("第 %d 行不是对象：%r" % (i, item))
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        spk = str(item.get("speaker") or "").strip().upper()
        # 允许直接用 voice id；否则按序号 A/B 交替兜底
        turns.append({"speaker": spk or ("A" if i % 2 else "B"), "text": text})
    if not turns:
        raise SystemExit("脚本文件里没有任何有效对白：%s" % p)
    return turns, title


def _abort(stage_name: str, exc: Exception, out_dir: Path, stage: dict, t0: float) -> int:
    """把领域异常翻译成一行可读错误 + 一份失败摘要，返回非零退出码。

    为什么不在 `__main__` 里一把兜住：阶段不同，用户要采取的动作完全不同
    （脚本阶段查 Key/网络、合成阶段查显存、后期阶段查 FFmpeg），
    所以失败时必须指明**卡在哪一阶段**，而不是丢一个栈给用户自己看。
    """
    stage["ok"] = False
    stage["failed_stage"] = stage_name
    stage["error"] = "%s: %s" % (type(exc).__name__, exc)
    stage["elapsed_s"] = round(time.perf_counter() - t0, 2)
    summary = out_dir / "run_summary.json"
    try:
        summary.write_text(json.dumps(stage, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        summary = None
    print("\n[失败] %s阶段：%s" % (stage_name, exc), file=sys.stderr)
    if summary is not None:
        print("       失败摘要：%s" % summary, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="主题 → 对话脚本 → 语音合成 → FFmpeg 后期 → mp3",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    src = ap.add_argument_group("内容来源")
    src.add_argument("--topic", default="", help="节目主题；与 --turns-json 二选一")
    src.add_argument("--turns-json", default="",
                     help="已有脚本 JSON（给定时跳过 LLM，直接进合成）")
    src.add_argument("--duration", type=float, default=0.0,
                     help="目标时长（分钟）；与 --words 二选一，都缺则按 5 分钟")
    src.add_argument("--words", type=int, default=0, help="目标字数（优先于 --duration）")
    src.add_argument("--style", default="", help="语言风格提示")
    src.add_argument("--outline", default="", help="大纲/要点提示")
    src.add_argument("--reasoner", action="store_true", help="改用推理模型（较慢、较贵）")

    tts = ap.add_argument_group("语音合成")
    tts.add_argument("--speed", type=float, default=1.0, help="语速")
    tts.add_argument("--tone", default="", help="情绪指令（instruct2），留空则不用")
    tts.add_argument("--no-tts", action="store_true",
                     help="只出脚本，不做合成与后期（排障用）")

    post = ap.add_argument_group("后期与导出")
    post.add_argument("--intro", default=None, help="片头素材路径（覆盖默认配置）")
    post.add_argument("--outro", default=None, help="片尾素材路径（覆盖默认配置）")
    post.add_argument("--no-intro", action="store_true", help="显式禁用片头")
    post.add_argument("--no-outro", action="store_true", help="显式禁用片尾")
    post.add_argument("--no-normalize", action="store_true", help="跳过 loudnorm")

    out = ap.add_argument_group("输出")
    out.add_argument("--out", default="", help="输出目录；默认 outputs/cli_runs/<时间戳>")
    out.add_argument("--name", default="", help="mp3 文件名（不含扩展名）；默认由主题生成")
    out.add_argument("--drop-wav", action="store_true", help="不保留拼接后的中间 wav")
    out.add_argument("--quiet", action="store_true", help="只输出最终 JSON 摘要")

    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING if args.quiet else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if not args.topic and not args.turns_json:
        ap.error("需要 --topic 或 --turns-json 之一")

    from api.config import Settings
    from api.services import postprocess as pp

    settings = Settings()
    if args.no_normalize:
        settings = settings.model_copy(update={"audio_normalize": False})

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) if args.out else (ROOT / "outputs" / "cli_runs" / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)

    name = args.name or slugify(args.topic or "episode")
    stage: dict = {"ok": False, "stages": {}}
    t_all = time.perf_counter()

    def say(msg: str) -> None:
        if not args.quiet:
            print(msg, flush=True)

    # ---------------- 阶段 1：脚本 ----------------
    if args.turns_json:
        say("[1/3] 读取脚本 %s（跳过 LLM）" % args.turns_json)
        turns, title = load_turns_from_json(args.turns_json)
        if not name or name == "episode":
            name = slugify(title or Path(args.turns_json).stem)
        stage["stages"]["script"] = {"source": str(args.turns_json), "lines": len(turns),
                                     "title": title}
    else:
        say("[1/3] 生成脚本：%s" % args.topic)
        from api.services.script_gen import ScriptGenError, ScriptGenerator

        gen = ScriptGenerator(settings)
        try:
            sres = gen.generate(
                topic=args.topic,
                target_words=(args.words or None),
                duration_min=(args.duration or None),
                style=args.style, outline=args.outline, use_reasoner=args.reasoner)
        except ScriptGenError as exc:
            return _abort("脚本生成", exc, out_dir, stage, t_all)
        turns = [{"speaker": t.speaker, "text": t.text} for t in sres.script.lines]
        title = sres.script.title
        # 把**正文**一并写进 script.json 的 script 键：load_turns_from_json 认识
        # {script:{lines:[]}}，于是本轮的 script.json 可直接喂回 `--turns-json` 复现，
        # 不必重调 LLM。此前只存生成报告（无 lines），产物无法自我复现。
        report = sres.to_dict()
        report["script"] = {"title": title, "lines": turns}
        (out_dir / "script.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        say("      脚本已落盘：%s（可用 --turns-json 复现本轮，跳过 LLM）"
            % (out_dir / "script.json"))
        say("      标题：%s" % title)
        # 用 script.total_chars 而不是 sum(len(text))：偏差是按 count_chars（去空白）算的，
        # 两个口径混用会出现「字数是 A、偏差却按 B 算」的对不上账
        say("      %d 行 / %d 字（目标 %d，偏差 %+.1f%%）；模型返回 %s；调用 %d 次；%.1f s"
            % (len(turns), sres.script.total_chars, sres.target_words,
               sres.word_deviation_pct, sres.model_returned, sres.calls, sres.elapsed_s))
        if not sres.usable:
            say("      [warn] 脚本未通过硬约束：%s" % "；".join(sres.errors))
        stage["stages"]["script"] = {
            "topic": args.topic, "title": title, "lines": len(turns),
            "usable": sres.usable, "first_pass": sres.first_pass,
            "target_words": sres.target_words,
            "actual_words": sres.script.total_chars,
            "word_deviation": sres.word_deviation,           # 比值
            "word_deviation_pct": sres.word_deviation_pct,   # 百分数
            "model_returned": sres.model_returned, "model_mapped": sres.model_mapped,
            "calls": sres.calls, "correction_rounds": sres.correction_rounds,
            "degraded": sres.degraded, "flagged": sres.flagged,
            "elapsed_s": round(sres.elapsed_s, 2)}

    if args.no_tts:
        stage["ok"] = True
        stage["note"] = "--no-tts：已停在脚本阶段"
        stage["elapsed_s"] = round(time.perf_counter() - t_all, 2)
        (out_dir / "run_summary.json").write_text(
            json.dumps(stage, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(stage, ensure_ascii=False, indent=2))
        return 0

    # ---------------- 阶段 2：合成 ----------------
    say("[2/3] 语音合成 %d 轮（首次加载模型约 20 s，请稍候）" % len(turns))
    from api.services.tts import TTSEngine, TTSError

    try:
        eng = TTSEngine.instance(settings)
    except TTSError as exc:
        return _abort("模型加载", exc, out_dir, stage, t_all)
    t_tts = time.perf_counter()

    def on_tts(i: int, total: int, r) -> None:
        if not args.quiet:
            mark = "缓存" if r.cached else "合成"
            print("      %d/%d %s %s %s %.2fs"
                  % (i, total, getattr(r.segment, "speaker", "?"), mark,
                     getattr(r.segment, "text", "")[:18], r.elapsed_s), flush=True)

    try:
        results = eng.synthesize_turns(turns, out_dir=out_dir / "wav",
                                       speed=args.speed, tone=args.tone,
                                       on_progress=on_tts)
    except TTSError as exc:
        return _abort("语音合成", exc, out_dir, stage, t_all)
    tts_elapsed = time.perf_counter() - t_tts
    cached_n = sum(1 for r in results if r.cached)
    audio_s = sum(r.duration_ms for r in results) / 1000.0
    stage["stages"]["tts"] = {
        "segments": len(results), "cached": cached_n,
        "audio_s": round(audio_s, 2), "elapsed_s": round(tts_elapsed, 2),
        "vram": eng.vram()}
    say("      共 %d 段（缓存命中 %d）；语音总长 %.1f s；耗时 %.1f s"
        % (len(results), cached_n, audio_s, tts_elapsed))

    # ---------------- 阶段 3：后期 + 导出 ----------------
    say("[3/3] 后期处理与 mp3 导出")
    intro_arg = False if args.no_intro else args.intro
    outro_arg = False if args.no_outro else args.outro
    t_pp = time.perf_counter()
    try:
        clips = pp.build_clips_from_results(results)
        pres = pp.postprocess(
            clips, out_dir=out_dir, name=name, settings=settings,
            intro=intro_arg, outro=outro_arg,
            keep_merged=not args.drop_wav,
            on_progress=(None if args.quiet
                         else lambda m, c=0, t=0: print("      " + m, flush=True)))
    except pp.PostprocessError as exc:   # 含 FFmpegNotFound
        return _abort("后期处理", exc, out_dir, stage, t_all)
    stage["stages"]["postprocess"] = pres.to_dict()

    stage["ok"] = True
    stage["output"] = {"dir": str(out_dir), "mp3": str(pres.mp3), "name": name}
    stage["elapsed_s"] = round(time.perf_counter() - t_all, 2)
    (out_dir / "run_summary.json").write_text(
        json.dumps(stage, ensure_ascii=False, indent=2), encoding="utf-8")

    say("")
    say("完成 → %s" % pres.mp3)
    say("  时长 %.1f s / %.2f MB / %d 段 / %d 处停顿"
        % (pres.duration_s, pres.size_bytes / 1048576, pres.segments, pres.pause_count))
    say("  响度归一 %s；片头 %s、片尾 %s；峰值 %.2f dB%s"
        % ("已应用" if pres.loudness_applied else "已跳过",
           "有" if pres.intro_used else "无", "有" if pres.outro_used else "无",
           pres.max_volume_db if pres.max_volume_db is not None else float("nan"),
           "（⚠️ 削波）" if pres.clipped else ""))
    for w in pres.warnings:
        say("  [warn] %s" % w)
    say("  摘要：%s" % (out_dir / "run_summary.json"))

    if args.quiet:
        print(json.dumps(stage, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
