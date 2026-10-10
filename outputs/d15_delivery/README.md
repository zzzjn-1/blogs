# D15 · 交付产物（答辩 PPT / 交付包 / 部署脚本）

本目录是《双人对话播客自动生成系统》的**最终交付产物快照**，与仓库源码同源同版本。
放在仓库内是为了让另一台电脑**只 clone 一次就能拿到复现所需的一切**。

> **交付包压缩件（`双人对话播客自动生成系统_交付包.zip`，约 19 MB）不在本目录**，
> 而是挂在 **GitHub Release** 附件上——避免 zip 进入 git 历史后无法瘦身。
> 需要整包下载时，去本仓库 Releases 页取。

---

## 目录内容

| 目录 | 内容 | 文件数 |
| --- | --- | --- |
| `答辩PPT/` | 现行版《答辩 PPT V2.0.0》（**17 页** / 17 页演讲备注 / 6 张真机截图）、`STORY.md` 叙事骨架、`DESIGN.md` 设计宪法、`slides/01~17.slide` DSL 源、`preview/p01~p17.png` 逐页视觉取证、`assets/` 配图、`预览_17页总览.png`、`archive/` 被取代的 D14 V1.0.0 | 47 |
| `交付包/` | 按《任务书》逐条对账后归集的交付物全集（`01_交付文档` 77 · `02_演示产物` 61 · `03_源代码` 168 · `04_部署与启动脚本` 2 · `05_验收证据` 28 · 根 2） | 338 |
| `部署脚本/` | `start_dev.bat` / `stop_dev.bat`，一键起停前后端（绑定 `127.0.0.1`） | 2 |

`交付包/交付清单_MANIFEST.json` 逐文件给出 **字节数 + sha256**，可逐字节复核完整性。
`交付包/交付说明.md` 是包内导航与任务书对账结论摘要。

---

## 从零复现（另一台电脑）

### 1. 环境前提

| 项 | 要求 | 说明 |
| --- | --- | --- |
| 操作系统 | Windows 10/11 | 启停脚本为 `.bat`；核心链路亦可手工在 Linux 复现 |
| Python | **3.10**（非 3.11+） | 上游 CosyVoice 依赖约束，见 `交付包/01_交付文档/双人对话播客自动生成系统_环境配置手册_V1.0.0.md` §2 |
| Node.js | ≥ 20 | 前端 Vite 5 |
| GPU | NVIDIA，**净可用显存 ≥ 4.32 GB** | 本机实测口径；fp16 + ONNX CPU 兜底 |
| FFmpeg | 任意近期版本 | 后期拼接与 loudnorm |
| 模型权重 | CosyVoice2-0.5B（约 **9.17 GB**） | **不在仓库内**，按环境配置手册单独获取 |
| DeepSeek API Key | 需自备 | 写入 `.env`（模板见 `交付包/03_源代码/.env.example`） |

### 2. 取代码

```bash
git clone https://github.com/zzzjn-1/blogs.git
cd blogs
```

### 3. 装依赖

```bash
# 后端（Python 3.10）
pip install -r requirements-app.txt
pip install -r requirements-win.txt        # Windows 专用：torch CUDA / pynini 相关

# 前端
cd frontend && npm ci && cd ..
```

### 4. 配环境变量

```bash
cp .env.example .env        # 然后填入自己的 DeepSeek API Key
```

> `.env` **含真实密钥，永不入库**，仓库只提供 `.env.example`。

### 5. 起服务

```bat
outputs\d15_delivery\部署脚本\start_dev.bat     :: 起后端 + 前端
outputs\d15_delivery\部署脚本\stop_dev.bat      :: 收工
```

### 6. 跑一期（等价于 Web 上点一次「提交」）

```bash
python scripts/make_episode.py --topic "人工智能会不会取代程序员" --words 800
```

### 7. 自测

```bash
pytest --junitxml=outputs/self_junit.xml        # 后端回归（须带 junitxml，本机 console 汇总被抑制）
python scripts/check_frontend.py                # 前端门禁
python scripts/verify_pptx_delivery.py <pptx> <slides_dir>   # PPT 交付六项判据 A~F
```

---

## 已实测的复现证据

| 项 | 结果 |
| --- | --- |
| 后端回归 | **411 通过 / 0 失败 / 0 错误**（junit 口径） |
| 变异测试 | **35 / 35 如期变红** |
| 前端 | vitest 34 条 / 3 文件通过；变异 7/7 |
| 逐句一致率 | **1120 / 1120 = 100.00%**（36 集全量普查） |
| 演示成片 | 3 期 900 字档：301.68 / 257.06 / 279.36 s，合计 **838.10 s** |
| 答辩 PPT | 17 页 / 17 备注 / 6 图，`verify_pptx_delivery.py` **A~F 全过** |

---

## 本目录**不含**什么（以及为什么）

| 未包含 | 体积 | 原因 |
| --- | --- | --- |
| 模型权重 `pretrained_models/` | 9.17 GB | 公开可获取，不随仓分发 |
| 交付包 zip | 19 MB | 走 GitHub Release 附件，避免污染 git 历史 |
| `data/`（句级缓存、SQLite） | 528 MB+ | 可再生，且依赖本机运行状态 |
| `frontend/node_modules/` | 189 MB | `npm ci` 可完全重建 |
| `.env` | 6 KB | **含真实密钥** |
| 上游 `CosyVoice/` 克隆 | 21 MB | 上游代码；本项目改动以 `patches/` 固化 |
