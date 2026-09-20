# -*- coding: utf-8 -*-
"""CosyVoice 单例合成封装 + 音色档案 + 句级缓存。

对应开发计划书 D3「tts.py（低显存优化版：单例 + fp16 + inference_mode + 40 字切句
+ 缓存 + 每句清显存 + onnx 走 CPU）」与 4.6 数据模型中的 `audio_cache`。

显存约束（实测，计划书 V1.4.0）：
    净可用约 4.3 GB；峰值门槛 allocated ≤ 3.6 GB（主）/ reserved ≤ 4.2 GB（辅）。
    三条已废措施：① RTF 不是 2~3；② 音色档案只省约 5%（非最大杠杆）；
    ③ PYTORCH_CUDA_ALLOC_CONF=expandable_segments 在 Windows 无效，禁止启用。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from api.config import Settings, get_settings
from api.services.normalize import (ScriptLine, Segment, build_script_lines,
                                    build_segments, load_polyphone)
from api.services.synth_trace import make_tracer

log = logging.getLogger(__name__)

# CosyVoice3 的 prompt_text 必须带该前缀（见 cosyvoice/llm/llm.py:479 断言）
CV3_PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"
# instruct2 的指令也必须以 <|endofprompt|> 收尾
INSTRUCT_PREFIX = "You are a helpful assistant. "


class TTSError(RuntimeError):
    """合成链路错误（显存不足 / 权重缺失 / 上游异常）。"""


class VoiceNotFound(TTSError):
    pass


class TextFrontendUnavailable(TTSError):
    """文本规范化前端不可用 —— 必须 fail-fast，否则数字会被静默原样朗读。"""


# --------------------------------------------------------------------------- #
# 音色档案
# --------------------------------------------------------------------------- #

@dataclass
class VoiceProfile:
    id: str
    name: str
    wav_path: Path
    prompt_text: str
    gender: str = "unknown"
    style: str = "neutral"
    description: str = ""
    # 参考音频 + 转写文本的内容指纹（[PATCH-CACHE-01]）。
    # 进句级缓存键：替换 voice_x.wav / prompt_text 后缓存自然失效，
    # 避免「换了参考音频却仍命中旧音频」这种静默错误。
    fingerprint: str = ""

    def registered_prompt_text(self, model_version: str) -> str:
        """注册音色档案时用的 prompt_text（随模型版本适配）。"""
        if "cosyvoice3" in model_version.lower() or "fun-cosyvoice3" in model_version.lower():
            return CV3_PROMPT_PREFIX + self.prompt_text
        return self.prompt_text

    def inference_prompt_text(self) -> str:
        """推理时传给 inference_zero_shot 的 prompt_text。

        使用 zero_shot_spk_id 时该参数不参与编码（只用于上游的「合成文本过短」告警），
        因此这里传**纯转写文本**，避免带上 <|endofprompt|> 造成误告警。
        """
        return self.prompt_text

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "gender": self.gender,
                "style": self.style, "description": self.description,
                "prompt_text": self.prompt_text, "wav": self.wav_path.name,
                "fingerprint": self.fingerprint}


def _voice_fingerprint(wav_path: Path, prompt_text: str) -> str:
    """音色档案内容指纹（[PATCH-CACHE-01]）：参考音频字节 + 转写文本。

    只取 12 位十六进制，足以区分不同档案，又便于日志阅读。
    读文件失败时返回空串（不阻断建档，但缓存键会退化为「无指纹」）。
    """
    try:
        h = hashlib.sha1(wav_path.read_bytes() + b"|" + prompt_text.encode("utf-8"))
        return h.hexdigest()[:12]
    except Exception as exc:  # noqa: BLE001
        log.warning("音色档案 %s 指纹计算失败（%s），该音色不参与缓存指纹失效",
                    wav_path.name, exc)
        return ""


class VoiceRegistry:
    """从 backend/voices/ 读取音色档案（voice_x.json + voice_x.wav）。"""

    def __init__(self, voices_dir: Path) -> None:
        self.dir = Path(voices_dir)
        self._voices: dict[str, VoiceProfile] = {}

    def load(self) -> list[VoiceProfile]:
        self._voices.clear()
        if not self.dir.is_dir():
            return []
        for meta_file in sorted(self.dir.glob("*.json")):
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("音色档案解析失败，已跳过 %s: %s", meta_file.name, exc)
                continue
            vid = str(meta.get("id") or meta_file.stem)
            wav = self.dir / str(meta.get("wav") or f"{vid}.wav")
            text = str(meta.get("prompt_text") or "").strip()
            if not wav.is_file():
                log.warning("音色 %s 的 wav 缺失，已跳过：%s", vid, wav)
                continue
            if not text:
                log.warning("音色 %s 缺 prompt_text，已跳过（零样本合成必须提供转写文本）", vid)
                continue
            self._voices[vid] = VoiceProfile(
                id=vid, name=str(meta.get("name") or vid), wav_path=wav,
                prompt_text=text, gender=str(meta.get("gender") or "unknown"),
                style=str(meta.get("style") or "neutral"),
                description=str(meta.get("description") or ""),
                fingerprint=_voice_fingerprint(wav, text),
            )
        return list(self._voices.values())

    def get(self, voice_id: str) -> VoiceProfile:
        if voice_id not in self._voices:
            raise VoiceNotFound(f"音色不存在：{voice_id}；已加载 {sorted(self._voices)}")
        return self._voices[voice_id]

    def list(self) -> list[VoiceProfile]:
        return list(self._voices.values())

    def __contains__(self, voice_id: object) -> bool:
        return voice_id in self._voices


def _resolve_speaker(settings: Settings, registry: "VoiceRegistry", speaker: str) -> str:
    """脚本 speaker（A/B）→ 音色档案 id。

    单音色阶段（未有 voice_b）按 SPEAKER_FALLBACK_ENABLED 回退到 A 并**显式告警**，
    避免「两个说话人听起来一样」这种不报错的功能退化。
    """
    spk = (str(speaker or "A").strip().upper()[:1]) or "A"
    wanted = settings.speaker_voice_map.get(spk, settings.speaker_a_voice)
    if wanted in registry:
        return wanted
    fallback = settings.speaker_a_voice
    if settings.speaker_fallback_enabled and fallback in registry:
        log.warning("说话人 %s 配置的音色 %s 不存在，已回退到 %s（单音色模式）",
                    spk, wanted, fallback)
        return fallback
    available = [v.id for v in registry.list()]
    if len(available) == 1:
        log.warning("说话人 %s 的音色 %s 不存在，回退到唯一可用音色 %s", spk, wanted, available[0])
        return available[0]
    raise VoiceNotFound(f"说话人 {spk} 的音色 {wanted} 不存在；已加载 {sorted(available)}")


# --------------------------------------------------------------------------- #
# 合成结果
# --------------------------------------------------------------------------- #

@dataclass
class SynthResult:
    segment: Segment
    wav_path: Path
    text_hash: str
    duration_ms: int
    cached: bool
    elapsed_s: float
    peak_alloc_mb: float = 0.0

    def to_dict(self) -> dict:
        return {"seq": self.segment.seq, "line_seq": self.segment.line_seq,
                "speaker": self.segment.speaker, "read_text": self.segment.read_text,
                "text_hash": self.text_hash, "wav_path": str(self.wav_path),
                "duration_ms": self.duration_ms, "cached": self.cached,
                "elapsed_s": round(self.elapsed_s, 2)}


# --------------------------------------------------------------------------- #
# 引擎
# --------------------------------------------------------------------------- #

class TTSEngine:
    """CosyVoice 进程内单例。线程不安全，配合单并发任务队列使用（TTS_MAX_WORKERS=1）。"""

    _instance: "TTSEngine | None" = None

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.registry = VoiceRegistry(self.settings.voices_path)
        self.model = None
        self.model_class_name = ""
        self.sample_rate = 24000
        self.text_frontend = ""
        self.load_seconds = 0.0
        self._polyphone: dict[str, str] = {}
        self.memory_trace: list[dict] = []
        self._spk_cache: dict[str, str] = {}
        self._spk_warned: set[str] = set()
        # 逐段阶段计时（默认关；见 synth_trace.py 与 R21）
        self.tracer = make_tracer(self.settings)

    # ---------------- 单例 ----------------
    @classmethod
    def instance(cls, settings: Settings | None = None) -> "TTSEngine":
        if cls._instance is None:
            cls._instance = cls(settings)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """测试用：释放单例（会同时卸载模型）。"""
        if cls._instance is not None:
            cls._instance.unload()
        cls._instance = None

    # ---------------- 加载 ----------------
    def load(self) -> "TTSEngine":
        if self.model is not None:
            return self

        s = self.settings
        if not s.model_dir_path.is_dir():
            raise TTSError(f"模型目录不存在：{s.model_dir_path}")

        # 环境变量必须在 import cosyvoice 之前生效（PATCH-VRAM-01 在导入期读它）
        os.environ["COSYVOICE_ONNX_DEVICE"] = s.cosyvoice_onnx_device
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        # 明确禁用 Windows 下无效的措施，避免误导
        os.environ.pop("PYTORCH_CUDA_ALLOC_CONF", None)

        cosy_root = s.cosyvoice_root_path
        matcha = cosy_root / "third_party" / "Matcha-TTS"
        for p in (str(matcha), str(cosy_root)):   # Matcha 走 sys.path 注入，不 pip install
            if p not in sys.path:
                sys.path.insert(0, p)

        import torch  # noqa: PLC0415
        from cosyvoice.cli.cosyvoice import AutoModel  # noqa: PLC0415

        t0 = time.time()
        self.model = AutoModel(model_dir=str(s.model_dir_path), fp16=s.cosyvoice_fp16)
        self.load_seconds = time.time() - t0
        self.sample_rate = self.model.sample_rate
        self.model_class_name = type(self.model).__name__

        # ---- R14 fail-fast：文本规范化前端不可用时，上游会静默跳过规范化 ----
        self.text_frontend = getattr(self.model.frontend, "text_frontend", "")
        if not self.text_frontend:
            raise TextFrontendUnavailable(
                "CosyVoice 文本规范化前端不可用（frontend.text_frontend == ''）。"
                "上游对 ttsfrd / wetext 导入失败一律 except 兜底，会导致数字被原样朗读而"
                "不报错。排查顺序（日志中搜 [PATCH-TN-01] 可见根因）："
                "① 确认本地 wetext FST 目录存在（zh/en 的 tn tagger 与 verbalizer 各一份），"
                "必要时用环境变量 COSYVOICE_WETEXT_DIR 显式指定；"
                "② 若缓存缺失，执行 python scripts/fetch_wetext.py 抓取"
                "（ModelScope 匿名限流需退避重试）。"
                "注意：PATCH-TN-01 之后稳态启动不再需要联网，若仍为空则大概率是缓存文件缺失，"
                "而非网络问题。"
            )
        log.info("text_frontend=%s | model=%s | 加载 %.1fs",
                 self.text_frontend, self.model_class_name, self.load_seconds)
        if s.tts_convergence_guard:
            self._verify_ratio_matches_upstream()

        # ---- 注册音色档案（一次性，之后每句只算文本 token）----
        self.registry.load()
        for v in self.registry.list():
            self._register_voice(v)
        self._spk_cache.clear()
        log.info("已注册音色档案：%s", [v.id for v in self.registry.list()])
        for spk in sorted(self.settings.speaker_voice_map):
            vid = self.resolve_speaker(spk)
            log.info("说话人 %s → 音色 %s", spk, vid)

        if self._polyphone is None or not self._polyphone:
            self._polyphone = load_polyphone(str(s.path(s.polyphone_dict)))

        if s.tts_warmup:
            self._warmup()
        return self

    def _register_voice(self, voice: VoiceProfile) -> None:
        spk_id = voice.id
        if spk_id in self.model.list_available_spks():
            return
        ok = self.model.add_zero_shot_spk(
            voice.registered_prompt_text(self.settings.model_version),
            str(voice.wav_path), spk_id)
        if ok is not True:
            raise TTSError(f"音色档案注册失败：{spk_id}")

    def _warmup(self) -> None:
        """CUDA 首次调用含内核编译，必须单独计掉，否则首句 RTF 严重失真。"""
        try:
            voices = self.registry.list()
            if not voices:
                return
            t0 = time.time()
            self._infer("这是一次用于预热的合成测试。", voices[0].id, 1.0, "")
            log.info("预热耗时 %.2fs", time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            log.warning("预热失败（不影响后续合成）：%s", exc)

    def unload(self) -> None:
        if self.model is not None:
            del self.model
            self.model = None
        try:
            import torch  # noqa: PLC0415
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    @property
    def loaded(self) -> bool:
        return self.model is not None

    # ---------------- 说话人解析 ----------------
    def resolve_speaker(self, speaker: str) -> str:
        """脚本 speaker（A/B）→ 音色档案 id，结果记忆化（避免每句重复告警）。

        额外对**既非 A/B、也非已注册音色 id** 的输入显式告警（每个取值告警一次）：
        这类输入会被静默当成说话人 A 的音色，属「不声不响的功能退化」——
        同 R14 / 显存告警的取向，宁可吵一点也不要静默。
        """
        raw = str(speaker if speaker is not None else "").strip()

        # [FIX-SPK-ID-01] 已注册音色 id（如 voice_b）直接寻址。
        # 原实现只对 raw 做 `raw.upper()[:1]`，"voice_b" → "V"，既非 A 也非 B，
        # 于是 _resolve_speaker 回退到 speaker_a_voice —— 传音色 id 的调用方
        # （make_intro_outro.py --speaker voice_b、外部 API）会静默拿到**女声**，
        # 且因为 `raw in self.registry` 判断通过而不触发「未知说话人」告警。
        if raw in self.registry:
            self._spk_cache.setdefault(raw, raw)
            return raw

        key = raw.upper()[:1] or "A"
        if (raw.upper() not in ("A", "B") and raw not in self.registry
                and raw not in self._spk_warned):
            self._spk_warned.add(raw)
            log.warning("未知的说话人标识 %r，已按说话人 %s 处理（可用：A/B 或音色 id %s）",
                        speaker, key, sorted(v.id for v in self.registry.list()))
        if key not in self._spk_cache:
            self._spk_cache[key] = _resolve_speaker(self.settings, self.registry, key)
        return self._spk_cache[key]

    # ---------------- 缓存 ----------------
    def cache_key(self, text: str, speaker: str, speed: float, tone: str) -> str:
        """sha1(voice_id|voice_fp|model_ver|seed|text|speed|tone)。

        speaker 先解析为音色档案 id：同一音色下脚本 speaker A/B 的同名句子应共享缓存，
        换音色则自然失效（这正是不把脚本 speaker 直接入键的原因）。

        [PATCH-CACHE-01] 额外纳入**音色档案内容指纹**：原公式只含 voice_id，
        替换 backend/voices/voice_x.wav 或 prompt_text 后键不变，会静默命中旧音频。

        [PATCH-SEED-01] 再纳入**随机种子**：种子直接决定采样噪声，换种子即换音频；
        不入键就会在改 TTS_RANDOM_SEED 后静默复用旧种子的音频。

        [PATCH-CACHE-02] 再纳入**生成长度上限系数** `tts_max_token_text_ratio`：
        未收敛句会被截断在 `text_len × ratio ÷ 帧率`，故该系数直接决定那些音频的内容，
        属输出身份的一部分。实测证据：同一 cache_dir 下把 ratio 20 改为 12，
        12 句全部命中旧缓存（撞顶句本应得到 9.60 s 截断版却复用了 16.00 s 版）。

        注意：以上改动均使**既有缓存键失效**（含落库的 audio_cache.text_hash），
        本项目缓存已按新口径清空重建，无历史包袱。
        """
        voice = self.registry.get(self.resolve_speaker(speaker))
        seed = self.settings.tts_random_seed
        seed_tag = "off" if seed < 0 else str(seed)
        raw = (f"{voice.id}|{voice.fingerprint}|{self.settings.model_version}|{seed_tag}"
               f"|{self.settings.tts_max_token_text_ratio}"
               f"|{text}|{speed:g}|{tone}")
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_paths(self, key: str) -> tuple[Path, Path]:
        d = self.settings.cache_path / key[:2]
        return d / f"{key}.wav", d / f"{key}.json"

    def _read_cache(self, key: str) -> tuple[Path, int] | None:
        wav, meta = self._cache_paths(key)
        if not wav.is_file():
            return None
        duration_ms = 0
        if meta.is_file():
            try:
                duration_ms = int(json.loads(meta.read_text(encoding="utf-8")).get("duration_ms", 0))
            except Exception:  # noqa: BLE001
                duration_ms = 0
        if duration_ms <= 0:
            duration_ms = self._wav_duration_ms(wav)
        return wav, duration_ms

    def _write_cache(self, key: str, wav_path: Path, speaker: str, text: str,
                     duration_ms: int) -> None:
        wav, meta = self._cache_paths(key)
        if wav.resolve() != Path(wav_path).resolve():
            wav.parent.mkdir(parents=True, exist_ok=True)
            import shutil  # noqa: PLC0415
            shutil.copy2(wav_path, wav)
        meta.write_text(json.dumps({
            "text_hash": key, "speaker": speaker, "read_text": text,
            "duration_ms": duration_ms, "sample_rate": self.sample_rate,
            "model_version": self.settings.model_version,
            "voice_fingerprint": self.registry.get(self.resolve_speaker(speaker)).fingerprint,
            "random_seed": self.settings.tts_random_seed,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _wav_duration_ms(path: Path) -> int:
        try:
            import soundfile as sf  # noqa: PLC0415
            info = sf.info(str(path))
            return int(round(info.frames / info.samplerate * 1000))
        except Exception:  # noqa: BLE001
            return 0

    # ---------------- 显存 ----------------
    def vram(self) -> dict:
        try:
            import torch  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return {}
        if not torch.cuda.is_available():
            return {"cuda": False}
        free, total = torch.cuda.mem_get_info()
        return {
            "cuda": True,
            "allocated_mb": round(torch.cuda.memory_allocated() / 1048576, 1),
            "reserved_mb": round(torch.cuda.memory_reserved() / 1048576, 1),
            "peak_alloc_mb": round(torch.cuda.max_memory_allocated() / 1048576, 1),
            "peak_reserved_mb": round(torch.cuda.max_memory_reserved() / 1048576, 1),
            "system_used_mb": round((total - free) / 1048576, 1),
            "system_total_mb": round(total / 1048576, 1),
        }

    def check_budget(self) -> tuple[bool, str]:
        """双口径显存检查（allocated 主 / reserved 辅）。"""
        v = self.vram()
        if not v or not v.get("cuda"):
            return True, "CUDA 不可用，跳过显存门槛检查"
        sa = self.settings
        ok_a = v["peak_alloc_mb"] <= sa.tts_vram_budget_alloc_mb
        ok_r = v["peak_reserved_mb"] <= sa.tts_vram_budget_reserved_mb
        msg = (f"peak_allocated={v['peak_alloc_mb']} MB（≤{sa.tts_vram_budget_alloc_mb} "
               f"{'达标' if ok_a else '超限'}） / peak_reserved={v['peak_reserved_mb']} MB "
               f"（≤{sa.tts_vram_budget_reserved_mb} {'达标' if ok_r else '超限'}）")
        return (ok_a and ok_r), msg

    # ---------------- 推理 ----------------
    def _apply_seed(self, attempt: int = 0) -> int | None:
        """固定随机种子（P2 决策①），返回实际使用的种子；关闭时返回 None。

        三条不可省的约束：

        1. **必须逐句重设，不能进程内只设一次**。采样会消耗全局 RNG，若只在启动时设一次，
           第 N 句的结果就依赖前 N-1 句被合成过什么 —— 预热与否、句级缓存命中与否都会
           改变输出，缓存语义立刻不自洽。
        2. **守卫重试必须偏移种子**（`attempt`）。固定种子下原地重试必然复现同一残句，
           重试退化成空转；这也是把 attempt 一路透传进 `_infer` 的原因。
        3. 种子只影响**采样噪声**，不影响分词与切句，故 `cache_key` 无需纳入；
           种子固定后「同 key → 同音频」才真正成立。
        """
        base = self.settings.tts_random_seed
        if base < 0:
            return None
        seed = base + attempt * self.settings.tts_seed_retry_stride
        try:
            from cosyvoice.utils.common import set_all_random_seed  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            log.warning("固定随机种子失败（%s），本次合成为非确定性结果", exc)
            return None
        set_all_random_seed(seed)
        return seed

    def _infer(self, text: str, speaker: str, speed: float, tone: str,
               text_frontend: bool = True, seed_offset: int = 0) -> tuple[object, float]:
        """底层推理，返回 (speech tensor, 耗时秒)。

        `seed_offset` 供收敛守卫重试时偏移随机种子；默认 0 即使用基准种子。
        """
        import torch  # noqa: PLC0415
        voice = self.registry.get(self.resolve_speaker(speaker))
        self._apply_seed(seed_offset)
        t0 = time.time()
        speech = None
        with torch.inference_mode():
            if tone:
                instruct = tone if "<|endofprompt|>" in tone else (
                    INSTRUCT_PREFIX + tone.rstrip("。.") + "。<|endofprompt|>")
                gen = self.model.inference_instruct2(
                    text, instruct, str(voice.wav_path), zero_shot_spk_id=voice.id,
                    stream=False, speed=speed, text_frontend=text_frontend)
            else:
                gen = self.model.inference_zero_shot(
                    text, voice.inference_prompt_text(), str(voice.wav_path),
                    zero_shot_spk_id=voice.id, stream=False, speed=speed,
                    text_frontend=text_frontend)
            for out in gen:
                speech = out["tts_speech"]
        if speech is None:
            raise TTSError(f"推理未产出音频：text={text[:30]!r} speaker={speaker}")
        return speech, time.time() - t0

    # ---------------- 生成收敛守卫（[PATCH-GUARD-01]） ----------------
    def _verify_ratio_matches_upstream(self) -> None:
        """[PATCH-GUARD-02] 校验守卫用的时长上限与上游真实生效值一致。

        背景（实测，见 D4 前置检查报告 V1.2.0 第 6.8 节）：
        `tts_max_token_text_ratio` **只被 `_cap_seconds()` 读取用于算阈值**，
        上游 `Qwen2LM.inference(max_token_text_ratio: float = 20)` 是**硬编码默认**，
        而 `AutoModel.inference_zero_shot` 的签名里根本没有该参数、也不向下透传。
        实测现场：把配置从 20 改成 12 后，日志打出「上限 9.60s」而实际输出**仍是 16.00s**
        —— 守卫阈值与真实上限脱节，会把合法的长句误判为未收敛而白白重试。

        因此这里 fail-fast：配置值必须等于上游默认值。要真正下调上限，
        必须先完成 cli → model → llm 三层透传（属 P2 决策②，尚未实施）。
        """
        llm = getattr(getattr(self.model, "model", None), "llm", None)
        if llm is None:
            log.debug("[PATCH-GUARD-02] 取不到 llm 实例，跳过上限一致性校验")
            return
        try:
            import inspect  # noqa: PLC0415
            param = inspect.signature(type(llm).inference).parameters.get(
                "max_token_text_ratio")
            upstream = float(param.default) if param is not None else None
        except Exception as exc:  # noqa: BLE001
            log.warning("[PATCH-GUARD-02] 无法读取上游 max_token_text_ratio（%s），跳过校验", exc)
            return
        if upstream is None:
            log.warning("[PATCH-GUARD-02] 上游 %s.inference 无 max_token_text_ratio 参数，"
                        "守卫阈值无法保证与实际一致", type(llm).__name__)
            return
        configured = float(self.settings.tts_max_token_text_ratio)
        if configured != upstream:
            raise TTSError(
                f"[PATCH-GUARD-02] tts_max_token_text_ratio={configured:g} 与上游实际生效值 "
                f"{upstream:g} 不一致。该配置目前**不控制**生成长度上限，只影响守卫阈值；"
                f"两者不一致会让守卫把合法长句误判为未收敛。要真正下调上限，必须在 "
                f"CosyVoice 的 cli → model → llm 三层透传该参数（P2 决策②）。"
                f"请把配置改回 {upstream:g}，或先完成透传补丁。"
            )
        log.info("[PATCH-GUARD-02] 上限一致性校验通过：ratio=%g（上游 %s.inference 默认值），"
                 "20 字句上限 %.2f s", configured, type(llm).__name__,
                 20 * configured / self.settings.tts_token_frame_rate)

    def _cap_seconds(self, text: str) -> float | None:
        """按 CosyVoice 的生成长度上限反推本句的时长天花板（秒）。

        上游 `Qwen2LM.inference_wrapper` 为 `for i in range(max_len)`，只有产出
        stop token 才提前 break；未收敛时吐满 `max_len` 个 token，故
        `时长上限 = text_len * max_token_text_ratio / token 帧率`。
        推不出来（前端或分词器不可用）时返回 None，调用方跳过守卫。
        """
        frontend = getattr(self.model, "frontend", None)
        if frontend is None:
            return None
        try:
            normalized = frontend.text_normalize(text, split=False, text_frontend=True)
            _, text_len = frontend._extract_text_token(normalized)  # noqa: SLF001
            return (int(text_len) * self.settings.tts_max_token_text_ratio
                    / self.settings.tts_token_frame_rate)
        except Exception as exc:  # noqa: BLE001
            log.debug("无法推算生成长度上限（%s），本句跳过收敛守卫", exc)
            return None

    # ---------------- CUDA OOM 重试（D12 稳定性加固）----------------
    @staticmethod
    def _is_cuda_oom(exc: BaseException) -> bool:
        """识别 CUDA 显存不足。

        `torch.cuda.OutOfMemoryError` 是 1.13+ 的专用异常类，但**不能只认它**：
        旧版本、以及被上游包一层的调用点都可能只抛
        `RuntimeError("CUDA out of memory. Tried to allocate ...")`。
        两条判据都过一遍，宁可多认一点（重试一次的代价远小于漏判）。
        """
        try:
            import torch  # noqa: PLC0415
            oom_cls = getattr(torch.cuda, "OutOfMemoryError", None)
            if oom_cls is not None and isinstance(exc, oom_cls):
                return True
        except Exception:  # noqa: BLE001 —— torch 不可用就退到文本判据
            pass
        return "out of memory" in str(exc).lower()

    def _infer_with_oom_retry(self, text: str, speaker: str, speed: float,
                              tone: str) -> tuple[object, float]:
        """单句推理，遇 CUDA OOM 则清一次显存缓存后重试（上限 `tts_oom_retry`）。

        为什么这个重试是**划算**的：本项目 OOM 多数不是真容量不足，而是
        「上一句留下的 reserved 空闲块 + 本句峰值」叠加 —— `empty_cache()`
        把空闲块还给驱动后通常就能过。重试耗尽才判定真不足，并抛出带
        处置建议的 `TTSError`（不抛裸 RuntimeError，省得用户只看到 traceback）。

        注意：重试**不偏移随机种子** —— 与收敛守卫不同，OOM 是资源问题而非
        采样问题，换种子既无意义还会凭空改变输出。
        """
        max_retry = max(0, int(getattr(self.settings, "tts_oom_retry", 0)))
        for attempt in range(max_retry + 1):
            try:
                return self._infer(text, speaker, speed, tone)
            except Exception as exc:  # noqa: BLE001
                if not self._is_cuda_oom(exc):
                    raise
                if attempt >= max_retry:
                    raise TTSError(
                        f"显存不足：句子「{text[:24]}…」在清空显存缓存后仍 OOM"
                        f"（已重试 {max_retry} 次）。"
                        f"预算 allocated≤{self.settings.tts_vram_budget_alloc_mb}MB / "
                        f"reserved≤{self.settings.tts_vram_budget_reserved_mb}MB。"
                        f"可尝试：①关掉其他占用 GPU 的进程；"
                        f"②调小 MAX_CHARS_PER_SEG（段越短峰值越低）；"
                        f"③调大 TTS_OOM_RETRY。原始错误：{exc}") from exc
                log.warning("[FIX-OOM-RETRY-01] CUDA OOM，清显存缓存后重试"
                            "（%d/%d）：%s", attempt + 1, max_retry, exc)
                try:
                    import torch  # noqa: PLC0415
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001 —— 清缓存失败不该顶掉重试
                    log.debug("empty_cache 失败，忽略", exc_info=True)
        raise AssertionError("unreachable")  # pragma: no cover

    def _guard_convergence(self, text: str, speaker: str, speed: float, tone: str,
                           speech: object, elapsed: float) -> tuple[object, float]:
        """未收敛则重试；重试后仍不收敛，取最短的一次并**显式告警**（不静默交付残句）。"""
        cap = self._cap_seconds(text)
        threshold = cap * self.settings.tts_guard_threshold if cap else None
        dur = speech.shape[1] / self.sample_rate
        if threshold is None or dur < threshold:
            return speech, elapsed

        best, best_dur, total = speech, dur, elapsed
        for attempt in range(1, self.settings.tts_guard_retry + 1):
            log.warning("[PATCH-GUARD-01] 生成未收敛：%d 字文本输出 %.2fs（上限 %.2fs），"
                        "第 %d/%d 次重试", len(text), best_dur, cap, attempt,
                        self.settings.tts_guard_retry)
            # 重试必须偏移随机种子：固定种子下原地重试会复现同一残句（P2 决策①）
            retry_speech, retry_elapsed = self._infer(text, speaker, speed, tone,
                                                      seed_offset=attempt)
            retry_dur = retry_speech.shape[1] / self.sample_rate
            total += retry_elapsed
            if retry_dur < best_dur:
                best, best_dur = retry_speech, retry_dur
            if retry_dur < threshold:
                log.info("[PATCH-GUARD-01] 第 %d 次重试后收敛：%.2fs", attempt, retry_dur)
                return retry_speech, total
        log.error("[PATCH-GUARD-01] %d 次重试后仍未收敛（最短 %.2fs / 上限 %.2fs）。"
                  "已取最短结果，该句音频可能被长度上限截断，请人工复核。",
                  self.settings.tts_guard_retry, best_dur, cap)
        return best, total

    def synthesize(self, text: str, speaker: str = "voice_a", *,
                   speed: float = 1.0, tone: str = "",
                   text_hash: str | None = None) -> SynthResult:
        """合成单句（≤ max_chars_per_seg），命中缓存则直接复用。"""
        import torch  # noqa: PLC0415
        import torchaudio  # noqa: PLC0415

        self.tracer.mark("s0_enter")
        self.load()
        self.tracer.mark("s1_load")
        text = (text or "").strip()
        if not text:
            raise TTSError("待合成文本为空")
        if len(text) > self.settings.max_chars_per_seg:
            raise TTSError(f"单句超长（{len(text)} > {self.settings.max_chars_per_seg}），"
                           f"请先经 normalize.split_sentences 切分")

        key = text_hash or self.cache_key(text, speaker, speed, tone)
        seg = Segment(seq=0, line_seq=0, speaker=speaker, text=text,
                      read_text=text, char_count=len(text))

        hit = self._read_cache(key)
        self.tracer.mark("s2_cache_lookup")
        if hit:
            wav_path, duration_ms = hit
            return SynthResult(segment=seg, wav_path=wav_path, text_hash=key,
                               duration_ms=duration_ms, cached=True, elapsed_s=0.0)

        self.settings.cache_path.mkdir(parents=True, exist_ok=True)
        tmp = self.settings.cache_path / key[:2] / f"{key}.wav"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        self.tracer.mark("s3_tmpdir")

        if self.settings.tts_empty_cache_each_seg and torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.tracer.mark("s4_empty_cache_pre")

        speech, elapsed = self._infer_with_oom_retry(text, speaker, speed, tone)
        self.tracer.mark("s5_infer")
        if self.settings.tts_convergence_guard:
            speech, elapsed = self._guard_convergence(text, speaker, speed, tone,
                                                      speech, elapsed)
        self.tracer.mark("s6_guard")
        torchaudio.save(str(tmp), speech, self.sample_rate)
        self.tracer.mark("s7_save_wav")
        duration_ms = int(round(speech.shape[1] / self.sample_rate * 1000))
        self._write_cache(key, tmp, speaker, text, duration_ms)
        self.tracer.mark("s8_write_cache")

        v = self.vram()
        self.tracer.mark("s9_vram")
        if self.settings.tts_empty_cache_each_seg and torch.cuda.is_available():
            torch.cuda.empty_cache()
        self.tracer.mark("s10_empty_cache_post")
        self.memory_trace.append({
            "seq": len(self.memory_trace) + 1, "allocated_mb": v.get("allocated_mb"),
            "reserved_mb": v.get("reserved_mb"), "peak_alloc_mb": v.get("peak_alloc_mb"),
            "elapsed_s": round(elapsed, 2), "chars": len(text),
        })
        return SynthResult(segment=seg, wav_path=tmp, text_hash=key,
                           duration_ms=duration_ms, cached=False, elapsed_s=elapsed,
                           peak_alloc_mb=v.get("peak_alloc_mb", 0.0))

    # ---------------- 批量 ----------------
    def synthesize_lines(self, lines: Sequence[ScriptLine], *,
                         out_dir: Path, speed: float = 1.0, tone: str = "",
                         on_progress: Callable[[int, int, SynthResult], None] | None = None,
                         ) -> list[SynthResult]:
        """把脚本行切句后逐句合成，落盘到 out_dir（按 seq 命名，便于拼接与试听）。"""
        # [FIX-COLD-CACHE-01] 必须在循环之前 load()，否则**冷引擎**调用必然抛
        # VoiceNotFound("…已加载 []")：
        #   循环里先调 cache_key()，再调 synthesize()；而 load() 只藏在 synthesize() 内部。
        #   cache_key() → resolve_speaker() → registry.get()，需要一个**已注册**的音色档案，
        #   于是第一轮就在 load() 之前撞上空注册表。
        # verify_d3.py 之所以没暴露：它自己先显式 load() 了一次，把坑盖住了。
        self.load()
        segs = build_segments(list(lines), max_chars=self.settings.max_chars_per_seg)
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        results: list[SynthResult] = []
        total = len(segs)
        for i, seg in enumerate(segs, 1):
            # 逐段阶段计时（默认关）。t0 = 本段循环体开始，t1 = 回调结束，
            # 于是「上一段 t1 → 本段 t0」的残余就是未被标记的循环/调度开销。
            self.tracer.begin(seg=i, speaker=seg.speaker, chars=len(seg.read_text),
                              text=seg.read_text[:40])
            ok = True
            try:
                key = self.cache_key(seg.read_text, seg.speaker, speed, tone)
                self.tracer.mark("l1_cache_key")
                r = self.synthesize(seg.read_text, seg.speaker, speed=speed, tone=tone,
                                    text_hash=key)
                # 落一份人类可读的命名副本（缓存目录保持内容寻址，不改名）
                named = out_dir / f"{seg.seq:04d}_{seg.speaker}_{key[:8]}.wav"
                if r.wav_path.resolve() != named.resolve() and not named.is_file():
                    import shutil  # noqa: PLC0415
                    shutil.copy2(r.wav_path, named)
                self.tracer.mark("l2_named_copy")
                r.segment = seg
                results.append(r)
                if on_progress:
                    on_progress(i, total, r)
                self.tracer.mark("l3_progress_cb")
            except BaseException:
                ok = False
                raise
            finally:
                self.tracer.end(audio_ms=r.duration_ms if ok and results else None,
                                cached=bool(results[-1].cached) if ok and results else None,
                                ok=ok)
        return results

    # ---------------- 便捷入口 ----------------
    def synthesize_turns(self, turns: Iterable[dict], *, out_dir: Path,
                         speed: float = 1.0, tone: str = "",
                         on_progress: Callable[[int, int, SynthResult], None] | None = None,
                         ) -> list[SynthResult]:
        """从 [{speaker, text}] 直接合成（含规范化与切句）。"""
        lines = build_script_lines(list(turns), max_chars=self.settings.max_chars_per_seg,
                                   polyphone=self._polyphone)
        return self.synthesize_lines(lines, out_dir=out_dir, speed=speed, tone=tone,
                                     on_progress=on_progress)
