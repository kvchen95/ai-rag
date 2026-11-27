"""生成层的解析契约（详细设计.md §9.2 / 附录 D.1）。

这个文件钉的是"模型说什么算数、说什么不算数"。它是这套系统里最容易被悄悄放宽的
一层：把"缺 citations"当成"模型忘了标"从而补一个空数组，整套拒答机制就废了，
而所有其他用例仍然全绿。
"""

from __future__ import annotations

import pytest

from api.generate.judge import build_judge_messages, judge, parse_verdict
from api.generate.llm import (
    GENERATION_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    DraftFormatError,
    LlmClient,
    build_generation_messages,
    parse_draft,
    strip_code_fence,
)


# --- 解析 §9.2 的四行表格 -----------------------------------------------------


def test_plain_json_is_parsed() -> None:
    draft = parse_draft(
        '{"status":"answered","answer":"每人每天 500 元[1]。","facts":[{"text":"每人每天 500 元","evidence_ids":["1"]}]}'
    )
    assert draft.status == "answered"
    assert draft.facts[0].evidence_ids == ("1",)
    assert draft.facts[0].text == "每人每天 500 元"
    assert draft.unwrapped is False


def test_code_fence_is_unwrapped_once() -> None:
    draft = parse_draft('```json\n{"status":"unknown","answer":"未找到。","facts":[]}\n```')
    assert draft.status == "unknown"
    assert draft.unwrapped is True, "拨过围栏要留痕：模型总在包围栏是提示词退化的信号"


def test_surrounding_prose_is_ignored_only_after_unwrapping() -> None:
    draft = parse_draft('以下是 JSON：\n```json\n{"status":"unknown","answer":"x","facts":[]}\n```')
    assert draft.status == "unknown"


def test_non_json_is_a_format_error() -> None:
    with pytest.raises(DraftFormatError):
        parse_draft("我认为是 500 元。")


def test_missing_status_is_a_format_error() -> None:
    with pytest.raises(DraftFormatError):
        parse_draft('{"answer":"x","citations":[1]}')


def test_missing_facts_field_is_a_format_error() -> None:
    """§9.2：缺 facts → 降级，不重试。"""
    with pytest.raises(DraftFormatError):
        parse_draft('{"status":"answered","answer":"500 元[1]。"}')


def test_answered_with_empty_answer_is_a_format_error() -> None:
    with pytest.raises(DraftFormatError):
        parse_draft('{"status":"answered","facts":[{"text":"   ","evidence_ids":["1"]}]}')


def test_answered_without_evidence_ids_is_a_format_error() -> None:
    """缺 evidence_ids 的 answered 输出被判定为格式错误。"""
    with pytest.raises(DraftFormatError):
        parse_draft('{"status":"answered","facts":[{"text":"500 元。","evidence_ids":[]}]}')


def test_out_of_range_citation_is_left_to_the_first_check() -> None:
    """§9.2 表格最后一行："引用编号超出证据范围 → 校验第一道即拒答"。

    解析层不知道证据范围，所以它必须放行——在这里自己判等于把第一道校验做进解析器，
    而那样第一道就永远不会被执行到（它会被"已经拦过"的理由跳过）。
    """
    draft = parse_draft('{"status":"answered","facts":[{"text":"x[9]。","evidence_ids":["9"]}]}')
    assert draft.facts[0].evidence_ids == ("9",)


def test_bool_is_not_accepted_as_a_citation_number() -> None:
    """`True` 是 int 的子类：不排除 bool 的话 `[true]` 会变成 `[1]`。"""
    with pytest.raises(DraftFormatError):
        parse_draft('{"status":"answered","facts":[{"text":"x[1]","evidence_ids":[true]}]}')


def test_citation_strings_that_are_digits_are_accepted() -> None:
    """模型偶尔把编号写成字符串；语义明确，认。"""
    draft = parse_draft('{"status":"answered","facts":[{"text":"x[1]","evidence_ids":["1"]}]}')
    assert draft.facts[0].evidence_ids == ("1",)


def test_duplicate_citations_are_deduplicated_in_order() -> None:
    draft = parse_draft('{"status":"answered","facts":[{"text":"x[1][2]","evidence_ids":[2,1,2]}]}')
    assert draft.facts[0].evidence_ids == ("2", "1")


def test_facts_are_optional_for_unknown() -> None:
    draft = parse_draft('{"status":"unknown","answer":"未找到。","facts":[]}')
    assert draft.facts == ()


def test_fact_without_evidence_ids_is_a_format_error() -> None:
    with pytest.raises(DraftFormatError):
        parse_draft(
            '{"status":"answered","facts":[{"text":"没引用","evidence_ids":[]}]}'
        )


def test_empty_draft_raw_is_kept_for_audit() -> None:
    draft = parse_draft('{"status":"unknown","answer":"未找到。","facts":[]}')
    assert draft.raw


# --- 提示词是常量、不是模板 ---------------------------------------------------


def test_generation_prompt_keeps_the_three_hard_constraints() -> None:
    """附录 D："三条不可弱化：只用证据、无证据说不知道、每句带引用。" """
    assert "只使用证据包内容" in GENERATION_SYSTEM_PROMPT
    assert "status=unknown" in GENERATION_SYSTEM_PROMPT
    assert "facts" in GENERATION_SYSTEM_PROMPT
    assert "evidence_ids" in GENERATION_SYSTEM_PROMPT
    assert '"status":"answered|unknown"' in GENERATION_SYSTEM_PROMPT


def test_judge_prompt_only_asks_for_a_verdict() -> None:
    assert "SUPPORTED" in JUDGE_SYSTEM_PROMPT
    assert "NOT_SUPPORTED" in JUDGE_SYSTEM_PROMPT
    assert '"verdict"' in JUDGE_SYSTEM_PROMPT


def test_messages_are_three_fixed_segments() -> None:
    messages = build_generation_messages("几点了", "[1] 来源：x\n正文")
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == GENERATION_SYSTEM_PROMPT
    assert "【证据包】" in messages[1]["content"]
    assert "【问题】" in messages[1]["content"]
    assert messages[1]["content"].rstrip().endswith("只输出 JSON。")


def test_judge_messages_carry_only_question_evidence_answer() -> None:
    """附录 D.4："判别器只吃'问题+被引证据+答案'，不吃整库上下文"。"""
    messages = build_judge_messages("住宿标准", "[1] 证据", "500 元[1]")
    body = messages[1]["content"]
    assert body.count("【") == 3
    assert "【问题】" in body and "【证据】" in body and "【答案】" in body


# --- 判别器 -------------------------------------------------------------------


def test_judge_verdict_json_is_parsed() -> None:
    assert parse_verdict('{"verdict":"SUPPORTED","reason":"直接引用"}') == ("SUPPORTED", "直接引用")


def test_judge_bare_verdict_word_is_accepted() -> None:
    """通用模型不一定守 JSON；整句只给结论时语义与 JSON 一致，按字面认。"""
    assert parse_verdict("NOT_SUPPORTED")[0] == "NOT_SUPPORTED"


def test_not_supported_is_checked_before_supported() -> None:
    """`NOT_SUPPORTED` 含 `SUPPORTED` 子串：顺序错了会把"不支持"读成"支持"。"""
    assert parse_verdict('{"verdict":"NOT_SUPPORTED"}')[0] == "NOT_SUPPORTED"


def test_ambiguous_judge_output_is_a_format_error() -> None:
    with pytest.raises(ValueError):
        parse_verdict("可能是 SUPPORTED 也可能 NOT_SUPPORTED")


def test_garbage_judge_output_is_a_format_error() -> None:
    with pytest.raises(ValueError):
        parse_verdict("我无法判断")


def test_missing_judge_client_yields_skipped_not_unsupported() -> None:
    """没配模型 → `skipped`。与"判了不支持"必须能分辨：一个是部署缺配置，一个是内容问题。"""
    result = judge(None, "q", "ev", "ans")
    assert result.verdict == "skipped"
    assert result.skipped is True
    assert result.supported is False


class _BrokenClient(LlmClient):
    """连接失败 / 5xx 的替身。"""

    def __init__(self) -> None:  # noqa: D107 - 只用于测试
        pass

    def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
        raise __import__("api.generate.llm", fromlist=["GenerationUnavailable"]).GenerationUnavailable(
            "boom"
        )


class _GarbageClient(LlmClient):
    def __init__(self) -> None:  # noqa: D107
        pass

    def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
        return "我有点不确定"


class _OkClient(LlmClient):
    def __init__(self, verdict: str) -> None:  # noqa: D107
        self.verdict = verdict
        self.calls: list[float] = []

    def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
        self.calls.append(temperature)
        return f'{{"verdict":"{self.verdict}","reason":"r"}}'


def test_judge_unavailable_is_not_supported_fail_closed() -> None:
    """§10.1 第四道："超时/格式错按不支持"——这是 Fail-Closed。"""
    result = judge(_BrokenClient(), "q", "ev", "ans")
    assert result.verdict == "NOT_SUPPORTED"
    assert result.skipped is False
    assert "判别器不可用" in result.reason


def test_judge_garbage_output_is_not_supported() -> None:
    result = judge(_GarbageClient(), "q", "ev", "ans")
    assert result.verdict == "NOT_SUPPORTED"
    assert "无法解析" in result.reason


def test_judge_always_runs_at_temperature_zero() -> None:
    """§9.2 / 附录 D：生成与判别都用 Temperature = 0。"""
    client = _OkClient("SUPPORTED")
    judge(client, "q", "ev", "ans")
    assert client.calls == [0.0]


# --- 围栏拨除的两个方向 -------------------------------------------------------


def test_strip_code_fence_reports_whether_it_changed_anything() -> None:
    assert strip_code_fence('{"a":1}') == ('{"a":1}', False)
    assert strip_code_fence('```json\n{"a":1}\n```')[1] is True


def test_braces_inside_plain_prose_still_fail_to_parse() -> None:
    """正文里恰好有花括号时，取出来的片段一样解析失败——与不取的结果相同。

    这条钉的是"宽松解析不能变成'什么都收'"：`按 {标准} 执行` 取出的 `{标准}`
    不是 JSON，必须抛格式错，而不是被当成一个空的草稿放过去。
    """
    body, _ = strip_code_fence("按 {标准} 执行")
    assert body == "{标准}"
    with pytest.raises(DraftFormatError):
        parse_draft("按 {标准} 执行")
