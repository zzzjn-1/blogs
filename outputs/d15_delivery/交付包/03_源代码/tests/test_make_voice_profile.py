# -*- coding: utf-8 -*-
"""音色建档工具 `scripts/make_voice_profile.py` 的离线单元测试。

只覆盖 2026-09-16 修掉的两个真实缺陷，避免回归：
  ① `--dry-run` 被「档案已存在需 --force」的前置校验挡住 → 试算失去意义；
  ② 脚本重跑时又复制一份**同样的新档**进 `_archive`，留下冗余归档目录，
     并在报告里打出「指纹未变」这种看似异常、实为误导的行。

需要 ffmpeg（D5 起为项目硬依赖），无则整模块 skip。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="需要 ffmpeg（D5 起为硬依赖）")


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "make_voice_profile_under_test", ROOT / "scripts" / "make_voice_profile.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mvp = _load_tool()

TEXT = "生活就像海洋只有意志坚强的人才能到达彼岸。"


@pytest.fixture
def src_wav(tmp_path: Path) -> Path:
    """4 s 单声道素材：头尾各 0.5 s 静音 + 中间 3 s 200 Hz 正弦。"""
    p = tmp_path / "src.wav"
    r = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
         "-i", "sine=frequency=200:duration=3:sample_rate=16000",
         "-af", "adelay=500:all=1,apad=pad_dur=0.5,volume=-20dB",
         "-ac", "1", "-c:a", "pcm_s16le", str(p)], capture_output=True)
    assert r.returncode == 0, r.stderr.decode("utf-8", "ignore")[:400]
    return p


def _argv(src: Path, voices: Path, *extra: str) -> list[str]:
    return ["--id", "voice_t", "--wav", str(src), "--text", TEXT,
            "--voices-dir", str(voices), *extra]


def test_dry_run_works_without_force_and_writes_nothing(tmp_path, src_wav):
    """试算不该要求覆盖确认，也不该落任何文件。"""
    voices = tmp_path / "voices"
    voices.mkdir()
    # 先造一个已存在的档案，制造「必须 --force」的前置条件
    (voices / "voice_t.json").write_text("{}", encoding="utf-8")

    rc = mvp.main(_argv(src_wav, voices, "--dry-run"))
    assert rc == 0, "已存在档案时 --dry-run 仍应可用（bug ①）"
    assert not (voices / "voice_t.wav").exists()
    assert (voices / "voice_t.json").read_text(encoding="utf-8") == "{}", "试算不得改写现档"
    assert not (voices / "_archive").exists()


def test_no_trim_is_reported_and_archived_without_duplication(tmp_path, src_wav):
    """不裁静音要如实写进描述；重跑不得产生冗余归档目录。"""
    voices = tmp_path / "voices"
    voices.mkdir()

    # 首次建档：此前无档案 → 不应产生归档
    assert mvp.main(_argv(src_wav, voices, "--no-trim", "--force")) == 0
    assert not (voices / "_archive").exists(), "首次建档没有旧档，不该有归档"

    meta = json.loads((voices / "voice_t.json").read_text(encoding="utf-8"))
    assert "不裁静音" in meta["description"], "描述须与 --no-trim 一致（bug ② 的另一面）"
    assert meta["prompt_text"] == TEXT
    first_bytes = (voices / "voice_t.wav").read_bytes()

    # 重跑同一参数：内容逐字节一致 → 跳过备份，不留冗余归档
    assert mvp.main(_argv(src_wav, voices, "--no-trim", "--force")) == 0
    assert not (voices / "_archive").exists(), "重跑不该产生冗余归档（bug ②）"
    assert (voices / "voice_t.wav").read_bytes() == first_bytes, "内容不变的重复建档应幂等"


def test_changed_content_does_back_up_old_profile(tmp_path, src_wav):
    """内容真的变了才备份——否则「跳过备份」就变成了不备份。"""
    voices = tmp_path / "voices"
    voices.mkdir()
    assert mvp.main(_argv(src_wav, voices, "--no-trim", "--force")) == 0
    old = (voices / "voice_t.wav").read_bytes()

    # 换目标电平 → 音频内容改变
    assert mvp.main(_argv(src_wav, voices, "--no-trim", "--force", "--peak-db", "-6.5")) == 0
    arch = sorted((voices / "_archive").glob("*_voice_t"))
    assert len(arch) == 1, "内容变化时必须留下 1 份旧档"
    assert (arch[0] / "voice_t.wav").read_bytes() == old
