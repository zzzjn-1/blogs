# -*- coding: utf-8 -*-
"""音频后期与导出（D5，对应计划书 4.6 后期链路与 11.2 命令模板）。

链路：格式统一 → 片头尾微淡化 → 停顿插入 → 拼接 → 两遍 loudnorm → mp3 导出

每条设计决定都有具体理由（多为实测踩坑，勿随意简化）：

1. **先统一、再拼接**（计划书 R12）。CosyVoice2 输出 24 kHz、CosyVoice1 输出 16 kHz、
   片头素材常为 44.1 kHz 立体声；参数不一致时 concat 会报错或**产出错误时长**。
   统一目标 44.1 kHz / 单声道 / 16-bit PCM，先于一切拼接动作执行。

2. **片头尾微淡化必须在拼接之前**（计划书 11.2 第 5.2 步标注「顺序关键」）。
   淡化要作用在**素材**上，产物才进入成片；若对合并后的整轨淡化，会吃掉正文首尾的字。

3. **wav 输出禁用 `-q:a`**。那是 LAME 参数，对 PCM 编码器无效（计划书 11.2 第 5.1 步注）。

4. **两遍 loudnorm**：第一遍 `print_format=json` 只测量，第二遍把实测值**动态回填**。
   ⚠️ FFmpeg 第一遍 JSON 里给的是 `input_i` / `input_tp` / `input_lra` / `input_thresh`
   （即「输入信号的测量结果」），第二遍的 `measured_*` 要填的正是这几个；
   计划书 11.2 写成 `output_*`，属笔误（已在 V1.8.0 更正）。
   `target_offset` 是增益偏移，实际生效值回在 JSON 的该字段里。

5. **concat 列表必须转义并加 `-safe 0`**。文件名里的单引号要写成 `'\\''`，
   反斜杠统一换正斜杠，否则含中文/空格的绝对路径会解析失败。

6. **语音内容禁止 `acrossfade`**。它会重叠 d 秒音频，等于吃掉字；
   仅当片头是纯音乐且确需叠化时才用（计划书 11.2 注）。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from api.config import PROJECT_ROOT, Settings, get_settings

log = logging.getLogger(__name__)


class PostprocessError(RuntimeError):
    """后期链路错误（FFmpeg 缺失 / 执行失败 / 输入非法）。"""


class FFmpegNotFound(PostprocessError):
    """FFmpeg / FFprobe 不可用。计划书 1.4 P-8 的前置项，缺失则后期模块全部功能不可用。"""


# --------------------------------------------------------------------------- #
# FFmpeg 定位
# --------------------------------------------------------------------------- #

_WINGET_GLOB = "Microsoft/WinGet/Packages"
_FALLBACK_DIRS = (r"C:\ffmpeg\bin", r"D:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin")


def _find_in_winget(name: str) -> str | None:
    """在 winget 的安装目录里找可执行文件（Windows 常见安装方式）。"""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    base = Path(local) / _WINGET_GLOB
    if not base.is_dir():
        return None
    exe = name + ".exe"
    # 包目录名形如 Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0.1-full_build/bin/
    for cand in sorted(base.glob(f"*FFmpeg*/**/bin/{exe}")):
        if cand.is_file():
            return str(cand)
    return None


def _resolve_one(configured: str, name: str) -> str | None:
    if configured:
        p = Path(configured)
        if p.is_dir():
            p = p / (name + ".exe") if os.name == "nt" else p / name
        if p.is_file():
            return str(p)
        return None  # 显式配置了但不存在 -> 交由上层报可读错误
    found = shutil.which(name)
    if found:
        return found
    found = _find_in_winget(name)
    if found:
        return found
    for d in _FALLBACK_DIRS:
        cand = Path(d) / (name + ".exe" if os.name == "nt" else name)
        if cand.is_file():
            return str(cand)
    return None


def find_ffmpeg(settings: Settings | None = None) -> tuple[str, str]:
    """返回 (ffmpeg, ffprobe) 绝对路径；找不到则抛 FFmpegNotFound（带可执行建议）。"""
    s = settings or get_settings()
    ffmpeg = _resolve_one(s.ffmpeg_bin, "ffmpeg")
    ffprobe = _resolve_one(s.ffprobe_bin, "ffprobe")
    missing = [n for n, v in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not v]
    if missing:
        raise FFmpegNotFound(
            "未找到 %s。请安装 FFmpeg 9.0+（需 loudnorm / libmp3lame）并注册 PATH，"
            "或在 .env 设置 FFMPEG_BIN / FFPROBE_BIN 指向可执行文件。"
            "winget: winget install --id Gyan.FFmpeg -e" % " / ".join(missing)
        )
    return ffmpeg, ffprobe  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 子进程封装
# --------------------------------------------------------------------------- #

@dataclass
class FFmpegRun:
    cmd: list[str]
    returncode: int
    stdout: bytes
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_ffmpeg(args: Sequence[str | os.PathLike], *, settings: Settings | None = None,
               timeout: int | None = None, expect_ok: bool = True) -> FFmpegRun:
    """执行 FFmpeg/FFprobe，失败时给出**可读错误**（含退出码与 stderr 尾部）。

    ⚠️ 一律用参数列表调用，不经 shell：路径含中文/空格/引号时 shell 会把它们
    重新切分或转码（本项目已多次踩坑）。
    """
    s = settings or get_settings()
    cmd = [str(a) for a in args]
    limit = timeout if timeout is not None else s.ffmpeg_timeout
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=limit)
    except FileNotFoundError as exc:
        raise FFmpegNotFound("无法执行 %s：%s" % (cmd[0], exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise PostprocessError(
            "FFmpeg 执行超时（%s 秒）：%s" % (limit, cmd[0])
        ) from exc

    run = FFmpegRun(cmd=cmd, returncode=cp.returncode, stdout=cp.stdout,
                    stderr=cp.stderr.decode("utf-8", "replace"))
    if expect_ok and not run.ok:
        tail = run.stderr.strip().splitlines()[-6:]
        raise PostprocessError(
            "FFmpeg 执行失败（退出码 %d）\n  命令：%s\n  错误：\n    %s"
            % (run.returncode, _brief_cmd(cmd), "\n    ".join(tail))
        )
    return run


def _brief_cmd(cmd: Sequence[str], limit: int = 220) -> str:
    # 只保留第一段之后的输入输出名，避免把整条 filter 串糊在报错里
    s = " ".join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd))
    return s if len(s) <= limit else s[:limit] + " ..."


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #

@dataclass
class AudioInfo:
    path: Path
    duration_s: float
    sample_rate: int
    channels: int
    codec: str
    size_bytes: int

    def to_dict(self) -> dict:
        return {"path": str(self.path), "duration_s": round(self.duration_s, 4),
                "sample_rate": self.sample_rate, "channels": self.channels,
                "codec": self.codec, "size_bytes": self.size_bytes}


def probe(path: str | os.PathLike, *, settings: Settings | None = None) -> AudioInfo:
    """读取音频格式与时长（ffprobe）。时长取容器的 format.duration，容错。"""
    p = Path(path)
    if not p.is_file():
        raise PostprocessError("音频文件不存在：%s" % p)
    _, ffprobe = find_ffmpeg(settings)
    run = run_ffmpeg(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_name,sample_rate,channels", "-show_entries", "format=duration",
         "-of", "json", str(p)],
        settings=settings, timeout=60,
    )
    data = json.loads(run.stdout.decode("utf-8", "replace") or "{}")
    streams = data.get("streams") or [{}]
    st = streams[0]
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return AudioInfo(
        path=p, duration_s=duration,
        sample_rate=int(st.get("sample_rate") or 0),
        channels=int(st.get("channels") or 0),
        codec=str(st.get("codec_name") or ""),
        size_bytes=p.stat().st_size,
    )


def duration_s(path: str | os.PathLike, *, settings: Settings | None = None) -> float:
    return probe(path, settings=settings).duration_s


def detect_volume(path: str | os.PathLike, *, settings: Settings | None = None) -> dict:
    """用 volumedetect 量峰值/均值电平 —— 用于「无爆音」验收。

    max_volume 若贴到 0.0 dB 即说明削波；本项目 loudnorm 后应 ≤ -1.0 dB。
    """
    ffmpeg, _ = find_ffmpeg(settings)
    run = run_ffmpeg([ffmpeg, "-hide_banner", "-i", str(path), "-af", "volumedetect",
                      "-f", "null", "-"], settings=settings, timeout=300)
    out: dict = {}
    for key in ("mean_volume", "max_volume"):
        m = re.search(r"%s:\s*(-?[\d.]+) dB" % key, run.stderr)
        if m:
            out[key] = float(m.group(1))
    return out


# --------------------------------------------------------------------------- #
# 基础处理
# --------------------------------------------------------------------------- #

def unify(src: str | os.PathLike, dst: str | os.PathLike, *,
          sample_rate: int = 44100, channels: int = 1,
          settings: Settings | None = None) -> Path:
    """格式统一为 44.1 kHz / 单声道 / 16-bit PCM。**一切拼接动作之前必须调用**（R12）。"""
    ffmpeg, _ = find_ffmpeg(settings)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                "-ar", str(sample_rate), "-ac", str(channels), "-c:a", "pcm_s16le",
                str(dst)], settings=settings)
    return dst


def make_silence(ms: int, dst: str | os.PathLike, *,
                 sample_rate: int = 44100, channels: int = 1,
                 settings: Settings | None = None) -> Path:
    """生成与统一后参数一致的静音段。

    注意：**wav 输出不能用 `-q:a`**（LAME 参数，对 PCM 无效）。
    ```
    ffmpeg -f lavfi -i anullsrc=r=44100:cl=mono -t 0.25 -c:a pcm_s16le silence.wav
    ```
    """
    ffmpeg, _ = find_ffmpeg(settings)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    layout = "mono" if channels == 1 else "stereo"
    run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "anullsrc=r=%d:cl=%s" % (sample_rate, layout),
                "-t", "%.3f" % (ms / 1000.0), "-c:a", "pcm_s16le", str(dst)],
               settings=settings)
    return dst


def apply_fade(src: str | os.PathLike, dst: str | os.PathLike, *,
               fade_in_ms: int = 0, fade_out_ms: int = 0,
               settings: Settings | None = None) -> Path:
    """微淡化，消除拼接处的爆音（计划书 11.2 第 5.2 步）。

    **必须在拼接之前**对素材做；对整轨做会吃掉正文首尾的字。
    淡出起点 = 时长 − 淡出时长，需先探测时长。
    """
    ffmpeg, _ = find_ffmpeg(settings)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    if fade_in_ms > 0:
        filters.append("afade=t=in:st=0:d=%.3f" % (fade_in_ms / 1000.0))
    if fade_out_ms > 0:
        total = duration_s(src, settings=settings)
        start = max(0.0, total - fade_out_ms / 1000.0)
        filters.append("afade=t=out:st=%.3f:d=%.3f" % (start, fade_out_ms / 1000.0))
    if not filters:
        shutil.copy2(src, dst)
        return dst
    run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                "-af", ",".join(filters), "-c:a", "pcm_s16le", str(dst)],
               settings=settings)
    return dst


def _concat_escape(p: Path) -> str:
    """concat demuxer 的行格式：file '<路径>'。

    - 反斜杠统一换成正斜杠（Windows 绝对路径更稳）；
    - 单引号写成 `'\\''`（先闭合、转义、再开启），否则含引号的路径会解析失败。
    """
    s = str(p.resolve()).replace("\\", "/")
    s = s.replace("'", "'\\''")
    return "file '%s'" % s


def concat(items: Sequence[str | os.PathLike], dst: str | os.PathLike, *,
           list_path: str | os.PathLike | None = None,
           settings: Settings | None = None) -> Path:
    """用 concat demuxer 无损拼接（要求所有输入已统一格式）。

    `-safe 0` 必需：否则绝对路径会被安全策略拒绝。
    """
    if not items:
        raise PostprocessError("拼接列表为空")
    ffmpeg, _ = find_ffmpeg(settings)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    lp = Path(list_path) if list_path else dst.with_suffix(".list.txt")
    lines = [_concat_escape(Path(i)) for i in items]
    lp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(lp),
                "-c:a", "pcm_s16le", str(dst)], settings=settings)
    return dst


# --------------------------------------------------------------------------- #
# 响度归一（两遍 loudnorm）
# --------------------------------------------------------------------------- #

def measure_loudness(src: str | os.PathLike, *, settings: Settings | None = None
                     ) -> dict:
    """第一遍：只测量，不写文件。JSON 走 stderr，需从 stderr 解析。"""
    s = settings or get_settings()
    ffmpeg, _ = find_ffmpeg(s)
    af = "loudnorm=I=%s:TP=%s:LRA=%s:print_format=json" % (
        _num(s.audio_loudness_i), _num(s.audio_true_peak), _num(s.audio_lra))
    run = run_ffmpeg([ffmpeg, "-hide_banner", "-i", str(src), "-af", af,
                      "-f", "null", "-"], settings=s, timeout=600)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", run.stderr, re.S)
    if not m:
        raise PostprocessError(
            "loudnorm 第一遍未返回可解析的测量 JSON（FFmpeg 版本需 ≥ 4.x）。\n"
            "  stderr 尾部：%s" % "\n    ".join(run.stderr.strip().splitlines()[-5:]))
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise PostprocessError("loudnorm 测量 JSON 解析失败：%s" % exc) from exc


def _num(v: float) -> str:
    """整数不带小数点，避免 `I=-16.0` 这类无害但难读的串。"""
    return str(int(v)) if float(v) == int(v) else str(v)


# 第一遍测量 JSON 里必须齐备的五个字段（第二遍要回填它们）
LOUDNESS_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")


def _as_float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def loudness_usable(measured: dict) -> tuple[bool, str]:
    """判断第一遍测量结果能否用于第二遍。

    ⚠️ **两条实测失效条件**（`scripts/verify_d5.py` 的 loudness 探针复核）：

    1. **输入短于约 0.4 s**：FFmpeg 的 loudnorm 给不出积分响度，JSON 里
       `input_i` 直接是 `-inf`（实测 0.05~0.35 s 均为 `-inf`，0.40 s 起为正常值）。
       第二遍会硬报 `Value -inf for parameter 'measured_I' out of range [-99 - 0]`。
    2. **纯数字静音**：无论多长，`input_i` 与 `input_tp` 都是 `-inf`（实测 2.0 s 静音）。

    这条是真问题而非测试假象：**极短的成片（例如只有一个短句）会整条链路失败**，
    所以必须降级为「跳过响度归一 + 告警」，绝不能直接抛错。
    """
    for k in LOUDNESS_KEYS:
        if k not in measured:
            return False, "缺少字段 %s" % k
        if _as_float(measured[k]) is None:
            return False, "%s=%r 非有限值" % (k, measured[k])
    i = _as_float(measured["input_i"])
    if i is None or not (-99.0 <= i <= 0.0):
        return False, "input_i=%s 超出 loudnorm 可接受区间 [-99, 0]" % measured["input_i"]
    return True, ""


def normalize_loudness(src: str | os.PathLike, dst: str | os.PathLike, *,
                       measured: dict | None = None,
                       settings: Settings | None = None) -> tuple[Path, dict]:
    """第二遍：把第一遍的实测值动态回填。

    ⚠️ 回填源是 **input_\\*** 字段（对输入信号的测量），不是 output_\\*：
    `measured_I ← input_i`、`measured_TP ← input_tp`、`measured_LRA ← input_lra`、
    `measured_thresh ← input_thresh`、`offset ← target_offset`。
    """
    s = settings or get_settings()
    ffmpeg, _ = find_ffmpeg(s)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    m = measured or measure_loudness(src, settings=s)
    ok, reason = loudness_usable(m)
    if not ok:
        raise PostprocessError(
            "loudnorm 测量结果不可用于第二遍：%s\n"
            "  常见原因：素材短于约 0.4 s，或全片为数字静音 —— 此时 FFmpeg 的 input_i 为 -inf。\n"
            "  调用方应改为「跳过响度归一 + 告警」（postprocess 已如此处理），不要重试。\n"
            "  实际键：%s" % (reason, sorted(m.keys())))
    af = ("loudnorm=I=%s:TP=%s:LRA=%s:measured_I=%s:measured_TP=%s:measured_LRA=%s:"
          "measured_thresh=%s:offset=%s:linear=true:print_format=summary") % (
        _num(s.audio_loudness_i), _num(s.audio_true_peak), _num(s.audio_lra),
        m["input_i"], m["input_tp"], m["input_lra"], m["input_thresh"],
        m["target_offset"])
    run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
                "-af", af, "-c:a", "pcm_s16le", str(dst)], settings=s, timeout=600)
    return dst, m


def export_mp3(wav: str | os.PathLike, dst: str | os.PathLike, *,
               sample_rate: int = 44100, channels: int = 1, bitrate: str = "128k",
               settings: Settings | None = None) -> Path:
    """导出成片 mp3（libmp3lame）。"""
    ffmpeg, _ = find_ffmpeg(settings)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
                "-ar", str(sample_rate), "-ac", str(channels),
                "-c:a", "libmp3lame", "-b:a", str(bitrate), str(dst)],
               settings=settings)
    return dst


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #

@dataclass
class Clip:
    """一段待拼接的正文音频（已按脚本顺序排列）。"""
    wav: Path
    speaker: str = "A"      # A/B，用于选择话轮/句间停顿
    line_seq: int = 0       # 脚本行号，用于判断是否同一行被切句

    @classmethod
    def from_synth(cls, synth_result, wav: str | os.PathLike | None = None) -> "Clip":
        """从 tts.SynthResult 构造（segment 里带 speaker / line_seq）。"""
        seg = getattr(synth_result, "segment", None)
        return cls(wav=Path(wav or synth_result.wav_path),
                   speaker=getattr(seg, "speaker", "A") or "A",
                   line_seq=int(getattr(seg, "line_seq", 0) or 0))


@dataclass
class PostprocessResult:
    mp3: Path
    merged_wav: Path | None
    duration_s: float
    size_bytes: int
    segments: int
    pause_count: int
    intro_used: bool
    outro_used: bool
    loudness_applied: bool
    loudness_measured: dict = field(default_factory=dict)
    loudness_after: dict = field(default_factory=dict)
    level_after: dict = field(default_factory=dict)
    build_dir: Path | None = None
    elapsed_s: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def max_volume_db(self) -> float | None:
        v = self.level_after.get("max_volume")
        return None if v is None else float(v)

    @property
    def clipped(self) -> bool:
        """峰值贴 0 dBFS 即视为削波（Done 要求「无爆音」）。"""
        mv = self.max_volume_db
        return mv is not None and mv >= -0.1

    def to_dict(self) -> dict:
        d = {"mp3": str(self.mp3), "merged_wav": str(self.merged_wav) if self.merged_wav else None,
             "duration_s": round(self.duration_s, 3), "size_bytes": self.size_bytes,
             "segments": self.segments, "pause_count": self.pause_count,
             "intro_used": self.intro_used, "outro_used": self.outro_used,
             "loudness_applied": self.loudness_applied,
             "loudness_measured": self.loudness_measured,
             "loudness_after": self.loudness_after,
             "level_after": self.level_after,
             "max_volume_db": self.max_volume_db, "clipped": self.clipped,
             "build_dir": str(self.build_dir) if self.build_dir else None,
             "elapsed_s": round(self.elapsed_s, 2), "warnings": list(self.warnings)}
        return d


def resolve_asset(value: str | os.PathLike | bool | None,
                  configured: Path | None) -> tuple[Path | None, bool]:
    """解析片头/片尾参数，返回 (路径, 是否显式禁用)。

    三态语义（CLI 与 API 共用，避免「传空串」这种容易被当成漏传的写法）：
    - ``None`` / ``True``  → 用配置里的默认素材（未配置则视为缺失，走告警分支）
    - ``False``            → **显式禁用**，不拼接也不告警（调用方主动要求）
    - 路径                  → 用该路径（不存在时仍走告警分支，便于发现写错的路径）
    """
    if value is False:
        return None, True
    if value is None or value is True:
        return configured, False
    return Path(value), False


def postprocess(clips: Sequence[Clip], *, out_dir: str | os.PathLike,
                name: str = "final", settings: Settings | None = None,
                intro: str | os.PathLike | bool | None = None,
                outro: str | os.PathLike | bool | None = None,
                keep_merged: bool = True,
                on_progress=None) -> PostprocessResult:
    """完整后期链路：统一 → 淡化 → 停顿 → 拼接 → loudnorm → mp3。

    clips 必须已按脚本顺序排列；停顿由相邻 clip 的 speaker 决定
    （换人 = 话轮停顿 500 ms，同人 = 句间停顿 250 ms），与计划书 11.2 第 5.3 步一致。

    ``intro`` / ``outro`` 支持三态（见 `resolve_asset`）：``None`` 用配置默认、
    ``False`` 显式禁用、路径则覆盖。
    """
    t0 = time.time()
    s = settings or get_settings()
    clips = [c if isinstance(c, Clip) else Clip(wav=Path(getattr(c, "wav", c)),
                                                speaker=getattr(c, "speaker", "A"))
             for c in clips]
    if not clips:
        raise PostprocessError("没有可拼接的音频片段（clips 为空）")
    for c in clips:
        if not Path(c.wav).is_file():
            raise PostprocessError("片段文件不存在：%s" % c.wav)

    out_dir = Path(out_dir)
    build = out_dir / ("build_" + name)
    norm_dir = build / "norm"
    norm_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    def progress(msg: str, cur: int = 0, total: int = 0) -> None:
        log.info(msg)
        if on_progress:
            on_progress(msg, cur, total)

    sr, ch = s.audio_sample_rate, s.audio_channels

    # ---- 1. 格式统一（一切拼接之前）----
    progress("[1/6] 格式统一 %d Hz / %d ch / pcm_s16le" % (sr, ch))
    normed: list[Path] = []
    for i, c in enumerate(clips):
        dst = norm_dir / ("seg_%04d.wav" % i)
        unify(c.wav, dst, sample_rate=sr, channels=ch, settings=s)
        normed.append(dst)
        progress("    seg %d/%d -> %s" % (i + 1, len(clips), dst.name), i + 1, len(clips))

    # ---- 2. 片头 / 片尾 ----
    intro_src, intro_off = resolve_asset(intro, s.intro_file)
    outro_src, outro_off = resolve_asset(outro, s.outro_file)
    seq: list[Path] = []

    if intro_off:
        intro_used = False
        progress("    片头已按调用方要求禁用（--no-intro）")
    elif intro_src and intro_src.is_file():
        norm_intro = unify(intro_src, norm_dir / "intro.wav",
                           sample_rate=sr, channels=ch, settings=s)
        seq.append(apply_fade(norm_intro, norm_dir / "intro_faded.wav",
                              fade_out_ms=s.audio_fade_ms, settings=s))
        intro_used = True
    else:
        intro_used = False
        msg = "片头素材缺失，已跳过：%s" % intro_src
        if s.audio_require_intro:
            raise PostprocessError(msg + "（AUDIO_REQUIRE_INTRO=true 时视为错误）")
        warnings.append(msg)
        progress("    [warn] " + msg)

    progress("[2/6] 片头/片尾微淡化 %d ms（必须在拼接之前）" % s.audio_fade_ms)

    # 正文首段淡入（计划书 11.2 第 5.2 步）
    if s.audio_fade_ms > 0:
        normed[0] = apply_fade(normed[0], norm_dir / "seg_0000_faded.wav",
                               fade_in_ms=s.audio_fade_ms, settings=s)

    # ---- 3. 停顿插入 ----
    sil_turn = make_silence(s.audio_pause_turn_ms, build / "silence_turn.wav",
                            sample_rate=sr, channels=ch, settings=s)
    sil_sent = make_silence(s.audio_pause_sentence_ms, build / "silence_sentence.wav",
                            sample_rate=sr, channels=ch, settings=s)
    progress("[3/6] 停顿：话轮 %d ms / 句间 %d ms"
             % (s.audio_pause_turn_ms, s.audio_pause_sentence_ms))

    pause_count = 0
    for i, wav in enumerate(normed):
        seq.append(wav)
        if i == len(normed) - 1:
            break  # 最后一段之后不插停顿（计划书 11.2 示例即如此）
        same = str(clips[i].speaker).upper() == str(clips[i + 1].speaker).upper()
        seq.append(sil_sent if same else sil_turn)
        pause_count += 1

    if outro_off:
        outro_used = False
        progress("    片尾已按调用方要求禁用（--no-outro）")
    elif outro_src and outro_src.is_file():
        norm_outro = unify(outro_src, norm_dir / "outro.wav",
                           sample_rate=sr, channels=ch, settings=s)
        seq.append(apply_fade(norm_outro, norm_dir / "outro_faded.wav",
                              fade_in_ms=s.audio_fade_ms, settings=s))
        outro_used = True
    else:
        outro_used = False
        msg = "片尾素材缺失，已跳过：%s" % outro_src
        warnings.append(msg)
        progress("    [warn] " + msg)

    # ---- 4. 拼接 ----
    progress("[4/6] 拼接 %d 个片段（含 %d 处停顿）" % (len(seq), pause_count))
    merged = concat(seq, build / "merged.wav", list_path=build / "concat_list.txt",
                    settings=s)

    # ---- 5. 响度归一 ----
    if s.audio_normalize:
        progress("[5/6] 两遍 loudnorm -> I=%s LUFS / TP=%s dBTP"
                 % (_num(s.audio_loudness_i), _num(s.audio_true_peak)))
        measured = measure_loudness(merged, settings=s)
        usable, reason = loudness_usable(measured)
        if usable:
            normalized, measured = normalize_loudness(merged, build / "normalized.wav",
                                                      measured=measured, settings=s)
            loudness_applied = True
            # 复核落点（linear 模式允许 ±0.5 LU 浮动）；超差只告警，不阻断交付
            loudness_after = measure_loudness(normalized, settings=s)
            landed = _as_float(loudness_after.get("input_i"))
            progress("    实测 %s -> %s LUFS（目标 %s）"
                     % (measured.get("input_i"),
                        loudness_after.get("input_i"), _num(s.audio_loudness_i)))
            if landed is not None and abs(landed - s.audio_loudness_i) > s.audio_loudness_tolerance:
                warnings.append("响度落点偏差 %.2f LU（目标 %s，实测 %.2f）"
                                % (landed - s.audio_loudness_i, _num(s.audio_loudness_i), landed))
        else:
            # 实测边界：短于约 0.4 s 或纯静音时 input_i = -inf，第二遍必然硬报错 → 降级
            normalized, loudness_applied, loudness_after = merged, False, {}
            msg = "响度归一已跳过：%s（素材短于约 0.4 s 或近似静音时 loudnorm 无法积分）" % reason
            warnings.append(msg)
            progress("    [warn] " + msg)
    else:
        normalized, measured, loudness_applied, loudness_after = merged, {}, False, {}
        warnings.append("已按配置跳过 loudnorm（AUDIO_NORMALIZE=false）")
        progress("[5/6] 跳过 loudnorm（AUDIO_NORMALIZE=false）")

    # ---- 6. 导出 mp3 ----
    progress("[6/6] 导出 mp3（libmp3lame %s）" % s.audio_mp3_bitrate)
    mp3 = export_mp3(normalized, out_dir / (name + ".mp3"), sample_rate=sr,
                     channels=ch, bitrate=s.audio_mp3_bitrate, settings=s)

    info = probe(mp3, settings=s)
    res = PostprocessResult(
        mp3=mp3,
        merged_wav=merged if keep_merged else None,
        duration_s=info.duration_s,
        size_bytes=info.size_bytes,
        segments=len(clips),
        pause_count=pause_count,
        intro_used=intro_used,
        outro_used=outro_used,
        loudness_applied=loudness_applied,
        loudness_measured=measured,
        loudness_after=loudness_after,
        level_after=detect_volume(mp3, settings=s),
        build_dir=build,
        elapsed_s=time.time() - t0,
        warnings=warnings,
    )
    progress("完成：%s（%.1f s，%.2f MB）" % (mp3, res.duration_s, res.size_bytes / 1048576))
    return res


def build_clips_from_results(results: Iterable, *, wav_of=None) -> list[Clip]:
    """把 tts.SynthResult 列表转成有序 Clip（保留 speaker / line_seq）。"""
    return [Clip.from_synth(r, wav=(wav_of(r) if wav_of else None)) for r in results]
