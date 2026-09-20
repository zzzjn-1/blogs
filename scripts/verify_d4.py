# -*- coding: utf-8 -*-
"""D4 验收脚本：10 主题抽测 + 模型名映射核对 + 错误路径可读性验证。

对应开发计划书 6.2 节 D4 行的 Done 条件：

    10 个不同主题抽测，8 个以上一次生成即可用（过程自检口径，正式验收见 D10）；
    API 超时 / 429 路径均有可读错误提示。

用法（项目根目录）：

    D:\\anaconda\\envs\\cosyvoice\\python.exe scripts/verify_d4.py

产物：
    outputs/d4_script_gen/verify_result.json   机器可读的汇总与逐题明细
    outputs/d4_script_gen/scripts/NN_slug.json 逐题完整脚本（供人工抽查）
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.config import Settings                    # noqa: E402
from api.services import script_gen as sg          # noqa: E402

OUT_DIR = ROOT / "outputs" / "d4_script_gen"
SCRIPTS_DIR = OUT_DIR / "scripts"

# (类别, 主题, 目标时长分钟)
#
# 上限为什么定在 2.5 分钟：单次响应受 LLM_MAX_TOKENS 限制，实测中文约 1.5~2.2 token/字，
# 4096 的 max_tokens 只能撑约 1900~2700 字；8 分钟（2080 字）会贴着上限、且模型倾向于
# 大幅欠产，属「容量」问题而非「质量」问题，单独由 probe_capacity() 量化，不混进本抽测。
TOPICS: list[tuple[str, str, float]] = [
    ("知识科普", "为什么天空是蓝色的", 1.5),
    ("知识科普", "睡眠是怎样影响记忆的", 2.0),
    ("知识科普", "城市地铁隧道是怎么挖出来的", 2.0),
    ("知识科普", "咖啡因进入身体之后发生了什么", 1.5),
    ("行业解读", "生成式人工智能对内容行业的影响", 2.0),
    ("行业解读", "预制菜产业链的机会与争议", 2.0),
    ("行业解读", "国产新能源汽车出海面临哪些门槛", 2.5),
    ("生活闲聊", "一个人旅行的意义", 1.5),
    ("生活闲聊", "怎么挑一本真正适合自己的书", 1.5),
    ("生活闲聊", "把兴趣做成副业是种什么体验", 2.0),
]

USABLE_TARGET = 8   # Done 条件：10 题里 ≥ 8 题一次生成即可用


def _setup_logging() -> None:
    # Windows 控制台默认 GBK，直接 print 中文会 UnicodeEncodeError —— 强制 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")     # type: ignore[union-attr]
        except Exception:                            # noqa: BLE001
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S", stream=sys.stdout, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _slug(text: str, limit: int = 24) -> str:
    keep = [c for c in text if c.isalnum() or "\u4e00" <= c <= "\u9fff"]
    return "".join(keep)[:limit] or "script"


def probe_prompts(base: Settings) -> dict:
    """提示词一致性自检 + 模板规模（同时给出「系统提示词可缓存前缀」的量级）。"""
    print("=" * 78)
    print("一、提示词装载与一致性自检（阈值脱节即 fail-fast，见 PATCH-GUARD-02 同类取向）")
    problems = sg.verify_prompt_consistency(base)
    prompts = sg.load_prompts(base)
    print("  提示词目录      :", prompts.dir)
    print("  一致性自检      :", "通过（无问题）" if not problems else problems)
    print("  system 字节数   :", len(prompts.system.encode("utf-8")),
          "（这部分是逐字节稳定的可缓存前缀）")
    print("  user 模板字节数 :", len(prompts.user_template.template.encode("utf-8")))
    print("  敏感词条数      :", len(sg.load_sensitive_words(base.sensitive_dict_path)))
    assert not problems, "提示词与配置不一致，拒绝继续"
    return {
        "prompt_dir": str(prompts.dir),
        "consistency_problems": problems,
        "system_bytes": len(prompts.system.encode("utf-8")),
        "user_template_bytes": len(prompts.user_template.template.encode("utf-8")),
        "sensitive_word_count": len(sg.load_sensitive_words(base.sensitive_dict_path)),
    }


def probe_model_mapping(base: Settings) -> dict:
    """模型名纪律：请求名 vs 响应体 model 字段。"""
    print("=" * 78)
    print("二、模型名映射核对（网关会静默改写模型名，结论一律以响应体 model 为准）")
    gen = sg.ScriptGenerator(base)
    probe = sg.ScriptGenerator(
        Settings(llm_model=base.llm_model, llm_max_tokens=64, script_self_check=False,
                 script_correction_retry=0, llm_max_retry=0),
        client=None)
    kwargs = probe._request_kwargs(                 # noqa: SLF001 —— 冒烟探测，故意直连
        [{"role": "system", "content": "只输出 JSON：{\"ok\":true}"},
         {"role": "user", "content": "返回 json"}],
        base.llm_model, temperature=0.0)
    resp, calls = probe._invoke(kwargs, float(base.llm_timeout))   # noqa: SLF001
    raw, returned, usage = sg._extract(resp)                       # noqa: SLF001
    requested = base.llm_model
    mapped = returned != requested
    print(f"  请求模型        : {requested}")
    print(f"  响应体 model    : {returned or '(空)'}")
    print(f"  是否被网关映射  : {'是' if mapped else '否'}")
    print(f"  返回内容        : {raw.strip()[:80]}")
    print(f"  用量            : {usage}")
    assert calls == 1
    return {"requested": requested, "returned": returned, "mapped": mapped,
            "usage": usage, "content": raw.strip()[:200],
            "generator_alive": gen is not None}


def run_topics(base: Settings) -> list[dict]:
    print("=" * 78)
    print(f"三、{len(TOPICS)} 主题抽测（一次生成即可用 = 结构完好 + 未降级 + 无合规风险）")
    print("-" * 78)
    gen = sg.ScriptGenerator(base)
    SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    for i, (category, topic, minutes) in enumerate(TOPICS, 1):
        tag = f"{i:02d}_{_slug(topic)}"
        t0 = time.perf_counter()
        try:
            res = gen.generate(topic=topic, duration_min=minutes, style="")
            row = res.to_dict()
            row.update({"index": i, "category": category, "duration_min": minutes,
                        "ok": True})
            (SCRIPTS_DIR / f"{tag}.json").write_text(
                json.dumps({"result": row, "script": res.script.to_dict()},
                           ensure_ascii=False, indent=2), encoding="utf-8")
        except sg.ScriptGenError as exc:
            row = {"index": i, "category": category, "topic": topic,
                   "duration_min": minutes, "ok": False, "usable": False,
                   "error": f"{type(exc).__name__}: {exc}",
                   "elapsed_s": round(time.perf_counter() - t0, 2)}
            print(f"[{i:02d}] {category} 《{topic}》 -> 失败：{exc}")
        rows.append(row)

        if row.get("ok"):
            print(f"[{i:02d}] {category:4s} 《{topic}》 "
                  f"{row['line_count']:3d} 行 / {row['actual_words']:4d} 字"
                  f"（配额 {row['target_words']}，区间 {row['word_range']}）"
                  f" | 请求 {row['calls']} 次 纠错 {row['correction_rounds']} 轮"
                  f" | 降级={'Y' if row['degraded'] else 'N'} "
                  f"命中={'Y' if row['flagged'] else 'N'}"
                  f" | 模型 {row['model_requested']}→{row['model_returned']}"
                  f" | {row['elapsed_s']:.1f}s")
        sys.stdout.flush()
    return rows


def probe_error_paths(base: Settings) -> list[dict]:
    """错误路径可读性：401 / 连接失败 / 超时 / 400 —— 都要给出中文可读原因。

    说明：真实 429 无法稳定制造（需要服务端配合），其归类与提示由
    `tests/test_script_gen.py::test_rate_limit_is_readable_and_retried` 等用例覆盖；
    本函数用真实网络条件覆盖 401 / 连不上 / 超时 / 400 四条。
    """
    print("=" * 78)
    print("四、错误路径可读性（真实网络条件）")
    print("-" * 78)
    # 本机不可达端点的探测必须绕开系统代理，否则会被中间代理转成 502，
    # 测到的是「上游错误」而不是「连不上」（上一轮就是这么被误导的）。
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    cases: list[tuple[str, Settings, str]] = [
        ("401 无效 Key", Settings(llm_api_key="sk-definitely-invalid-key",
                                  llm_max_retry=0, script_self_check=False), "auth"),
        ("连接失败（本机端口不可达）", Settings(llm_base_url="http://127.0.0.1:9",
                                                llm_max_retry=0,
                                                script_self_check=False), "conn"),
        ("请求超时（1s 硬超时）", Settings(llm_timeout=1, llm_timeout_long=1,
                                          llm_max_retry=0, script_self_check=False), "timeout"),
        ("400 参数不合法（temperature=99）", Settings(llm_temperature=99.0,
                                                      llm_max_retry=0,
                                                      script_self_check=False), "badrequest"),
    ]
    out: list[dict] = []
    for label, settings, expect in cases:
        item = {"case": label, "expect": expect}
        t0 = time.perf_counter()
        try:
            sg.ScriptGenerator(settings).generate(topic="错误路径验证", target_words=80)
            item.update({"raised": None, "message": "（未抛异常 —— 与预期不符）",
                         "readable": False})
            print(f"  {label:28s} -> 未抛异常 ⚠️")
        except sg.ScriptGenError as exc:
            cls = type(exc).__name__
            item.update({"raised": cls, "message": str(exc),
                         "readable": bool(str(exc).strip()) and "Traceback" not in str(exc)})
            print(f"  {label:28s} -> {cls}: {exc}")
        item["elapsed_s"] = round(time.perf_counter() - t0, 2)
        out.append(item)
        sys.stdout.flush()
    return out


def probe_capacity(base: Settings) -> dict:
    """单次调用的产出容量：中文每字消耗多少 completion token，由此反推时长上限。

    这一项解释「为什么抽测不覆盖 8 分钟档」—— 是 max_tokens 的容量问题，
    不是生成质量问题（目标时长的分段生成方案放到 D5/D6 处理）。
    """
    print("=" * 78)
    print("五、单次调用容量探针（决定「一次能生成多长的脚本」）")
    settings = Settings(llm_max_retry=0, script_correction_retry=0,
                        script_self_check=False)
    try:
        res = sg.ScriptGenerator(settings).generate(topic="一次调用的产出容量探针",
                                                    target_words=390)
    except sg.ScriptGenError as exc:
        print(f"  探针失败：{exc}")
        return {"ok": False, "error": str(exc)}

    chars = max(1, res.script.total_chars)
    completion = max(1, res.usage.get("completion_tokens", 0))
    per_char = completion / chars
    ceiling = int(settings.llm_max_tokens / per_char)
    info = {
        "ok": True,
        "actual_words": res.script.total_chars,
        "completion_tokens": completion,
        "tokens_per_char": round(per_char, 3),
        "llm_max_tokens": settings.llm_max_tokens,
        "single_call_ceiling_words": ceiling,
        "single_call_ceiling_minutes": round(ceiling / settings.words_per_minute, 2),
    }
    print(f"  本次产出            : {chars} 字 / {completion} completion tokens")
    print(f"  每字 token 消耗      : {per_char:.2f}")
    print(f"  LLM_MAX_TOKENS       : {settings.llm_max_tokens}")
    print(f"  单次调用字数上限     : 约 {ceiling} 字"
          f"（≈ {info['single_call_ceiling_minutes']} 分钟 @ {settings.words_per_minute} 字/分）")
    return info


def main() -> int:
    _setup_logging()
    base = Settings()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    started = time.time()
    prompts = probe_prompts(base)
    mapping = probe_model_mapping(base)
    rows = run_topics(base)
    errors = probe_error_paths(base)
    capacity = probe_capacity(base)

    usable = [r for r in rows if r.get("usable")]
    first_pass = [r for r in rows if r.get("first_pass")]
    degraded = [r for r in rows if r.get("degraded")]
    flagged = [r for r in rows if r.get("flagged")]
    failed = [r for r in rows if not r.get("ok")]
    quota_ok = [r for r in rows if r.get("word_quota_ok")]
    devs = sorted(r.get("word_deviation", 0.0) for r in rows if r.get("ok"))
    usage = Counter()
    for r in rows:
        for k, v in (r.get("usage") or {}).items():
            usage[k] += v
    model_pairs = Counter(f"{r.get('model_requested')}→{r.get('model_returned')}"
                          for r in rows if r.get("ok"))

    def _median(xs: list[float]) -> float:
        if not xs:
            return 0.0
        mid = len(xs) // 2
        return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "settings": {
            "llm_base_url": base.llm_base_url,
            "llm_model": base.llm_model,
            "llm_reasoner_model": base.llm_reasoner_model,
            "llm_max_retry": base.llm_max_retry,
            "llm_temperature": base.llm_temperature,
            "llm_max_tokens": base.llm_max_tokens,
            "script_max_chars_per_line": base.script_max_chars_per_line,
            "script_word_tolerance": base.script_word_tolerance,
            "script_word_quota_enforce": base.script_word_quota_enforce,
            "script_require_alternating": base.script_require_alternating,
            "script_correction_retry": base.script_correction_retry,
            "script_self_check": base.script_self_check,
        },
        "prompts": prompts,
        "model_mapping": mapping,
        "topics": rows,
        "error_paths": errors,
        "capacity": capacity,
        "summary": {
            "total": len(rows),
            "usable": len(usable),
            "usable_rate": round(len(usable) / len(rows), 4) if rows else 0.0,
            "first_pass": len(first_pass),
            "first_pass_rate": round(len(first_pass) / len(rows), 4) if rows else 0.0,
            "degraded": len(degraded),
            "flagged": len(flagged),
            "failed": len(failed),
            "usable_target": USABLE_TARGET,
            "done_condition_met": len(usable) >= USABLE_TARGET,
            "word_quota_ok": len(quota_ok),
            "word_quota_rate": round(len(quota_ok) / len(rows), 4) if rows else 0.0,
            "word_deviation_min": round(devs[0], 4) if devs else 0.0,
            "word_deviation_median": round(_median(devs), 4),
            "word_deviation_max": round(devs[-1], 4) if devs else 0.0,
            "model_pairs": dict(model_pairs),
            "usage_total": dict(usage),
            "wall_clock_s": round(time.time() - started, 1),
        },
    }

    path = OUT_DIR / "verify_result.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 78)
    print("六、汇总")
    s = summary["summary"]
    print(f"  主题数                        : {s['total']}")
    print(f"  ① 一次生成即可用（结构+合规+硬约束）: {s['usable']}/{s['total']}"
          f"（{s['usable_rate']:.0%}，Done 条件 ≥ {USABLE_TARGET}/{s['total']}）"
          f" -> {'PASS' if s['done_condition_met'] else 'FAIL'}")
    print(f"     ├ 其中一次通过（无内部纠错）      : {s['first_pass']}")
    print(f"     └ 降级 / 合规命中 / 异常失败      : "
          f"{s['degraded']} / {s['flagged']} / {s['failed']}")
    print(f"  ② 字数配额达标（独立指标，不判可用）: {s['word_quota_ok']}/{s['total']}"
          f"（{s['word_quota_rate']:.0%}）偏差 中位 {s['word_deviation_median']:+.1%}，"
          f"区间 {s['word_deviation_min']:+.1%} ~ {s['word_deviation_max']:+.1%}，"
          f"强制判定={'开' if summary['settings']['script_word_quota_enforce'] else '关'}")
    print(f"  模型映射                      : {s['model_pairs']}")
    print(f"  累计 tokens                   : {s['usage_total']}")
    print(f"  错误路径可读                  : "
          f"{sum(1 for e in errors if e.get('readable'))}/{len(errors)}")
    if capacity.get("ok"):
        print(f"  单次调用容量                  : {capacity['tokens_per_char']} token/字 -> "
              f"约 {capacity['single_call_ceiling_words']} 字"
              f"（≈{capacity['single_call_ceiling_minutes']} 分钟）")
    print(f"  总耗时                        : {s['wall_clock_s']}s")
    print(f"  证据文件                      : {path}")
    print()
    print("  注：① 是本脚本的判定口径；② 为已知能力边界（模型无法稳定控制总字数，")
    print("     三轮提示词标定各 10/10 偏低）。需要按字数卡验收时置")
    print("     SCRIPT_WORD_QUOTA_ENFORCE=true，并先补「扩写」或分段生成。")
    return 0 if s["done_condition_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
