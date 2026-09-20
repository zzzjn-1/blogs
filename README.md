# 双人对话播客自动生成系统

输入「主题 + 字数 + 风格 + 说话人」，自动产出一期**双人对话播客成片**：大模型写对话脚本 → CosyVoice2 **零样本音色克隆**逐句合成 → FFmpeg 后期拼接与响度归一 → 输出成片音频与 RSS 订阅源。

全流程带 Web 界面：注册登录、提交任务、任务详情与脚本回看、频道设置与订阅源下载。

> 演示成片（900 字档，真机实跑）见 [`demo/episodes/`](demo/episodes/)：
> `ep_20260920_110641` 301.68 s / `ep_20260920_111344` 257.06 s / `ep_20260920_112014` 279.36 s。

---

## 目录

- [核心能力](#核心能力)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [测试与质量门禁](#测试与质量门禁)
- [交付文档](#交付文档)
- [演示产物](#演示产物)
- [不在仓库中的内容](#不在仓库中的内容)
- [已知限制](#已知限制)

---

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 双人对话脚本生成 | 由大模型按「主题 / 字数 / 风格 / 说话人」四参数产出结构化脚本，含敏感词与合规预检 |
| 零样本音色克隆 | CosyVoice2-0.5B，A/B 两位说话人各一份参考音频，音色与语气零样本复刻 |
| 片头片尾 | 动态片头（占位符按实际参数替换）+ 固定片尾，素材自归一至约 −16 LUFS |
| 后期与导出 | 逐句音频拼接、句间停顿、整体 loudnorm 响度归一、导出 mp3 成片 |
| 句级缓存与续跑 | 以「文本 + 音色 + 参数」为键缓存逐句音频；中断后续跑逐段遍历、仅对未命中段真推理 |
| 任务状态机 | `SCRIPTING → SCRIPT_READY → SYNTHESIZING → POSTPROCESSING → DONE`，异常落 `FAILED` 且支持 `FAILED → SCRIPTING` 重试 |
| RSS 订阅源 | `feedgen` 产出 podcast 标准订阅源，供播客客户端收录与下载 |
| Web 前端 | 公开页（注册 / 登录 / 提交 / 历史 / 任务详情）+ 频道设置；管理端基于 Ant Design |

## 技术栈

| 层 | 选型 |
| --- | --- |
| 语音合成 | CosyVoice2-0.5B（零样本克隆）、PyTorch + CUDA |
| 大模型 | DeepSeek（OpenAI 兼容协议），带指数退避重试 |
| 后端 | Python 3.10 / FastAPI 0.115 / uvicorn 0.30 |
| 数据库 | SQLite（WAL）+ SQLAlchemy 2.0 ORM |
| 鉴权 | JWT（PyJWT）+ bcrypt 口令散列 |
| 音频 | FFmpeg（外部可执行）+ pydub |
| 订阅源 | feedgen（podcast / atom 扩展） |
| 前端 | React 18 + Vite 5 + TypeScript + Tailwind（公开页）+ Ant Design 5（管理端） |
| 测试 | pytest 8（后端）、vitest 3（前端） |

## 目录结构

```
api/             FastAPI 服务层（路由 / 鉴权 / 数据模型 / 任务调度 / 合成引擎适配）
backend/         后台管理端与静态素材
  assets/          片头片尾、封面、音色参考音频、提示词、词表
  dicts/           多音字表、敏感词表
  prompts/         脚本生成与合规提示词
  voices/          A/B 说话人音色档案与参考音频
frontend/        公开站点前端（React + Vite + Tailwind）
scripts/         工程脚本：数据准备、核验、压测、变异检查、工作区清理
tests/           后端测试
docs/            交付文档（**镜像目录**，真源在工作区，见下）
demo/            演示产物：成片 mp3、演示台账、真实浏览器截图
outputs/         运行期产物与评测报告（音频不入库，JSON / PNG 报告入库）
patches/         对上游 CosyVoice 的改动补丁（上游仓库本体不入库）
```

## 快速开始

### 前置条件

- Windows（本项目在 Windows 11 + RTX 4050 上开发，显存净可用约 4.3 GB）
- **Python 3.10**（CosyVoice 要求；开发环境为 conda env `cosyvoice`）
- FFmpeg 已加入 `PATH`
- Node.js 18+
- NVIDIA GPU + 可用的 CUDA 环境

### 1. 获取模型权重

权重约 9 GB，**不入库**。按 [`docs/…部署运维手册_V1.1.0.md`](docs/) 的说明获取，或使用仓库内脚本：

```bash
python scripts/download_models.py
```

### 2. 安装依赖

```bash
pip install -r requirements-win.txt   # CosyVoice 推理引擎依赖（先装）
pip install -r requirements-app.txt   # 应用层依赖（后装）

cd frontend && npm install
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

然后在 `.env` 中至少填好两项（`.env` 已列入 `.gitignore`，**不会入库**）：

- `LLM_API_KEY` —— 大模型 API Key
- `JWT_SECRET` —— JWT 签名密钥，建议 ≥ 32 字节随机串

### 4. 启动服务

```bash
# 后端（默认 http://127.0.0.1:8000，接口文档 /docs）
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000

# 前端（默认 http://127.0.0.1:5173）
cd frontend && npm run dev
```

### 5. 生成一期播客

```bash
python scripts/make_episode.py --topic "熬夜之后怎么补回来" --words 900
```

## 测试与质量门禁

```bash
# 后端回归（注意：本机 console 汇总行会被抑制，请用 --junitxml 读计数）
python -m pytest --junitxml=outputs/pytest.xml

# 前端门禁：契约 → 类型 → 单测 → 构建
python scripts/check_frontend.py --full

# 文档门禁：Mermaid 语法 + 引用一致性
python scripts/check_spec_docs.py

# 交付文档镜像漂移检查（docs/ 与工作区真源是否一致）
python scripts/sync_docs.py --check
```

本项目对「判据可信度」要求较严：核验脚本普遍自带 `--self-test`，会**注入缺陷并要求对应判据变红**——只覆盖部分分支、或注入值恰好等于原值的判据一律视为无效。已落地的变异检查共 35 条（后端 28 / 前端 7），要求全红命中。

## 交付文档

`docs/` 是交付文档的**只读镜像**（源目录在外部的文档工作区）。请勿直接编辑 `docs/` 下的文件——改动会被下一次 `python scripts/sync_docs.py` 覆盖。

主要文档：

| 文档 | 内容 |
| --- | --- |
| `…产品需求规格说明书_V1.3.0.md` | 需求条目与实现对照 |
| `…开发计划书_V1.21.0.md` | 总体方案、阶段划分（D0~D14）、验收口径 |
| `…部署运维手册_V1.1.0.md` | 环境搭建、权重获取、启停、故障处置 |
| `…API接口文档_V1.0.0.md` | 接口清单与请求/响应示例 |
| `…数据库设计文档_V1.0.0.md` | 表结构与字段语义 |
| `…UI-UX设计说明_V1.0.0.md` | 页面结构与交互约定 |
| `…测试报告_V1.1.0.md` | 测试范围、用例与结果 |
| `…D14演示脚本_V1.1.0.md` | 5 分钟演示话术与录屏分镜 |
| `…D14验收自测表_V1.1.0.md` | 验收自测清单与结论 |

`docs/` 下另有 D0~D12 各阶段实施报告，以及 `docs/archive/` 保存的历史版本（按约定升版即重命名归档，历史文件冻结不改）。

## 演示产物

| 路径 | 内容 |
| --- | --- |
| `demo/episodes/*.mp3` | 3 期演示成片（128 kbps，与生产输出完全一致，未二次转码） |
| `demo/demo_pack.json` | 演示数据集台账：只声明身份（哪几期），测量值一律由核验脚本现测 |
| `demo/screenshots/*.png` | 6 个页面的真实浏览器截图（Edge，1440×900） |
| `outputs/d14_ppt/` | 答辩演示文稿 |

演示数据的核验入口：

```bash
python scripts/verify_demo_pack.py            # 跨产物三方核对 + ffprobe 实测时长
python scripts/verify_demo_pack.py --self-test  # 12 条判据注入缺陷，全部应变红
```

## 不在仓库中的内容

| 内容 | 原因 | 获取方式 |
| --- | --- | --- |
| `pretrained_models/` | 约 9 GB 权重 | 按部署运维手册或 `scripts/download_models.py` |
| `CosyVoice/` | 上游仓库本体 | `git clone` 上游；本项目改动见 `patches/` |
| `data/` | 运行期数据库、逐句缓存、成片（GB 级） | 运行后自动生成 |
| `.env` | 含真实 API Key 与 JWT 密钥 | 由 `.env.example` 复制后自行填写 |
| `node_modules/`、`frontend/dist/` | 可重建 | `npm install` / `npm run build` |
| `outputs/**/*.wav,*.mp3,*.m4a` | 体积大且可重生成 | 运行后自动生成（演示成片已单独放 `demo/`） |

## 已知限制

如实申明，避免误读：

- **浏览器兼容性只验证了 Edge（Chromium 内核）**。验证机未安装 Firefox / Safari，未做验证。
- **RSS 未做在线 W3C 校验**，也未在真实播客客户端中收录验证（两项均需公网 URL）。当前采用「离线跨产物核对（XML ↔ 数据库 ↔ 磁盘）+ 第三方解析器（feedparser）复核」替代，**这不等于 W3C 校验通过**。
- **MOS 为主观听测结论**：由 3 位听测人确认「都可接受」，未保留评分表与均分，因此仓库内不给出 MOS 数值。
- **单卡单并发**：合成引擎以单线程池串行调度以独占 GPU，未做多卡或并发扩展。
- 「全新环境 30 分钟内起服务」的计时**以模型权重已就位为前提**；冷装（含权重下载）需另行计时。
- 前端打包**尚未做代码分割**，生产构建产物体积偏大。

## 致谢

语音合成基于 [CosyVoice](https://github.com/FunAudioLLM/CosyVoice)（FunAudioLLM），本项目对其的改动以补丁形式保存在 `patches/` 下。
