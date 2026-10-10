# -*- coding: utf-8 -*-
"""任务运行器（状态机 + 流水线）单元测试（计划书 4.5 / 4.3）。

不依赖 GPU / 网络：用 FakeEngine / FakeGenerator / FakePostprocess 注入，
直接同步调用 `_run_script` / `_run_pipeline` 驱动整条状态机，断言：
- 合法迁移可达 DONE 并落 Episode + RSS；
- 非法迁移抛 `IllegalTransition`；
- FAILED → SYNTHESIZING 的 retry 回边可用；
- `_upsert_cache` 幂等。
"""
from __future__ import annotations

import json
import logging
import time
import types
import wave
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree as ET

from api.config import Settings
from api.db import init_db, reset_engine, session_scope
from api.models import (
    AudioCache, Episode, ScriptLine, SegStatus, Task, TaskStatus, User,
)
from api.services.task_runner import (
    IllegalTransition,
    TaskRunner,
    _STAGE_PROGRESS,
    _transition,
    TRANSITIONS,
)


def _tmp_settings(tmp_path) -> Settings:
    return Settings(
        data_dir=str(tmp_path / "data"),
        db_path=str(tmp_path / "data" / "db.sqlite"),
        cache_dir=str(tmp_path / "data" / "cache"),
        work_dir=str(tmp_path / "data" / "work"),
        audio_dir=str(tmp_path / "data" / "audio"),
        podcast_dir=str(tmp_path / "data" / "podcast"),
        jwt_secret="x" * 40,
        public_base_url="http://test",
        auth_cookie_secure=False,
    )


# --------------------------------------------------------------------------- #
# 假依赖
# --------------------------------------------------------------------------- #

class _FakeScript:
    def __init__(self, lines, title, summary):
        self.lines = lines
        self.title = title
        self.summary = summary


class FakeGenerator:
    def generate(self, topic, duration_min=1.0, style=""):
        L = types.SimpleNamespace
        lines = [L(seq=1, speaker="A", text="第一句内容。"),
                 L(seq=2, speaker="B", text="第二句内容。")]
        return types.SimpleNamespace(
            script=_FakeScript(lines, "标题", "摘要"), target_words=20)


class FakeSeg:
    def __init__(self, line_seq, speaker, read_text):
        self.line_seq = line_seq
        self.speaker = speaker
        self.read_text = read_text


class FakeSynthResult:
    def __init__(self, segment, text_hash, wav_path, duration_ms):
        self.segment = segment
        self.text_hash = text_hash
        self.wav_path = wav_path
        self.duration_ms = duration_ms


class FakeEngine:
    def __init__(self, settings: Settings):
        self.s = settings
        self.calls = 0

    def cache_key(self, read_text, speaker, speed, tone):
        return f"ck|{speaker}|{read_text}|{speed}|{tone}"

    def synthesize(self, text, speaker="voice_a", *, speed=1.0, tone="",
                   text_hash=None):
        """单句合成：供**动态片头**使用（主链路 `TaskRunner._build_dynamic_intro`）。

        之前 FakeEngine 没有这个方法，动态片头路径会因 AttributeError 被
        `except` 静默吞掉 —— 等于「这条路径永远没被测到」。补上后即可断言
        片头真的被合成并传给了后期。
        """
        self.synth_texts = getattr(self, "synth_texts", [])
        self.synth_texts.append(text)
        out = Path(self.s.work_path) / "_fake_intro"
        out.mkdir(parents=True, exist_ok=True)
        wav = out / ("%s.wav" % abs(hash((text, speaker, speed, tone))))
        with wave.open(str(wav), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(44100)
            wf.writeframes(b"\x00\x00" * 200)
        return types.SimpleNamespace(
            segment=FakeSeg(0, speaker, text),
            text_hash=text_hash or self.cache_key(text, speaker, speed, tone),
            wav_path=str(wav), duration_ms=1000, cached=False, elapsed_s=0.0)

    def synthesize_lines(self, norm_lines, out_dir, speed=1.0, tone="",
                          on_progress=None):
        self.calls += 1
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        results = []
        for i, nl in enumerate(norm_lines, 1):
            key = self.cache_key(nl.read_text, nl.speaker, speed, tone)
            wav = out / f"seg_{i}.wav"
            with wave.open(str(wav), "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(b"\x00\x00" * 200)
            results.append(FakeSynthResult(
                FakeSeg(nl.seq, nl.speaker, nl.read_text), key, str(wav), 1000))
            if on_progress:
                on_progress(i, len(norm_lines), None)
        return results


class FakePostprocess:
    def __call__(self, clips, out_dir, name, settings, intro=None, outro=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        mp3 = out / f"{name}.mp3"
        mp3.write_bytes(b"FAKEMP3")
        # 记录片头尾，供断言「动态片头确实传进了后期」
        self.last_intro = intro
        self.last_outro = outro
        return types.SimpleNamespace(mp3=str(mp3))


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _seed_user_and_task(s, task_id="t1") -> int:
    with session_scope(s) as db:
        u = User(username="u1", password_hash="x")
        db.add(u)
        db.flush()
        uid = u.id
        db.add(Task(id=task_id, user_id=uid, topic="x",
                    target_duration_sec=120, target_word_count=20,
                    status=TaskStatus.PENDING, progress=0, stage=""))
        return uid


def _poll(s, task_id, want, timeout=20.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with session_scope(s) as db:
            t = db.get(Task, task_id)
            last = t.status if t else None
            if last in want:
                return last
        time.sleep(0.05)
    raise AssertionError(f"轮询超时：期望 {want}，实际 {last}")


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #

def test_transitions_table_is_authoritative():
    # 与 4.5 mermaid 逐边对齐：FAILED 能回到 SYNTHESIZING（断点续跑回边）；
    # D12 起再增一条 FAILED -> SCRIPTING（崩溃恢复重放脚本阶段，见 TRANSITIONS 注释）
    assert TaskStatus.FAILED in TRANSITIONS
    assert TRANSITIONS[TaskStatus.FAILED] == {TaskStatus.SYNTHESIZING,
                                             TaskStatus.SCRIPTING}
    # SCRIPT_READY 可进入合成或取消
    assert TaskStatus.SYNTHESIZING in TRANSITIONS[TaskStatus.SCRIPT_READY]
    assert TaskStatus.CANCELED in TRANSITIONS[TaskStatus.SCRIPT_READY]
    assert TaskStatus.CANCELED in TRANSITIONS[TaskStatus.PENDING]


def test_illegal_transition_raises(tmp_path):
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    with session_scope(s) as db:
        u = User(username="x", password_hash="x")
        db.add(u)
        db.flush()
        t = Task(id="x", user_id=u.id, topic="x", status=TaskStatus.DONE,
                 progress=100, stage="")
        db.add(t)
        db.flush()
        try:
            _transition(db, t, TaskStatus.PENDING)
            raise AssertionError("应当抛 IllegalTransition")
        except IllegalTransition:
            pass


def test_upsert_cache_idempotent(tmp_path):
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s)
    runner = TaskRunner(s, make_engine=lambda: FakeEngine(s),
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: FakePostprocess())

    class _R:
        segment = FakeSeg(1, "A", "t")
        text_hash = "abc"
        wav_path = "p/wav"
        duration_ms = 10

    with session_scope(s) as db:
        runner._upsert_cache(db, _R())
        runner._upsert_cache(db, _R())  # 第二次应更新而非新增
        n = db.query(AudioCache).filter(AudioCache.text_hash == "abc").count()
        assert n == 1


def test_stage_synthesize_rejects_non_ready(tmp_path):
    """合成阶段仅接受 SCRIPT_READY / FAILED；PENDING 直接非法迁移。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")  # 初始 PENDING
    runner = TaskRunner(s)
    try:
        runner._stage_synthesize("t1")
        raise AssertionError("应当抛 IllegalTransition")
    except IllegalTransition:
        pass


def test_dynamic_intro_is_synthesized_and_passed_to_postprocess(tmp_path):
    """动态片头（含日期/主题占位符）应被合成，并真正传给后期。

    回归背景：`_stage_postprocess` 原本硬编码 `intro=None, outro=None`，
    片头尾素材**根本没进成片**；且假引擎缺 `synthesize` 时，合成失败会被
    `except` 静默吞掉，测试仍显示「通过」——这条用例专门防这种假绿。
    """
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")

    engine = FakeEngine(s)
    post = FakePostprocess()
    runner = TaskRunner(s, make_engine=lambda: engine,
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: post)

    runner.submit_script("t1")
    _poll(s, "t1", {TaskStatus.SCRIPT_READY, TaskStatus.FAILED})
    runner.submit_synthesize("t1").result(timeout=30)

    with session_scope(s) as db:
        assert db.get(Task, "t1").status == TaskStatus.DONE

    assert engine.synth_texts, "动态片头未被合成"
    assert post.last_intro is not None, "片头未传给后期"
    assert post.last_outro is not None, "片尾未传给后期"
    # 渲染应已替换占位符：不得再残留「（日期）」/「（主题）」
    joined = "".join(engine.synth_texts)
    assert "（日期）" not in joined and "（主题）" not in joined


def test_intro_template_disabled_falls_back_to_none(tmp_path):
    """`intro_template` 置空 ⇒ 不做动态合成（回退固定素材 / 无片头）。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")

    engine = FakeEngine(s)
    runner = TaskRunner(s, make_engine=lambda: engine,
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: FakePostprocess())
    # 直接测最小单元，避免跑整条流水线
    with session_scope(s) as db:
        task = db.get(Task, "t1")
        s.intro_template = ""
        assert runner._build_dynamic_intro(
            task, engine=engine, work_dir=s.work_path / "t1") is None


# --------------------------------------------------------------------------- #
# [FIX-SYNTH-DBLOCK-01] 合成阶段不得把写事务带进 synthesize_lines
# --------------------------------------------------------------------------- #

class _ProbeEngine(FakeEngine):
    """在合成过程中替 `_on_synth_progress` 探针：

    1. 量每次进度回调的墙钟耗时（被 SQLite 忙等卡住时会到 ~5.5s）；
    2. 用**另一个 session** 把它写进去的值读回来（写失败就读到旧值）；
    3. 顺手做一次真实写入，验证写锁确实已释放。

    第 3 步是这条用例真正想钉住的机制：只要 `_stage_synthesize` 还把 session
    开在 `synthesize_lines` 外面，这里的写入必然抛 `database is locked`。
    """

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.cb_ms: list[float] = []
        self.seen_progress: list[int] = []

    def synthesize_lines(self, norm_lines, out_dir, speed=1.0, tone="",
                         on_progress=None):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        results = []
        for i, nl in enumerate(norm_lines, 1):
            key = self.cache_key(nl.read_text, nl.speaker, speed, tone)
            wav = out / f"seg_{i}.wav"
            with wave.open(str(wav), "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(b"\x00\x00" * 200)
            results.append(FakeSynthResult(
                FakeSeg(nl.seq, nl.speaker, nl.read_text), key, str(wav), 1000))
            if on_progress:
                t0 = time.perf_counter()
                on_progress(i, len(norm_lines), None)
                self.cb_ms.append((time.perf_counter() - t0) * 1000)
                with session_scope(self.s) as db:
                    self.seen_progress.append(db.get(Task, "t1").progress)
                    db.get(Task, "t1").stage = "probe-write"  # 写锁未释放则这里炸
        return results


def test_synth_does_not_hold_write_lock_across_synthesis(tmp_path):
    """回归：合成期间不得持有写事务，否则每句进度回写要忙等 5.5s 并静默失败。

    实测背景（D10 压测 / R21 插桩）：
      - 服务端逐段打点 `l2_named_copy → l3_progress_cb` = 5521 / 5522 / 5534 ms；
      - 独立进程同口径 = 23.8 ms；
      - 最小复现 probe_sqlite_busy.py：长事务未提交 5.52s + database is locked，
        已提交 0.00s 成功。
    修法是让「写规范化读文」的事务在进合成**之前**提交。
    """
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")

    eng = _ProbeEngine(s)
    runner = TaskRunner(s, make_engine=lambda: eng,
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: FakePostprocess())
    # 先让脚本阶段真的写出 script_lines（否则合成阶段会以「脚本为空」直接退出）
    runner.submit_script("t1")
    _poll(s, "t1", {TaskStatus.SCRIPT_READY, TaskStatus.FAILED})
    with session_scope(s) as db:
        t = db.get(Task, "t1")
        assert t.status == TaskStatus.SCRIPT_READY, t.status
        assert t.script_lines, "脚本阶段未落 script_lines"
        progress_at_ready = t.progress

    runner._stage_synthesize("t1")     # 不走线程池，便于直接断言

    # ① 回调不能被 SQLite 忙等卡住（回归时这里会是 ~5500ms）
    assert eng.cb_ms, "进度回调没被调用"
    assert max(eng.cb_ms) < 1000, f"进度回调被阻塞：{eng.cb_ms}"

    # ② 进度必须真的写进去并从另一个 session 可见
    lo, hi = _STAGE_PROGRESS[TaskStatus.SYNTHESIZING]
    assert eng.seen_progress[0] != progress_at_ready, (
        f"合成期间进度停在 {progress_at_ready}，说明回写全部失败：{eng.seen_progress}")
    assert all(lo <= p <= hi for p in eng.seen_progress), eng.seen_progress
    assert eng.seen_progress == sorted(eng.seen_progress), "进度应单调不减"

    # ③ 结构不变式：合成收尾后 text_hash / seg_status 照旧回写
    with session_scope(s) as db:
        lines = list(db.get(Task, "t1").script_lines)
        assert lines and all(ln.seg_status == SegStatus.DONE for ln in lines)
        assert all(ln.text_hash for ln in lines)
        assert all(int(ln.duration_ms or 0) > 0 for ln in lines)


def test_progress_write_failure_logs_limited_warning(tmp_path, monkeypatch, caplog):
    """进度回写失败**不许静默**：留限流告警，且不中断合成。"""
    import api.services.task_runner as tr

    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")
    runner = TaskRunner(s)

    @contextmanager
    def _boom(*_a, **_k):
        raise RuntimeError("database is locked")
        yield  # pragma: no cover

    monkeypatch.setattr(tr, "session_scope", _boom)
    with caplog.at_level(logging.WARNING):
        for i in range(1, 6):
            runner._on_synth_progress("t1", i, 5)   # 5 次失败，全被吞

    warned = [r.getMessage() for r in caplog.records if "进度回写失败" in r.getMessage()]
    assert len(warned) == 3, f"应限流为 3 条告警，实际 {len(warned)}：{warned}"
    assert runner._progress_failures == 5


# --------------------------------------------------------------------------- #
# 完整流水线（假依赖）
# --------------------------------------------------------------------------- #

def test_full_pipeline_reaches_done(tmp_path):
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")

    runner = TaskRunner(s, make_engine=lambda: FakeEngine(s),
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: FakePostprocess())

    runner.submit_script("t1")
    _poll(s, "t1", {TaskStatus.SCRIPT_READY, TaskStatus.FAILED})
    with session_scope(s) as db:
        assert db.get(Task, "t1").status == TaskStatus.SCRIPT_READY

    fut = runner.submit_synthesize("t1")
    # 阻塞直到流水线（含 finally 清理）结束，避免文件系统断言与后台线程竞态
    fut.result(timeout=30)
    with session_scope(s) as db:
        t = db.get(Task, "t1")
        assert t.status == TaskStatus.DONE, t.status
        ep = db.query(Episode).filter(Episode.task_id == "t1").first()
        assert ep is not None
        # 终态清理：work 目录应被删除
        assert not (s.work_path / "t1").exists()
        # RSS 落盘（glob 避免依赖 token 取值）
        xmls = list(s.podcast_path.glob("*.xml"))
        assert xmls, "未生成 RSS XML"


def _run_pipeline_once(s, task_id="t1") -> None:
    """用假依赖把一条任务从 PENDING 推到 DONE（阻塞到流水线结束）。"""
    runner = TaskRunner(s, make_engine=lambda: FakeEngine(s),
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: FakePostprocess())
    runner.submit_script(task_id)
    _poll(s, task_id, {TaskStatus.SCRIPT_READY, TaskStatus.FAILED})
    runner.submit_synthesize(task_id).result(timeout=30)


def _feed_items(s) -> list:
    xmls = list(s.podcast_path.glob("*.xml"))
    assert len(xmls) == 1, f"应恰有一份 feed，实际 {len(xmls)}"
    root = ET.fromstring(xmls[0].read_text(encoding="utf-8"))
    return root.findall("channel/item")


def test_feed_really_contains_the_episode_just_packaged(tmp_path):
    """[FIX-FEED-LATEST-01]「RSS 文件存在」不等于「这一期在里面」。

    原用例只断言 `xmls` 非空——而缺陷恰恰是**文件在、内容缺最新一期**：
    封装阶段本任务还停在 `PACKAGING`，而 feed 的过滤条件是 `Task.status == DONE`，
    于是刚做好的这期被它自己的过滤条件挡掉（实测 14/14 份 feed 都恰好少一条，
    且**恒定少一条**，因此格外容易被当成正常）。断言必须落到 item 内容上。
    """
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")
    _run_pipeline_once(s, "t1")

    with session_scope(s) as db:
        ep = db.query(Episode).filter(Episode.task_id == "t1").one()
        guid, declared_size, mp3_path = ep.feed_guid, int(ep.file_size), Path(ep.mp3_path)
        assert db.get(Task, "t1").status == TaskStatus.DONE

    # ⚠️ 期望值**必须取自磁盘**，不能取 `ep.file_size`。
    # 初版把两边都写成库里的值，等于「产物跟它自己比」——变异 M3（file_size 与
    # 磁盘差 1 字节）注入后照样绿，是本台第一轮抓出的摆设断言。
    disk = mp3_path.stat().st_size
    assert declared_size == disk, \
        f"episodes.file_size={declared_size} 与磁盘实际 {disk} 不一致"

    items = _feed_items(s)
    assert len(items) == 1, f"封装当刻应恰好含本期 1 条，实际 {len(items)} 条"
    assert items[0].findtext("guid") == guid
    enc = items[0].find("enclosure")
    assert enc is not None and int(enc.get("length")) == disk, \
        f"enclosure@length 必须等于磁盘实际字节数 {disk}（不是时长、不是 0）"


def test_feed_keeps_done_episodes_when_a_new_one_is_packaged(tmp_path):
    """纳入「本期」不得挤掉已 DONE 的旧单集（否则就是把落后的那一期换成新一期）。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    uid = _seed_user_and_task(s, "t1")
    _run_pipeline_once(s, "t1")
    with session_scope(s) as db:
        db.add(Task(id="t2", user_id=uid, topic="y", target_duration_sec=120,
                    target_word_count=20, status=TaskStatus.PENDING,
                    progress=0, stage=""))
    _run_pipeline_once(s, "t2")

    with session_scope(s) as db:
        guids = {e.feed_guid for e in db.query(Episode).all()}
    assert len(guids) == 2
    items = _feed_items(s)
    assert {it.findtext("guid") for it in items} == guids, \
        f"feed 应同时含两期，实际 {[it.findtext('guid') for it in items]}"


# --------------------------------------------------------------------------- #
# 终态清理的可观测性（data/work 回收）
# --------------------------------------------------------------------------- #

def test_cleanup_work_actually_deletes(tmp_path):
    """正常路径：目录删干净，且**不该**产生告警。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    runner = TaskRunner(s)

    work = Path(s.work_path) / "t1"
    work.mkdir(parents=True, exist_ok=True)
    (work / "seg.wav").write_bytes(b"x" * 1024)

    runner._cleanup_work("t1")
    assert not work.exists(), "work 目录应被删除"


def test_cleanup_work_warns_when_delete_blocked(tmp_path, monkeypatch, caplog):
    """删不掉工作目录时**必须告警**（附残留体积），而不是静默累积。

    这条锁的是本项目的「同一族缺陷」：`rmtree(ignore_errors=True)` 把失败
    伪装成什么都没发生 —— 沙箱 bulk-delete 守卫会拦下删除，data/work 于是
    攒到 GB 级而日志里一个字都没有。告警是唯一的发现手段，所以要被测试钉住。
    """
    import api.services.task_runner as tr

    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    runner = TaskRunner(s)

    work = Path(s.work_path) / "t1"
    work.mkdir(parents=True, exist_ok=True)
    (work / "seg.wav").write_bytes(b"x" * (2 * 1024 * 1024))

    # 模拟「删除被守卫拦下」：rmtree 变成空操作（这正是沙箱里的实际效果）
    monkeypatch.setattr(tr.shutil, "rmtree", lambda *_a, **_k: None)

    with caplog.at_level(logging.WARNING, logger="api.services.task_runner"):
        runner._cleanup_work("t1")

    assert work.exists(), "前提不成立：目录应仍在（rmtree 被替换为空操作）"
    msgs = [r.getMessage() for r in caplog.records if "[WORK]" in r.getMessage()]
    assert len(msgs) == 1, f"应有且仅有一条 [WORK] 告警，实际 {msgs}"
    assert "2.0 MB" in msgs[0], f"告警应带上残留体积：{msgs[0]}"
    assert "cleanup_workspace.py" in msgs[0], f"告警应给出回收命令：{msgs[0]}"


def test_cleanup_work_failure_does_not_fail_the_task(tmp_path, monkeypatch):
    """清理失败**绝不能**把成片判成 FAILED —— 那是运维问题，不是任务问题。"""
    import api.services.task_runner as tr

    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")
    runner = TaskRunner(s, make_engine=lambda: FakeEngine(s),
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: FakePostprocess())

    monkeypatch.setattr(tr.shutil, "rmtree", lambda *_a, **_k: None)

    runner.submit_script("t1")
    _poll(s, "t1", {TaskStatus.SCRIPT_READY, TaskStatus.FAILED})
    fut = runner.submit_synthesize("t1")
    fut.result(timeout=30)

    with session_scope(s) as db:
        assert db.get(Task, "t1").status == TaskStatus.DONE
    assert (s.work_path / "t1").exists(), "前提不成立：目录应残留"


# --------------------------------------------------------------------------- #
# [D11] bad-case 修复手段 —— 多音字词典必须在**生产链路**上真的生效
# --------------------------------------------------------------------------- #

class _PolyphoneGenerator(FakeGenerator):
    """脚本里故意写一个会被词典改写的词。"""

    def generate(self, topic, duration_min=1.0, style=""):
        L = types.SimpleNamespace
        lines = [L(seq=1, speaker="A", text="重庆的天气最近怎么样？"),
                 L(seq=2, speaker="B", text="听说那边经常下雨。")]
        return types.SimpleNamespace(
            script=_FakeScript(lines, "标题", "摘要"), target_words=20)


def _drive_synthesis(tmp_path, dict_content: dict | None, caplog=None):
    """跑完脚本阶段 + 合成阶段，返回落库后的 script_lines（按 seq）。"""
    s = _tmp_settings(tmp_path)
    if dict_content is not None:
        dict_file = tmp_path / "poly.json"
        dict_file.write_text(json.dumps(dict_content, ensure_ascii=False),
                             encoding="utf-8")
        s.polyphone_dict = str(dict_file)

    reset_engine()
    init_db(s)
    _seed_user_and_task(s, "t1")
    runner = TaskRunner(s, make_engine=lambda: FakeEngine(s),
                        make_generator=lambda: _PolyphoneGenerator(),
                        make_postprocess=lambda: FakePostprocess())

    runner.submit_script("t1")
    _poll(s, "t1", {TaskStatus.SCRIPT_READY, TaskStatus.FAILED})

    if caplog is not None:
        with caplog.at_level(logging.WARNING):
            runner._stage_synthesize("t1")
    else:
        runner._stage_synthesize("t1")

    with session_scope(s) as db:
        return s, db.query(ScriptLine).filter(
            ScriptLine.task_id == "t1").order_by(ScriptLine.seq).all()


def test_polyphone_dict_applies_on_production_synthesis_path(tmp_path, caplog):
    """**词典必须在 `_stage_synthesize` 上真的被加载并改写落库读文。**

    这条不是 `test_normalize.py` 那些纯函数用例的重复：那边只证明
    `normalize_text(text, polyphone=...)` 本身正确，**证明不了生产链路把词典传了进来**。
    链路断在中间（漏传参数、路径配错、读到空表）时，纯函数测试**照样全绿** ——
    这正是「单测绿 ≠ 接线对」的典型。变异 M9 就是靠这条抓住的。
    """
    s, lines = _drive_synthesis(
        tmp_path, {"_readme": ["说明"], "重庆": "崇庆"}, caplog)

    assert lines[0].text == "重庆的天气最近怎么样？", "原文不得被改动"
    assert "崇庆" in lines[0].read_text, "读文应已被词典改写"
    assert "重庆" not in lines[0].read_text
    assert lines[1].read_text == lines[1].text, "词典是整词替换，不该改写无关行"

    msgs = [r.getMessage() for r in caplog.records if "[POLYPHONE]" in r.getMessage()]
    assert len(msgs) == 1, f"不可逆改写必须留痕，实际日志：{msgs}"
    assert "t1" in msgs[0] and "1 行" in msgs[0], msgs[0]


def test_polyphone_dict_default_empty_does_not_rewrite(tmp_path):
    """对照组：词典为空时读文保持原文。

    没有这条，「读文变了」就分不清是词典起的作用，还是文本本来就会变。
    """
    _, lines = _drive_synthesis(tmp_path, {"_readme": ["空表"]})
    assert "重庆" in lines[0].read_text
    assert "崇庆" not in lines[0].read_text
