# -*- coding: utf-8 -*-
"""生成一期完整节目（LLM 脚本 → 双音色合成 → 动态片头尾 → 成片 + 文稿）。

与既有脚本的分工
----------------
- `scripts/cli.py`：CLI 链路，**不含动态片头**（片头是固定素材）；
- `scripts/verify_intro_e2e.py`：验证用，正文是写死的 4 句、环境隔离到临时目录；
- **本脚本**：真正"做一期节目"的入口。走 API 流水线（`TaskRunner`），
  因此能拿到**动态片头**（按当期日期 + 脚本标题现合成）与片尾，并复用主缓存。

用法（需 conda env `cosyvoice`）
-------------------------------
    python scripts/make_episode.py --topic "哲学"
    python scripts/make_episode.py --topic "哲学" --words 800 --style "轻松对谈"
    python scripts/make_episode.py --topic "哲学" --no-intro      # 不要片头
    python scripts/make_episode.py --topic "哲学" --dry-run       # 只生成脚本，不合成

产物（outputs/episodes/<slug>/）
-------------------------------
- `<slug>.mp3`      成片
- `script.md`       对话文稿（可直接当博客正文）
- `meta.json`       元数据：标题/字数/时长/响度/片头尾是否生效/模型与耗时
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from api.config import get_settings                      # noqa: E402
from api.db import init_db, session_scope                # noqa: E402
from api.models import ScriptLine, Task, TaskStatus, User  # noqa: E402
from api.services import postprocess as pp               # noqa: E402
from api.services.script_gen import ScriptGenerator      # noqa: E402
from api.services.task_runner import TaskRunner           # noqa: E402


def slugify(text: str, limit: int = 40) -> str:
    """中文友好的文件名：保留中日韩字符与字母数字，其余转连字符。"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip(), flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "episode")[:limit]


def write_script_md(path: Path, title: str, summary: str, turns: list[dict],
                    topic: str, created: str) -> None:
    lines = [
        "# %s" % title,
        "",
        "> 主题：%s　|　生成时间：%s" % (topic, created),
        "",
    ]
    if summary:
        lines += [summary, ""]
    lines += ["## 对话文稿", ""]
    name = {"A": "**A**（女声）", "B": "**B**（男声）"}
    for t in turns:
        spk = str(t.get("speaker", "A")).upper()[:1]
        lines.append("- %s：%s" % (name.get(spk, spk), t.get("text", "")))
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成一期完整播客节目")
    ap.add_argument("--topic", required=True, help="节目主题")
    ap.add_argument("--words", type=int, default=800, help="目标字数（默认 800）")
    ap.add_argument("--style", default="", help="语言风格提示，如「轻松对谈」")
    ap.add_argument("--out", default="", help="输出目录名（默认由标题生成）")
    ap.add_argument("--no-intro", action="store_true", help="禁用片头")
    ap.add_argument("--no-outro", action="store_true", help="禁用片尾")
    ap.add_argument("--dry-run", action="store_true", help="只生成脚本并出稿，不合成")
    args = ap.parse_args()

    s = get_settings()
    if args.no_intro:
        s.intro_template = ""
        s.intro_path = ""
    if args.no_outro:
        s.outro_path = ""

    print("=" * 70)
    print("生成一期节目：%s" % args.topic)
    print("=" * 70)
    print("目标字数 : %d" % args.words)
    print("片头     : %s" % ("关闭" if args.no_intro else "动态模板 %r" % s.intro_template))
    print("片尾     : %s" % ("关闭" if args.no_outro else s.outro_path))

    # ---------------- 1. LLM 生成脚本 ----------------
    t0 = time.time()
    print("\n--- 1/3 脚本生成（DeepSeek）---")
    gen = ScriptGenerator(s)
    res = gen.generate(topic=args.topic, target_words=args.words, style=args.style)
    script = res.script
    turns = script.as_turns()
    chars = script.total_chars
    print("  标题     : %s" % script.title)
    print("  行数     : %d 行 / %d 字" % (len(turns), chars))
    print("  模型     : %s%s（调用 %d 次，纠错 %d 轮）"
          % (res.model_returned, " [映射]" if res.model_mapped else "",
             res.calls, res.correction_rounds))
    print("  耗时     : %.1fs" % (time.time() - t0))
    for w in list(res.warnings)[:5]:
        print("  [WARN] %s" % w)

    if args.dry_run:
        outdir = Path(_ROOT) / "outputs" / "episodes" / (args.out or slugify(script.title))
        outdir.mkdir(parents=True, exist_ok=True)
        write_script_md(outdir / "script.md", script.title, script.summary,
                        turns, args.topic, datetime.now().strftime("%Y-%m-%d %H:%M"))
        print("\n[DRY-RUN] 文稿已写入：%s" % (outdir / "script.md"))
        return 0

    # ---------------- 2. 落库 ----------------
    init_db(s)
    task_id = "ep_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    dur_est = int(chars / max(1, s.words_per_minute) * 60)
    with session_scope(s) as db:
        # get-or-create：local 用户已存在时直接复用，避免 UNIQUE(username) 冲突致整期崩溃
        # （实测：首期创建后，后续重跑每次都因重复 INSERT 'local' 而 IntegrityError，
        #  合成从未执行，成片永远停在首期旧文件 —— 这也是片尾文案改了却"依旧有旧句"的根因）
        u = db.query(User).filter_by(username="local").first()
        if u is None:
            # 占位哈希**故意留空串**（不是 "x"）：空串由 `api/security.verify_password`
            # 显式判为「校验失败」，而 "x" 会让 bcrypt 抛 ValueError 再被吞成 False ——
            # 症状一模一样，排查时容易误判成「口令打错」。
            #
            # ⚠️ 代价：全新环境（库是新建的）里这个账号**登不进 Web**。
            #    演示前必须跑 `python scripts/ensure_demo_account.py --create`
            #    写入合法口令哈希；该脚本会用 verify_password 双向回验。
            #    D14 之前没人发现这条，是因为此前所有验证都走 CLI/API，
            #    没有一条用例真的用浏览器登录过它。
            u = User(username="local", password_hash="")
            db.add(u)
            db.flush()
        db.add(Task(id=task_id, user_id=u.id, topic=args.topic,
                    target_duration_sec=dur_est, target_word_count=args.words,
                    status=TaskStatus.SCRIPT_READY, progress=0, stage="",
                    script_title=script.title))
        db.flush()
        # 用 enumerate 定序，不依赖 as_turns() 里是否有 seq 键
        # （实测该键缺失，取默认值 0 会撞 script_lines(task_id, seq) 唯一约束）
        for i, t in enumerate(turns, 1):
            text = str(t.get("text", ""))
            db.add(ScriptLine(task_id=task_id, seq=i,
                              speaker=str(t.get("speaker", "A")).upper()[:1],
                              text=text, read_text=text))

    # ---------------- 3. 合成 + 后期 ----------------
    print("\n--- 2/3 语音合成（CosyVoice，单并发）---")
    rec = {}

    class SpyRunner(TaskRunner):
        def _build_dynamic_intro(self, task, *, engine, work_dir):
            out = super()._build_dynamic_intro(task, engine=engine, work_dir=work_dir)
            rec["intro_path"] = str(out) if out else None
            return out

    runner = SpyRunner(s)
    t1 = time.time()
    try:
        runner.submit_synthesize(task_id).result(timeout=3600)
    except BaseException as exc:                      # noqa: BLE001
        # 后期收尾要清理 work 目录（上百个中间 wav）。某些环境（沙箱/删批守卫、
        # 杀软）会拦截批量删除并抛 SystemExit —— 此时**成片往往已经产出**，
        # 不该让清理失败毁掉整期节目。这里只告警，随后按文件是否存在判定成败。
        print("  [WARN] 流水线收尾异常（多为清理临时目录被拦截）：%s: %s"
              % (type(exc).__name__, str(exc)[:120]))
    synth_s = time.time() - t1
    print("  合成耗时 : %.1fs（%d 字，%.3f s/字）"
          % (synth_s, chars, synth_s / max(1, chars)))

    print("\n--- 3/3 后期与导出 ---")
    with session_scope(s) as db:
        task = db.get(Task, task_id)
        status = str(task.status)

    final = s.audio_path / task_id / "final.mp3"
    if not final.is_file():
        print("  [FAIL] 成片缺失：%s（status=%s）" % (final, status))
        return 1

    info = pp.probe(final, settings=s)
    # `probe` 只给格式与时长，响度要单独测（loudnorm 第一遍，不写文件）
    try:
        loud = pp.measure_loudness(final, settings=s)
        loud_i, true_peak = loud.get("input_i"), loud.get("input_tp")
    except Exception as exc:                          # noqa: BLE001
        print("  [WARN] 响度测量失败：%s" % exc)
        loud_i = true_peak = None
    outdir = Path(_ROOT) / "outputs" / "episodes" / (args.out or slugify(script.title))
    outdir.mkdir(parents=True, exist_ok=True)
    mp3 = outdir / ("%s.mp3" % (args.out or slugify(script.title)))
    shutil.copy2(final, mp3)

    write_script_md(outdir / "script.md", script.title, script.summary,
                    turns, args.topic, datetime.now().strftime("%Y-%m-%d %H:%M"))

    meta = {
        "topic": args.topic,
        "title": script.title,
        "summary": script.summary,
        "created": datetime.now().isoformat(timespec="seconds"),
        "words": args.words,
        "chars": chars,
        "lines": len(turns),
        "status": status,
        "synth_elapsed_s": round(synth_s, 2),
        "sec_per_char": round(synth_s / max(1, chars), 4),
        "mp3": str(mp3),
        "mp3_bytes": mp3.stat().st_size,
        "duration_s": round(info.duration_s, 2) if info else None,
        "loudness_i": loud_i,
        "true_peak": true_peak,
        "dynamic_intro": rec.get("intro_path"),
        "model": res.model_returned,
        "llm_calls": res.calls,
    }
    (outdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print("  成片     : %s（%.2f s / %d B）"
          % (mp3.name, meta["duration_s"] or 0, meta["mp3_bytes"]))
    print("  响度     : %s LUFS / 真峰 %s dBTP" % (meta["loudness_i"], meta["true_peak"]))
    print("  文稿     : script.md")
    print("\n输出目录：%s" % outdir)
    print("RESULT: %s" % ("DONE" if status == "DONE" else status))
    return 0 if status == "DONE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
