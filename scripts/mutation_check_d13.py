"""D13 变异测试台 —— 证明「最新一期进没进订阅源」这条断言真的会红。

## 为什么 D13 需要一个变异台

D13 收口 D7~D9 遗留的「`feed.xml` 校验」时，发现了一个**长期存在的生产缺陷**：
封装阶段先写 feed、后转 DONE，而 feed 的过滤条件是「任务已 DONE」——于是
**刚做好的这一期被它自己的过滤条件挡在订阅源之外**，14/14 份 feed 都恰好少一条。

它之所以能活到现在，是因为原有用例只断言「RSS 文件存在」：

    xmls = list(s.podcast_path.glob("*.xml"))
    assert xmls, "未生成 RSS XML"

**文件在、内容却缺最新一期** —— 断言没落到内容上，于是代码坏了照样绿。
这正是本项目反复强调的「摆设测试」：断言里被守的条件没参与判定。

三条变异分别钉住修复的三半，缺一不可：

  M1  把「带上本期」这一句删掉            → 内容里没有本期
  M2  只留本期、丢掉已 DONE 的旧单集      → 把「落后一期」换成「永远只有一期」
  M3  让 episodes.file_size 与磁盘差 1 字节 → 证明「字节数三方核对」也是活的

用法：python scripts/mutation_check_d13.py    （退出码 0 = 全部如期变红且还原干净）
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

_FEED_TEST = "tests/test_task_runner.py::test_feed_really_contains_the_episode_just_packaged"
_KEEP_TEST = "tests/test_task_runner.py::test_feed_keeps_done_episodes_when_a_new_one_is_packaged"


@dataclass(frozen=True)
class Mutation:
    mid: str
    target: str
    old: str
    new: str
    test: str
    why: str


MUTATIONS: list[Mutation] = [
    Mutation(
        mid="M1",
        target="api/services/task_runner.py",
        old=("                write_feed_xml(db, user=user, settings=self.s,\n"
             "                               include_task_id=task.id)"),
        new="                write_feed_xml(db, user=user, settings=self.s)",
        test=_FEED_TEST,
        why="封装时不带上本期 —— 回到 [FIX-FEED-LATEST-01] 之前：订阅源恒久落后一期",
    ),
    Mutation(
        mid="M2",
        target="api/services/podcast_rss.py",
        old=("    if include_task_id:\n"
             "        q = q.filter(or_(Task.status == TaskStatus.DONE,\n"
             "                         Task.id == include_task_id))"),
        new=("    if include_task_id:\n"
             "        q = q.filter(Task.id == include_task_id)"),
        test=_KEEP_TEST,
        why="只为「本期」出 feed —— 旧单集被挤掉，订阅源永远只剩一条",
    ),
    Mutation(
        mid="M3",
        target="api/services/task_runner.py",
        old="            ep.file_size = int(size)",
        new="            ep.file_size = int(size) + 1",
        test=_FEED_TEST,
        why="enclosure@length 与磁盘实际字节数不一致（计划书 4.9 点名的那类错）",
    ),
]


def _run(test: str) -> bool:
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
    print("D13 变异测试：注入缺陷 → 该红必须红 → 还原 → 复跑必须绿")
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
    print(f"变异测试全部通过：{len(todo)}/{len(todo)} —— 每条断言都确实会因缺陷变红。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
