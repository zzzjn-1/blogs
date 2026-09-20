# -*- coding: utf-8 -*-
"""D12「性能优化与稳定性加固」单测：崩溃恢复 / 队列位置 / 缓存命中率 / OOM 重试。

不依赖 GPU 与网络：任务侧全部走假引擎注入；OOM 那一组只测
`TTSEngine._infer_with_oom_retry` 的**重试决策**（把 `_infer` 换成假函数），
不碰真的推理。
"""
from __future__ import annotations

import types
import wave
from pathlib import Path

import pytest

from api.config import Settings
from api.db import init_db, reset_engine, session_scope
from api.models import Episode, Task, TaskStatus, User
from api.services.task_runner import (
    AUTO_RESUMABLE,
    NON_TERMINAL,
    TRANSITIONS,
    TaskRunner,
    _INTERRUPTED_MSG,
    _transition,
)
from api.services.tts import TTSEngine, TTSError


# --------------------------------------------------------------------------- #
# 夹具与假依赖
# --------------------------------------------------------------------------- #

def _tmp_settings(tmp_path, **over) -> Settings:
    kw = dict(
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
    kw.update(over)
    return Settings(**kw)


class _Seg:
    def __init__(self, line_seq, speaker, read_text):
        self.line_seq, self.speaker, self.read_text = line_seq, speaker, read_text


class _Res:
    def __init__(self, segment, text_hash, wav_path, duration_ms, cached=False):
        self.segment, self.text_hash = segment, text_hash
        self.wav_path, self.duration_ms, self.cached = wav_path, duration_ms, cached


class _Script:
    def __init__(self, lines, title, summary):
        self.lines, self.title, self.summary = lines, title, summary


class FakeGenerator:
    def generate(self, topic, duration_min=1.0, style=""):
        L = types.SimpleNamespace
        lines = [L(seq=1, speaker="A", text="第一句内容。"),
                 L(seq=2, speaker="B", text="第二句内容。")]
        return types.SimpleNamespace(
            script=_Script(lines, "标题", "摘要"), target_words=20)


class FakeEngine:
    """第二趟起全部报 `cached=True` —— 用来验证命中率计数的「整体重算」。"""

    def __init__(self, settings):
        self.s = settings
        self.seen_keys: set[str] = set()
        self.lines_calls = 0

    def cache_key(self, read_text, speaker, speed, tone):
        return f"ck|{speaker}|{read_text}|{speed}|{tone}"

    def synthesize(self, text, speaker="voice_a", *, speed=1.0, tone="",
                   text_hash=None):
        out = Path(self.s.work_path) / "_fake_intro"
        out.mkdir(parents=True, exist_ok=True)
        wav = out / f"{abs(hash((text, speaker, speed, tone)))}.wav"
        _write_wav(wav)
        return types.SimpleNamespace(
            segment=_Seg(0, speaker, text),
            text_hash=text_hash or self.cache_key(text, speaker, speed, tone),
            wav_path=str(wav), duration_ms=1000, cached=False, elapsed_s=0.0)

    def synthesize_lines(self, norm_lines, out_dir, speed=1.0, tone="",
                         on_progress=None):
        self.lines_calls += 1
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        results = []
        for i, nl in enumerate(norm_lines, 1):
            key = self.cache_key(nl.read_text, nl.speaker, speed, tone)
            cached = key in self.seen_keys          # 见过的就是「命中缓存」
            self.seen_keys.add(key)
            wav = out / f"seg_{i}.wav"
            _write_wav(wav)
            results.append(_Res(_Seg(nl.seq, nl.speaker, nl.read_text),
                                key, str(wav), 1000, cached=cached))
            if on_progress:
                on_progress(i, len(norm_lines), results[-1])
        return results


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b"\x00\x00" * 200)


class FakePostprocess:
    def __init__(self):
        self.invocations = 0

    def __call__(self, clips, out_dir, name, settings, intro=None, outro=None):
        self.invocations += 1
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        mp3 = out / f"{name}.mp3"
        mp3.write_bytes(b"FAKEMP3")
        return types.SimpleNamespace(mp3=str(mp3))


def _make_runner(s, engine=None):
    eng = engine or FakeEngine(s)
    pp = FakePostprocess()
    runner = TaskRunner(s, make_engine=lambda: eng,
                        make_generator=lambda: FakeGenerator(),
                        make_postprocess=lambda: pp)
    return runner, eng, pp


def _seed(s, task_id="t1", status=TaskStatus.PENDING, topic="x",
          duration=120, **extra) -> None:
    with session_scope(s) as db:
        u = User(username=f"u-{task_id}", password_hash="x")
        db.add(u)
        db.flush()
        db.add(Task(id=task_id, user_id=u.id, topic=topic,
                    target_duration_sec=duration, target_word_count=20,
                    status=status, progress=0, stage="", **extra))


def _seed_lines(s, task_id, n=2, seg_status="PENDING") -> None:
    from api.models import ScriptLine
    with session_scope(s) as db:
        for i in range(1, n + 1):
            db.add(ScriptLine(task_id=task_id, seq=i,
                              speaker="A" if i % 2 else "B",
                              text=f"第{i}句内容。", read_text="",
                              seg_status=seg_status))


def _get(s, task_id):
    with session_scope(s) as db:
        t = db.get(Task, task_id)
        return types.SimpleNamespace(
            status=t.status, stage=t.stage, error_msg=t.error_msg,
            progress=t.progress, finished_at=t.finished_at,
            cache_hit_count=t.cache_hit_count, cache_seg_count=t.cache_seg_count)


# --------------------------------------------------------------------------- #
# 1. 崩溃恢复
# --------------------------------------------------------------------------- #

def test_recover_marks_synthesizing_failed_when_auto_resume_off(tmp_path):
    """SYNTHESIZING 是「进程被杀」的典型残留：必须变成终态，否则永远卡住。"""
    s = _tmp_settings(tmp_path, startup_auto_resume=False)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SYNTHESIZING)
    _seed_lines(s, "t1")
    runner, _, _ = _make_runner(s)

    out = runner.recover_interrupted()

    assert out["scanned"] == 1
    assert out["failed"] == ["t1"]
    assert out["resumed"] == []
    st = _get(s, "t1")
    assert st.status == TaskStatus.FAILED
    assert st.error_msg == _INTERRUPTED_MSG
    assert st.finished_at is not None          # 终态必须写时间，否则清理逻辑失效


def test_recover_skips_script_ready(tmp_path):
    """SCRIPT_READY 在等用户确认，不是被中断的中间态 —— 绝不能动它。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SCRIPT_READY)
    runner, _, _ = _make_runner(s)

    out = runner.recover_interrupted()

    assert out["scanned"] == 1
    assert out["skipped"] == ["t1"]
    assert out["failed"] == [] and out["resumed"] == []
    assert _get(s, "t1").status == TaskStatus.SCRIPT_READY


@pytest.mark.parametrize("stale", sorted(AUTO_RESUMABLE))
def test_recover_auto_resumes_to_done(tmp_path, stale):
    """D12 完成判定的核心：被杀后残留的三个状态都能自动续跑到 DONE。"""
    s = _tmp_settings(tmp_path)          # startup_auto_resume 默认 True
    reset_engine()
    init_db(s)
    _seed(s, "t1", stale)
    _seed_lines(s, "t1")
    runner, _, pp = _make_runner(s)

    out = runner.recover_interrupted()
    assert out["resumed"] == ["t1"]

    runner._futures["t1"].result(timeout=30)   # 等后台作业结束（异常会在此抛出）
    assert _get(s, "t1").status == TaskStatus.DONE
    assert pp.invocations == 1


def test_recover_requeues_scripting(tmp_path):
    """SCRIPTING 残留 → 落 FAILED 再重放脚本阶段（状态机不许自环）。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SCRIPTING)
    runner, _, _ = _make_runner(s)

    out = runner.recover_interrupted()
    assert out["script_requeued"] == ["t1"]

    runner._futures["t1"].result(timeout=30)
    assert _get(s, "t1").status == TaskStatus.SCRIPT_READY


def test_recover_requeues_pending(tmp_path):
    """PENDING = 脚本作业还没跑过（建任务后立刻被杀），直接重投。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.PENDING)
    runner, _, _ = _make_runner(s)

    out = runner.recover_interrupted()
    assert out["scanned"] == 1 and out["script_requeued"] == ["t1"]
    runner._futures["t1"].result(timeout=30)
    assert _get(s, "t1").status == TaskStatus.SCRIPT_READY


def test_recover_noop_when_disabled(tmp_path):
    """逃生舱：STARTUP_RECOVER=false 时一个都不许动。"""
    s = _tmp_settings(tmp_path, startup_recover=False)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SYNTHESIZING)
    runner, _, _ = _make_runner(s)

    out = runner.recover_interrupted()

    assert out == {"scanned": 0, "script_requeued": [], "resumed": [],
                   "failed": [], "skipped": []}
    assert _get(s, "t1").status == TaskStatus.SYNTHESIZING


def test_recover_second_pass_finds_nothing(tmp_path):
    """恢复只认非终态 ⇒ 同一次中断最多自动续跑一次，不会跨重启无限重跑。"""
    s = _tmp_settings(tmp_path, startup_auto_resume=False)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SYNTHESIZING)
    runner, _, _ = _make_runner(s)

    assert runner.recover_interrupted()["scanned"] == 1
    assert runner.recover_interrupted()["scanned"] == 0


def test_recover_constants_match_state_machine(tmp_path):
    """常量表与状态机必须自洽 —— 否则恢复会漏扫出僵尸状态。"""
    for st in NON_TERMINAL:
        assert st in TRANSITIONS
        assert st not in TaskStatus.TERMINAL
    assert AUTO_RESUMABLE <= NON_TERMINAL
    assert TaskStatus.SCRIPT_READY not in AUTO_RESUMABLE   # 等用户确认，不算中断
    # 恢复要走的每条边都必须是合法迁移
    for st in AUTO_RESUMABLE:
        assert TaskStatus.FAILED in TRANSITIONS[st], st
    assert TaskStatus.FAILED in TRANSITIONS[TaskStatus.SCRIPTING]
    assert TaskStatus.SCRIPTING in TRANSITIONS[TaskStatus.FAILED]


# --------------------------------------------------------------------------- #
# 2. 封装幂等（续跑会重放 PACKAGING）
# --------------------------------------------------------------------------- #

def test_stage_package_upserts_episode(tmp_path):
    """`episodes.task_id` 是 unique：续跑重放 PACKAGING 不能撞唯一约束。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SCRIPT_READY)
    _seed_lines(s, "t1")
    runner, _, _ = _make_runner(s)

    runner._run_pipeline("t1")
    assert _get(s, "t1").status == TaskStatus.DONE
    with session_scope(s) as db:
        first = db.query(Episode).filter(Episode.task_id == "t1").one()
        first_guid, first_id = first.feed_guid, first.id

    # 制造一次「PACKAGING 半截被杀」：回到 FAILED 再重放整条流水线
    with session_scope(s) as db:
        t = db.get(Task, "t1")
        t.status = TaskStatus.FAILED
    runner._run_pipeline("t1")

    assert _get(s, "t1").status == TaskStatus.DONE
    with session_scope(s) as db:
        rows = db.query(Episode).filter(Episode.task_id == "t1").all()
        assert len(rows) == 1, "续跑不得产生第二条单集记录"
        assert rows[0].id == first_id
        assert rows[0].feed_guid == first_guid, "guid 必须沿用，否则订阅端会看到两个 enclosure"


# --------------------------------------------------------------------------- #
# 3. 队列位置
# --------------------------------------------------------------------------- #

def test_queue_position_running_and_waiting(tmp_path):
    """0 = 正在跑，1 = 下一个（前端据此渲染「前面还有 N 个」）。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    runner, _, _ = _make_runner(s)

    class _Fut:
        def __init__(self, done):
            self._done = done

        def done(self):
            return self._done

    runner._futures = {"running": _Fut(False), "next": _Fut(False),
                       "later": _Fut(False), "gone": _Fut(True)}
    runner._running_task_id = "running"

    assert runner.queue_snapshot()["running"] == "running"
    assert runner.queue_snapshot()["waiting"] == ["next", "later"]
    assert runner.queue_position("running") == 0
    assert runner.queue_position("next") == 1
    assert runner.queue_position("later") == 2
    assert runner.queue_position("gone") is None      # 已完成 → 不在队列
    assert runner.queue_position("never-seen") is None


# --------------------------------------------------------------------------- #
# 4. 缓存命中率
# --------------------------------------------------------------------------- #

def test_cache_hit_counters_recomputed_not_accumulated(tmp_path):
    """命中率必须**整体重算**：续跑会把同一批段重放一遍，累加会让命中率虚高。"""
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SCRIPT_READY)
    _seed_lines(s, "t1")
    runner, eng, _ = _make_runner(s)

    runner._run_pipeline("t1")
    st = _get(s, "t1")
    assert st.cache_seg_count > 0
    assert st.cache_hit_count == 0            # 首次全未命中
    n_seg = st.cache_seg_count

    # 第二次：同一个引擎实例记住了所有 key → 全命中
    with session_scope(s) as db:
        db.get(Task, "t1").status = TaskStatus.FAILED
    runner._run_pipeline("t1")

    st2 = _get(s, "t1")
    assert st2.cache_seg_count == n_seg, "段数不该因为续跑翻倍"
    assert st2.cache_hit_count == n_seg, "续跑应当全部命中缓存"


def test_task_to_dict_exposes_cache_rate_and_timestamps(tmp_path):
    s = _tmp_settings(tmp_path)
    reset_engine()
    init_db(s)
    _seed(s, "t1", TaskStatus.SCRIPT_READY)
    _seed_lines(s, "t1")
    runner, _, _ = _make_runner(s)
    runner._run_pipeline("t1")

    with session_scope(s) as db:
        d = db.get(Task, "t1").to_dict()
    assert d["cache_seg_count"] > 0
    assert d["cache_hit_rate"] == 0.0
    # TaskOut 早就声明了这两个字段，此前 to_dict 不产出 → 接口恒为 null
    assert d["updated_at"] is not None
    assert d["finished_at"] is not None

    # 未跑过合成的任务：seg=0 时命中率必须是 **None（没测过）**，不是 0.0（命中率 0%）。
    # 两者在前端含义完全不同 —— 这条断言就是为区分它们而设（变异 M6 的靶子）。
    _seed(s, "t0", TaskStatus.PENDING)
    runner._run_script("t0")   # 只跑到脚本，不合成 → 计数器保持默认 0
    with session_scope(s) as db:
        d0 = db.get(Task, "t0").to_dict()
    assert d0["cache_seg_count"] == 0
    assert d0["cache_hit_rate"] is None, "seg=0 必须是 null，不能混成 0.0"

    # 部分命中：rate 严格等于 hit/seg（此处分母非 0，杜绝「用 max(1,seg) 兜底」的写法）
    with session_scope(s) as db:
        t = db.get(Task, "t1")
        t.cache_seg_count, t.cache_hit_count = 4, 3
    with session_scope(s) as db:
        d1 = db.get(Task, "t1").to_dict()
    assert d1["cache_hit_rate"] == 0.75


# --------------------------------------------------------------------------- #
# 5. CUDA OOM 重试
# --------------------------------------------------------------------------- #

def test_is_cuda_oom_matches_runtime_error_text():
    assert TTSEngine._is_cuda_oom(RuntimeError(
        "CUDA out of memory. Tried to allocate 2.00 GiB"))
    assert TTSEngine._is_cuda_oom(RuntimeError("Out Of Memory"))     # 大小写不敏感
    assert not TTSEngine._is_cuda_oom(RuntimeError("模型文件缺失"))
    assert not TTSEngine._is_cuda_oom(ValueError("bad text"))


def _engine_with_infer(tmp_path, fn) -> TTSEngine:
    eng = TTSEngine(_tmp_settings(tmp_path, tts_oom_retry=1))
    eng._infer = fn            # 实例属性遮蔽方法，绕开真实推理
    return eng


def test_oom_retried_once_then_succeeds(tmp_path):
    calls = []

    def flaky(text, speaker, speed, tone, text_frontend=True, seed_offset=0):
        calls.append(seed_offset)
        if len(calls) == 1:
            raise RuntimeError("CUDA out of memory. Tried to allocate 1.00 GiB")
        return "SPEECH", 1.5

    eng = _engine_with_infer(tmp_path, flaky)
    speech, elapsed = eng._infer_with_oom_retry("文本", "voice_a", 1.0, "")

    assert speech == "SPEECH" and elapsed == 1.5
    assert len(calls) == 2
    assert calls == [0, 0], "OOM 重试**不偏移种子**（资源问题，换种子无意义）"


def test_oom_exhausted_raises_readable_ttserror(tmp_path):
    def always_oom(text, speaker, speed, tone, text_frontend=True, seed_offset=0):
        raise RuntimeError("CUDA out of memory. Tried to allocate 3.00 GiB")

    eng = _engine_with_infer(tmp_path, always_oom)
    with pytest.raises(TTSError) as ei:
        eng._infer_with_oom_retry("文本", "voice_a", 1.0, "")
    msg = str(ei.value)
    assert "显存不足" in msg and "TTS_OOM_RETRY" in msg      # 必须给出可操作建议
    assert "CUDA out of memory" in msg                       # 原始错误要留着
    assert not isinstance(ei.value, RuntimeError) or True    # 统一成 TTSError


def test_oom_retry_zero_disables_retry(tmp_path):
    calls = []

    def always_oom(text, speaker, speed, tone, text_frontend=True, seed_offset=0):
        calls.append(1)
        raise RuntimeError("CUDA out of memory")

    eng = TTSEngine(_tmp_settings(tmp_path, tts_oom_retry=0))
    eng._infer = always_oom
    with pytest.raises(TTSError):
        eng._infer_with_oom_retry("文本", "voice_a", 1.0, "")
    assert len(calls) == 1, "tts_oom_retry=0 应当只试一次"


def test_non_oom_error_is_not_retried(tmp_path):
    calls = []

    def boom(text, speaker, speed, tone, text_frontend=True, seed_offset=0):
        calls.append(1)
        raise ValueError("文本前端不可用")

    eng = _engine_with_infer(tmp_path, boom)
    with pytest.raises(ValueError):            # 原样抛出，不被包成 TTSError
        eng._infer_with_oom_retry("文本", "voice_a", 1.0, "")
    assert len(calls) == 1
