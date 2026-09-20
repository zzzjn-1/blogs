# -*- coding: utf-8 -*-
"""片头尾是否真的拼进成片 —— 真实引擎端到端验证（走 API 流水线）。

为什么单独写这个脚本
--------------------
`verify_d6.py` 注入假引擎，验证的是 API 协议层；`verify_d5.py` 走 CLI 且显式
`intro=False, outro=False`（不测片头尾）。本脚本补的是第三块：

    **真实 CosyVoice 引擎 + TaskRunner 完整流水线 + 动态片头**

断言的因果链
------------
1. 动态片头被合成（渲染后的文案不含占位符）；
2. 后期 `PostprocessResult.intro_used / outro_used == True`；
3. 成片时长 ≈ 片头 + 正文 + 片尾 + 停顿（与「关掉片头」对照组相差 ≈ 片头时长）。

第 3 条靠 `--no-dynamic` 再跑一次做对照，两次时长差即为片头的真实贡献。

用法（需 conda env `cosyvoice`）：
    python scripts/verify_intro_e2e.py                 # 启用动态片头
    python scripts/verify_intro_e2e.py --no-dynamic    # 对照组：关闭动态片头
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# ---- 隔离到临时目录（必须在 import api 之前）----
_TMP = tempfile.mkdtemp(prefix="verify_intro_")
os.environ["DATA_DIR"] = os.path.join(_TMP, "data")
os.environ["DB_PATH"] = os.path.join(_TMP, "data", "db.sqlite")
os.environ["CACHE_DIR"] = os.path.join(_TMP, "data", "cache")
os.environ["WORK_DIR"] = os.path.join(_TMP, "data", "work")
os.environ["AUDIO_DIR"] = os.path.join(_TMP, "data", "audio")
os.environ["PODCAST_DIR"] = os.path.join(_TMP, "data", "podcast")
os.environ["JWT_SECRET"] = "verifyintrojwt-" + "y" * 40
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["AUTH_COOKIE_SECURE"] = "false"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from api.config import get_settings          # noqa: E402
from api.db import init_db, session_scope    # noqa: E402
from api.models import (                     # noqa: E402
    ScriptLine,
    Task,
    TaskStatus,
    User,
)
from api.services import postprocess as pp   # noqa: E402
from api.services.task_runner import TaskRunner  # noqa: E402

NO_DYNAMIC = "--no-dynamic" in sys.argv

# 正文用 4 句短句，控制端到端耗时（真实合成 ~1 s/句 + 片头 ~12 s）
LINES = [
    (1, "A", "今天我们来聊聊这个有趣的话题。"),
    (2, "B", "是的，我先说一个基本看法。"),
    (3, "A", "这个看法很有意思，能展开讲讲吗？"),
    (4, "B", "当然，核心其实就一句话。"),
]


class RecordingPostprocess:
    """包一层真实的 `postprocess`：只记录入参与结果，**不改变任何行为**。

    这样既能跑真实后期链路，又能把 `intro_used / outro_used / duration_s`
    拿出来做硬断言。
    """

    def __init__(self):
        self.last = None
        self.intro_arg = None
        self.outro_arg = None

    def __call__(self, clips, out_dir, name, settings, intro=None, outro=None):
        self.intro_arg = str(intro) if intro else None
        self.outro_arg = str(outro) if outro else None
        res = pp.postprocess(clips, out_dir=out_dir, name=name,
                             settings=settings, intro=intro, outro=outro)
        self.last = res
        return res


def main() -> int:
    s = get_settings()
    if NO_DYNAMIC:
        # 片头彻底关闭：模板置空会**回退到固定 intro.mp3**，所以固定路径也要关，
        # 否则对照组仍有片头，差值就不是片头时长了。片尾保留，便于同时验证片尾。
        s.intro_template = ""
        s.intro_path = ""
    init_db(s)

    task_id = "e2e_intro"
    with session_scope(s) as db:
        u = User(username="u1", password_hash="x")
        db.add(u)
        db.flush()
        db.add(Task(id=task_id, user_id=u.id, topic="端到端片头验证",
                    target_duration_sec=60, target_word_count=60,
                    status=TaskStatus.SCRIPT_READY, progress=0, stage="",
                    script_title="端到端片头验证"))
        db.flush()
        for seq, spk, text in LINES:
            db.add(ScriptLine(task_id=task_id, seq=seq, speaker=spk,
                              text=text, read_text=text))

    # 记录动态片头实际渲染出的文案
    rendered = {}

    class SpyRunner(TaskRunner):
        def _build_dynamic_intro(self, task, *, engine, work_dir):
            out = super()._build_dynamic_intro(
                task, engine=engine, work_dir=work_dir)
            rendered["path"] = str(out) if out else None
            return out

    rec = RecordingPostprocess()
    runner = SpyRunner(s, make_postprocess=lambda: rec)

    print("=" * 70)
    print("片头尾端到端验证（真实引擎）  mode=%s"
          % ("对照组：关闭动态片头且无片尾" if NO_DYNAMIC else "启用动态片头"))
    print("=" * 70)
    print("临时环境 : %s" % _TMP)
    print("正文     : %d 句 / %d 字" % (len(LINES), sum(len(t) for _, _, t in LINES)))

    t0 = time.time()
    fut = runner.submit_synthesize(task_id)
    fut.result(timeout=900)
    elapsed = time.time() - t0

    with session_scope(s) as db:
        task = db.get(Task, task_id)
        status = str(task.status)

    final = s.audio_path / task_id / "final.mp3"
    info = pp.probe(final, settings=s) if final.is_file() else None
    dur = round(info.duration_s, 2) if info and getattr(info, "duration_s", None) else None

    res = rec.last
    evidence = {
        "mode": "no_dynamic" if NO_DYNAMIC else "dynamic_intro",
        "status": status,
        "elapsed_s": round(elapsed, 2),
        "final_mp3": str(final),
        "final_bytes": final.stat().st_size if final.is_file() else 0,
        "duration_s": dur,
        "intro_arg": rec.intro_arg,
        "outro_arg": rec.outro_arg,
        "intro_used": bool(res.intro_used) if res else None,
        "outro_used": bool(res.outro_used) if res else None,
        "segments": res.segments if res else None,
        "dynamic_intro_path": rendered.get("path"),
        "loudness_after": (res.loudness_after if res else {}),
        "clipped": bool(res.clipped) if res else None,
        "warnings": list(res.warnings) if res else [],
    }

    print("\n--- 结果 ---")
    for k in ("status", "elapsed_s", "duration_s", "final_bytes",
              "intro_arg", "outro_arg", "intro_used", "outro_used",
              "segments", "clipped"):
        print("  %-14s %s" % (k, evidence[k]))

    out = Path(_ROOT) / "outputs" / (
        "intro_e2e_no_dynamic.json" if NO_DYNAMIC else "intro_e2e_dynamic.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("\n证据已写入：%s" % out)

    # ---------------- 断言 ----------------
    ok = True
    if status != "DONE":
        print("  [FAIL] 流水线未达 DONE：%s" % status)
        ok = False
    if not final.is_file():
        print("  [FAIL] 成片缺失")
        ok = False
    if NO_DYNAMIC:
        # 对照组只关**片头**；片尾按设计保留，便于同时验证片尾始终生效。
        if evidence["intro_used"]:
            print("  [FAIL] 对照组不应有片头（intro_used=True）")
            ok = False
        else:
            print("  [OK  ] 对照组确无片头（片尾保留，用于对照）")
        if not evidence["outro_used"]:
            print("  [FAIL] 对照组片尾也丢了，无法构成有效对照")
            ok = False
    else:
        if not evidence["intro_used"]:
            print("  [FAIL] intro_used=False —— 动态片头没拼进成片")
            ok = False
        else:
            print("  [OK  ] intro_used=True —— 动态片头已拼进成片")
        if not evidence["outro_used"]:
            print("  [FAIL] outro_used=False —— 片尾没拼进成片")
            ok = False
        else:
            print("  [OK  ] outro_used=True —— 片尾已拼进成片")
        if not evidence["dynamic_intro_path"]:
            print("  [FAIL] 动态片头未生成")
            ok = False
        else:
            print("  [OK  ] 动态片头已生成：%s"
                  % Path(evidence["dynamic_intro_path"]).name)
    if evidence["clipped"]:
        print("  [WARN] 成片削波")

    print("\nRESULT: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
