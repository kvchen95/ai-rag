"""五道校验与降级决策（详细设计.md §10.1 / §10.3）。

按 facts 契约校验：fact.text + fact.evidence_ids（chunk_id）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from api.errors import ErrorCode
from api.generate.calculator import validate_calculation
from api.generate.evidence import Evidence, EvidencePack
from api.generate.judge import JudgeResult, judge
from api.generate.llm import Draft, LlmClient, Fact
from api.generate.render import render_draft
from api.observability import get_logger, log_event
from api.verify.numbers import (
    find_value_conflicts,
    locate_proper_nouns,
    locate_quantities,
)

logger = get_logger("api.verify.checks")

CHECK_CITATION_RANGE = "citation_range"
CHECK_EVERY_CLAIM = "every_claim_cited"
CHECK_GROUNDING = "numeric_and_named"
CHECK_NLI = "nli_support"
CHECK_CONSISTENCY = "internal_consistency"
CHECK_DRAFT_STATUS = "draft_status"

CHECKS: tuple[str, ...] = (
    CHECK_CITATION_RANGE,
    CHECK_EVERY_CLAIM,
    CHECK_GROUNDING,
    CHECK_CONSISTENCY,
    CHECK_NLI,
)

FALLBACK_ORIGINAL_ONLY = "original_only"
FALLBACK_PARTIAL = "partial"
FALLBACK_SYSTEM = "system"

DegradeTier = Literal["original_only", "partial", "system"]

TIER_BY_CHECK: dict[str, DegradeTier] = {
    CHECK_DRAFT_STATUS: FALLBACK_ORIGINAL_ONLY,
    CHECK_CITATION_RANGE: FALLBACK_ORIGINAL_ONLY,
    CHECK_EVERY_CLAIM: FALLBACK_ORIGINAL_ONLY,
    CHECK_GROUNDING: FALLBACK_ORIGINAL_ONLY,
    CHECK_NLI: FALLBACK_ORIGINAL_ONLY,
    CHECK_CONSISTENCY: FALLBACK_ORIGINAL_ONLY,
}

CODE_BY_CHECK: dict[str, ErrorCode] = {
    CHECK_DRAFT_STATUS: ErrorCode.NO_EVIDENCE,
    CHECK_CITATION_RANGE: ErrorCode.UNVERIFIABLE,
    CHECK_EVERY_CLAIM: ErrorCode.UNVERIFIABLE,
    CHECK_GROUNDING: ErrorCode.UNVERIFIABLE,
    CHECK_NLI: ErrorCode.UNVERIFIABLE,
    CHECK_CONSISTENCY: ErrorCode.UNVERIFIABLE,
}


@dataclass(frozen=True, slots=True)
class CheckFailure:
    check: str
    detail: str
    offenders: tuple[str, ...] = ()

    @property
    def tier(self) -> DegradeTier:
        return TIER_BY_CHECK[self.check]

    @property
    def code(self) -> ErrorCode:
        return CODE_BY_CHECK[self.check]

    def as_audit(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "detail": self.detail,
            "offenders": list(self.offenders),
            "tier": self.tier,
            "code": self.code.value,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    passed: bool
    checks_run: tuple[str, ...]
    failure: CheckFailure | None = None
    judge: JudgeResult | None = None
    answer: str = ""
    citations: tuple[int, ...] = ()
    elapsed_ms: float = 0.0
    cited_evidence: tuple[Evidence, ...] = ()

    @property
    def gate2_verdict(self) -> str:
        if self.judge is None:
            return "skipped"
        if self.judge.skipped:
            return "skipped"
        return "supported" if self.judge.supported else "not_supported"

    @property
    def fallback_tier(self) -> DegradeTier | None:
        return None if self.passed else (self.failure.tier if self.failure else FALLBACK_SYSTEM)

    @property
    def error_code(self) -> ErrorCode | None:
        return None if self.passed else (self.failure.code if self.failure else ErrorCode.INTERNAL)

    def as_audit(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks_run": list(self.checks_run),
            "failure": None if self.failure is None else self.failure.as_audit(),
            "judge": None if self.judge is None else self.judge.as_audit(),
            "fallback": self.fallback_tier,
            "gate2": self.gate2_verdict,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def _facts(draft: Draft) -> tuple[Fact, ...]:
    return tuple(draft.facts or ())


def _evidence_by_chunk(pack: EvidencePack) -> dict[str, Evidence]:
    return {item.chunk_id: item for item in pack.items}


def _cited_for_fact(pack: EvidencePack, fact: Fact) -> tuple[Evidence, ...]:
    by_chunk = _evidence_by_chunk(pack)
    out: list[Evidence] = []
    for cid in dict.fromkeys(fact.evidence_ids):
        ev = by_chunk.get(cid)
        if ev is not None:
            out.append(ev)
    return tuple(out)


def _all_cited(pack: EvidencePack, draft: Draft) -> tuple[Evidence, ...]:
    seen: set[str] = set()
    out: list[Evidence] = []
    for fact in _facts(draft):
        for ev in _cited_for_fact(pack, fact):
            if ev.chunk_id not in seen:
                seen.add(ev.chunk_id)
                out.append(ev)
    return tuple(out)


def _evidence_text(items: Sequence[Evidence]) -> str:
    return "\n".join(item.text for item in items)


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_citation_range(draft: Draft, pack: EvidencePack) -> CheckFailure | None:
    known = set(_evidence_by_chunk(pack))
    offenders: list[str] = []
    for fact in _facts(draft):
        for cid in fact.evidence_ids:
            if cid not in known and cid not in offenders:
                offenders.append(cid)
    if offenders:
        return CheckFailure(
            check=CHECK_CITATION_RANGE,
            detail="引用的 evidence_id 不在证据包中",
            offenders=tuple(offenders[:8]),
        )
    return None


def check_every_claim_cited(draft: Draft, _pack: EvidencePack) -> CheckFailure | None:
    facts = _facts(draft)
    if not facts:
        return CheckFailure(check=CHECK_EVERY_CLAIM, detail="草稿没有任何可校验的主张（主张为空）")
    naked = [fact.text for fact in facts if not fact.evidence_ids]
    if naked:
        return CheckFailure(
            check=CHECK_EVERY_CLAIM,
            detail=f"{len(naked)} 条 fact 没有 evidence_ids",
            offenders=tuple(naked[:5]),
        )
    return None


def check_grounding(draft: Draft, pack: EvidencePack, *, question: str = "") -> CheckFailure | None:
    offenders: list[str] = []
    for fact in _facts(draft):
        cited = _cited_for_fact(pack, fact)
        if not cited:
            continue
        blob = _evidence_text(cited)
        if fact.kind == "calculation":
            if validate_calculation(fact.expression, fact.result, blob) is None:
                offenders.append(f"「{_clip(fact.text)}」的计算无法由被引证据复算")
            continue
        missing_numbers, _ = locate_quantities(fact.text, blob)
        missing_names, _ = locate_proper_nouns(fact.text, blob)
        parts: list[str] = []
        if missing_numbers:
            parts.append("数字 " + "、".join(item.raw for item in missing_numbers))
        if missing_names:
            parts.append("专名 " + "、".join(missing_names))
        if parts:
            offenders.append(f"「{_clip(fact.text)}」里的{'、'.join(parts)}在被引证据里定位不到")
    if offenders:
        return CheckFailure(
            check=CHECK_GROUNDING,
            detail=f"{len(offenders)} 条 fact 的数字或专名无法在被引证据里定位",
            offenders=tuple(offenders[:5]),
        )
    return None


def check_nli(
    client: LlmClient | None, question: str, draft: Draft, pack: EvidencePack
) -> tuple[CheckFailure | None, JudgeResult]:
    facts = _facts(draft)
    if not facts:
        result = judge(client, question, "", "")
        return CheckFailure(check=CHECK_NLI, detail="无 fact 可校验"), result
    # §10.1 第四道：判别器吃“问题 + 被引证据 + 答案”。按整份渲染后的答案判，
    # 而不是逐 fact 判，避免把复合问题拆成多条事实后误报“漏答子问题”。
    cited = _all_cited(pack, draft)
    result = judge(client, question, _evidence_text(cited), render_draft(draft, pack).answer)
    if not result.supported:
        return (
            CheckFailure(
                check=CHECK_NLI,
                detail="判别器判不支持" if not result.skipped else "判别器未运行（未配置），按不支持处理",
                offenders=(result.reason,) if result.reason else (),
            ),
            result,
        )
    return None, result


def check_consistency(draft: Draft, _pack: EvidencePack) -> CheckFailure | None:
    conflicts = find_value_conflicts([fact.text for fact in _facts(draft)])
    if conflicts:
        return CheckFailure(
            check=CHECK_CONSISTENCY,
            detail=f"{len(conflicts)} 处同一属性出现两个值",
            offenders=tuple(
                f"{attribute} 同时是 {left} 与 {right}（单位 {unit}）"
                for attribute, unit, left, right in conflicts
            )[:5],
        )
    return None


def verify_draft(
    draft: Draft,
    pack: EvidencePack,
    *,
    question: str,
    judge_client: LlmClient | None = None,
) -> VerificationReport:
    started = time.perf_counter()

    if not draft.answered:
        return VerificationReport(
            passed=False,
            checks_run=(),
            failure=CheckFailure(
                check=CHECK_DRAFT_STATUS,
                detail="模型判定证据不足（status=unknown）",
                offenders=(),
            ),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    ran: list[str] = []
    judge_result: JudgeResult | None = None
    for check in CHECKS:
        ran.append(check)
        if check == CHECK_CITATION_RANGE:
            failure = check_citation_range(draft, pack)
        elif check == CHECK_EVERY_CLAIM:
            failure = check_every_claim_cited(draft, pack)
        elif check == CHECK_GROUNDING:
            failure = check_grounding(draft, pack, question=question)
        elif check == CHECK_NLI:
            failure, judge_result = check_nli(judge_client, question, draft, pack)
        else:
            failure = check_consistency(draft, pack)
        if failure is not None:
            return _failed(failure, tuple(ran), started, judge_result, pack, draft)

    if judge_result is None:
        judge_result = judge(judge_client, question, "", "")
    rendered = render_draft(draft, pack)
    answer = rendered.answer
    citations = rendered.citations
    return VerificationReport(
        passed=True,
        checks_run=tuple(ran),
        judge=judge_result,
        answer=answer,
        citations=citations,
        cited_evidence=_all_cited(pack, draft),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _failed(
    failure: CheckFailure,
    ran: tuple[str, ...],
    started: float,
    judge_result: JudgeResult | None,
    pack: EvidencePack,
    draft: Draft,
) -> VerificationReport:
    log_event(
        logger,
        "verification_failed",
        level=30,
        check=failure.check,
        tier=failure.tier,
        code=failure.code.value,
        offenders=len(failure.offenders),
    )
    return VerificationReport(
        passed=False,
        checks_run=ran,
        failure=failure,
        judge=judge_result,
        cited_evidence=_all_cited(pack, draft),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


__all__ = [
    "CHECKS",
    "CHECK_CITATION_RANGE",
    "CHECK_CONSISTENCY",
    "CHECK_DRAFT_STATUS",
    "CHECK_EVERY_CLAIM",
    "CHECK_GROUNDING",
    "CHECK_NLI",
    "CODE_BY_CHECK",
    "CheckFailure",
    "DegradeTier",
    "FALLBACK_ORIGINAL_ONLY",
    "FALLBACK_PARTIAL",
    "FALLBACK_SYSTEM",
    "TIER_BY_CHECK",
    "VerificationReport",
    "check_citation_range",
    "check_consistency",
    "check_every_claim_cited",
    "check_grounding",
    "check_nli",
    "verify_draft",
]
