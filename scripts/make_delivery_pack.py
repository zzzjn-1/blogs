# -*- coding: utf-8 -*-
"""把「需要交付的东西」归集成一个独立文件夹（并压成同名 zip）。

为什么需要它
------------
《项目任务书》点名要求的交付物散落在三个不同的目录树里：
  * 交付文档  → 工作区 `D:\\新建文件夹\\blogs\\双人对话播客自动生成系统\\`（唯一真源）
  * 演示产物  → 仓库 `demo/`、`outputs/d14_ppt/`、`outputs/d14_demo/`
  * 源代码    → 仓库 `api/ backend/ frontend/ scripts/ tests/ patches/`
外加部署脚本与验收证据。人工拷一次容易漏、且无法复核；本脚本把「打包范围」
写成唯一的、可读的数据结构（下面的 `PLAN`），并在包根落一份
`交付清单_MANIFEST.json`（逐文件字节数 + sha256），让验收方可以逐字节比对。

纪律
----
1. **打包不修改任何源文件**（只读源 + 写目标）。
2. **不带密钥**：`.env` 永不入包，只带 `.env.example`；入包前跑一次密钥特征扫描。
3. **不带可再生/超大件**：`node_modules` / `dist` / `pretrained_models` / `data` /
   `outputs` 全量 / `CosyVoice`（上游克隆）一律排除，只取被文档当证据引用的子集。
4. **不做破坏性删除**：目标目录已存在时**原地刷新**（逐文件覆盖），不删任何文件；
   若发现包里有本计划不再产出的**陈旧文件**，只**列出并提示**，由人来决定去留。
   （本机沙箱对「单次删除 > 50 个文件」抛 `SystemExit`，且它**不是** `Exception`，
   `rmtree(ignore_errors=True)` 吞不掉 → 重跑必须设计成幂等覆盖，而非「清空重建」。）

用法
----
    python scripts/make_delivery_pack.py                  # 打包 + 压缩
    python scripts/make_delivery_pack.py --no-zip         # 只打包
    python scripts/make_delivery_pack.py --check          # 只报告会打进去什么，不写盘
    python scripts/make_delivery_pack.py --self-test      # 注入式自检（7 条，须全绿）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

REPO = r"D:\podcast-ai"
DOCS_SRC = r"D:\新建文件夹\blogs\双人对话播客自动生成系统"
OUT_ROOT = r"D:\新建文件夹\blogs"
PACK_NAME = "双人对话播客自动生成系统_交付包"

# ---------------------------------------------------------------- 排除规则
EXCL_DIR = {
    "node_modules", "dist", ".git", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".vite", ".idea", ".vscode", "_archive_trash",
}
EXCL_FILE_SUFFIX = (".pyc", ".pyo", ".tmp", ".bak", ".log.bak")
EXCL_FILE_PREFIX = ("~$",)          # Office 临时锁文件
# 前端构建工具会留下的时间戳临时配置
EXCL_FILE_RE = re.compile(r"^vitest\.config\.ts\.timestamp-.*\.mjs$")

# 密钥特征：命中即中止打包（防止把 .env 里的东西带出去）
SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9]{24,}"                      # OpenAI 风格
    r"|AKIA[0-9A-Z]{16}"                        # AWS AccessKeyId
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)
SECRET_SCAN_SUFFIX = (".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md",
                      ".txt", ".bat", ".sh", ".yml", ".yaml", ".ini", ".cfg",
                      ".env.example")
# 行内豁免：命中密钥特征的**测试夹具**必须显式标注，好让 review 时一眼能 greppable 到。
# （此前用「子串白名单」放行 `sk-definitely-invalid-key`，但那个串含连字符、
#   压根匹配不到 SECRET_RE —— 白名单从未生效，是死代码。改为显式标记。）
ALLOW_MARKER = "allow-secret"

# ---------------------------------------------------------------- 打包范围
# (相对包内路径, 源绝对路径, 类型)  类型: tree(整棵) / file(单文件)
PLAN: list[tuple[str, str, str]] = [
    # ---- 1. 交付文档（唯一真源，含 archive/ 历史版本）
    ("01_交付文档", DOCS_SRC, "tree"),

    # ---- 2. 演示产物
    ("02_演示产物/成片（3 期）", os.path.join(REPO, "demo", "episodes"), "tree"),
    ("02_演示产物/界面截图（Edge）", os.path.join(REPO, "demo", "screenshots"), "tree"),
    ("02_演示产物/演示台账", os.path.join(REPO, "demo", "demo_pack.json"), "file"),
    ("02_演示产物/演示台账", os.path.join(REPO, "outputs", "d14_demo"), "tree"),
    ("02_演示产物/答辩PPT", os.path.join(
        REPO, "outputs", "d14_ppt",
        "双人对话播客自动生成系统_D14答辩PPT_V1.0.0",
        "双人对话播客自动生成系统_D14答辩PPT_V1.0.0.pptx"), "file"),

    # ---- 3. 源代码（不含依赖与构建产物）
    ("03_源代码/api", os.path.join(REPO, "api"), "tree"),
    ("03_源代码/backend", os.path.join(REPO, "backend"), "tree"),
    ("03_源代码/frontend", os.path.join(REPO, "frontend"), "tree"),
    ("03_源代码/scripts", os.path.join(REPO, "scripts"), "tree"),
    ("03_源代码/tests", os.path.join(REPO, "tests"), "tree"),
    ("03_源代码/patches", os.path.join(REPO, "patches"), "tree"),
    ("03_源代码", os.path.join(REPO, "requirements-app.txt"), "file"),
    ("03_源代码", os.path.join(REPO, "requirements-win.txt"), "file"),
    ("03_源代码", os.path.join(REPO, "conftest.py"), "file"),
    ("03_源代码", os.path.join(REPO, "pytest.ini"), "file"),
    ("03_源代码", os.path.join(REPO, ".env.example"), "file"),
    ("03_源代码", os.path.join(REPO, ".gitignore"), "file"),
    ("03_源代码", os.path.join(REPO, "README.md"), "file"),

    # ---- 4. 部署与启动脚本
    ("04_部署与启动脚本", os.path.join(OUT_ROOT, "start_dev.bat"), "file"),
    ("04_部署与启动脚本", os.path.join(OUT_ROOT, "stop_dev.bat"), "file"),

    # ---- 5. 验收证据（只取被报告引用为证据的子集）
    ("05_验收证据/回归与变异", os.path.join(REPO, "outputs", "d13_junit_final.xml"), "file"),
    ("05_验收证据/回归与变异", os.path.join(REPO, "outputs", "cleanup_verify_junit.xml"), "file"),
    ("05_验收证据/回归与变异", os.path.join(REPO, "outputs", "d11_mutation.log"), "file"),
    ("05_验收证据/回归与变异", os.path.join(REPO, "outputs", "frontend_mutation.log"), "file"),
    ("05_验收证据/回归与变异", os.path.join(REPO, "outputs", "cleanup_verify_frontend.log"), "file"),
    ("05_验收证据/一致率", os.path.join(REPO, "outputs", "consistency"), "tree"),
    ("05_验收证据/订阅源核对", os.path.join(REPO, "outputs", "d13_feed_verify.json"), "file"),
    ("05_验收证据/演示核验", os.path.join(REPO, "outputs", "d14_demo_verify.json"), "file"),
    ("05_验收证据/浏览器取证", os.path.join(REPO, "outputs", "d14_browser", "evidence.json"), "file"),
    ("05_验收证据/环境与冒烟", os.path.join(REPO, "outputs", "smoke_result.json"), "file"),
    ("05_验收证据/环境与冒烟", os.path.join(REPO, "outputs", "d3_verify_result.json"), "file"),
    ("05_验收证据/环境与冒烟", os.path.join(REPO, "outputs", "d5_verify_result.json"), "file"),
    ("05_验收证据/环境与冒烟", os.path.join(REPO, "outputs", "d6_verify_evidence.json"), "file"),
    ("05_验收证据/环境与冒烟", os.path.join(REPO, "outputs", "cli_selftest"), "tree"),
    ("05_验收证据/环境与冒烟", os.path.join(REPO, "outputs", "d12_crash_recover"), "tree"),
    ("05_验收证据/文档终检", os.path.join(REPO, "outputs", "check_spec_docs_final.log"), "file"),
]


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def skip_file(name: str) -> bool:
    if name.startswith(EXCL_FILE_PREFIX):
        return True
    if name.endswith(EXCL_FILE_SUFFIX):
        return True
    if EXCL_FILE_RE.match(name):
        return True
    # `.env` 及其任意变体（.env.local / .env.production ...）一律不入包，
    # 只放行模板 `.env.example`
    if name.startswith(".env") and name != ".env.example":
        return True
    return False


def find_stale(pack: str, planned: set[str]) -> list[str]:
    """包内存在、但本次计划不再产出的文件（陈留物）。只报告，不删除。"""
    stale: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(pack):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), pack).replace(os.sep, "/")
            if rel not in planned:
                stale.append(rel)
    return sorted(stale)


def collect(src: str, kind: str) -> list[tuple[str, str]]:
    """返回 [(绝对路径, 包内相对路径)]。kind=tree 时相对路径带目录前缀。"""
    if kind == "file":
        if not os.path.isfile(src):
            raise FileNotFoundError(src)
        return [(src, os.path.basename(src))]

    out: list[tuple[str, str]] = []
    base = os.path.dirname(src) if os.path.isfile(src) else src
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCL_DIR)
        for name in sorted(filenames):
            if skip_file(name):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            out.append((full, rel))
    return out


def scan_secrets(pairs: list[tuple[str, str]], pack_rel: str) -> list[str]:
    """扫描疑似密钥。带 `allow-secret` 标记的行视为已登记的测试夹具，放行。"""
    hits: list[str] = []
    for full, _rel in pairs:
        if not full.lower().endswith(SECRET_SCAN_SUFFIX):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if ALLOW_MARKER in line:
                        continue
                    if SECRET_RE.search(line):
                        hits.append("%s:%d" % (pack_rel, i))
        except OSError:
            pass
    return hits


def build(check_only: bool) -> int:
    pack = os.path.join(OUT_ROOT, PACK_NAME)

    # 1) 展开打包计划
    tasks: list[tuple[str, str, str]] = []   # (包内目录, 源文件, 包内文件名)
    missing: list[str] = []
    for pack_dir, src, kind in PLAN:
        if not os.path.exists(src):
            missing.append(src)
            continue
        for full, rel in collect(src, kind):
            tasks.append((pack_dir, full, rel))

    if missing:
        print("以下打包源不存在（请先核实 PLAN）：")
        for m in missing:
            print("  !! %s" % m)
        return 2

    # 2) 密钥扫描（在任何写盘之前）
    all_pairs = [(t[1], os.path.join(t[0], t[2])) for t in tasks]
    hits = scan_secrets(all_pairs, PACK_NAME)
    if hits:
        print("检测到疑似真实密钥特征，已中止打包：")
        for h in hits[:20]:
            print("  !! %s" % h)
        print("（若确为测试夹具，请在该行加 `%s` 标记后再打包）" % ALLOW_MARKER)
        return 3
    print("密钥扫描：通过（%d 个待打包文本文件，无未登记的真实密钥特征）" % len(all_pairs))

    total_bytes = sum(os.path.getsize(t[1]) for t in tasks)
    print("待打包：%d 个文件 / %.2f MB" % (len(tasks), total_bytes / 1048576))

    if check_only:
        by_dir: dict[str, int] = {}
        for d, _f, _r in tasks:
            by_dir[d] = by_dir.get(d, 0) + 1
        print("\n按目录分布：")
        for d in sorted(by_dir):
            print("  %-34s %d" % (d, by_dir[d]))
        print("\n--check 未写盘。")
        return 0

    # 3) 目标目录：原地刷新（覆盖式，不删文件）
    existed = os.path.isdir(pack)
    os.makedirs(pack, exist_ok=True)
    if existed:
        print("目标已存在，按幂等覆盖方式原地刷新（不删除任何文件）")

    # 4) 复制
    manifest: list[dict] = []
    for pack_dir, src, rel in tasks:
        dst = os.path.join(pack, pack_dir.replace("/", os.sep), rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        manifest.append({
            "path": os.path.relpath(dst, pack).replace(os.sep, "/"),
            "bytes": os.path.getsize(dst),
            "sha256": sha256(dst),
        })

    # 4b) 陈旧文件体检（只报告，不删除）
    planned = {m["path"] for m in manifest} | {"交付说明.md", "交付清单_MANIFEST.json"}
    stale = find_stale(pack, planned)
    if stale:
        print("包内存在 %d 个本计划不再产出的陈旧文件（**未删除**，请人工确认去留）：" % len(stale))
        for s in stale[:30]:
            print("   ~ %s" % s)

    # 5) 交付说明
    readme = os.path.join(pack, "交付说明.md")
    with open(readme, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_readme(manifest, pack))
    manifest.append({
        "path": "交付说明.md",
        "bytes": os.path.getsize(readme),
        "sha256": sha256(readme),
    })

    # 6) 逐文件复核（源 vs 包）
    bad = verify(tasks, pack)
    if bad:
        print("字节级复核发现 %d 处不一致：" % len(bad))
        for b in bad[:20]:
            print("  !! %s" % b)
        return 4

    manifest.sort(key=lambda m: m["path"])
    with open(os.path.join(pack, "交付清单_MANIFEST.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({
            "打包时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "文件数": len(manifest),
            "总字节": sum(m["bytes"] for m in manifest),
            "文件": manifest,
        }, f, ensure_ascii=False, indent=1)

    n = len(manifest)
    total = sum(m["bytes"] for m in manifest)
    print("打包完成：%d 个文件 / %.2f MB → %s" % (n, total / 1048576, pack))
    print("字节级复核：全部一致")
    return 0


def verify(tasks: list[tuple[str, str, str]], pack: str) -> list[str]:
    problems: list[str] = []
    for pack_dir, src, rel in tasks:
        dst = os.path.join(pack, pack_dir.replace("/", os.sep), rel.replace("/", os.sep))
        if not os.path.exists(dst):
            problems.append("缺失：%s" % dst)
        elif os.path.getsize(src) != os.path.getsize(dst):
            problems.append("字节数不一致：%s" % os.path.relpath(dst, pack))
        elif sha256(src) != sha256(dst):
            problems.append("内容不一致：%s" % os.path.relpath(dst, pack))
    return problems


def render_readme(manifest: list[dict], pack: str) -> str:
    docs = sorted({m["path"].split("/")[-1] for m in manifest if m["path"].startswith("01_交付文档/") and m["path"].count("/") == 1})
    return README_TMPL.replace("{{DOC_COUNT}}", str(len(docs))).replace(
        "{{DOC_LIST}}", "\n".join("| `%s` |" % d for d in docs)
    ).replace("{{TOTAL}}", "%.2f MB" % (sum(m["bytes"] for m in manifest) / 1048576))


README_TMPL = """# 双人对话播客自动生成系统 · 交付包

> 本包按《双人对话播客自动生成系统项目任务书 V1.0.0》逐条对账后归集，
> 对账过程与结论见 `01_交付文档/双人对话播客自动生成系统_任务书交付物对照与查漏补缺报告_V1.0.0.md`。

包内共 **{{DOC_COUNT}} 份当前版交付文档**（另含 `archive/` 历史版本，供版本沿革追溯），
全部通过 `scripts/check_spec_docs.py` 的 Mermaid 语法校验与机械 lint。

---

## 一、包内导航

| 目录 | 内容 | 说明 |
| --- | --- | --- |
| `01_交付文档/` | 交付文档真源镜像 | 32 份当前版；`archive/` 为被取代的历史版本 |
| `02_演示产物/` | 成片 3 期（mp3）、Edge 界面截图 6 张、答辩 PPT、演示台账与运行日志 | 成片由生产链路实跑，非手工剪辑 |
| `03_源代码/` | `api/ backend/ frontend/ scripts/ tests/ patches/` + 依赖清单与配置样例 | **不含** `node_modules` / `dist` / 模型权重 / 数据库 |
| `04_部署与启动脚本/` | `start_dev.bat` / `stop_dev.bat` | 一键起停前后端，绑定 `127.0.0.1` |
| `05_验收证据/` | 回归与变异、一致率普查、订阅源核对、演示核验、浏览器取证、环境冒烟、文档终检 | 均为报告正文引用的原始证据 |

`交付清单_MANIFEST.json` 逐文件给出 **字节数 + sha256**，可用于逐字节复核本包完整性。

## 二、任务书对账结论（摘要）

* **「四、项目任务分解」10 项 —— 10/10 已覆盖。**
* **技术栈 9 项一致；1 项偏离已声明**：解释器为 Python 3.10（任务书写 3.11+），
  原因见《环境配置手册 V1.0.0》§2，属上游 CosyVoice 依赖约束，非自由选择。
* **补缺 7 件「独立成件」**：任务书点名、但此前内容散落在其他文档、没有独立文件的交付物
  已各自成文 —— 系统架构设计文档（任务书的「架构设计图」）、环境配置手册、部署基线报告、
  合成基线报告、MOS 评测报告、一致率比对报告、前端组件库清单。
* **顺带修掉 2 处既有文档缺陷**（K5 / K6，详见对照报告），其中 K6 属**安全相关漂移**
  （手册把后端描述成绑 `0.0.0.0`，实物已改为 `127.0.0.1`）。

### 有意未闭合、如实声明

| 项 | 现状 | 为什么不闭合 |
| --- | --- | --- |
| 浏览器兼容性 | **仅实测 Edge**（Chromium 内核） | 本机无 Firefox/Safari；已用 CDP 真实浏览器取证留档，不作无依据声称 |
| MOS 数值均分 | **只有人证结论，无均分** | 3 位真人听测反馈「都没问题」，用户裁定达标；未回填评分表，故不编造均分 |
| 在线 W3C 校验 / 真实播客客户端收录 | **未做** | 需要公网可达的 feed URL，本机环境不具备 |

## 三、跑起来需要什么

硬件与依赖的完整清单见 `01_交付文档/双人对话播客自动生成系统_环境配置手册_V1.0.0.md`，
启停与排障见 `01_交付文档/双人对话播客自动生成系统_部署运维手册_V1.2.0.md`。核心约束：

* **GPU**：NVIDIA，**净可用显存 ≥ 4.32 GB**（本机实测口径）
* **Python**：**3.10**（CosyVoice 依赖）
* **Node**：≥ 20（前端 Vite 5）
* **模型权重**：CosyVoice2-0.5B，约 **9.17 GB**，**不在本包内**，需按手册单独获取
* **其他**：FFmpeg、DeepSeek API Key（写入 `.env`，模板见 `03_源代码/.env.example`）

一键启动（Windows）：

```bat
04_部署与启动脚本\\start_dev.bat      :: 起后端 + 前端，绑定 127.0.0.1
04_部署与启动脚本\\stop_dev.bat       :: 收工
```

单期生成（等价于 Web 上点一次「提交」）：

```bat
python scripts\\make_episode.py --topic "人工智能会不会取代程序员" --words 800
```

## 四、本包**不含**什么（以及为什么）

| 未包含 | 体积 | 原因 |
| --- | --- | --- |
| 模型权重 `pretrained_models/` | 9.17 GB | 可从公开源获取，无需随包分发 |
| 句级缓存 `data/cache/` | 528 MB | 可再生；它是续跑命中的基础，依赖库内状态 |
| 全量 `outputs/` | 599 MB | 只把被文档当作证据引用的子集放进 `05_验收证据/` |
| `frontend/node_modules/` | 189 MB | `npm ci` 可完全重建 |
| `.env` | 6 KB | **含真实密钥，永不入包**；只带 `.env.example` |
| `CosyVoice/`（上游克隆） | 21 MB | 上游代码，非本项目产出 |

## 五、交付文档清单（当前版）

| 文档 |
| --- |
{{DOC_LIST}}

> 以上 32 份当前版文档，连同 `archive/` 历史版本、演示产物（3 期成片 + 6 张截图 + 答辩 PPT）、
> 源代码与依赖清单、启动脚本、验收证据，**全部入包，体积合计约 {{TOTAL}}**；
> 另附本说明与 `交付清单_MANIFEST.json`（后者的 `文件数` / `总字节` 已把两者一并计入）。
"""


def make_zip() -> int:
    pack = os.path.join(OUT_ROOT, PACK_NAME)
    if not os.path.isdir(pack):
        print("包目录不存在，跳过压缩")
        return 1
    zip_base = os.path.join(OUT_ROOT, PACK_NAME)
    zip_path = zip_base + ".zip"
    if os.path.exists(zip_path):
        os.remove(zip_path)          # 单文件删除，不触发批量删除守卫
    archive = shutil.make_archive(zip_base, "zip", root_dir=OUT_ROOT, base_dir=PACK_NAME)
    size = os.path.getsize(archive)
    print("压缩完成：%s（%.2f MB）" % (archive, size / 1048576))
    return 0


def self_test() -> int:
    """注入式自检：每条规则都要能证明「会红」。
    没跑过自检，就不该声称密钥扫描 / 排除规则真的生效。
    """
    import tempfile

    results: list[bool] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + note) if note else ""))
        results.append(bool(ok))

    with tempfile.TemporaryDirectory() as td:
        # 拼出来的假凭据：源码里不出现紧邻的 `sk-<24+字母数字>`，故不会自我命中
        secret = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"

        # T1 真实密钥特征必须被扫出
        p = os.path.join(td, "leak.py")
        with open(p, "w", encoding="utf-8") as f:
            f.write('KEY = "%s"\n' % secret)
        hits = scan_secrets([(p, "leak.py")], "pack")
        check("T1 真实密钥特征能被扫出", len(hits) == 1, str(hits))

        # T2 显式豁免标记必须放行（否则测试夹具会让打包永久不可用）
        p2 = os.path.join(td, "fake.py")
        with open(p2, "w", encoding="utf-8") as f:
            f.write('KEY = "%s"  # %s\n' % (secret, ALLOW_MARKER))
        raw = scan_secrets([(p2, "fake.py")], "pack")
        check("T2 `%s` 标记生效，已登记的夹具被放行" % ALLOW_MARKER, not raw, str(raw))

        # T3 .env 全系排除 / .env.example 放行
        check(
            "T3 .env 系列被排除，.env.example 放行",
            skip_file(".env") and skip_file(".env.local") and skip_file(".env.production")
            and not skip_file(".env.example"),
        )

        # T4 临时件排除
        check(
            "T4 临时件被排除（Office 锁 / vitest 时间戳 / pyc / bak）",
            skip_file("~$x.pptx") and skip_file("vitest.config.ts.timestamp-1-a.mjs")
            and skip_file("a.pyc") and skip_file("a.bak") and not skip_file("main.tsx"),
        )

        # T5 目录级排除 + 实际采集结果
        d = os.path.join(td, "tree")
        for rel in ["node_modules/x/a.js", "src/keep.ts", ".env", ".env.example"]:
            fp = os.path.join(d, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write("x")
        got = sorted(r for _f, r in collect(d, "tree"))
        check(
            "T5 tree 采集排除 node_modules/.env，保留源码与 .env.example",
            got == [".env.example", "src/keep.ts"], str(got),
        )

        # T6 陈留物必须被体检出来
        pk = os.path.join(td, "pack")
        for rel in ["a.md", "old/ghost.md"]:
            fp = os.path.join(pk, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(fp), exist_ok=True)
            with open(fp, "w", encoding="utf-8") as f:
                f.write("x")
        st = find_stale(pk, {"a.md"})
        check("T6 陈留物能被体检出来（只报告不删除）", st == ["old/ghost.md"], str(st))

    # T7 打包范围内每个源路径都必须存在 —— 防止源文件改名后「静默少打」
    missing = [s for _d, s, _k in PLAN if not os.path.exists(s)]
    check("T7 PLAN 内 %d 个源路径全部存在" % len(PLAN), not missing, str(missing))

    n_ok = sum(1 for r in results if r)
    print("\n自检结论：%d/%d 如期通过" % (n_ok, len(results)))
    return 0 if n_ok == len(results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="归集交付物 → 独立文件夹（+ 同名 zip）")
    ap.add_argument("--check", action="store_true", help="只报告会打进去什么，不写盘")
    ap.add_argument("--no-zip", action="store_true", help="只打包，不压缩")
    ap.add_argument("--self-test", action="store_true", help="注入式自检（验证排除规则与密钥扫描真的生效）")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    rc = build(args.check)
    if rc != 0 or args.check:
        return rc
    if args.no_zip:
        return 0
    return make_zip()


if __name__ == "__main__":
    sys.exit(main())
