"""五道校验与降级决策（详细设计.md §10.1 / §10.3）。.

按 facts 契约校验：fact.text + fact.evidence_ids（chunk_id）。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from api.errors import ErrorCode
from api.generate.calculator import validate_calculation
from api.generate.evidence import Evidence, EvidencePack
from api.generate.judge import JudgeResult, judge
from api.generate.llm import Draft, Fact, LlmClient
from api.generate.render import render_draft
from api.observability import get_logger, log_event
from api.resources import messages as msg
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
    """CheckFailure."""

    check: str
    detail: str
    offenders: tuple[str, ...] = ()

    @property
    def tier(self) -> DegradeTier:
        """Run tier."""
        return TIER_BY_CHECK[self.check]

    @property
    def code(self) -> ErrorCode:
        """Run code."""
        return CODE_BY_CHECK[self.check]

    def as_audit(self) -> dict[str, Any]:
        """Run as_audit."""
        return {
            "check": self.check,
            "detail": self.detail,
            "offenders": list(self.offenders),
            "tier": self.tier,
            "code": self.code.value,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """VerificationReport."""

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
        """Run gate2_verdict."""
        if self.judge is None:
            return "skipped"
        if self.judge.skipped:
            return "skipped"
        return "supported" if self.judge.supported else "not_supported"

    @property
    def fallback_tier(self) -> DegradeTier | None:
        """Run fallback_tier."""
        return (
            None
            if self.passed
            else (self.failure.tier if self.failure else FALLBACK_SYSTEM)
        )

    @property
    def error_code(self) -> ErrorCode | None:
        """Run error_code."""
        return (
            None
            if self.passed
            else (self.failure.code if self.failure else ErrorCode.INTERNAL)
        )

    def as_audit(self) -> dict[str, Any]:
        """Run as_audit."""
        return {
            "passed": self.passed,
            "checks_run": list(self.checks_run),
            "failure": None
            if self.failure is None
            else self.failure.as_audit(),
            "judge": None if self.judge is None else self.judge.as_audit(),
            "fallback": self.fallback_tier,
            "gate2": self.gate2_verdict,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def _facts(draft: Draft) -> tuple[Fact, ...]:
    """Run _facts."""
    return tuple(draft.facts if draft.facts else ())


def _evidence_by_chunk(pack: EvidencePack) -> dict[str, Evidence]:
    """Run _evidence_by_chunk."""
    return {item.chunk_id: item for item in pack.items}


def _cited_for_fact(pack: EvidencePack, fact: Fact) -> tuple[Evidence, ...]:
    """Run _cited_for_fact."""
    by_chunk = _evidence_by_chunk(pack)
    out: list[Evidence] = []
    for cid in dict.fromkeys(fact.evidence_ids):
        ev = by_chunk.get(cid)
        if ev is not None:
            out.append(ev)
    return tuple(out)


def _all_cited(pack: EvidencePack, draft: Draft) -> tuple[Evidence, ...]:
    """Run _all_cited."""
    seen: set[str] = set()
    out: list[Evidence] = []
    for fact in _facts(draft):
        for ev in _cited_for_fact(pack, fact):
            if ev.chunk_id not in seen:
                seen.add(ev.chunk_id)
                out.append(ev)
    return tuple(out)


def _evidence_text(items: Sequence[Evidence]) -> str:
    """Run _evidence_text."""
    return "\n".join(item.text for item in items)


def _clip(text: str, limit: int = 60) -> str:
    """Run _clip."""
    text = " ".join(((text if text else "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_citation_range(
    draft: Draft, pack: EvidencePack
) -> CheckFailure | None:
    """Run check_citation_range."""
    known = set(_evidence_by_chunk(pack))
    offenders: list[str] = []
    for fact in _facts(draft):
        for cid in fact.evidence_ids:
            if cid not in known and cid not in offenders:
                offenders.append(cid)
    if offenders:
        return CheckFailure(
            check=CHECK_CITATION_RANGE,
            detail=msg.CHECK_CITATION_OUT_OF_PACK,
            offenders=tuple(offenders[:8]),
        )
    return None


def check_every_claim_cited(
    draft: Draft, _pack: EvidencePack
) -> CheckFailure | None:
    """Run check_every_claim_cited."""
    facts = _facts(draft)
    if not facts:
        return CheckFailure(
            check=CHECK_EVERY_CLAIM,
            detail=msg.CHECK_NO_CLAIMS,
        )
    naked = [fact.text for fact in facts if not fact.evidence_ids]
    if naked:
        return CheckFailure(
            check=CHECK_EVERY_CLAIM,
            detail=msg.CHECK_NAKED_FACTS.format(count=len(naked)),
            offenders=tuple(naked[:5]),
        )
    return None


def check_grounding(
    draft: Draft, pack: EvidencePack, *, question: str = ""
) -> CheckFailure | None:
    """Run check_grounding."""
    offenders: list[str] = []
    for fact in _facts(draft):
        cited = _cited_for_fact(pack, fact)
        if not cited:
            continue
        blob = _evidence_text(cited)
        if fact.kind == "calculation":
            if validate_calculation(fact.expression, fact.result, blob) is None:
                offenders.append(
                    msg.CHECK_CALC_UNVERIFIABLE.format(
                        text=_clip(fact.text)
                    )
                )
            continue
        missing_numbers, _ = locate_quantities(fact.text, blob)
        missing_names, _ = locate_proper_nouns(fact.text, blob)
        parts: list[str] = []
        if missing_numbers:
            parts.append(
                msg.CHECK_NUMBER_PREFIX
                + "、".join(item.raw for item in missing_numbers)
            )
        if missing_names:
            parts.append(msg.CHECK_NAME_PREFIX + "、".join(missing_names))
        if parts:
            offenders.append(
                msg.CHECK_UNLOCATABLE.format(
                    text=_clip(fact.text), missing="、".join(parts)
                )
            )
    if offenders:
        return CheckFailure(
            check=CHECK_GROUNDING,
            detail=msg.CHECK_UNLOCATABLE_DETAIL.format(
                count=len(offenders)
            ),
            offenders=tuple(offenders[:5]),
        )
    return None


def check_nli(
    client: LlmClient | None, question: str, draft: Draft, pack: EvidencePack
) -> tuple[CheckFailure | None, JudgeResult]:
    """Run check_nli."""
    facts = _facts(draft)
    if not facts:
        result = judge(client, question, "", "")
        return CheckFailure(check=CHECK_NLI, detail=msg.CHECK_NO_FACTS), result
    # §10.1 第四道：判别器吃“问题 + 被引证据 + 答案”。按整份渲染后的答案判，
    # 而不是逐 fact 判，避免把复合问题拆成多条事实后误报“漏答子问题”。
    cited = _all_cited(pack, draft)
    result = judge(
        client,
        question,
        _evidence_text(cited),
        render_draft(draft, pack).answer,
    )
    if not result.supported:
        return (
            CheckFailure(
                check=CHECK_NLI,
                detail=msg.CHECK_JUDGE_NOT_SUPPORTED
                if not result.skipped
                else msg.CHECK_JUDGE_SKIPPED,
                offenders=(result.reason,) if result.reason else (),
            ),
            result,
        )
    return None, result


def check_consistency(draft: Draft, _pack: EvidencePack) -> CheckFailure | None:
    """Run check_consistency."""
    conflicts = find_value_conflicts([fact.text for fact in _facts(draft)])
    if conflicts:
        return CheckFailure(
            check=CHECK_CONSISTENCY,
            detail=msg.CHECK_CONFLICT_DETAIL.format(count=len(conflicts)),
            offenders=tuple(
                msg.CHECK_CONFLICT_ITEM.format(
                    attribute=attribute,
                    left=left,
                    right=right,
                    unit=unit,
                )
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
    """Run verify_draft."""
    started = time.perf_counter()

    if not draft.answered:
        return VerificationReport(
            passed=False,
            checks_run=(),
            failure=CheckFailure(
                check=CHECK_DRAFT_STATUS,
                detail=msg.CHECK_DRAFT_UNKNOWN,
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
            failure, judge_result = check_nli(
                judge_client, question, draft, pack
            )
        else:
            failure = check_consistency(draft, pack)
        if failure is not None:
            return _failed(
                failure, tuple(ran), started, judge_result, pack, draft
            )

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
    """Run _failed."""
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
