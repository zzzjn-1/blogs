# -*- coding: utf-8 -*-
"""script_gen / schemas 离线单测。

全部用**注入的假客户端**跑，不接触真实 DeepSeek API ——
CI 与本地都不应因为一次网络抖动而红。
"""
from __future__ import annotations

import json
import string
import types

import httpx
import openai
import pytest

from api.config import Settings
from api.schemas import PodcastScript, ScriptTurn, count_chars
from api.services import script_gen as sg


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #

def make_response(content: str, *, model: str = "deepseek-chat",
                  finish_reason: str = "stop", prompt_tokens: int = 100,
                  completion_tokens: int = 200) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        model=model,
        usage=types.SimpleNamespace(prompt_tokens=prompt_tokens,
                                    completion_tokens=completion_tokens,
                                    total_tokens=prompt_tokens + completion_tokens),
        choices=[types.SimpleNamespace(
            finish_reason=finish_reason,
            message=types.SimpleNamespace(content=content))])


class FakeClient:
    """最小 openai 客户端替身：只实现 script_gen 用到的那几个成员。"""

    def __init__(self, responder):
        self.responder = responder
        self.requests: list[dict] = []
        self.timeouts: list[float] = []
        self.chat = types.SimpleNamespace(completions=self)

    def with_options(self, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responder(kwargs)


def script_json(*, total_chars: int, lines: int = 10, alt: bool = True,
                title: str = "人工智能与日常生活的十个交集",
                summary: str = "本期从通勤、写作与购物三个场景聊人工智能带来的实际变化。",
                over_chars: int = 0, seqs: list[int] | None = None) -> str:
    """造一份合法 JSON 脚本。total_chars 均分到 lines 行；over_chars>0 时首行超长。"""
    per = total_chars // lines
    body = []
    for i in range(lines):
        speaker = ("A", "B")[i % 2] if alt else "A"
        text = "字" * (per + (over_chars if i == 0 else 0))
        body.append({"seq": i + 1, "speaker": speaker, "text": text})
    if seqs:
        for i, s in enumerate(seqs):
            body[i]["seq"] = s
    return json.dumps({"title": title, "summary": summary, "lines": body},
                      ensure_ascii=False)


def base_settings(**overrides) -> Settings:
    """测试用配置：关掉自检与重试等待，让每个用例都快且确定。"""
    defaults = dict(llm_max_retry=0, script_self_check=False,
                    script_correction_retry=2)
    defaults.update(overrides)
    return Settings(**defaults)


def http_status_error(cls, code: int, message: str):
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(code, request=request, json={"error": {"message": message}})
    return cls(message, response=response, body=None)


# --------------------------------------------------------------------------- #
# 字口径与结构校验
# --------------------------------------------------------------------------- #

def test_count_chars_ignores_whitespace_only():
    assert count_chars("你好，世界") == 5
    assert count_chars("你 好 ， 世 界") == 5
    assert count_chars("  \n\t ") == 0


def test_podcast_script_rejects_non_continuous_seq():
    with pytest.raises(Exception) as ei:
        PodcastScript.model_validate({
            "title": "标题", "summary": "",
            "lines": [{"seq": 1, "speaker": "A", "text": "你好"},
                      {"seq": 3, "speaker": "B", "text": "你也好"}],
        })
    assert "seq 必须从 1 连续递增" in str(ei.value)


def test_podcast_script_rejects_bad_speaker_and_newline():
    with pytest.raises(Exception):
        PodcastScript.model_validate({"title": "t", "summary": "",
                                      "lines": [{"seq": 1, "speaker": "C", "text": "x"}]})
    with pytest.raises(Exception):
        PodcastScript.model_validate({"title": "t", "summary": "",
                                      "lines": [{"seq": 1, "speaker": "A", "text": "上\n下"}]})


def test_check_policy_reports_each_violation():
    script = PodcastScript(
        title="标题", summary="",
        lines=[ScriptTurn(seq=1, speaker="A", text="字" * 41),
               ScriptTurn(seq=2, speaker="A", text="字" * 20)])
    rep = script.check_policy(max_chars=40, target_words=100, tolerance=0.10,
                              require_alternating=True)
    joined = " | ".join(rep.errors)
    assert "超过单行上限 40" in joined
    assert "全篇只出现说话人 A" in joined
    assert "说话人相同" in joined
    # 字数配额默认不判可用性，单列 quota 档
    assert "总字数 61 不在配额区间 90~110" in " | ".join(rep.quota)
    assert not rep.ok and rep.needs_retry
    # 关键字数配额即为硬约束时回到 errors
    strict = script.check_policy(max_chars=40, target_words=100, tolerance=0.10,
                                 require_alternating=True, enforce_word_quota=True)
    assert "总字数 61 不在配额区间 90~110" in " | ".join(strict.errors)


def test_word_quota_alone_does_not_make_report_not_ok():
    """配额单列一档是本模块的核心设计：它驱动重试，但不判「不可用」。"""
    script = PodcastScript(title="t", summary="",
                           lines=[ScriptTurn(seq=1, speaker="A", text="字" * 10),
                                  ScriptTurn(seq=2, speaker="B", text="字" * 10)])
    rep = script.check_policy(max_chars=40, target_words=1000, tolerance=0.10)
    assert rep.errors == [] and rep.quota and rep.ok and rep.needs_retry


def test_check_policy_caps_repeated_errors():
    lines = [ScriptTurn(seq=i, speaker="A", text="字" * 50) for i in range(1, 15)]
    rep = PodcastScript(title="t", summary="", lines=lines).check_policy(
        max_chars=40, require_alternating=False)
    assert any("另有" in e for e in rep.errors)
    assert len(rep.errors) <= 20


def test_as_turns_feeds_normalize():
    script = PodcastScript(title="t", summary="",
                           lines=[ScriptTurn(seq=1, speaker="A", text="你好"),
                                  ScriptTurn(seq=2, speaker="B", text="你好啊")])
    assert script.as_turns() == [{"speaker": "A", "text": "你好"},
                                 {"speaker": "B", "text": "你好啊"}]
    assert script.as_dialogue() == "A：你好\nB：你好啊"


# --------------------------------------------------------------------------- #
# 提示词一致性（启动期 fail-fast）
# --------------------------------------------------------------------------- #

def test_prompt_consistency_ok_by_default():
    assert sg.verify_prompt_consistency(Settings()) == []


def test_prompt_consistency_detects_line_limit_mismatch():
    s = Settings()
    p = sg.load_prompts(s)
    broken = sg.Prompts(system=p.system.replace("单行字符数上限 = 40", "单行字符数上限 = 50"),
                        user_template=p.user_template,
                        compliance_system=p.compliance_system, dir=p.dir)
    problems = sg.verify_prompt_consistency(s, broken)
    assert any("单行上限不一致" in x for x in problems)


def test_generator_refuses_to_start_on_inconsistent_prompt():
    s = Settings()
    p = sg.load_prompts(s)
    broken = sg.Prompts(system=p.system.replace("说话人必须交替出现", "尽量交替"),
                        user_template=p.user_template,
                        compliance_system=p.compliance_system, dir=p.dir)
    with pytest.raises(sg.PromptInconsistent) as ei:
        sg.ScriptGenerator(s, prompts=broken)
    assert "交替" in str(ei.value)


def test_prompt_consistency_reports_missing_placeholder():
    s = Settings()
    p = sg.load_prompts(s)
    broken = sg.Prompts(system=p.system,
                        user_template=string.Template("主题：${topic}"),
                        compliance_system=p.compliance_system, dir=p.dir)
    problems = sg.verify_prompt_consistency(s, broken)
    assert any("$target_words" in x for x in problems)


# --------------------------------------------------------------------------- #
# 提示词缓存前提：系统提示词逐字节稳定、且不含用户变量
# --------------------------------------------------------------------------- #

def test_system_prompt_is_byte_stable_across_requests():
    s = base_settings()

    def responder(kwargs):
        return make_response(script_json(total_chars=200))

    client = FakeClient(responder)
    gen = sg.ScriptGenerator(s, client=client)
    gen.generate(topic="人工智能与通勤", target_words=200)
    gen.generate(topic="城市夜跑的正确姿势", target_words=200, style="犀利")

    first, second = client.requests[0]["messages"], client.requests[1]["messages"]
    assert first[0]["content"] == second[0]["content"], "系统提示词必须逐字节稳定"
    assert "人工智能与通勤" not in first[0]["content"]
    assert "城市夜跑的正确姿势" not in second[0]["content"]
    assert "人工智能与通勤" in first[1]["content"]
    assert "城市夜跑的正确姿势" in second[1]["content"]
    # JSON 模式要求提示词里有 json 字样，否则 API 直接拒绝该参数
    assert "json" in first[0]["content"].lower()


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #

def test_generate_happy_path_single_call():
    s = base_settings()
    client = FakeClient(lambda kw: make_response(script_json(total_chars=200)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)

    assert res.usable and res.first_pass
    assert res.calls == 1 and res.correction_rounds == 0
    assert res.script.total_chars == 200
    assert res.model_requested == "deepseek-chat"
    assert res.model_returned == "deepseek-chat"
    assert res.model_mapped is False
    assert res.usage["total_tokens"] == 300
    assert client.requests[0]["response_format"] == {"type": "json_object"}
    assert client.requests[0]["max_tokens"] == s.llm_max_tokens


def test_model_mapping_is_recorded_and_flagged():
    """网关把 deepseek-chat 映射成 deepseek-flash —— 必须如实记录，不能以请求参数为准。"""
    s = base_settings()
    client = FakeClient(lambda kw: make_response(script_json(total_chars=200),
                                                 model="deepseek-flash"))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.model_requested == "deepseek-chat"
    assert res.model_returned == "deepseek-flash"
    assert res.model_mapped is True


def test_duration_is_converted_to_word_quota():
    s = base_settings()
    client = FakeClient(lambda kw: make_response(script_json(total_chars=260)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="城市夜跑", duration_min=1.0)
    assert res.target_words == s.words_per_minute
    assert res.word_range == (234, 286)
    assert res.usable


def test_long_script_uses_long_timeout():
    s = base_settings()
    client = FakeClient(lambda kw: make_response(script_json(total_chars=2600, lines=65)))
    sg.ScriptGenerator(s, client=client).generate(topic="长主题", target_words=2600)
    assert client.timeouts[0] == float(s.llm_timeout_long)


# --------------------------------------------------------------------------- #
# 纠错重试
# --------------------------------------------------------------------------- #

def test_policy_violation_triggers_correction_with_specific_error():
    s = base_settings()
    # 每行 40 字正好卡在上限，首行多 1 字 → 只触发「单行超长」这一类错误
    bad = script_json(total_chars=200, lines=5, over_chars=1)
    good = script_json(total_chars=200, lines=5)
    seq = iter([bad, good])
    client = FakeClient(lambda kw: make_response(next(seq)))

    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)

    assert res.usable and res.correction_rounds == 1 and res.calls == 2
    assert res.errors == []
    correction_msgs = client.requests[1]["messages"]
    assert correction_msgs[2]["role"] == "assistant"
    assert correction_msgs[2]["content"] == bad
    assert correction_msgs[3]["role"] == "user"
    assert "超过单行上限 40" in correction_msgs[3]["content"]
    # 纠错轮必须保住稳定前缀（否则提示词缓存全废）
    assert correction_msgs[0]["content"] == client.requests[0]["messages"][0]["content"]


def test_correction_message_reports_deficit():
    """只说区间、不说差值，模型每轮只补几十字（实测 452→459 仍不达标）；必须给出差值。"""
    msg = sg.build_correction_message(["总字数 452 不在配额区间 468~572"], max_chars=40,
                                      target_words=520, tolerance=0.10, actual_words=452)
    assert "468 至 572 字之间" in msg
    assert "还差 16 字" in msg
    assert "重新输出一份完整的 JSON" in msg


def test_correction_message_reports_surplus():
    msg = sg.build_correction_message(["x"], max_chars=40, target_words=200,
                                      tolerance=0.10, actual_words=300)
    assert "超出 80 字" in msg


def test_correction_message_repeats_line_limit_and_alternation():
    msg = sg.build_correction_message(["x"], max_chars=40, target_words=None,
                                      tolerance=0.10)
    assert "每行不超过 40 个字符" in msg
    assert "交替" in msg


def test_schema_failure_also_triggers_correction():
    s = base_settings()
    seq = iter(["这不是 JSON", script_json(total_chars=200)])
    client = FakeClient(lambda kw: make_response(next(seq)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.usable and res.correction_rounds == 1
    assert "不是合法 JSON" in client.requests[1]["messages"][3]["content"]


def test_fenced_json_is_accepted():
    s = base_settings()
    fenced = "```json\n" + script_json(total_chars=200) + "\n```"
    client = FakeClient(lambda kw: make_response(fenced))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.usable and res.correction_rounds == 0


def test_wrapped_json_is_unwrapped():
    s = base_settings()
    wrapped = json.dumps({"script": json.loads(script_json(total_chars=200))},
                         ensure_ascii=False)
    client = FakeClient(lambda kw: make_response(wrapped))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.usable


def test_retry_exhausted_but_structurally_valid_is_delivered_with_quota_warning():
    """字数配额始终不达标：结构是好的，不该被丢进正则降级 —— 原样交付并如实上报偏差。"""
    s = base_settings(script_correction_retry=1)
    client = FakeClient(lambda kw: make_response(script_json(total_chars=400)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.degraded is False
    assert res.errors == [], "配额未达不属硬约束"
    assert any("总字数 400 不在配额区间" in w for w in res.warnings)
    assert res.word_quota_ok is False
    assert res.word_deviation == 1.0            # 比值：400 / 200 - 1
    assert res.word_deviation_pct == 100.0      # 百分数（曾经这里直接返回比值，差 100 倍）
    assert res.usable is True
    assert res.calls == 2, "配额仍应驱动纠错重试（尽力补足）"


def test_word_quota_enforced_makes_result_unusable():
    """置 SCRIPT_WORD_QUOTA_ENFORCE=true 时，配额回到硬约束语义。"""
    s = base_settings(script_correction_retry=1, script_word_quota_enforce=True)
    client = FakeClient(lambda kw: make_response(script_json(total_chars=400)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.errors and "总字数 400 不在配额区间" in res.errors[0]
    assert res.usable is False


def test_word_quota_ok_is_reported_on_happy_path():
    s = base_settings()
    client = FakeClient(lambda kw: make_response(script_json(total_chars=200)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.word_quota_ok is True
    assert res.word_deviation == 0.0 and res.word_deviation_pct == 0.0
    assert res.to_dict()["avg_chars_per_line"] == 20.0


def test_word_deviation_ratio_and_percent_are_consistent():
    """比值与百分数必须只差一个 100 的因子 —— 这条守住「命名与量纲一致」。"""
    s = base_settings()
    client = FakeClient(lambda kw: make_response(script_json(total_chars=170)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.word_deviation == -0.15
    assert res.word_deviation_pct == -15.0
    d = res.to_dict()
    assert d["word_deviation"] == -0.15 and d["word_deviation_pct"] == -15.0


# --------------------------------------------------------------------------- #
# 正则降级
# --------------------------------------------------------------------------- #

def test_degrades_to_loose_parse_when_structure_never_parses():
    s = base_settings()
    plain = "\n".join(f"{'A' if i % 2 == 1 else 'B'}：这是第{i}句纯文本台词"
                      for i in range(1, 7))
    client = FakeClient(lambda kw: make_response(plain))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)

    assert res.degraded is True
    assert res.usable is False
    assert len(res.script.lines) == 6
    assert res.script.lines[0].speaker == "A"
    assert res.script.lines[1].speaker == "B"
    assert any("降级" in w for w in res.warnings)


def test_hopeless_output_raises_schema_error():
    s = base_settings()
    client = FakeClient(lambda kw: make_response("完全不是脚本的一段散文。"))
    with pytest.raises(sg.ScriptSchemaError) as ei:
        sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert "结构化输出不可用" in str(ei.value)


def test_parse_loose_rejects_single_speaker():
    assert sg.parse_loose("A：只有一个人说话\nA：还是我") is None
    assert sg.parse_loose("没有任何台词") is None


def test_parse_loose_reads_markdown_and_title():
    raw = "# 标题：城市夜跑的十个细节\n**A**：先热身再跑。\n- B：跑鞋别太软。\n"
    script = sg.parse_loose(raw)
    assert script is not None
    assert [ln.speaker for ln in script.lines] == ["A", "B"]
    assert script.title


# --------------------------------------------------------------------------- #
# 传输层错误：可读化 + 是否重试
# --------------------------------------------------------------------------- #

def test_rate_limit_is_readable_and_retried():
    s = base_settings(llm_max_retry=1, llm_timeout=1)
    state = {"n": 0}

    def responder(kw):
        state["n"] += 1
        if state["n"] == 1:
            raise http_status_error(openai.RateLimitError, 429, "rate limit exceeded")
        return make_response(script_json(total_chars=200))

    client = FakeClient(responder)
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.usable
    assert res.calls == 2, "429 属可重试错误，应退避后重试"


def test_rate_limit_exhausted_gives_actionable_message():
    s = base_settings(llm_max_retry=0)

    def responder(kw):
        raise http_status_error(openai.RateLimitError, 429, "rate limit exceeded")

    with pytest.raises(sg.LLMRateLimited) as ei:
        sg.ScriptGenerator(s, client=FakeClient(responder)).generate(
            topic="人工智能", target_words=200)
    assert "429" in str(ei.value) and "退避重试" in str(ei.value)


def test_insufficient_balance_402_is_mapped():
    s = base_settings(llm_max_retry=0)

    def responder(kw):
        raise http_status_error(openai.APIStatusError, 402, "Insufficient Balance")

    with pytest.raises(sg.LLMRateLimited) as ei:
        sg.ScriptGenerator(s, client=FakeClient(responder)).generate(
            topic="人工智能", target_words=200)
    assert "余额不足" in str(ei.value)


def test_timeout_is_retryable_and_readable():
    s = base_settings(llm_max_retry=1, llm_timeout=3)
    state = {"n": 0}

    def responder(kw):
        state["n"] += 1
        if state["n"] == 1:
            raise openai.APITimeoutError(
                httpx.Request("POST", "https://api.deepseek.com/chat/completions"))
        return make_response(script_json(total_chars=200))

    res = sg.ScriptGenerator(s, client=FakeClient(responder)).generate(
        topic="人工智能", target_words=200)
    assert res.usable and res.calls == 2


def test_connection_error_points_at_the_proxy(monkeypatch):
    """本机默认挂着 HTTP(S)_PROXY；代理返回 502 时 SDK 只报 "Connection error."。

    实测（D5 端到端）就栽在这上面：不点出代理，用户会去查「网线」而不是查代理。
    """
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:63446")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:63446")
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")

    def responder(kw):
        raise openai.APIConnectionError(request=request)

    with pytest.raises(sg.LLMRetryable) as ei:
        sg.ScriptGenerator(base_settings(), client=FakeClient(responder)).generate(
            topic="人工智能", target_words=200)
    msg = str(ei.value)
    assert "无法连接 DeepSeek" in msg
    assert "HTTPS_PROXY" in msg and "127.0.0.1:63446" in msg
    assert "NO_PROXY" in msg, "必须给出可直接照做的绕行方式"


def test_proxy_hint_is_silent_without_proxy_env(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    assert sg._proxy_hint() == "", "没配代理就不要凭空提示代理"


def test_proxy_credentials_are_masked(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:s3cret@proxy.local:8080")
    hint = sg._proxy_hint()
    assert "s3cret" not in hint and "alice" not in hint
    assert "***@proxy.local:8080" in hint


def test_auth_error_is_not_retried():
    s = base_settings(llm_max_retry=2)

    def responder(kw):
        raise http_status_error(openai.AuthenticationError, 401, "invalid api key")

    client = FakeClient(responder)
    with pytest.raises(sg.LLMAuthError) as ei:
        sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert "401" in str(ei.value) and "重新签发" in str(ei.value)
    assert len(client.requests) == 1, "401 重试没有意义，不应退避重试"


def test_missing_api_key_fails_fast():
    s = base_settings(llm_api_key="")
    with pytest.raises(sg.LLMAuthError) as ei:
        sg.ScriptGenerator(s).generate(topic="人工智能", target_words=200)
    assert "未配置 LLM_API_KEY" in str(ei.value)


def test_truncated_output_error_is_informative():
    s = base_settings(llm_max_retry=0)
    client = FakeClient(lambda kw: make_response("{}", finish_reason="length"))
    with pytest.raises(sg.LLMError) as ei:
        sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert "截断" in str(ei.value)


def test_empty_topic_rejected():
    s = base_settings()
    with pytest.raises(sg.ScriptGenError):
        sg.ScriptGenerator(s, client=FakeClient(lambda kw: None)).generate(topic="   ")


# --------------------------------------------------------------------------- #
# 敏感词与合规自检
# --------------------------------------------------------------------------- #

def test_load_and_scan_sensitive_words():
    s = Settings()
    words = sg.load_sensitive_words(s.sensitive_dict_path)
    assert words, "基线词库不应为空"
    assert all(w.group and w.word for w in words)
    hits = sg.scan_sensitive("他偶尔上一次 网络赌博 的平台", words)
    assert [h.word for h in hits] == ["网络赌博"], "插空写法也应命中"
    assert hits[0].group == "赌博"
    assert sg.scan_sensitive("今天天气不错，适合跑步", words) == []


def test_missing_sensitive_dict_only_warns(tmp_path):
    assert sg.load_sensitive_words(tmp_path / "nope.txt") == []


def test_sensitive_hit_flags_and_skips_self_check():
    s = base_settings(script_self_check=True)
    texts = ["有人靠网络赌博赚钱吗", "这种想法非常危险，千万别碰"]
    payload = json.dumps({
        "title": "城市夜跑的十个细节",
        "summary": "本期聊聊夜跑装备、配速与安全这三件最容易踩坑的事。",
        "lines": [{"seq": 1, "speaker": "A", "text": texts[0]},
                  {"seq": 2, "speaker": "B", "text": texts[1]}],
    }, ensure_ascii=False)
    client = FakeClient(lambda kw: make_response(payload))
    # 字数配额取实际字数，使本用例只检验「敏感词短路自检」这一件事
    target = sum(count_chars(t) for t in texts)
    res = sg.ScriptGenerator(s, client=client).generate(topic="夜跑", target_words=target)

    assert res.flagged is True
    assert [h.word for h in res.sensitive_hits] == ["网络赌博"]
    assert res.self_check is None
    assert len(client.requests) == 1, "词库已判风险时应短路掉自检调用"


def test_self_check_unsafe_flags_result():
    s = base_settings(script_self_check=True)

    def responder(kw):
        if "合规审查员" in kw["messages"][0]["content"]:
            return make_response(json.dumps({"safe": False, "risk": "high",
                                             "reason": "出现具体疗效承诺"},
                                            ensure_ascii=False))
        return make_response(script_json(total_chars=200))

    res = sg.ScriptGenerator(s, client=FakeClient(responder)).generate(
        topic="人工智能", target_words=200)
    assert res.flagged is True
    assert res.self_check == {"safe": False, "risk": "high", "reason": "出现具体疗效承诺"}
    assert res.usable is False


def test_self_check_safe_result_keeps_result_usable():
    s = base_settings(script_self_check=True)

    def responder(kw):
        if "合规审查员" in kw["messages"][0]["content"]:
            return make_response(json.dumps({"safe": True, "risk": "none",
                                             "reason": "内容为常识性讨论，无风险点"},
                                            ensure_ascii=False))
        return make_response(script_json(total_chars=200))

    res = sg.ScriptGenerator(s, client=FakeClient(responder)).generate(
        topic="人工智能", target_words=200)
    assert res.flagged is False and res.usable is True
    assert res.self_check["safe"] is True


def test_self_check_string_false_is_not_treated_as_true():
    """形如 "false" 的字符串必须判为不安全 —— bool("false") 是 True，是个经典陷阱。"""
    s = base_settings(script_self_check=True)

    def responder(kw):
        if "合规审查员" in kw["messages"][0]["content"]:
            return make_response(json.dumps({"safe": "false", "risk": "low",
                                             "reason": "含未标注的推测性数据"},
                                            ensure_ascii=False))
        return make_response(script_json(total_chars=200))

    res = sg.ScriptGenerator(s, client=FakeClient(responder)).generate(
        topic="人工智能", target_words=200)
    assert res.flagged is True


def test_self_check_failure_does_not_break_generation():
    s = base_settings(script_self_check=True)

    def responder(kw):
        if "合规审查员" in kw["messages"][0]["content"]:
            raise http_status_error(openai.APIStatusError, 500, "boom")
        return make_response(script_json(total_chars=200))

    res = sg.ScriptGenerator(s, client=FakeClient(responder)).generate(
        topic="人工智能", target_words=200)
    assert res.self_check is None
    assert res.self_check_error
    assert res.usable is True, "自检失败不应阻断主流程"


def test_extra_fields_are_reported_as_warning():
    s = base_settings()
    payload = json.loads(script_json(total_chars=200))
    payload["speaker_names"] = {"A": "小林"}
    client = FakeClient(lambda kw: make_response(json.dumps(payload, ensure_ascii=False)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    assert res.usable
    assert any("多余字段" in w for w in res.warnings)


def test_result_to_dict_is_json_serializable():
    s = base_settings()
    client = FakeClient(lambda kw: make_response(script_json(total_chars=200)))
    res = sg.ScriptGenerator(s, client=client).generate(topic="人工智能", target_words=200)
    dumped = json.dumps(res.to_dict(), ensure_ascii=False)
    assert json.loads(dumped)["usable"] is True
