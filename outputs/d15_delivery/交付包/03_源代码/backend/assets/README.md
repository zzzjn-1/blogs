# backend/assets —— 片头 / 片尾素材

| 文件 | 时长 | 规格 | 状态 |
| --- | --- | --- | --- |
| `intro.mp3` | 6.54 s | 44.1 kHz / 单声道 / 128 kbps | ✅ **女声素材**（`voice_a`，**纯克隆 `tone=""`**，语速 1.05） |
| `outro.mp3` | 7.46 s | 44.1 kHz / 单声道 / 128 kbps | ✅ **女声素材**（`voice_a`，**纯克隆 `tone=""`**，语速 1.05） |

> 表中时长为 **ffprobe 实测**（2026-09-20 复测）；响度 **−16.5 LUFS**、真峰 **−1.9 dBFS**（`ffmpeg ebur128` 摘要读数）。
> 这两个文件是 **零样本纯克隆（`tone=""`）** 产物 —— 与 2026-09-16 那批 instruct2 版**不是同一代**，
> 因此下文历史章节里的时长 / 语速 / F0 数字**都不能用来描述当前素材**（见「instruct2 回显」一节）。

## 🔁 片头已改为「动态生成」（2026-09-16）

用户指定片头文案：

```
大家好，今天是（日期），今天的主题是（主题）。
```

带占位符 ⇒ **片头不能是固定 mp3**，必须按当期信息现合成。因此：

- 主链路 `TaskRunner._stage_postprocess` 先调 `_build_dynamic_intro()`：
  渲染模板（日期取当天、主题取 `task.script_title`）→ 女声合成 → 归一 → 传给后期；
- 仅当 `INTRO_TEMPLATE` 为空**或不含占位符**时，才回退到本目录的固定 `intro.mp3`；
- 片尾仍是固定素材 `outro.mp3`，未做动态化。

> ⚠️ **本目录的 `intro.mp3` 现在是回退素材**：默认流水线下片头每期现合成，
> 改这里的 mp3 **不会**影响成片。

配置（`api/config.py`）：

```
INTRO_TEMPLATE="大家好，今天是（日期），今天的主题是（主题）。"
INTRO_SPEAKER=voice_a
INTRO_TONE="用自然放松、亲切温和的语气说这句话，像和朋友聊天一样，语调轻快上扬但不夸张"
INTRO_SPEED=1.05        # 与片尾同档（用户 2026-09-16 拍板的手感）
```

> ⚠️ `INTRO_TONE` **当前是死配置**：主链路的片头合成处**有意传 `tone=""`**，
> 该字段被显式忽略（见下文「instruct2 回显」一节）。改它不会有任何效果。

占位符三种写法都认：`（日期）` / `(日期)` / `{date}`，`（主题）` / `(主题)` / `{topic}`。
主题过长会自动截断（保住模板骨架 + 省略号），不会超出单句字数上限 `max_chars_per_seg`。

> **渲染用 `str.replace` 而非 `str.format`**：文案是用户自由文本，可能含 `{}`，
> 用 format 会抛 `KeyError`。渲染逻辑集中在 `api/services/intro_outro.py`，
> 主链路与 `scripts/make_intro_outro.py` 共用，避免两处漂移。

**离线预览**某期片头（生成到 `data/work/assets_voice/`，不落盘）：

```bash
python scripts/make_intro_outro.py --only intro --use-template --topic "人工智能"
```

## 这两个文件是什么

由 `scripts/make_intro_outro.py` 用 **女声 `voice_a`**（用户本人 `life.m4a`，
统一自相关 F0 175.8 Hz）朗读**通用开场白 / 结束语**合成，并已做响度归一。

| 位置 | 文案 |
| --- | --- |
| 片头 | 大家好，欢迎收听本期播客！今天，我们用一段轻松的对话，好好聊聊这个话题。 |
| 片尾 | 好啦，以上就是今天的全部内容。感谢你的陪伴和收听，我们下期再见！ |

## 🚨 语气指令文本不生效 + instruct2 会把参考音频回显到开头（两条均实测确认）

**实测结论（2026-09-16）**：`--tone` 的文本内容**不影响合成结果**，它只决定「走不走 instruct2 通道」。

| 配置（同文案 / 同语速 1.0） | 时长 | F0 | SHA1 |
| --- | --- | --- | --- |
| `--tone ""`（纯克隆 zero_shot） | 6.88 s | 197.5 Hz | 不同 |
| `--tone "…自然放松…但不夸张"` | 12.76 s | 179.8 Hz | `42a575dd…` |
| `--tone "…开心地聊天…带着笑意"` | 12.76 s | 179.8 Hz | `42a575dd…` ← **与上一条字节完全相同** |

两条语义不同的指令产出**逐字节相同**的音频 → 指令文本未被模型使用。

**根因**（`CosyVoice/cosyvoice/cli/frontend.py` 的 `frontend_zero_shot`）：

```python
if zero_shot_spk_id == '':
    prompt_text_token = self._extract_text_token(prompt_text)  # ← 才会用 instruct
else:
    model_input = {**self.spk2info[zero_shot_spk_id]}          # ← 忽略 prompt_text
```

当时 `api/services/tts.py` 在 `tone` **非空**时调用
`inference_instruct2(..., zero_shot_spk_id=voice.id)`，传的 id 非空 ⇒ 永远走 else 分支，
instruct 被丢弃（`tone` 实际只起「开不开 instruct2」的开关作用）。

**影响**：
- 开/关 tone 有巨大差异（6.88 s ↔ 12.76 s，F0 197.5 ↔ 179.8）——这是通道切换的效果；
- 想靠指令文本做「活泼 / 温柔 / 正式」的区分**做不到**；
- **更致命**：`inference_instruct2` 在 `zero_shot_spk_id` 非空时，会把 `prompt_wav`
  （`voice_a.wav`，内容正是「生活就像海洋…」）当作声学前缀**回显到输出开头** ——
  素材开头会先朗读一遍参考音频旧句（爱情期成片片头 0–5 s 即此句）。

**已实施的修复（2026-09-17）**：片头/片尾一律改走 `inference_zero_shot`（即 `tone=""`），
**保留** `zero_shot_spk_id=voice.id`（换用预计算 `spk2info`，只输出目标文本、不回显）。
落点：
- `api/services/task_runner.py` 的 `_stage_postprocess` 片头合成处**强制传 `tone=""`**
  （`INTRO_TONE` 在此被**有意忽略**，防止再次误引入该 bug）；
- `scripts/make_intro_outro.py` 的 `DEFAULT_TONE = ""`（脚本侧同样强制）。

> 早期文档曾把修复方向写成「把 `zero_shot_spk_id` 改为空串、让前端 tokenize instruct」——
> **那是错的，未采用**：会重新启用 instruct2 通道，等于把回显 bug 请回来。
> 根因在 `instruct2` 这一条通道，而不是 `zero_shot_spk_id` 非空。

素材因此**时长显著变短**（同一文案：instruct2 12.14 s → 纯克隆 6.54 s）。
代价是放弃 instruct2 的语气指令能力，当前只能靠 `speed` 与文案调听感。

### 语速（当前唯一生效的语气旋钮）

- 通道：**纯克隆 `tone=""`**（2026-09-17 起，见上一节）
- 语速：片头 `1.05` / 片尾 `1.05`（`make_intro_outro.py` 的 `DEFAULT_SPEED_INTRO` / `DEFAULT_SPEED`，两档同值）
- 素材实测：intro 6.54 s / outro 7.46 s，响度均 −16.5 LUFS

**⚠️ 以下整段数据属 instruct2 时代（2026-09-16），仅作历史留档，不代表当前素材。**
当时片头曾按语速回调：1.41 偏僵硬 → 1.00 偏慢（12.76 s / 2.82 字/秒）→ 1.10（11.60 s / 3.10 字/秒），
比温和档 2.98 略快、比片尾 3.23 略慢，落在用户认可的中间档。

当时「实测语调确实上扬」（统一自相关法，全帧中位）：

| 素材 | 时长 | 语速 | F0 | 与源录音 175.8 Hz 的差 | 底噪 | SNR |
| --- | --- | --- | --- | --- | --- | --- |
| intro（instruct2 / 1.10） | 11.60 s | 3.10 字/秒 | 186.1 Hz | +10.3 Hz | −69.40 dBFS | 54.62 dB |
| intro（instruct2 / 1.00，偏慢已弃用） | 12.76 s | 2.82 字/秒 | 179.8 Hz | +4.0 Hz | −74.63 dBFS | 61.23 dB |
| intro（instruct2 / 1.20，备档） | 10.62 s | 3.39 字/秒 | 190.1 Hz | +14.3 Hz | −68.53 dBFS | 53.87 dB |
| outro（instruct2 / 1.05） | 9.90 s | 3.23 字/秒 | 169.0 Hz | −3.8 Hz（收尾回落，正常） | −75.10 dBFS | 60.54 dB |

> **提速后 F0 反而回升**（179.8 → 186.1，1.20 档更达 190.1）：语速加快使单位时间内音节更密集，
> 测量到的中位 F0 随之上移。因此「提速」与「上扬」在此处是同向的，不必为保语调而牺牲语速。
> 底噪从 −74.63 升到 −69.40 dBFS 属同一机制的副作用（静音间隙被压缩），仍在干净档。

基线：D6 试跑 A 段 `voice_a` SNR = 46.31 dB → 本底噪水平属「干净」档。

> ⚠️ 上表的 F0 差异**不是**指令文本造成的（见上一节），而是「instruct2 通道 vs 纯克隆通道」的差异。
> 纯克隆同文案实测 F0 197.5 Hz、但时长只有 6.88 s 且底噪高 14 dB。
> 当时的选择是**开 tone 换「放松 / 慢」的听感**；该选项现已因回显 bug 被放弃
> （片头尾强制 `tone=""`），语气只能靠 `speed` 与文案调。

> **语速机制（重要）**：`speed` 在 `CosyVoice/cosyvoice/cli/model.py` 的 `token2wav()` 里实现为
> `F.interpolate(tts_mel, size=int(mel_len / speed))` —— **在 mel 域做时间缩放**，
> 因此**只改语速、不改音高**。实测提速 1.05→1.41（时长 14.54→10.82 s）后
> **F0 仅从 197.5 掉到 196.3 Hz**，活泼语调完整保留，可放心用 speed 调语速。
> 语速可精确预测：`时长 ≈ 基准时长 / speed`（基准 = speed 1.0 时的时长）。

**历史**：此前为 `scripts/make_assets.py` 生成的**合成音占位素材**（两音上行/下行，
片头 2.40 s / 片尾 2.20 s），仅用于让端到端链路跑通（R18）。
旧占位素材已备份至 `backend/assets/_archive/20260916_142348/`。

## ⚠️ 为什么脚本里要自己走 loudnorm

`api/services/postprocess.py` 对片头尾**只做 `unify()` + `apply_fade()`，不做 loudnorm**
（见 `postprocess()` 的 intro/outro 分支）。因此**素材自身响度就是成片响度**，
若直接丢入未归一的音频，片头会明显比正片偏轻或偏响。

所以 `make_intro_outro.py` 在导出前调用 `normalize_loudness()`，把片头尾对齐正片目标
（`settings.audio_loudness_i` ≈ −16 LUFS）：

- **当前素材**实测（2026-09-20 复测）：**−16.5 / −16.5 LUFS**，真峰 **−1.9 dBFS**（`ffmpeg ebur128`）；
- instruct2 时代实测：−16.99 / −17.25 LUFS，真峰约 −1.9 dBTP；
- 与正片（−16.15 ~ −16.46 LUFS）同档。

## 规格要求（替换时保持即可零改动沿用）

- 容器 / 编码：mp3（`libmp3lame`）
- 采样率：**44100 Hz**（与 `AUDIO_SAMPLE_RATE` 一致）
- 声道：**单声道**
- 码率：**128 kbps**（与 `AUDIO_MP3_BITRATE` 一致）

任意格式也能用——`postprocess.py` 的 `unify()` 会先统一为 44.1 kHz / 单声道 / `pcm_s16le` 再拼接。

## 相关配置

```
INTRO_PATH=backend/assets/intro.mp3
OUTRO_PATH=backend/assets/outro.mp3
AUDIO_REQUIRE_INTRO=false    # 置 true 则素材缺失时直接报错，而非告警跳过
```

## 重新生成 / 换文案

```bash
python scripts/make_intro_outro.py                      # 用默认通用文案重建
python scripts/make_intro_outro.py --text-intro "……"     # 自定义开场白
python scripts/make_intro_outro.py --text-outro "……"     # 自定义结束语
python scripts/make_intro_outro.py --speed 1.1          # 调语速（片头尾各自可用 --speed-intro/--speed-outro）
python scripts/make_intro_outro.py --dry-run            # 只看计划，不加载模型
```

> ✅ **`--tone` 目前是「哑参数」**：脚本在合成处把 `tone=""` **写死**（与主链路一致），
> 传非空值**只改变控制台那一行回显，不影响合成结果**；保留该参数仅为兼容旧命令行。
> 因此片头/片尾不会再产出「带参考音频回显」的素材，无需调用方额外小心。
> 重建后建议先 `--only intro --dry-run` 核对文案与语速。

脚本会自动备份旧素材到 `_archive/<时间戳>/` 并打印电平台账（时长 / 响度 / 真峰）。
`tone` 与 `speed` 均进缓存键，换参数会自然生成新条目，无需手动清缓存。

## 版本沿革

**读表口径（重要）**：备份目录以**备份动作发生的时刻**命名，目录里装的是
**被替换下来的旧素材**，不是该时刻新产出的素材 —— 两者相差一代。
表中「时长」列**由 ffprobe 对目录内实际文件实测**（2026-09-20 逐目录复测），
因此与目录名严格对应；「说明」列描述的是该文件在当时的身份。

| 备份时刻 | 备份目录 | 目录内实际内容（旧素材） | 实测时长 |
| --- | --- | --- | --- |
| 2026-09-16 14:23 | `_archive/20260916_142348/` | 合成音占位素材 | 2.40 s / 2.20 s |
| 2026-09-16 15:02 | `_archive/20260916_150244/` | 初版女声素材（平实语气） | 8.76 s / 6.64 s |
| 2026-09-16 15:16 | `_archive/20260916_151659/` | （空目录，该次未留下素材） | — |
| 2026-09-16 15:18 | `_archive/20260916_151820/` | 活泼语气版（片头，偏慢已弃用） | 14.54 s |
| 2026-09-16 15:36 | `_archive/20260916_153643/` | 片头提速 1.41（用户反馈「僵硬」，已弃用） | 10.82 s |
| 2026-09-16 15:52 | `_archive/20260916_155253/` | 放松指令 + 语速 1.00（文案改「大家好」，用户反馈**偏慢**） | 12.76 s |
| 2026-09-17 10:52 | `_archive/20260917_105208/` | 09-16 15:52 定稿的 instruct2 版 | 11.60 s / 9.90 s |
| 2026-09-17 11:32 | `_archive/20260917_113204/` | 最后一次 instruct2 版（带参考音频回显） | 12.14 s / 9.90 s |
| — | **当前素材（未备份，就地生效）** | 纯克隆 `tone=""`，语速 1.05 | **6.54 s / 7.46 s** |

> **本表已修正**：修订前的表格把「该时刻新产出的素材」与「该时刻的备份目录」配成一行，
> 导致整表错位一代，并把 09-16 的 instruct2 版（11.60 s / 9.90 s）标成「当前」。
> 实际当前素材见上表末行。09-17 的两次重建（改用 `tone=""`）此前完全未记录。
