"""D12 变异测试台：故意注入缺陷，验证对应单测**确实会红**。

为什么必须做这一步：「测试通过」只能证明「当前代码没触发断言失败」，
不能证明「测试真的在测这件事」。如果测试写空了（比如断言条件恒为真、
或根本没走到被测分支），代码改坏了它照样绿。变异测试就是给测试本身做体检：
把被测逻辑改坏一点点，**该红的必须红**；若仍绿，说明这条测试是摆设。

用法：
    python scripts/mutation_check_d12.py            # 全部变异
    python scripts/mutation_check_d12.py M2         # 只跑某一条

每条变异的流程：注入 → 跑指定测试（预期 FAIL）→ 还原 → 复跑（预期 PASS）。
任一步与预期不符即以非零码退出。
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


@dataclass(frozen=True)
class Mutation:
    mid: str
    target: str          # 仓库相对路径
    old: str             # 必须**恰好出现一次**的原文
    new: str             # 注入后的文本
    test: str            # 期望变红的测试 nodeid
    why: str             # 这条变异代表什么缺陷


MUTATIONS: list[Mutation] = [
    Mutation(
        mid="M1",
        target="api/services/task_runner.py",
        old="                if st == TaskStatus.SCRIPT_READY:\n                    out[\"skipped\"].append(t.id)\n                    continue",
        new="                if False and st == TaskStatus.SCRIPT_READY:\n                    out[\"skipped\"].append(t.id)\n                    continue",
        test="tests/test_d12_resilience.py::test_recover_skips_script_ready",
        why="恢复扫描不再跳过 SCRIPT_READY —— 会把「等用户确认」的任务误判为被中断",
    ),
    Mutation(
        mid="M2",
        target="api/services/task_runner.py",
        old="            task.cache_seg_count = len(results)\n            task.cache_hit_count = hits",
        new="            task.cache_seg_count = int(task.cache_seg_count or 0) + len(results)\n            task.cache_hit_count = int(task.cache_hit_count or 0) + hits",
        test="tests/test_d12_resilience.py::test_cache_hit_counters_recomputed_not_accumulated",
        why="命中读数改成累加 —— 续跑重跑整个合成阶段时同一批段被算两遍，命中率虚高",
    ),
    Mutation(
        mid="M3",
        target="api/services/tts.py",
        old="        return \"out of memory\" in str(exc).lower()",
        new="        return False",
        test="tests/test_d12_resilience.py::test_oom_retried_once_then_succeeds",
        why="OOM 识别失效 —— 真 OOM 不再重试，直接冒泡成裸 RuntimeError",
    ),
    Mutation(
        mid="M4",
        target="api/services/tts.py",
        old="        max_retry = max(0, int(getattr(self.settings, \"tts_oom_retry\", 0)))",
        new="        max_retry = 0  # 变异：重试额度被吞",
        test="tests/test_d12_resilience.py::test_oom_retried_once_then_succeeds",
        why="重试额度失效 —— 配了 TTS_OOM_RETRY 也不生效",
    ),
    Mutation(
        mid="M5",
        target="api/services/task_runner.py",
        old="            return waiting.index(task_id) + 1",
        new="            return waiting.index(task_id)",
        test="tests/test_d12_resilience.py::test_queue_position_running_and_waiting",
        why="排队位次差一 —— 前端「前面还有 N 个」会少报 1，且与 0=正在跑 语义撞车",
    ),
    Mutation(
        mid="M6",
        target="api/models.py",
        old="            \"cache_hit_rate\": (round(int(self.cache_hit_count or 0)\n                                    / int(self.cache_seg_count), 4)\n                               if int(self.cache_seg_count or 0) else None),",
        new="            \"cache_hit_rate\": round(int(self.cache_hit_count or 0)\n                                    / max(1, int(self.cache_seg_count or 0)), 4),",
        test="tests/test_d12_resilience.py::test_task_to_dict_exposes_cache_rate_and_timestamps",
        why="seg=0 时把命中率算成 0.0 而非 null —— 「没测过」被误报成「命中率 0%」",
    ),
    Mutation(
        mid="M7",
        target="api/services/task_runner.py",
        old="                if auto_resume:\n                    out[\"resumed\"].append(t.id)\n                    plan.append((t.id, \"synth\"))",
        new="                if False:\n                    out[\"resumed\"].append(t.id)\n                    plan.append((t.id, \"synth\"))",
        test="tests/test_d12_resilience.py::test_recover_auto_resumes_to_done",
        why="自动续跑失效 —— 崩溃后任务只被标 FAILED，不恢复，等于没做 D12 的续跑能力",
    ),
    Mutation(
        mid="M8",
        target="api/services/task_runner.py",
        old="        # 句级缓存 + 行状态回写（新事务，一行代码改一次库，不再横跨合成）\n        hits = sum(1 for r in results if getattr(r, \"cached\", False))",
        new=("        # 变异：开一个**未提交**的写事务并故意不关闭，让它横跨下面的合成分段回写\n"
             "        # 注意：赋的值必须与现值不同，否则 SQLAlchemy 视为未脏、不发 UPDATE，\n"
             "        # 变异就退化成空操作（第一版就踩了这个坑，测试照样绿）。\n"
             "        _poison = session_scope(self.s)\n"
             "        _poison_db = _poison.__enter__()\n"
             "        _poison_db.get(Task, task_id).cache_seg_count = -1\n"
             "        _poison_db.flush()\n"
             "        hits = sum(1 for r in results if getattr(r, \"cached\", False))"),
        test="tests/test_task_runner.py::test_synth_does_not_hold_write_lock_across_synthesis",
        why="R21 回潮 —— 写事务再次横跨合成段，进度回写会撞 SQLite 写锁忙等",
    ),
    Mutation(
        mid="M9",
        target="api/services/task_runner.py",
        old="            if work.exists():\n                log.warning(",
        new=("            if False:  # 变异：删不掉也静默 —— 重回「累积到 GB 级而日志空白」\n"
             "                log.warning("),
        test="tests/test_task_runner.py::test_cleanup_work_warns_when_delete_blocked",
        why="工作目录删不掉却不再告警 —— 又回到 except: pass 那一族（D12 主题缺陷的回潮）",
    ),
    Mutation(
        mid="M10",
        target="api/services/task_runner.py",
        old="            if work.exists():\n                log.warning(",
        new=("            if work.exists():\n"
             "                raise RuntimeError(f\"工作目录未能删除：{work}\")  # 变异：清理失败=任务失败\n"
             "                log.warning("),
        test="tests/test_task_runner.py::test_cleanup_work_failure_does_not_fail_the_task",
        why="把「清理失败」升级成任务级异常 —— 运维问题会污染成片结果（成片其实已经 DONE）",
    ),
]


def _run(test: str) -> bool:
    """跑单个测试，返回是否通过。"""
    proc = subprocess.run(
        [PY, "-m", "pytest", test, "-q", "--no-header",
         "-p", "no:cacheprovider", "--no-summary"],
        cwd=ROOT, capture_output=True, text=True, errors="replace")
    return proc.returncode == 0


def _apply(mut: Mutation) -> str:
    path = ROOT / mut.target
    src = path.read_text(encoding="utf-8")
    n = src.count(mut.old)
    if n != 1:
        raise SystemExit(f"[{mut.mid}] 变异锚点在 {mut.target} 出现 {n} 次（应为 1），"
                         "源码已变动，先修锚点再跑。")
    path.write_text(src.replace(mut.old, mut.new, 1), encoding="utf-8")
    return src


def main() -> int:
    only = {a for a in sys.argv[1:] if not a.startswith("-")}
    todo = [m for m in MUTATIONS if not only or m.mid in only]
    failures: list[str] = []
    print("=" * 72)
    print("D12 变异测试：注入缺陷 → 该红必须红 → 还原 → 复跑必须绿")
    print("=" * 72)
    for mut in todo:
        target = ROOT / mut.target
        original = _apply(mut)
        try:
            after = _run(mut.test)
        finally:
            target.write_text(original, encoding="utf-8")
        restored = _run(mut.test)

        ok = (not after) and restored
        print(f"\n[{mut.mid}] {mut.why}")
        print(f"      锚点 {mut.target}")
        print(f"      测试 {mut.test}")
        print(f"      注入后 = {'PASS ✗ 测试是摆设(应红未红)' if after else 'FAIL ✓ 如期变红'}")
        print(f"      还原后 = {'PASS ✓' if restored else 'FAIL ✗ 还原不干净'}")
        print(f"      判定 {'OK' if ok else 'BROKEN'}")
        if not ok:
            failures.append(mut.mid)
    print("\n" + "=" * 72)
    if failures:
        print(f"变异测试未通过：{failures}（这些测试没在测它声称测的东西）")
        return 1
    print(f"变异测试全部通过：{len(todo)}/{len(todo)} —— 每条测试都确实会因缺陷变红。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
