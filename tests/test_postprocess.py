# -*- coding: utf-8 -*-
"""音频后期与导出的单元测试（D5）。

**全部用例只依赖 FFmpeg，不加载模型、不占显存**，因此可在无 GPU 的机器上跑。
真实端到端（主题 → mp3，含 LLM + TTS）放在 `scripts/verify_d5.py`，不在这里。

测试音频一律用 `lavfi` 现场合成，不引入二进制素材（避免仓库里塞 wav）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import wave
from array import array
from pathlib import Path

import pytest

from api.config import Settings, get_settings
from api.services import postprocess as pp

# lavfi 的 sine 源固定输出 1/8 满幅（实测峰值 -18.06 dBFS）——见下方 tone() 说明
SINE_PEAK_DBFS = -18.06

# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def ff():
    """确保 FFmpeg 可用；不可用则整体跳过（而不是报一堆红）。"""
    try:
        return pp.find_ffmpeg(get_settings())
    except pp.FFmpegNotFound as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def no_assets() -> dict:
    """显式禁用片头片尾，供「时长守恒」类断言使用。

    ⚠️ 必须传 `False` 而**不是** `None`。`None` 的语义是「用配置里的默认素材」，
    而 `backend/assets/{intro,outro}.mp3` 在 D5 已经生成了占位素材（共 4.6 s）。
    早先这些用例传 `None` 之所以能过，只是因为那时素材文件恰好不存在——
    测试挂在对环境状态的隐式依赖上，属**假通过**；素材一落地就集体多出 4.6 s。
    片头片尾自身的行为由 `test_postprocess_with_intro_and_outro` 等专项用例覆盖。
    """
    return {"intro": False, "outro": False}


def tone(path: Path, *, sr: int = 44100, ch: int = 1, dur: float = 0.5,
         freq: int = 440, peak_db: float = -6.0, ffmpeg: str | None = None) -> Path:
    """合成一段**指定峰值电平**的正弦音（可指定采样率/声道/时长）。

    ⚠️ lavfi 的 `sine` 源固定输出 1/8 满幅 —— 实测峰值 **-18.06 dBFS**
    （`volumedetect` 复核：max_volume = -18.1、mean_volume = -21.1），
    所以想要 P dBFS 就必须额外加 `(P + 18.06) dB`。不知道这条会写出
    「以为 -6 dBFS、实际 -24 dBFS」的假素材，让电平类断言全部错位。
    """
    ffmpeg = ffmpeg or pp.find_ffmpeg(get_settings())[0]
    path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i",
        "sine=frequency=%d:sample_rate=%d:duration=%.4f" % (freq, sr, dur),
        "-af", "volume=%.2fdB" % (peak_db - SINE_PEAK_DBFS),
        "-ac", str(ch),
    ]
    # 按扩展名选编码器：写 .mp3 必须用 libmp3lame，
    # 往 mp3 容器里塞 PCM 会报 "Exactly one MP3 audio stream is required"
    if path.suffix.lower() == ".mp3":
        args += ["-ar", str(sr), "-c:a", "libmp3lame", "-b:a", "128k"]
    else:
        args += ["-c:a", "pcm_s16le"]
    pp.run_ffmpeg(args + [str(path)])
    return path


def read_mono_pcm(path: Path) -> tuple[array, int]:
    """读 16-bit PCM wav 的样本与采样率（统一后的产物都是这个格式）。"""
    with wave.open(str(path), "rb") as w:
        assert w.getsampwidth() == 2, "期望 16-bit PCM，实际 %d 字节" % w.getsampwidth()
        assert w.getnchannels() == 1, "期望单声道，实际 %d" % w.getnchannels()
        sr = w.getframerate()
        data = array("h")
        data.frombytes(w.readframes(w.getnframes()))
    return data, sr


def window_rms(samples: array, start: int, length: int) -> float:
    seg = samples[start:start + length]
    if not seg:
        return 0.0
    return (sum(float(v) * v for v in seg) / len(seg)) ** 0.5


# --------------------------------------------------------------------------- #
# FFmpeg 定位
# --------------------------------------------------------------------------- #

def test_find_ffmpeg_returns_existing_files(ff, settings):
    ffmpeg, ffprobe = ff
    assert Path(ffmpeg).is_file()
    assert Path(ffprobe).is_file()
    assert settings.ffmpeg_bin == "" or Path(settings.ffmpeg_bin).exists()


def test_find_ffmpeg_missing_configured_path_is_readable(settings):
    """显式配置了不存在的路径 → 必须给可读错误，而不是静默回退到别的 ffmpeg。"""
    bad = settings.model_copy(update={"ffmpeg_bin": "Z:/no/such/ffmpeg.exe"})
    with pytest.raises(pp.FFmpegNotFound) as ei:
        pp.find_ffmpeg(bad)
    msg = str(ei.value)
    assert "ffmpeg" in msg
    assert "FFMPEG_BIN" in msg and "winget" in msg


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #

def test_probe_reports_format_and_duration(tmp_path, ff, settings):
    p = tone(tmp_path / "a.wav", sr=24000, ch=1, dur=1.25)
    info = pp.probe(p, settings=settings)
    assert info.sample_rate == 24000
    assert info.channels == 1
    assert info.codec == "pcm_s16le"
    assert info.duration_s == pytest.approx(1.25, abs=0.01)
    assert info.size_bytes == p.stat().st_size


def test_probe_missing_file_raises(tmp_path, ff, settings):
    with pytest.raises(pp.PostprocessError) as ei:
        pp.probe(tmp_path / "nope.wav", settings=settings)
    assert "不存在" in str(ei.value)


def test_detect_volume_reports_levels(tmp_path, ff, settings):
    p = tone(tmp_path / "a.wav", dur=0.4, peak_db=-6.0)
    vol = pp.detect_volume(p, settings=settings)
    assert "mean_volume" in vol and "max_volume" in vol
    # -6 dBFS 的正弦，峰值应落在 -6 dB 附近
    assert -7.5 < vol["max_volume"] < -5.0


# --------------------------------------------------------------------------- #
# 格式统一（R12 的核心约束）
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("src_sr,src_ch", [(16000, 1), (24000, 1), (44100, 2), (48000, 2)])
def test_unify_normalizes_to_target(tmp_path, ff, settings, src_sr, src_ch):
    src = tone(tmp_path / "src.wav", sr=src_sr, ch=src_ch, dur=0.3)
    dst = pp.unify(src, tmp_path / "norm.wav",
                   sample_rate=settings.audio_sample_rate,
                   channels=settings.audio_channels, settings=settings)
    info = pp.probe(dst, settings=settings)
    assert (info.sample_rate, info.channels) == (44100, 1)
    assert info.codec == "pcm_s16le"
    assert info.duration_s == pytest.approx(0.3, abs=0.01)


def test_unify_output_is_readable_by_stdlib_wave(tmp_path, ff, settings):
    """统一后的产物必须是 16-bit PCM —— 这样 stdlib wave 也能读（区别于模型输出的 float32）。"""
    src = tone(tmp_path / "src.wav", sr=24000, ch=1, dur=0.2)
    dst = pp.unify(src, tmp_path / "norm.wav", settings=settings)
    samples, sr = read_mono_pcm(dst)
    assert sr == 44100
    assert len(samples) > 0


# --------------------------------------------------------------------------- #
# 静音与淡化
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("ms", [250, 500])
def test_make_silence_duration(tmp_path, ff, settings, ms):
    p = pp.make_silence(ms, tmp_path / ("s%d.wav" % ms), settings=settings)
    info = pp.probe(p, settings=settings)
    assert info.duration_s == pytest.approx(ms / 1000.0, abs=0.01)
    assert info.sample_rate == 44100 and info.channels == 1


def test_apply_fade_in_starts_from_silence(tmp_path, ff, settings):
    src = tone(tmp_path / "src.wav", dur=0.5, peak_db=-3.0)
    out = pp.apply_fade(src, tmp_path / "fi.wav", fade_in_ms=30, settings=settings)
    samples, sr = read_mono_pcm(out)
    head = window_rms(samples, 0, int(sr * 0.005))       # 前 5 ms
    mid = window_rms(samples, int(sr * 0.20), int(sr * 0.02))
    assert head < mid * 0.35, "淡入未生效：head=%s mid=%s" % (head, mid)


def test_apply_fade_out_ends_at_silence(tmp_path, ff, settings):
    src = tone(tmp_path / "src.wav", dur=0.5, peak_db=-3.0)
    out = pp.apply_fade(src, tmp_path / "fo.wav", fade_out_ms=30, settings=settings)
    samples, sr = read_mono_pcm(out)
    tail = window_rms(samples, len(samples) - int(sr * 0.005), int(sr * 0.005))
    mid = window_rms(samples, int(sr * 0.20), int(sr * 0.02))
    assert tail < mid * 0.35, "淡出未生效：tail=%s mid=%s" % (tail, mid)


def test_apply_fade_without_params_copies(tmp_path, ff, settings):
    src = tone(tmp_path / "src.wav", dur=0.2)
    out = pp.apply_fade(src, tmp_path / "copy.wav", settings=settings)
    assert out.is_file()
    assert pp.probe(out, settings=settings).duration_s == pytest.approx(0.2, abs=0.01)


# --------------------------------------------------------------------------- #
# 拼接
# --------------------------------------------------------------------------- #

def test_concat_duration_is_conserved(tmp_path, ff, settings):
    parts = [tone(tmp_path / ("p%d.wav" % i), dur=d) for i, d in enumerate((0.4, 0.6, 0.5))]
    parts = [pp.unify(p, tmp_path / ("n%d.wav" % i), settings=settings)
             for i, p in enumerate(parts)]
    out = pp.concat(parts, tmp_path / "merged.wav", settings=settings)
    assert pp.probe(out, settings=settings).duration_s == pytest.approx(1.5, abs=0.02)


def test_concat_handles_unicode_and_quote_in_path(tmp_path, ff, settings):
    """路径含中文、空格、单引号时必须正确转义（concat 列表最容易在这里翻车）。"""
    d = tmp_path / "中文 目录'quoted"
    p1 = pp.unify(tone(d / "片段'01.wav", dur=0.3), d / "n1.wav", settings=settings)
    p2 = pp.unify(tone(d / "片段 02.wav", dur=0.3), d / "n2.wav", settings=settings)
    out = pp.concat([p1, p2], d / "merged'.wav", settings=settings)
    assert pp.probe(out, settings=settings).duration_s == pytest.approx(0.6, abs=0.02)


def test_concat_empty_raises(tmp_path, ff, settings):
    with pytest.raises(pp.PostprocessError):
        pp.concat([], tmp_path / "x.wav", settings=settings)


def test_concat_writes_list_file(tmp_path, ff, settings):
    a = pp.unify(tone(tmp_path / "a.wav", dur=0.2), tmp_path / "na.wav", settings=settings)
    b = pp.unify(tone(tmp_path / "b.wav", dur=0.2), tmp_path / "nb.wav", settings=settings)
    lp = tmp_path / "list.txt"
    pp.concat([a, b], tmp_path / "m.wav", list_path=lp, settings=settings)
    text = lp.read_text(encoding="utf-8")
    assert text.count("file '") == 2
    assert "-safe" not in text


# --------------------------------------------------------------------------- #
# 响度归一
# --------------------------------------------------------------------------- #

def test_measure_loudness_returns_input_fields(tmp_path, ff, settings):
    """⚠️ 固化一条实测事实：loudnorm 第一遍 JSON 给的是 **input_\\*** 字段。

    计划书 11.2 第 5.4 步写成 `output_*`，属笔误（V1.8.0 已更正）。
    这个用例的作用就是防止有人按文档改回 output_* 而把二遍 loudnorm 写坏。
    """
    src = tone(tmp_path / "src.wav", dur=0.6, peak_db=-12.0)
    m = pp.measure_loudness(src, settings=settings)
    for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        assert k in m, "缺少 %s，实际键：%s" % (k, sorted(m))
    assert float(m["input_i"]) < 0


def test_normalize_loudness_hits_target(tmp_path, ff, settings):
    """两遍 loudnorm 后应落在目标响度附近（linear 模式允许 ±1 LU 浮动）。"""
    src = tone(tmp_path / "quiet.wav", dur=1.2, peak_db=-30.0)
    out, measured = pp.normalize_loudness(src, tmp_path / "norm.wav", settings=settings)
    after = pp.measure_loudness(out, settings=settings)
    assert float(after["input_i"]) == pytest.approx(settings.audio_loudness_i, abs=1.0)


def test_normalize_loudness_missing_field_raises(tmp_path, ff, settings):
    src = tone(tmp_path / "src.wav", dur=0.3)
    with pytest.raises(pp.PostprocessError) as ei:
        pp.normalize_loudness(src, tmp_path / "o.wav", measured={"input_i": "-20"},
                              settings=settings)
    assert "缺少字段" in str(ei.value)


def test_normalize_loudness_respects_true_peak(tmp_path, ff, settings):
    """真峰必须留在 0 dBFS 之下（爆音验收的等价条件）。"""
    src = tone(tmp_path / "hot.wav", dur=1.0, peak_db=-1.0)
    out, _ = pp.normalize_loudness(src, tmp_path / "n.wav", settings=settings)
    vol = pp.detect_volume(out, settings=settings)
    assert vol["max_volume"] < 0.0


# --------------------------------------------------------------------------- #
# 响度测量可用性守卫（实测边界，别删）
# --------------------------------------------------------------------------- #

def test_loudness_usable_accepts_normal_measurement():
    ok, reason = pp.loudness_usable({"input_i": "-21.75", "input_tp": "-18.06",
                                     "input_lra": "1.20", "input_thresh": "-33.31",
                                     "target_offset": "0.39"})
    assert ok is True and reason == ""


def test_loudness_usable_rejects_non_finite():
    """input_i = -inf 是 loudnorm 对过短/静音输入的返回值，必须识别出来。"""
    ok, reason = pp.loudness_usable({"input_i": "-inf", "input_tp": "-18.06",
                                     "input_lra": "1.20", "input_thresh": "-33.31",
                                     "target_offset": "0.39"})
    assert ok is False
    assert "非有限值" in reason


def test_loudness_usable_rejects_missing_field():
    ok, reason = pp.loudness_usable({"input_i": "-20"})
    assert ok is False and "缺少字段" in reason


def test_normalize_loudness_rejects_non_finite_measurement(tmp_path, ff, settings):
    src = tone(tmp_path / "src.wav", dur=0.3)
    bad = {"input_i": "-inf", "input_tp": "-inf", "input_lra": "0",
           "input_thresh": "-inf", "target_offset": "0"}
    with pytest.raises(pp.PostprocessError) as ei:
        pp.normalize_loudness(src, tmp_path / "o.wav", measured=bad, settings=settings)
    msg = str(ei.value)
    assert "不可用于第二遍" in msg and "-inf" in msg


def test_postprocess_very_short_input_skips_loudnorm(tmp_path, ff, settings, no_assets):
    """⚠️ 实测：素材短于约 0.4 s 时第一遍给 input_i = -inf，第二遍必硬报错。

    正确行为是**降级为跳过响度归一 + 告警**，而不是让整条链路失败 ——
    否则「只有一个短句」的成片根本产不出来。
    （边界实测：0.05/0.10/0.20/0.30/0.35 s 均为 -inf，0.40 s 起为 -21.75。）
    """
    clips = _clips(tmp_path, [(0.25, "A")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="short", settings=settings,
                         **no_assets)
    assert res.loudness_applied is False
    assert any("跳过" in w and "响度" in w for w in res.warnings), res.warnings
    assert res.mp3.is_file()


def test_postprocess_silent_input_skips_loudnorm(tmp_path, ff, settings, no_assets):
    """纯数字静音无论多长都给 -inf（实测 2.0 s 静音），同样走降级分支。"""
    sil = pp.make_silence(1200, tmp_path / "s.wav", settings=settings)
    res = pp.postprocess([pp.Clip(wav=sil, speaker="A", line_seq=1)],
                         out_dir=tmp_path / "out", name="sil", settings=settings,
                         **no_assets)
    assert res.loudness_applied is False
    assert res.mp3.is_file()


def test_postprocess_reports_loudness_landing(tmp_path, ff, settings, no_assets):
    """成片响度落点可复核（linear 模式允许 ±1 LU）。"""
    clips = _clips(tmp_path, [(1.5, "A"), (1.5, "B")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="lufs", settings=settings,
                         **no_assets)
    assert res.loudness_applied is True
    landed = float(res.loudness_after["input_i"])
    assert landed == pytest.approx(settings.audio_loudness_i, abs=1.0)


def test_pause_lengths_follow_settings(tmp_path, ff, settings, no_assets):
    """停顿时长必须**真的**由配置决定 —— 这是 D11「bad case 修复（停顿）」的手段基础。

    真人听测若报「换人处停顿生硬」或「同一个人连着说不喘气」，唯一的手段就是改
    `audio_pause_turn_ms` / `audio_pause_sentence_ms`。此前**没有任何测试**锁住这件事
    （`grep pause tests/` 只命中响度相关用例），也就是说这两个旋钮是否真的接在
    输出上，全靠人读代码相信 —— 属于「没跑过就等于没覆盖」。

    序列 A→B→B→A 同时覆盖两种停顿：
      话轮停顿 2 处（A→B、B→A，speaker 变化）＋ 句间停顿 1 处（B→B）；
      末段之后不插停顿（见 `postprocess` 的 `if i == len(normed) - 1: break`）。
    """
    specs = [(0.8, "A"), (0.8, "B"), (0.8, "B"), (0.8, "A")]

    def run(cfg, tag):
        work = tmp_path / tag
        work.mkdir(parents=True, exist_ok=True)
        clips = _clips(work, specs, ff, cfg)
        res = pp.postprocess(clips, out_dir=work / "out", name=tag,
                             settings=cfg, **no_assets)
        return res, pp.probe(res.mp3, settings=cfg).duration_s

    base = settings.model_copy(update={
        "audio_pause_turn_ms": 500, "audio_pause_sentence_ms": 250})
    wide = settings.model_copy(update={
        "audio_pause_turn_ms": 1000, "audio_pause_sentence_ms": 1000})

    res_a, dur_a = run(base, "base")
    res_b, dur_b = run(wide, "wide")

    assert res_a.pause_count == 3, "前提不成立：A-B-B-A 应有 3 处停顿"
    assert res_b.pause_count == 3

    # 话轮 500→1000（2 处，+1000ms）＋ 句间 250→1000（1 处，+750ms）= +1.75s
    assert dur_b - dur_a == pytest.approx(1.75, abs=0.15), (dur_a, dur_b)


# --------------------------------------------------------------------------- #
# mp3 导出
# --------------------------------------------------------------------------- #

def test_export_mp3_spec(tmp_path, ff, settings):
    src = tone(tmp_path / "src.wav", dur=1.0)
    mp3 = pp.export_mp3(src, tmp_path / "out.mp3", settings=settings)
    info = pp.probe(mp3, settings=settings)
    assert info.codec == "mp3"
    assert (info.sample_rate, info.channels) == (44100, 1)
    kbps = info.size_bytes * 8 / max(info.duration_s, 0.001) / 1000.0
    assert 100 < kbps < 150, "码率偏离 128k 过多：%.1f kbps" % kbps


# --------------------------------------------------------------------------- #
# 编排：postprocess
# --------------------------------------------------------------------------- #

def _clips(tmp_path, specs, ff, settings):
    """specs: [(duration, speaker)] → 统一后的 Clip 列表。"""
    out = []
    for i, (dur, spk) in enumerate(specs):
        raw = tone(tmp_path / ("raw%d.wav" % i), dur=dur, freq=300 + 50 * i)
        wav = pp.unify(raw, tmp_path / ("k%d.wav" % i), settings=settings)
        out.append(pp.Clip(wav=wav, speaker=spk, line_seq=i + 1))
    return out


def test_postprocess_mixed_sample_rates_duration_is_correct(tmp_path, ff, settings, no_assets):
    """R12 验收：混入 16k / 24k / 44.1k 立体声，成片时长仍必须守恒。"""
    raw = [
        tone(tmp_path / "r0.wav", sr=16000, ch=1, dur=0.4),
        tone(tmp_path / "r1.wav", sr=24000, ch=1, dur=0.5),
        tone(tmp_path / "r2.wav", sr=44100, ch=2, dur=0.3),
    ]
    clips = [pp.Clip(wav=p, speaker="A" if i < 2 else "B", line_seq=i + 1)
             for i, p in enumerate(raw)]
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="mix", settings=settings,
                         **no_assets)
    # 同人 A→A 插句间 250ms，换人 A→B 插话轮 500ms
    expect = 0.4 + 0.5 + 0.3 + 0.25 + 0.5
    assert res.duration_s == pytest.approx(expect, abs=0.06)
    assert res.segments == 3 and res.pause_count == 2


def test_postprocess_pause_selection_matches_speakers(tmp_path, ff, settings, no_assets):
    clips = _clips(tmp_path, [(0.3, "A"), (0.3, "A"), (0.3, "B"), (0.3, "B")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="p", settings=settings,
                         **no_assets)
    assert res.pause_count == 3
    # A→A(250) + A→B(500) + B→B(250) = 1000 ms
    assert res.duration_s == pytest.approx(1.2 + 1.0, abs=0.06)


def test_postprocess_single_clip_has_no_pause(tmp_path, ff, settings, no_assets):
    clips = _clips(tmp_path, [(0.4, "A")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="one", settings=settings,
                         **no_assets)
    assert res.pause_count == 0
    assert res.duration_s == pytest.approx(0.4, abs=0.06)


def test_postprocess_explicit_disable_does_not_warn(tmp_path, ff, settings):
    """显式禁用（CLI 的 --no-intro/--no-outro）是调用方的意图，不该刷「素材缺失」告警。"""
    clips = _clips(tmp_path, [(0.3, "A")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="off", settings=settings,
                         intro=False, outro=False)
    assert not res.intro_used and not res.outro_used
    missing = [w for w in res.warnings if "缺失" in w]
    assert not missing, "显式禁用不应产生「素材缺失」告警：%s" % missing


def test_postprocess_uses_configured_assets_by_default(tmp_path, ff, settings):
    """不传 intro/outro 时用配置里的默认素材 —— 这正是 `None` 的语义。"""
    if not (settings.intro_file and settings.intro_file.is_file()):
        pytest.skip("未配置片头素材（backend/assets/intro.mp3 不存在）")
    clips = _clips(tmp_path, [(0.3, "A")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="default", settings=settings)
    assert res.intro_used, "配置了片头却没被用上：%s" % settings.intro_file
    if settings.outro_file and settings.outro_file.is_file():
        assert res.outro_used
    assert not [w for w in res.warnings if "缺失" in w]


def test_postprocess_with_intro_and_outro(tmp_path, ff, settings):
    clips = _clips(tmp_path, [(0.3, "A"), (0.3, "B")], ff, settings)
    intro = tone(tmp_path / "intro.mp3", sr=44100, ch=2, dur=0.4)
    outro = tone(tmp_path / "outro.mp3", dur=0.3)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="full", settings=settings,
                         intro=intro, outro=outro)
    assert res.intro_used and res.outro_used
    # 0.4(intro) + 0.3 + 0.5(话轮) + 0.3 + 0.3(outro)
    assert res.duration_s == pytest.approx(1.8, abs=0.08)
    assert not res.warnings


def test_postprocess_missing_assets_warns_not_fails(tmp_path, ff, settings):
    clips = _clips(tmp_path, [(0.3, "A")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="nointro", settings=settings,
                         intro=tmp_path / "nothere.mp3", outro=tmp_path / "nothere2.mp3")
    assert not res.intro_used and not res.outro_used
    missing = [w for w in res.warnings if "缺失" in w]
    assert len(missing) == 2
    assert res.mp3.is_file(), "片头缺失不应阻断成片产出"


def test_postprocess_require_intro_raises(tmp_path, ff, settings):
    """AUDIO_REQUIRE_INTRO=true 时，片头缺失必须报错而不是悄悄降级。

    这里给一个**确定不存在**的路径（而不是 None）——None 会退回配置默认素材，
    而 backend/assets/intro.mp3 在 D5 已存在，那样就永远触发不到这个分支。
    """
    clips = _clips(tmp_path, [(0.3, "A")], ff, settings)
    strict = settings.model_copy(update={"audio_require_intro": True})
    with pytest.raises(pp.PostprocessError) as ei:
        pp.postprocess(clips, out_dir=tmp_path / "out", name="strict", settings=strict,
                       intro=tmp_path / "definitely-missing-intro.mp3", outro=False)
    assert "REQUIRE_INTRO" in str(ei.value)


def test_postprocess_empty_clips_raises(tmp_path, ff, settings):
    with pytest.raises(pp.PostprocessError):
        pp.postprocess([], out_dir=tmp_path / "out", settings=settings)


def test_postprocess_missing_clip_raises(tmp_path, ff, settings):
    clips = [pp.Clip(wav=tmp_path / "ghost.wav", speaker="A")]
    with pytest.raises(pp.PostprocessError) as ei:
        pp.postprocess(clips, out_dir=tmp_path / "out", settings=settings)
    assert "片段文件不存在" in str(ei.value)


def test_postprocess_no_clipping_and_mp3_spec(tmp_path, ff, settings, no_assets):
    clips = _clips(tmp_path, [(0.4, "A"), (0.4, "B")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="final", settings=settings,
                         **no_assets)
    assert res.mp3.is_file()
    info = pp.probe(res.mp3, settings=settings)
    assert (info.codec, info.sample_rate, info.channels) == ("mp3", 44100, 1)
    assert not res.clipped, "成片削波：max_volume=%s dB" % res.max_volume_db
    assert res.max_volume_db is not None and res.max_volume_db < -1.0


def test_postprocess_can_skip_loudnorm(tmp_path, ff, settings, no_assets):
    clips = _clips(tmp_path, [(0.3, "A")], ff, settings)
    raw = settings.model_copy(update={"audio_normalize": False})
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="raw", settings=raw,
                         **no_assets)
    assert res.loudness_applied is False
    assert any("跳过 loudnorm" in w for w in res.warnings)


def test_postprocess_result_to_dict_is_json_serializable(tmp_path, ff, settings, no_assets):
    clips = _clips(tmp_path, [(0.3, "A"), (0.3, "B")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="j", settings=settings,
                         **no_assets)
    blob = json.dumps(res.to_dict(), ensure_ascii=False)
    assert "duration_s" in blob and "max_volume_db" in blob


def test_postprocess_build_dir_keeps_intermediates(tmp_path, ff, settings, no_assets):
    """中间产物保留：出问题时能直接听/看每一步，而不是黑盒。"""
    clips = _clips(tmp_path, [(0.3, "A"), (0.3, "B")], ff, settings)
    res = pp.postprocess(clips, out_dir=tmp_path / "out", name="b", settings=settings,
                         **no_assets)
    assert res.build_dir.is_dir()
    names = {p.name for p in res.build_dir.iterdir()}
    assert {"merged.wav", "concat_list.txt", "norm"} <= names
    assert (res.build_dir / "normalized.wav").is_file()


def test_postprocess_progress_callback_receives_stages(tmp_path, ff, settings, no_assets):
    clips = _clips(tmp_path, [(0.3, "A")], ff, settings)
    seen: list[str] = []
    pp.postprocess(clips, out_dir=tmp_path / "out", name="cb", settings=settings,
                   on_progress=lambda msg, cur, total: seen.append(msg), **no_assets)
    joined = " ".join(seen)
    for stage in ("[1/6]", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]"):
        assert stage in joined


# --------------------------------------------------------------------------- #
# 子进程封装的可读错误
# --------------------------------------------------------------------------- #

def test_run_ffmpeg_failure_message_is_readable(ff, settings):
    ffmpeg, _ = ff
    with pytest.raises(pp.PostprocessError) as ei:
        pp.run_ffmpeg([ffmpeg, "-hide_banner", "-i", "Z:/definitely/not/here.wav",
                       "-f", "null", "-"], settings=settings)
    msg = str(ei.value)
    assert "退出码" in msg and "错误" in msg


def test_run_ffmpeg_timeout_message_is_readable(ff, settings):
    """`-re` 强制按实时速率读取，10 s 素材配 1 s 超时必然触发超时分支。"""
    ffmpeg, _ = ff
    with pytest.raises(pp.PostprocessError) as ei:
        pp.run_ffmpeg([ffmpeg, "-hide_banner", "-re", "-f", "lavfi",
                       "-i", "sine=duration=10", "-f", "null", "-"],
                      settings=settings, timeout=1)
    assert "超时" in str(ei.value)


# --------------------------------------------------------------------------- #
# Clip 适配
# --------------------------------------------------------------------------- #

def test_clip_from_synth_reads_segment_attributes(tmp_path, ff, settings):
    class _Seg:
        speaker = "B"
        line_seq = 7

    class _Res:
        segment = _Seg()
        wav_path = tmp_path / "x.wav"

    c = pp.Clip.from_synth(_Res())
    assert (c.speaker, c.line_seq, c.wav) == ("B", 7, tmp_path / "x.wav")


def test_build_clips_from_results_keeps_order(tmp_path, ff, settings):
    class _Seg:
        def __init__(self, spk, ls):
            self.speaker, self.line_seq = spk, ls

    class _Res:
        def __init__(self, spk, ls, p):
            self.segment, self.wav_path = _Seg(spk, ls), p

    results = [_Res("A", 1, tmp_path / "a.wav"), _Res("B", 2, tmp_path / "b.wav")]
    clips = pp.build_clips_from_results(results)
    assert [c.speaker for c in clips] == ["A", "B"]
    assert [c.line_seq for c in clips] == [1, 2]


# --------------------------------------------------------------------------- #
# 配置默认值（防止有人把统一格式改坏）
# --------------------------------------------------------------------------- #

def test_audio_settings_defaults_are_sane(settings):
    assert settings.audio_sample_rate == 44100
    assert settings.audio_channels == 1
    assert settings.audio_loudness_i == -16.0
    assert settings.audio_true_peak <= -1.0, "真峰必须留余量，避免 LAME 转码削波"
    assert settings.audio_fade_ms > 0, "无淡化必然在拼接处产生爆音"
    assert settings.audio_pause_turn_ms > settings.audio_pause_sentence_ms
    assert settings.audio_mp3_bitrate.endswith("k")
    assert settings.ffmpeg_timeout >= 60
