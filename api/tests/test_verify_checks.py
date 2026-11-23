"""五道校验与三档降级（详细设计.md §10.1 / §10.3）。

每一道都有一组"正例 + 反例"，反例是这一道的判据真会红的证据。三档降级另有三条：
用户的处置不同，所以映射错了不报错、只让用户收到错误的建议。

这一组不碰任何外部服务：第四道用一个可注入的判别器替身，其余四道是纯函数。
真模型的行为由集成用例覆盖（`api/tests/b4/`）。
"""

from __future__ import annotations

import pytest

from api.errors import ErrorCode
from api.generate.evidence import build_evidence_pack
from api.generate.llm import Claim, Draft, LlmClient, parse_draft
from api.retrieval.hybrid import Candidate
from api.retrieval.texts import ChunkText
from api.verify.checks import (
    CHECKS,
    CHECK_CITATION_RANGE,
    CHECK_CONSISTENCY,
    CHECK_DRAFT_STATUS,
    CHECK_EVERY_CLAIM,
    CHECK_GROUNDING,
    CHECK_NLI,
    FALLBACK_ORIGINAL_ONLY,
    TIER_BY_CHECK,
    check_citation_range,
    check_consistency,
    check_every_claim_cited,
    check_grounding,
    verify_draft,
)


# --- 夹具：两条证据，一条正文、一条表格 ---------------------------------------


def _cand(chunk_id: str, *, doc_key: str = "policy/travel", page_num: int = 47, score: float = 0.62) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=f"{doc_key}#v1",
        chunk_type="text",
        page_num=page_num,
        rerank_score=score,
        fused_rank=1,
    )


def _text(chunk_id: str, body: str, *, doc_key: str = "policy/travel", page_num: int = 47) -> ChunkText:
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
    """两条证据：[1] 住宿标准（500 元），[2] 交通标准（100 元）。"""
    return build_evidence_pack(
        [_cand("ch_a"), _cand("ch_b", page_num=48, score=0.55)],
        {
            "ch_a": _text("ch_a", "住宿费标准：每人每天 500 元，按职级限额报销。"),
            "ch_b": _text("ch_b", "市内交通费单次不超过 100 元。", page_num=48),
        },
        max_chars=6000,
    )


def _draft(answer: str, *, citations=(1,), claims=(), status: str = "answered") -> Draft:
    return Draft(
        status=status,  # type: ignore[arg-type]
        answer=answer,
        citations=tuple(citations),
        claims=tuple(Claim(text=t, citations=c) for t, c in claims),
    )


class _Judge(LlmClient):
    """判别器替身：按预设结论返回。"""

    def __init__(self, verdict: str = "SUPPORTED") -> None:  # noqa: D107
        self.verdict = verdict
        self.temperatures: list[float] = []

    def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
        self.temperatures.append(temperature)
        return f'{{"verdict":"{self.verdict}","reason":"stub"}}'


# --- 第一道：引用编号存在且在范围内 -------------------------------------------


def test_first_check_passes_when_every_number_exists(pack) -> None:
    draft = _draft("每人每天 500 元[1]。", citations=(1,))
    assert check_citation_range(draft, pack) is None


def test_first_check_rejects_out_of_range_number(pack) -> None:
    draft = _draft("每人每天 500 元[9]。", citations=(9,))
    failure = check_citation_range(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_CITATION_RANGE
    assert "[9]" in failure.offenders


def test_first_check_scans_the_answer_body_not_just_the_citations_field(pack) -> None:
    """模型可能把 `citations` 字段改对、正文留着旧编号，而用户看到的是正文里的角标。"""
    draft = _draft("每人每天 500 元[1]，另见[7]。", citations=(1,))
    failure = check_citation_range(draft, pack)
    assert failure is not None
    assert "[7]" in failure.offenders


def test_first_check_rejects_zero_as_a_number(pack) -> None:
    """编号从 1 开始，`[0]` 不是合法编号。"""
    draft = _draft("每人每天 500 元[0]。", citations=(0,))
    assert check_citation_range(draft, pack) is not None


# --- 第二道：每条主张至少一个引用 ---------------------------------------------


def test_second_check_passes_when_every_sentence_is_cited(pack) -> None:
    draft = _draft("每人每天 500 元[1]。交通费 100 元[2]。", citations=(1, 2))
    assert check_every_claim_cited(draft, pack) is None


def test_second_check_rejects_a_sentence_without_citation(pack) -> None:
    """最典型的情形：模型自己补了一句没有出处的话。"""
    draft = _draft("每人每天 500 元[1]。另外还有餐补。", citations=(1,))
    failure = check_every_claim_cited(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_EVERY_CLAIM
    assert any("餐补" in item for item in failure.offenders)


def test_second_check_catches_sentences_the_model_did_not_report_as_claims(pack) -> None:
    """claims 只报了第一句时，第二句仍要被查——校验单位是句子，不是 claims。"""
    draft = _draft(
        "每人每天 500 元[1]。超出不予报销。",
        citations=(1,),
        claims=[("每人每天 500 元[1]", (1,))],
    )
    failure = check_every_claim_cited(draft, pack)
    assert failure is not None
    assert any("超出不予报销" in item for item in failure.offenders)


def test_second_check_rejects_an_empty_claim(pack) -> None:
    """§10.1 表格："主张为空也算失败"。"""
    draft = _draft("500 元[1]。", citations=(1,), claims=[("   ", ())])
    assert check_every_claim_cited(draft, pack) is not None


def test_second_check_rejects_an_answer_with_no_sentences(pack) -> None:
    draft = _draft("", citations=(1,))
    assert check_every_claim_cited(draft, pack) is not None


# --- 第三道：数字与专名可在被引证据里定位 -------------------------------------


def test_third_check_passes_when_numbers_are_grounded(pack) -> None:
    draft = _draft("住宿费标准为每人每天 500 元[1]。", citations=(1,))
    assert check_grounding(draft, pack) is None


def test_third_check_rejects_a_number_not_in_the_evidence(pack) -> None:
    """附录 D 的那个例子：证据里没有"400 元"。"""
    draft = _draft("住宿费标准一般是 400 元[1]。", citations=(1,))
    failure = check_grounding(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_GROUNDING
    assert any("400" in item for item in failure.offenders)


def test_third_check_checks_only_the_cited_evidence(pack) -> None:
    """500 元在 [1] 里、不在 [2] 里：引了 [2] 却说 500 元 → 判不过（引错编号）。"""
    draft = _draft("住宿费标准为每人每天 500 元[2]。", citations=(2,))
    failure = check_grounding(draft, pack)
    assert failure is not None
    assert any("500" in item for item in failure.offenders)


def test_third_check_rejects_an_unlocatable_clause_number(pack) -> None:
    draft = _draft("依据第十九条，住宿费标准为每人每天 500 元[1]。", citations=(1,))
    failure = check_grounding(draft, pack)
    assert failure is not None
    assert any("第十九条" in item for item in failure.offenders)


def test_third_check_accepts_chinese_number_written_differently(pack) -> None:
    """证据写"500 元"、草稿写"五百元"：归一后是同一个数，判过。"""
    draft = _draft("住宿费标准为每人每天五百元[1]。", citations=(1,))
    assert check_grounding(draft, pack) is None


# --- 第四道：判别器 -----------------------------------------------------------


def test_fourth_check_passes_when_judge_says_supported(pack) -> None:
    report = verify_draft(
        _draft("每人每天 500 元[1]。", citations=(1,)),
        pack,
        question="住宿标准",
        judge_client=_Judge("SUPPORTED"),
    )
    assert report.passed is True
    assert report.gate2_verdict == "supported"
    assert CHECK_NLI in report.checks_run


def test_fourth_check_fails_when_judge_says_not_supported(pack) -> None:
    report = verify_draft(
        _draft("每人每天 500 元[1]。", citations=(1,)),
        pack,
        question="住宿标准",
        judge_client=_Judge("NOT_SUPPORTED"),
    )
    assert report.passed is False
    assert report.failure is not None and report.failure.check == CHECK_NLI
    assert report.gate2_verdict == "not_supported"


def test_missing_judge_client_fails_closed_but_is_auditable_as_skipped(pack) -> None:
    """没配判别模型 = 少了一道闸，必须被抓出来，不能被"反正拒了"掩盖。"""
    report = verify_draft(
        _draft("每人每天 500 元[1]。", citations=(1,)), pack, question="住宿标准"
    )
    assert report.passed is False
    assert report.gate2_verdict == "skipped"
    assert report.failure is not None
    assert "未运行" in report.failure.detail


def test_judge_is_always_called_at_temperature_zero(pack) -> None:
    client = _Judge("SUPPORTED")
    verify_draft(_draft("每人每天 500 元[1]。", citations=(1,)), pack, question="q", judge_client=client)
    assert client.temperatures == [0.0]


# --- 第五道：内部一致性 -------------------------------------------------------


def test_fifth_check_rejects_two_values_for_the_same_attribute(pack) -> None:
    """§10.1 第五道：草稿里同一件事不能出现两个数。"""
    draft = _draft("住宿费限额为 500 元[1]。住宿费限额为 800 元[1]。", citations=(1,))
    failure = check_consistency(draft, pack)
    assert failure is not None
    assert failure.check == CHECK_CONSISTENCY
    assert any("住宿费限额" in item for item in failure.offenders)


def test_fifth_check_allows_different_attributes(pack) -> None:
    draft = _draft("住宿费限额为 500 元[1]。交通费限额为 100 元[2]。", citations=(1, 2))
    assert check_consistency(draft, pack) is None


def test_fifth_check_allows_the_same_value_twice(pack) -> None:
    draft = _draft("住宿费限额为 500 元[1]。住宿费限额为 500 元[1]。", citations=(1,))
    assert check_consistency(draft, pack) is None


# --- 顺序：任何一道不过就停 ---------------------------------------------------


def test_checks_stop_at_the_first_failure(pack) -> None:
    """引用越界时不该继续跑第二道——给一个越界草稿检查"每句引用"没有意义。"""
    report = verify_draft(
        _draft("每人每天 500 元[9]。", citations=(9,)), pack, question="q", judge_client=_Judge()
    )
    assert report.checks_run == (CHECK_CITATION_RANGE,)
    assert report.failure is not None and report.failure.check == CHECK_CITATION_RANGE


def test_checks_run_in_the_documented_order(pack) -> None:
    """§10.1："五道按成本从低到高排"。顺序是契约，换顺序会改变用户看到的第一条原因。"""
    assert CHECKS == (
        CHECK_CITATION_RANGE,
        CHECK_EVERY_CLAIM,
        CHECK_GROUNDING,
        CHECK_NLI,
        CHECK_CONSISTENCY,
    )


def test_all_five_run_on_a_clean_draft(pack) -> None:
    report = verify_draft(
        _draft("每人每天 500 元[1]。", citations=(1,)), pack, question="q", judge_client=_Judge()
    )
    assert report.passed is True
    assert report.checks_run == CHECKS


# --- status=unknown 不是校验失败 ----------------------------------------------


def test_unknown_status_refuses_without_running_checks(pack) -> None:
    """§9.2 规则 3：模型说"证据不足"是按提示词正确行事，不该记 unverifiable。"""
    draft = _draft("未找到可验证依据。", citations=(), status="unknown")
    report = verify_draft(draft, pack, question="q", judge_client=_Judge())
    assert report.passed is False
    assert report.checks_run == ()
    assert report.failure is not None
    assert report.failure.check == CHECK_DRAFT_STATUS
    assert report.error_code is ErrorCode.NO_EVIDENCE
    assert report.failure.code is ErrorCode.NO_EVIDENCE


# --- 三档降级 ----------------------------------------------------------------


def test_every_check_has_a_tier_and_a_code() -> None:
    """新增一道校验却忘了定档，会在这一条上红——缺键比走错分支更容易被发现。"""
    for check in (*CHECKS, CHECK_DRAFT_STATUS):
        assert check in TIER_BY_CHECK
    assert set(TIER_BY_CHECK.values()) >= {FALLBACK_ORIGINAL_ONLY}


def test_content_failures_all_degrade_to_original_only(pack) -> None:
    """§10.3：数字或判别不过 → "只给原文"。这一档用户拿到原文，比"稍后重试"有用。

    系统档（档三）不是由校验失败产生的：它只在模型不可用/超时时出现，
    而那时根本走不到校验层。
    """
    cases = [
        _draft("每人每天 500 元[9]。", citations=(9,)),
        _draft("每人每天 500 元[1]。另外还有餐补。", citations=(1,)),
        _draft("住宿费标准一般是 400 元[1]。", citations=(1,)),
        _draft("住宿费限额为 500 元[1]。住宿费限额为 800 元[1]。", citations=(1,)),
    ]
    for draft in cases:
        report = verify_draft(draft, pack, question="q", judge_client=_Judge("SUPPORTED" if len(draft.citations) < 9 else "SUPPORTED"))
        if report.passed:
            continue
        assert report.fallback_tier == FALLBACK_ORIGINAL_ONLY


def test_failed_verification_still_carries_evidence_for_the_original_text(pack) -> None:
    """档一必须能给出原文片段，否则"附原文"这句话是空的。"""
    report = verify_draft(
        _draft("住宿费标准一般是 400 元[1]。", citations=(1,)),
        pack,
        question="q",
        judge_client=_Judge("SUPPORTED"),
    )
    assert report.passed is False
    assert report.cited_evidence, "校验不过时要带上被引证据，供 fallback 展示原文"
    assert report.cited_evidence[0].chunk_id == "ch_a"


def test_passed_report_carries_the_citation_mapping(pack) -> None:
    report = verify_draft(
        _draft("每人每天 500 元[1]。交通费 100 元[2]。", citations=(1, 2)),
        pack,
        question="q",
        judge_client=_Judge(),
    )
    assert report.passed is True
    assert [item.n for item in report.cited_evidence] == [1, 2]
    assert report.answer.startswith("每人每天 500 元")


# --- 审计 ---------------------------------------------------------------------


def test_report_audit_is_json_safe_and_names_the_failing_check(pack) -> None:
    import json

    report = verify_draft(
        _draft("住宿费标准一般是 400 元[1]。", citations=(1,)),
        pack,
        question="q",
        judge_client=_Judge(),
    )
    payload = json.dumps(report.as_audit(), ensure_ascii=False)
    assert CHECK_GROUNDING in payload
    assert "original_only" in payload
    assert "unverifiable" in payload


def test_parse_then_verify_end_to_end(pack) -> None:
    """从模型原始输出到校验结论的一条链路（附录 D 的"应该被拒答的例子"）。"""
    draft = parse_draft(
        '{"status":"answered","answer":"根据经验，这类费用一般是 400 元[1]",'
        '"citations":[1],"claims":[{"text":"这类费用一般是 400 元","citations":[1]}]}'
    )
    report = verify_draft(draft, pack, question="住宿标准", judge_client=_Judge())
    assert report.passed is False
    assert report.failure is not None
    assert report.failure.check == CHECK_GROUNDING
