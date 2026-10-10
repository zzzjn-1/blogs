"""D11 变异测试台 —— 证明 D11 的判据与链路断言真的会红。

「一个不能失败的检查等于没有检查」。D11 新增两类检查，各自都有「看起来绿、
其实没在检查」的失败模式，本脚本逐个把它们钉出来：

  A. **一致率判据**（`scripts/verify_consistency.py`）
     一致率是 M3 的验收指标，判据形同虚设则「100% 达标」毫无价值。
     其中 **M7（比较符号反向）与 M8（把放弃的任务也算进分母）是口径类缺陷** ——
     不会让程序崩，只会让结论悄悄变错，正是单测必须钉死的地方。

  B. **多音字词典的生产链路**（`api/services/task_runner.py`）
     词典的纯函数行为早有单测（`tests/test_normalize.py`），但那只证明
     `normalize_text(text, polyphone=…)` 正确，**证明不了生产链路把词典传进来了**。
     M9 注入的正是「加载了但没接上」这种断线，M10 注入「改写无痕迹」。

每个变异都要求： 注入 → 必须变红 → 立即还原（字节级校验）→ 全部还原后必须变绿。
并打印**哪个测试红的**，用来证明覆盖归属，而不是只看退出码。

用法：python scripts/mutation_check_d11.py
退出码 0 = 全部如期变红且还原干净。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONSISTENCY = ROOT / "scripts" / "verify_consistency.py"
RUNNER = ROOT / "api" / "services" / "task_runner.py"
MOS = ROOT / "scripts" / "collect_mos.py"
POSTPROCESS = ROOT / "api" / "services" / "postprocess.py"

T_CONSISTENCY = "tests/test_verify_consistency.py"
T_RUNNER = "tests/test_task_runner.py"
T_MOS = "tests/test_collect_mos.py"
T_POST = "tests/test_postprocess.py"

#: (编号, 文件, 原文, 注入后, 说明, 目标测试)
MUTATIONS: list[tuple[str, Path, str, str, str, str]] = [
    (
        "M1", CONSISTENCY,
        'if wav_dur_ms > dur + tol_ms:',
        'if False:',
        "去掉 L4b 粒度不变量：首段长于整行也不报错",
        T_CONSISTENCY,
    ),
    (
        "M2", CONSISTENCY,
        'if wav_dur_ms is None:',
        'if False:',
        "缓存 wav 读不出来时不再判失败（音轨丢失变静默）",
        T_CONSISTENCY,
    ),
    (
        "M3", CONSISTENCY,
        'if not (line.get("read_text") or "").strip():',
        'if False:',
        "不再检查 read_text 是否落库",
        T_CONSISTENCY,
    ),
    (
        "M4", CONSISTENCY,
        'if status != "DONE" or dur <= 0:',
        'if status != "DONE":',
        "只看 seg_status，不再要求 duration_ms > 0",
        T_CONSISTENCY,
    ),
    (
        "M5", CONSISTENCY,
        'frames = data_len / (channels * (bits // 8))',
        'frames = data_len / (bits // 8)',
        "立体声按字节数当帧数：时长算成 2 倍",
        T_CONSISTENCY,
    ),
    (
        "M6", CONSISTENCY,
        'if fmt is None or data_len is None or len(fmt) < 16:\n        return None',
        'if fmt is None or data_len is None or len(fmt) < 16:\n        return 0',
        "坏 wav 不返回 None 而返回 0（失败被伪装成「时长 0」）",
        T_CONSISTENCY,
    ),
    (
        "M7", CONSISTENCY,
        'if sum_line_ms > mp3_ms:',
        'if sum_line_ms < mp3_ms:',
        "期级不变量比较符号反向：正常成片全被判失败",
        T_CONSISTENCY,
    ),
    (
        "M8", CONSISTENCY,
        '            idle_tasks.append({\n'
        '                "task_id": tid,\n'
        '                "lines": len(rows),\n'
        '                "statuses": sorted({r["seg_status"] for r in rows}),\n'
        '            })\n            continue',
        '            idle_tasks.append({\n'
        '                "task_id": tid,\n'
        '                "lines": len(rows),\n'
        '                "statuses": sorted({r["seg_status"] for r in rows}),\n'
        '            })\n            ep = {}',
        "把未成片（放弃）任务也放进一致率分母 → 把「没做完」误报成「不一致」",
        T_CONSISTENCY,
    ),
    (
        "M9", RUNNER,
        'poly = load_polyphone(str(self.s.path(self.s.polyphone_dict)))',
        'poly = {}',
        "词典加载了却没用上（漏传参数）：纯函数测试照样全绿，只有链路测试能抓",
        T_RUNNER,
    ),
    (
        "M10", RUNNER,
        '            if warned:',
        '            if False:',
        "不可逆改写不再留痕：一致率核对时无法知道读文被谁改过",
        T_RUNNER,
    ),
    (
        "M11", MOS,
        'if n < MIN_RATERS:',
        'if n < 1:',
        "人数阈值失效：1~2 人也给结论（计划书要求 3~5 人取均值）",
        T_MOS,
    ),
    (
        "M12", MOS,
        'if mean >= TARGET_MEAN:',
        'if mean >= 3.0:',
        "悄悄放宽验收线：3.0 也算达标（验收线是 3.5）",
        T_MOS,
    ),
    (
        "M13", MOS,
        'if f != int(f):',
        'if False:',
        "允许半整数档位：3.5 被 int() 静默截断成 3，等于悄悄改分",
        T_MOS,
    ),
    (
        "M14", MOS,
        '    if not sh.errors:\n        sh.rows = rows',
        '    sh.rows = rows',
        "填写不合格的表也参与算分：乱填的表能凑人头、还能拉均值",
        T_MOS,
    ),
    (
        "M15", POSTPROCESS,
        'sil_sent = make_silence(s.audio_pause_sentence_ms, build / "silence_sentence.wav",',
        'sil_sent = make_silence(250, build / "silence_sentence.wav",',
        "句间停顿旋钮没接线（硬编码回 250）：改配置对成片无影响",
        T_POST,
    ),
]


def _pytest(test_path: str) -> tuple[int, list[str]]:
    """跑一轮目标测试，返回 (退出码, 变红的测试节点名列表)。"""
    p = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q", "--tb=no", "-rf"],
        cwd=str(ROOT), capture_output=True, encoding="utf-8", errors="replace",
    )
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, sorted(set(re.findall(r"FAILED (\S+)", out)))


def main() -> int:
    targets = (CONSISTENCY, RUNNER, MOS, POSTPROCESS)
    for path in targets:
        if not path.is_file():
            raise SystemExit(f"找不到被测文件：{path}")
    originals = {p: p.read_bytes() for p in targets}
    texts = {p: b.decode("utf-8") for p, b in originals.items()}
    print("被测文件：" + "、".join(p.name for p in targets) + "\n")

    bad = 0
    for mid, path, old, new, desc, tests in MUTATIONS:
        print(f"--- {mid} [{path.name}] :: {desc}")
        n = texts[path].count(old)
        if n != 1:
            print(f"    [FAIL] 锚点命中 {n} 次（要求恰好 1 次）—— 锚点已漂移，变异无效")
            bad += 1
            continue
        if old == new:
            # 曾经踩过：注入值与原值相同 → 变异退化成空操作
            print("    [FAIL] 注入值与原文相同，变异是空操作")
            bad += 1
            continue

        path.write_text(texts[path].replace(old, new, 1), encoding="utf-8")
        try:
            code, failed = _pytest(tests)
        finally:
            path.write_bytes(originals[path])     # 立即还原，绝不留注入态

        if path.read_bytes() != originals[path]:
            print("    [FAIL] 还原后字节不一致！")
            bad += 1
            continue

        if code == 0:
            print("    [FAIL] 注入后仍然全绿 —— 这条缺陷没有被任何测试覆盖")
            bad += 1
        else:
            names = ", ".join(f.split("::")[-1] for f in failed) or "(未解析到)"
            print(f"    [ok]   如期变红；变红用例：{names}")

    print("\n--- 还原复跑（全部测试文件都必须全绿）")
    for tests in (T_CONSISTENCY, T_RUNNER, T_MOS, T_POST):
        code, failed = _pytest(tests)
        if code == 0:
            print(f"    [ok]   {tests} 全绿")
        else:
            print(f"    [FAIL] {tests} 仍有失败：{', '.join(failed)}")
            bad += 1

    print("\n" + "=" * 60)
    if bad:
        print(f"结果：{len(MUTATIONS) - bad} / {len(MUTATIONS)} 如期变红")
    else:
        print(f"结果：{len(MUTATIONS)} / {len(MUTATIONS)} 全部如期变红，且还原干净")
    print("=" * 60)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
