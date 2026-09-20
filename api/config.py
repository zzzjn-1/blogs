# -*- coding: utf-8 -*-
"""应用配置：全部从环境变量 / .env 读取，禁止硬编码密钥（见计划书 4.8、11.3）。"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = PROJECT_ROOT / ".env"

#: JWT_SECRET 的占位嫌疑词（小写匹配）。宁可误报也别漏报 —— 漏报的代价是登录态可被伪造。
_JWT_PLACEHOLDER_HINTS = (
    "change", "placeholder", "your", "example", "todo", "replace",
    "secret", "dev-only", "test", "xxx",
)


class Settings(BaseSettings):
    """Pydantic 强类型配置。字段名小写，环境变量名自动大写匹配。"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE), env_file_encoding="utf-8",
        extra="ignore", case_sensitive=False,
    )

    # ---------------- 服务 ----------------
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    public_base_url: str = "http://127.0.0.1:8000"

    # ---------------- 目录 ----------------
    data_dir: str = "./data"
    work_dir: str = "./data/work"
    cache_dir: str = "./data/cache"

    # ---------------- 文本与切句 ----------------
    max_chars_per_seg: int = 40
    polyphone_dict: str = "backend/dicts/polyphone.json"

    # ---------------- CosyVoice ----------------
    tts_model: str = "cosyvoice2"          # cosyvoice2 / cosyvoice3 / auto
    cosyvoice_root: str = "CosyVoice"
    cosyvoice_model_dir: str = "pretrained_models/CosyVoice2-0.5B"
    cosyvoice_fp16: bool = True
    tts_device: str = "cuda:0"
    cosyvoice_onnx_device: str = "cpu"     # onnx 组件走 CPU，省 0.5~1.0 GB（PATCH-VRAM-01）
    tts_warmup: bool = True
    tts_empty_cache_each_seg: bool = True
    tts_max_workers: int = 1

    # ---------------- 逐段合成计时插桩（性能诊断用，默认关）----------------
    # 开启后每合成一段就往 jsonl 追加一条阶段时间线，用于定位 R21（段间固定开销）。
    # **必须默认关**：开启会带来逐段文件 IO，绝不允许污染生产批次的耗时口径；
    # 压测报告里的数字一律来自关闭状态。实现见 api/services/synth_trace.py。
    synth_trace: bool = False
    synth_trace_path: str = "outputs/synth_trace/synth_trace.jsonl"

    # ---------------- 生成收敛守卫（PATCH-GUARD-01）----------------
    # CosyVoice 的 Qwen2LM.inference_wrapper 为 `for i in range(max_len)`，只在产出
    # stop token 时 break；未收敛时会一路解码到 max_len 个 token，输出是被截断的残句，
    # 且峰值显存随序列长度单调抬升（实测 16.00 s 样本把 reserved 顶到 4318 MB）。
    # 其中 max_len = int(text_len * tts_max_token_text_ratio)，时长 = max_len / token 帧率；
    # 20 字中文句 text_len=20 -> max_len=400 -> 400/25 = 16.00 s，与实测完全吻合。
    tts_convergence_guard: bool = True
    tts_token_frame_rate: int = 25              # CosyVoice2 语音 token 帧率（Hz）
    tts_max_token_text_ratio: int = 20          # 与 CosyVoice Qwen2LM.inference 默认值保持一致
    tts_guard_threshold: float = 0.97           # 输出时长 ≥ 上限×该系数即判为未收敛
    tts_guard_retry: int = 2                    # 判为未收敛后的重试次数

    # ---------------- 随机种子固定（P2 决策①）----------------
    # 推理链路上有三处随机源：① LLM 的 top-k/top-p 多项式采样
    # （cosyvoice/utils/common.py: sampling_ids_logits_process → prob.multinomial）；
    # ② flow matching 的先验噪声（flow_matching.py: torch.randn_like(mu)）；
    # ③ HiFi-GAN 声码器噪声（hifigan/generator.py: torch.randn_like(sine_waves)）。
    # 三者都走 torch 全局 RNG，故 set_all_random_seed 一处即可覆盖。
    # 固定后：同一 (文本, 音色, 语速, 语气) → 同一音频，句级缓存语义自洽；
    # 代价是已按旧（随机）口径落盘的缓存失去可比性，需按新口径重测。
    tts_random_seed: int = 42                   # 固定种子；填负数表示关闭，回到非确定性采样
    tts_seed_retry_stride: int = 1              # 收敛守卫重试时种子的偏移步长（见 tts.py 说明）

    # ---------------- 显存门槛（V1.4.0 双口径）----------------
    # 净可用约 4.3 GB；allocated 为主口径，reserved 为辅口径
    tts_vram_budget_alloc_mb: int = 3600
    tts_vram_budget_reserved_mb: int = 4200

    # ---------------- CUDA OOM 重试（D12 稳定性加固）----------------
    # 单句推理抛 CUDA OOM 时：清一次显存缓存，然后重试同一句，最多这么多次。
    # 依据：OOM 在本项目多由「残留 reserved 块 + 本句峰值」叠加触发，
    # `torch.cuda.empty_cache()` 把空闲 reserved 还给驱动后常见可恢复；
    # 若重试后仍 OOM，说明是真容量不足，直接失败并给出可读建议，不做无谓循环。
    # 默认 1 次：收益主要来自第一次重试，再多只是拉长失败路径。
    tts_oom_retry: int = 1

    # ---------------- 音色与说话人映射 ----------------
    voices_dir: str = "backend/voices"
    # 脚本里的 speaker 是 A/B，音色档案是 voice_a/voice_b，两者必须显式映射
    speaker_a_voice: str = "voice_a"
    speaker_b_voice: str = "voice_b"
    # 本期先用单音色跑通：voice_b 缺失时回退 voice_a（启动时告警，不静默）
    speaker_fallback_enabled: bool = True

    # ---------------- 音频后期（D5）----------------
    # 统一目标格式：44.1 kHz / 单声道 / 16-bit PCM。
    # ⚠️ 必须**先统一再拼接**：CosyVoice2 输出 24 kHz、CosyVoice1 输出 16 kHz、
    # 片头素材常为 44.1 kHz 立体声；参数不一致时 concat 会报错或产出错误时长（计划书 R12）。
    audio_sample_rate: int = 44100
    audio_channels: int = 1
    audio_loudness_i: float = -16.0          # 播客标准响度（LUFS）
    audio_true_peak: float = -1.5            # 真峰上限（dBTP），留余量防转码削波
    audio_lra: float = 11.0                  # 响度范围
    audio_pause_turn_ms: int = 500           # 话轮停顿（换人时插入）
    audio_pause_sentence_ms: int = 250       # 句间停顿（同一人连续句之间）
    audio_fade_ms: int = 30                  # 片头/片尾微淡化，消除拼接爆音
    audio_mp3_bitrate: str = "128k"
    # 关掉可省掉一次全片扫描（R13 的优化项之一）；关闭时仅记告警不报错
    audio_normalize: bool = True
    # 第二遍 loudnorm 后实测响度与目标值的允差（LUFS），超出即告警
    audio_loudness_tolerance: float = 1.0
    # 片头/片尾素材（计划书 1.4 P-5 前置项）。缺失时默认跳过并告警，不静默失败。
    intro_path: str = "backend/assets/intro.mp3"
    outro_path: str = "backend/assets/outro.mp3"
    audio_require_intro: bool = False
    # 动态片头：文案模板（含日期/主题占位符）。
    # 非空 ⇒ 每期按当期信息用 TTS 现合成片头，**优先于** intro_path 固定素材；
    # 置空 ⇒ 回退到 intro_path 固定 mp3（适合片头与当期内容无关的场景）。
    intro_template: str = "大家好，今天是（日期），今天的主题是（主题）。"
    # 动态片头用的音色 / 语气 / 语速（片头固定女声，与正片说话人无关）
    intro_speaker: str = "voice_a"
    intro_tone: str = (
        "用自然放松、亲切温和的语气说这句话，"
        "像和朋友聊天一样，语调轻快上扬但不夸张"
    )
    intro_speed: float = 1.05      # 与片尾同档（用户 2026-09-16 拍板的手感）

    # ---------------- FFmpeg 定位 ----------------
    # 留空则自动发现：PATH → winget 安装目录 → 常见安装路径。
    # 部署手册要求 FFmpeg ≥ 9.0 且 loudnorm / libmp3lame 可用（计划书 1.4 P-8）。
    ffmpeg_bin: str = ""
    ffprobe_bin: str = ""
    ffmpeg_timeout: int = 600                 # 单次 FFmpeg 调用的墙钟上限（秒）

    # ---------------- 大模型（DeepSeek，OpenAI 兼容协议；见计划书 4.10）----------------
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    # 备选模型：仅当主题涉及复杂因果/多观点论证且主模型连续 2 次可用性不达标时手动切换。
    # 注意：deepseek-reasoner 在本 Key 的网关上被静默映射为 deepseek-flash（实测），
    # 故此处填 /models 明确列出的 deepseek-v4-pro，规避映射歧义。
    llm_reasoner_model: str = "deepseek-v4-pro"
    llm_timeout: int = 60
    llm_timeout_long: int = 120                # 长脚本用（计划书 4.10 ⑤）
    llm_max_retry: int = 2                     # tenacity 重试次数（总尝试 = 次数 + 1）
    llm_temperature: float = 0.8               # 对话脚本需要一定表现力
    llm_max_tokens: int = 4096

    # ---------------- 脚本生成（D4）----------------
    # 约束值（每行字数上限 / 交替规则）同时写在系统提示词里，启动期做一致性自检
    # （verify_prompt_consistency），防止「提示词声明 40 字、校验器却按别的阈值判」
    # 这类阈值脱节 —— 与 [PATCH-GUARD-02] 同一类缺陷。
    prompts_dir: str = "backend/prompts"
    sensitive_dict: str = "backend/dicts/sensitive_words.txt"
    words_per_minute: int = 260                # 计划书 4.6 tasks.target_word_count 折算口径
    script_max_chars_per_line: int = 40        # 与 TTS 单句上限一致（计划书 4.4 S3）
    script_word_tolerance: float = 0.10        # 总字数允许偏离配额的比例
    # 总字数配额是否作为**硬性失败判据**。
    # 默认 false（V1.0.0 定稿，依据 D4 三轮实测）：模型每行长度稳定在 12~21 字符且不随
    # 提示词变化，总字数系统性偏低（三轮标定各 10/10 低于配额），把它当硬判据会让
    # 「可用率」恒为 0~70%。改为「驱动重试但不判可用性」，并把达标率单独上报
    # （ScriptGenResult.word_quota_ok / word_deviation_pct）。
    # 需要按字数卡验收时置 true；长期方案是补一轮「扩写」或分段生成（见 D4 报告遗留项）。
    script_word_quota_enforce: bool = False
    script_require_alternating: bool = True    # 强制 A/B 交替（双人对话听感前提）
    script_correction_retry: int = 2           # 结构校验失败后的纠错重试次数（计划书 4.10 ③）
    script_self_check: bool = True             # 生成后追加一次 LLM 合规自检（计划书 R9）

    # ---------------- 数据库（D6 服务化）----------------
    # SQLite 单文件库；连接串由 db_url 属性派生，勿在此硬编码。
    db_path: str = "./data/podcast.db"
    # 成片目录：data/audio/{task_id}/final.mp3（计划书 4.8 目录约定）。
    # 与 work_dir 分开的原因：work 是中间产物、终态即删；audio 是对外访问的成片，按保留期管理。
    audio_dir: str = "./data/audio"
    # RSS 订阅 XML 落盘目录（计划书 4.8 未显式列出，见 D6 报告「目录补充」）
    podcast_dir: str = "./data/podcast"

    # ---------------- 崩溃恢复（D12 稳定性加固）----------------
    # 进程被杀 / 断电后，库里会留下 SCRIPTING / SYNTHESIZING / POSTPROCESSING /
    # PACKAGING 这些**非终态**任务。不处理的话它们会永远停在「合成中」：
    # 前端进度条假死、单并发队列也永远等不到它结束。
    #
    # 两种口径：
    #   startup_recover     —— 启动时把它们标成 FAILED（附可读原因），保证没有僵尸状态；
    #   startup_auto_resume —— 在 FAILED 之上再自动重新投递合成流水线，真正「续跑」。
    #
    # 之所以敢自动续跑：合成阶段是**幂等**的 —— 已合成过的句子按 text_hash 命中
    # `audio_cache`，重放只补未命中的部分（见 task_runner 模块 docstring「断点续跑」）。
    # 关掉 auto_resume 就退化成「标记失败 + 由用户点重试」。
    startup_recover: bool = True
    startup_auto_resume: bool = True

    # ---------------- 鉴权（JWT + HttpOnly Cookie）----------------
    # 计划书 4.7.1：<audio>/<img>/<a download> 不会带 Authorization 头，
    # 故登录同时把 JWT 写进 HttpOnly Cookie，媒体接口从 Cookie 取凭据。
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    auth_cookie_name: str = "podcast_token"
    auth_cookie_path: str = "/"
    # 生产（HTTPS）必须置 true；本地 http 联调置 false，否则浏览器不回传 Cookie。
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"

    # ---------------- RSS 公开路径 token ----------------
    # enclosure 路径不可猜测（计划书 4.7.1）；长度即熵，勿调小。
    feed_token_bytes: int = 16
    feed_default_language: str = "zh-cn"
    feed_default_category: str = "Technology"
    feed_episode_limit: int = 100
    # 默认播客封面。支持三种写法：
    #   1) 空串：不设置默认封面（由 /api/feeds/me 手动上传/填写）
    #   2) 相对项目根目录的文件路径，如 backend/assets/cover.jpg
    #      -> 自动映射为 {public_base_url}/feed/{user_token}/cover.jpg
    #   3) 完整 URL（以 http:// 或 https:// 开头）：直接写入 itunes:image
    cover_url: str = ""

    # ---------------- 任务队列与保留期 ----------------
    # TTS 引擎是进程内单例且线程不安全（tts.py 注释），故队列并发恒为 1，不做成可调项。
    task_queue_poll_s: float = 0.5
    # 成片保留天数（计划书 10.2）；data/work/{task_id}/ 在终态即时清理，不受此值约束。
    audio_retention_days: int = 30
    # 终态是否即时清理中间产物（计划书 10.2 明确要求）。置 false 仅用于排障。
    work_cleanup_on_terminal: bool = True

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_not_placeholder(cls, v: str) -> str:
        """占位/弱密钥**只告警不报错**，便于本地起服务；生产由部署手册强制替换。

        [FIX-JWTSECRET-01] 此前这个校验器**只有名字、没有实现**（函数体是 `return v`），
        于是 `.env` 里的占位值（实测 23 字符、以 `chan` 开头）一路静默通过，
        等于把「JWT_SECRET 须替换」这条纪律写成了一句注释。现在真的检查。

        不报错的理由：本地联调不该被密钥策略卡住；但**必须吵**——
        否则部署到公网时没人会想起换它。判定标准刻意宽松（占位关键词或长度不足），
        只求抓住「明显没换」这一类。
        """
        val = (v or "").strip()
        if not val:
            log.warning("JWT_SECRET 为空：登录接口会直接拒绝签发令牌（见 api/security.py）。"
                        "请写入 .env，生产环境必须为随机长串。")
            return v
        low = val.lower()
        hits = [k for k in _JWT_PLACEHOLDER_HINTS if k in low]
        if hits or len(val) < 32:
            reason = (f"命中占位关键词 {hits}" if hits else f"长度仅 {len(val)} 字符（建议 ≥ 32）")
            log.warning("JWT_SECRET 疑似占位/强度不足（%s）。本地联调可继续，"
                        "但**对外部署前必须替换**为随机长串，否则任何人都能伪造登录态。", reason)
        return v

    # ---------------- 路径解析 ----------------
    def path(self, value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def data_path(self) -> Path:
        return self.path(self.data_dir)

    @property
    def db_file(self) -> Path:
        return self.path(self.db_path)

    @property
    def db_url(self) -> str:
        """SQLAlchemy 连接串。路径一律转 POSIX 分隔符，Windows 反斜杠会被当转义符。"""
        return "sqlite:///" + self.db_file.as_posix()

    @property
    def audio_path(self) -> Path:
        return self.path(self.audio_dir)

    @property
    def podcast_path(self) -> Path:
        return self.path(self.podcast_dir)

    def task_audio_dir(self, task_id: str) -> Path:
        return self.audio_path / str(task_id)

    def task_work_dir(self, task_id: str) -> Path:
        return self.work_path / str(task_id)

    @property
    def work_path(self) -> Path:
        return self.path(self.work_dir)

    @property
    def cache_path(self) -> Path:
        return self.path(self.cache_dir)

    @property
    def voices_path(self) -> Path:
        return self.path(self.voices_dir)

    @property
    def prompts_path(self) -> Path:
        return self.path(self.prompts_dir)

    @property
    def sensitive_dict_path(self) -> Path:
        return self.path(self.sensitive_dict)

    @property
    def intro_file(self) -> Path:
        return self.path(self.intro_path)

    @property
    def outro_file(self) -> Path:
        return self.path(self.outro_path)

    @property
    def cosyvoice_root_path(self) -> Path:
        return self.path(self.cosyvoice_root)

    @property
    def synth_trace_file(self) -> Path:
        return self.path(self.synth_trace_path)

    @property
    def model_dir_path(self) -> Path:
        return self.path(self.cosyvoice_model_dir)

    @property
    def model_version(self) -> str:
        """写入句级缓存键的模型版本号，防止换模型后命中旧音频。"""
        return self.model_dir_path.name or self.tts_model

    @property
    def speaker_voice_map(self) -> dict[str, str]:
        """脚本 speaker（A/B）→ 音色档案 id。"""
        return {"A": self.speaker_a_voice, "B": self.speaker_b_voice}

    def ensure_dirs(self) -> None:
        for p in (self.data_path, self.work_path, self.cache_path,
                  self.audio_path, self.podcast_path):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def apply_runtime_env(settings: Settings | None = None) -> None:
    """在与 torch 相关的 import 之前设置运行时环境变量。

    注意：**不要**设置 PYTORCH_CUDA_ALLOC_CONF=expandable_segments ——
    实测在 Windows 上 `not supported on this platform`，属无效措施（计划书 V1.4.0 已剔除）。
    """
    s = settings or get_settings()
    os.environ.setdefault("COSYVOICE_ONNX_DEVICE", s.cosyvoice_onnx_device)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
