"""五道校验与三档降级（详细设计.md §10.1 / §10.3）。

按 facts 单一事实源契约校验：Fact(text, evidence_ids)。
"""

from __future__ import annotations

import json

import pytest

from api.errors import ErrorCode
from api.generate.evidence import build_evidence_pack
from api.generate.llm import Draft, Fact, LlmClient, parse_draft
from api.retrieval.hybrid import Candidate
from api.retrieval.texts import ChunkText
from api.verify.checks import (
    CHECK_CITATION_RANGE,
    CHECK_CONSISTENCY,
    CHECK_DRAFT_STATUS,
    CHECK_EVERY_CLAIM,
    CHECK_GROUNDING,
    CHECK_NLI,
    CHECKS,
    FALLBACK_ORIGINAL_ONLY,
    TIER_BY_CHECK,
    check_citation_range,
    check_consistency,
    check_every_claim_cited,
    check_grounding,
    verify_draft,
)


def _cand(
    chunk_id: str,
    *,
    doc_key: str = "policy/travel",
    page_num: int = 47,
    score: float = 0.62,
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=f"{doc_key}#v1",
        chunk_type="text",
        page_num=page_num,
        rerank_score=score,
        fused_rank=1,
    )


def _text(
    chunk_id: str,
    body: str,
    *,
    doc_key: str = "policy/travel",
    page_num: int = 47,
) -> ChunkText:
    return ChunkText(
        chunk_id=chunk_id,
        doc_key=doc_key,
        doc_name="差旅管理办法-2025版.pdf",
        page_num=page_num,
        bbox=[[0.1, 0.2, 0.9, 0.3]],
        text=body,
        chunk_type="text",
    )


@pytest.fixture
def pack():
    return build_evidence_pack(
        [_cand("ch_a"), _cand("ch_b", page_num=48, score=0.55)],
        {
            "ch_a": _text(
                "ch_a", "住宿费标准：每人每天 500 元，按职级限额报销。"
            ),
            "ch_b": _text("ch_b", "市内交通费单次不超过 100 元。", page_num=48),
        },
        max_chars=6000,
    )


def _draft(*facts: Fact, status: str = "answered") -> Draft:
    return Draft(status=status, facts=tuple(facts))  # type: ignore[arg-type]


class _Judge(LlmClient):
    def __init__(self, verdict: str = "SUPPORTED") -> None:
        self.verdict = verdict
        self.temperatures: list[float] = []
        self.calls: int = 0
        self.messages: list[list[dict[str, str]]] = []

    def complete(self, messages, *, temperature: float = 0.0) -> str:
        self.calls += 1
        self.temperatures.append(temperature)
        self.messages.append(messages)
        return f'{{"verdict":"{self.verdict}","reason":"stub"}}'


# --- 第一道：引用的 evidence_id 存在
# -------------------------------------------


def test_first_check_passes_when_every_evidence_id_exists(pack) -> None:
    draft = _draft(Fact("每人每天 500 元", ("ch_a",)))
    assert check_citation_range(draft, pack) is None


def test_first_check_rejects_unknown_evidence_id(pack) -> None:
    draft = _draft(Fact("每人每天 500 元", ("ch_z",)))
    failure = check_citation_range(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_CITATION_RANGE
    assert "ch_z" in failure.offenders


def test_first_check_scans_all_facts(pack) -> None:
    draft = _draft(
        Fact("每人每天 500 元", ("ch_a",)),
        Fact("交通费 100 元", ("ch_missing",)),
    )
    failure = check_citation_range(draft, pack)
    assert failure is not None
    assert "ch_missing" in failure.offenders


# --- 第二道：每条 fact 至少一个 evidence_id
# ------------------------------------


def test_second_check_passes_when_every_fact_has_evidence(pack) -> None:
    draft = _draft(
        Fact("每人每天 500 元", ("ch_a",)),
        Fact("交通费 100 元", ("ch_b",)),
    )
    assert check_every_claim_cited(draft, pack) is None


def test_second_check_rejects_fact_without_evidence_ids(pack) -> None:
    draft = _draft(Fact("每人每天 500 元", ()))
    failure = check_every_claim_cited(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_EVERY_CLAIM
    assert any("每人每天 500 元" in item for item in failure.offenders)


def test_second_check_rejects_no_facts(pack) -> None:
    draft = _draft(status="answered")
    assert check_every_claim_cited(draft, pack) is not None


def test_second_check_rejects_empty_fact_text(pack) -> None:
    draft = _draft(Fact("   ", ()))
    assert check_every_claim_cited(draft, pack) is not None


# --- 第三道：数字与专名可在被引证据里定位 -------------------------------------


def test_third_check_passes_when_numbers_are_grounded(pack) -> None:
    draft = _draft(Fact("住宿费标准为每人每天 500 元", ("ch_a",)))
    assert check_grounding(draft, pack) is None


def test_third_check_rejects_a_number_not_in_the_evidence(pack) -> None:
    draft = _draft(Fact("住宿费标准一般是 400 元", ("ch_a",)))
    failure = check_grounding(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_GROUNDING
    assert any("400" in item for item in failure.offenders)


def test_third_check_checks_only_the_cited_evidence(pack) -> None:
    draft = _draft(Fact("住宿费标准为每人每天 500 元", ("ch_b",)))
    failure = check_grounding(draft, pack)
    assert failure is not None
    assert any("500" in item for item in failure.offenders)


def test_third_check_rejects_an_unlocatable_clause_number(pack) -> None:
    draft = _draft(Fact("依据第十九条，住宿费标准为每人每天 500 元", ("ch_a",)))
    failure = check_grounding(draft, pack)
    assert failure is not None
    assert any("第十九条" in item for item in failure.offenders)


def test_third_check_accepts_chinese_number_written_differently(pack) -> None:
    draft = _draft(Fact("住宿费标准为每人每天五百元", ("ch_a",)))
    assert check_grounding(draft, pack) is None


# --- 第四道：判别器 -----------------------------------------------------------


def test_fourth_check_passes_when_judge_says_supported(pack) -> None:
    report = verify_draft(
        _draft(Fact("每人每天 500 元", ("ch_a",))),
        pack,
        question="住宿标准",
        judge_client=_Judge("SUPPORTED"),
    )
    assert report.passed is True
    assert report.gate2_verdict == "supported"
    assert CHECK_NLI in report.checks_run


def test_fourth_check_fails_when_judge_says_not_supported(pack) -> None:
    report = verify_draft(
        _draft(Fact("每人每天 500 元", ("ch_a",))),
        pack,
        question="住宿标准",
        judge_client=_Judge("NOT_SUPPORTED"),
    )
    assert report.passed is False
    assert report.failure is not None and report.failure.check == CHECK_NLI
    assert report.gate2_verdict == "not_supported"


def test_missing_judge_client_fails_closed_but_is_auditable_as_skipped(
    pack,
) -> None:
    report = verify_draft(
        _draft(Fact("每人每天 500 元", ("ch_a",))), pack, question="住宿标准"
    )
    assert report.passed is False
    assert report.gate2_verdict == "skipped"
    assert report.failure is not None
    assert "未运行" in report.failure.detail


def test_composite_question_calls_judge_once_with_full_answer(pack) -> None:
    client = _Judge("SUPPORTED")
    report = verify_draft(
        _draft(
            Fact("住宿费标准为每人每天 500 元", ("ch_a",)),
            Fact("市内交通费单次不超过 100 元", ("ch_b",)),
        ),
        pack,
        question="营业收入是多少？同比增长多少？",
        judge_client=client,
    )
    assert report.passed is True
    assert client.calls == 1
    assert client.temperatures == [0.0]
    assert len(client.messages) == 1
    user_content = client.messages[0][1]["content"]
    assert "住宿费标准为每人每天 500 元" in user_content
    assert "市内交通费单次不超过 100 元" in user_content
    assert report.answer.count("住宿费标准为每人每天 500 元") == 1
    assert report.answer.count("市内交通费单次不超过 100 元") == 1
    assert [item.chunk_id for item in report.cited_evidence] == ["ch_a", "ch_b"]


def test_single_fact_question_still_calls_judge_once(pack) -> None:
    client = _Judge("SUPPORTED")
    report = verify_draft(
        _draft(Fact("每人每天 500 元", ("ch_a",))),
        pack,
        question="住宿标准",
        judge_client=client,
    )
    assert report.passed is True
    assert client.calls == 1
    assert report.answer == "每人每天 500 元[1]"


# --- 第五道：内部一致性 -------------------------------------------------------


def test_fifth_check_rejects_two_values_for_the_same_attribute(pack) -> None:
    draft = _draft(
        Fact("住宿费限额为 500 元", ("ch_a",)),
        Fact("住宿费限额为 800 元", ("ch_a",)),
    )
    failure = check_consistency(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_CONSISTENCY
    assert any("住宿费限额" in item for item in failure.offenders)


def test_fifth_check_allows_different_attributes(pack) -> None:
    draft = _draft(
        Fact("住宿费限额为 500 元", ("ch_a",)),
        Fact("交通费限额为 100 元", ("ch_b",)),
    )
    assert check_consistency(draft, pack) is None


def test_fifth_check_allows_the_same_value_twice(pack) -> None:
    draft = _draft(
        Fact("住宿费限额为 500 元", ("ch_a",)),
        Fact("住宿费限额为 500 元", ("ch_a",)),
    )
    assert check_consistency(draft, pack) is None


# --- 顺序 ---------------------------------------------------------------------


def test_checks_stop_at_the_first_failure(pack) -> None:
    report = verify_draft(
        _draft(Fact("每人每天 500 元", ("ch_z",))),
        pack,
        question="q",
        judge_client=_Judge(),
    )
    assert report.checks_run == (CHECK_CITATION_RANGE,)
    assert (
        report.failure is not None
        and report.failure.check == CHECK_CITATION_RANGE
    )


def test_checks_run_in_the_documented_order(pack) -> None:
    assert CHECKS == (
        CHECK_CITATION_RANGE,
        CHECK_EVERY_CLAIM,
        CHECK_GROUNDING,
        CHECK_CONSISTENCY,
        CHECK_NLI,
    )


def test_all_five_run_on_a_clean_draft(pack) -> None:
    report = verify_draft(
        _draft(Fact("每人每天 500 元", ("ch_a",))),
        pack,
        question="q",
        judge_client=_Judge(),
    )
    assert report.passed is True
    assert report.checks_run == CHECKS


# --- status=unknown -----------------------------------------------------------


def test_unknown_status_refuses_without_running_checks(pack) -> None:
    draft = _draft(Fact("未找到可验证依据。", ()), status="unknown")
    report = verify_draft(draft, pack, question="q", judge_client=_Judge())
    assert report.passed is False
    assert report.checks_run == ()
    assert report.failure is not None
    assert report.failure.check == CHECK_DRAFT_STATUS
    assert report.error_code is ErrorCode.NO_EVIDENCE
    assert report.failure.code is ErrorCode.NO_EVIDENCE


# --- 三档降级 ----------------------------------------------------------------


def test_every_check_has_a_tier_and_a_code() -> None:
    for check in (*CHECKS, CHECK_DRAFT_STATUS):
        assert check in TIER_BY_CHECK
    assert set(TIER_BY_CHECK.values()) >= {FALLBACK_ORIGINAL_ONLY}


def test_content_failures_all_degrade_to_original_only(pack) -> None:
    cases = [
        _draft(Fact("每人每天 500 元", ("ch_z",))),
        _draft(Fact("每人每天 500 元", ("ch_a",)), Fact("另外还有餐补", ())),
        _draft(Fact("住宿费标准一般是 400 元", ("ch_a",))),
        _draft(
            Fact("住宿费限额为 500 元", ("ch_a",)),
            Fact("住宿费限额为 800 元", ("ch_a",)),
        ),
    ]
    for draft in cases:
        report = verify_draft(
            draft, pack, question="q", judge_client=_Judge("SUPPORTED")
        )
        if report.passed:
            continue
        assert report.fallback_tier == FALLBACK_ORIGINAL_ONLY


def test_failed_verification_still_carries_evidence_for_the_original_text(
    pack,
) -> None:
    report = verify_draft(
        _draft(Fact("住宿费标准一般是 400 元", ("ch_a",))),
        pack,
        question="q",
        judge_client=_Judge("SUPPORTED"),
    )
    assert report.passed is False
    assert (
        report.cited_evidence
    ), "校验不过时要带上被引证据，供 fallback 展示原文"
    assert report.cited_evidence[0].chunk_id == "ch_a"


def test_passed_report_carries_the_citation_mapping(pack) -> None:
    report = verify_draft(
        _draft(
            Fact("每人每天 500 元", ("ch_a",)),
            Fact("交通费 100 元", ("ch_b",)),
        ),
        pack,
        question="q",
        judge_client=_Judge(),
    )
    assert report.passed is True
    assert [item.n for item in report.cited_evidence] == [1, 2]
    assert report.answer.startswith("每人每天 500 元")


# --- 审计 ---------------------------------------------------------------------


def test_report_audit_is_json_safe_and_names_the_failing_check(pack) -> None:
    report = verify_draft(
        _draft(Fact("住宿费标准一般是 400 元", ("ch_a",))),
        pack,
        question="q",
        judge_client=_Judge(),
    )
    payload = json.dumps(report.as_audit(), ensure_ascii=False)
    assert CHECK_GROUNDING in payload
    assert "original_only" in payload
    assert "unverifiable" in payload


def test_parse_then_verify_end_to_end(pack) -> None:
    draft = parse_draft(
        '{"status":"answered",'
        '"facts":[{"text":"根据经验，这类费用一般是 400 元",'
        '"evidence_ids":["ch_a"]}]}'
    )
    report = verify_draft(
        draft, pack, question="住宿标准", judge_client=_Judge()
    )
    assert report.passed is False
    assert report.failure is not None
    assert report.failure.check == CHECK_GROUNDING


def test_fact_without_evidence_ids_fails_explicitly(pack) -> None:
    draft = _draft(Fact("每人每天 500 元", ()))
    failure = check_every_claim_cited(draft, pack)
    assert failure is not None
    assert "evidence_ids" in failure.detail
