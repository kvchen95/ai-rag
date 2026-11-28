"""复合问独立门控与聚合；不把强子问题的证据借给弱子问题。."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from api.retrieval.gate import GateDecision
from api.resources import messages as msg
from api.temporal.decompose import Decomposition, SubQuestion


@dataclass(frozen=True, slots=True)
class SubQuestionGate:
    """SubQuestionGate."""

    subquestion: SubQuestion
    snapshot: Any

    @property
    def passed(self) -> bool:
        """Run passed."""
        return self.snapshot.verdict in ("pass", "partial")

    @property
    def verdict(self) -> str:
        """Run verdict."""
        return self.snapshot.verdict

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            **self.subquestion.as_dict(),
            "verdict": self.verdict,
            "gate_result": self.snapshot.decision.as_audit(),
            "time_scope": self.snapshot.plan.temporal.as_dict()
            if self.snapshot.plan.temporal is not None
            else self.subquestion.temporal.as_dict(),
            "evidence_ids": [
                item.chunk_id for item in self.snapshot.decision.evidence
            ],
            "filter": self.snapshot.filter_json,
        }


@dataclass(frozen=True, slots=True)
class CompoundGateResult:
    """CompoundGateResult."""

    decomposition: Decomposition
    results: tuple[SubQuestionGate, ...]
    verdict: str
    uncovered: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    @property
    def decision(self) -> GateDecision:
        """Run decision."""
        # 合成门控出口；证据只来自通过子问题，且按 chunk_id 去重。
        from api.retrieval.gate import GateDecision

        if self.verdict in ("pass", "partial"):
            evidence: list[Any] = []
            seen: set[str] = set()
            for result in self.results:
                for candidate in result.snapshot.decision.evidence:
                    if candidate.chunk_id not in seen:
                        seen.add(candidate.chunk_id)
                        evidence.append(candidate)
            return GateDecision(
                verdict=self.verdict,  # type: ignore[arg-type]
                evidence=tuple(evidence),
                uncovered=self.uncovered,
                reason=msg.COMPOUND_REASON,
            )
        failed = [result for result in self.results if not result.passed]
        first = (
            failed[0].snapshot.decision
            if failed
            else self.results[0].snapshot.decision
        )
        return replace(
            first,
            verdict=self.verdict,  # type: ignore[arg-type]
            evidence=(),
            uncovered=self.uncovered,
            reason="；".join(
                f"{item.subquestion.id}: {item.snapshot.decision.reason}"
                for item in failed
            ),
        )

    def as_audit(self) -> dict[str, Any]:
        """Run as_audit."""
        return {
            "verdict": self.verdict,
            "decomposition": self.decomposition.as_dict(),
            "subquestions": [item.as_dict() for item in self.results],
            "evidence_map": {
                candidate.chunk_id: result.subquestion.id
                for result in self.results
                for candidate in result.snapshot.decision.evidence
            },
            "uncovered": list(self.uncovered),
        }


def aggregate_gates(
    decomposition: Decomposition, snapshots: Sequence[Any]
) -> CompoundGateResult:
    """聚合规则：全过 pass；有过有不过 partial；无成功按拒答原因收口。."""
    if len(snapshots) != len(decomposition.subquestions):
        raise ValueError(msg.COMPOUND_MISMATCH)
    results = tuple(
        SubQuestionGate(sub, snap)
        for sub, snap in zip(
            decomposition.subquestions, snapshots, strict=False
        )
    )
    passed = [item for item in results if item.passed]
    uncovered = tuple(
        item.subquestion.question
        for item in results
        if not item.passed or item.snapshot.decision.uncovered
    )
    if len(passed) == len(results):
        verdict = "pass"
    elif passed:
        verdict = "partial"
    elif any(item.verdict == "reject_none" for item in results):
        verdict = "reject_none"
    elif any(item.verdict == "reject_low" for item in results):
        verdict = "reject_low"
    else:
        verdict = "clarify"
    evidence_ids = tuple(
        dict.fromkeys(
            candidate.chunk_id
            for item in passed
            for candidate in item.snapshot.decision.evidence
        )
    )
    return CompoundGateResult(
        decomposition, results, verdict, uncovered, evidence_ids
    )


aggregate_compound = aggregate_gates
aggregate = aggregate_gates

__all__ = [
    "CompoundGateResult",
    "SubQuestionGate",
    "aggregate",
    "aggregate_compound",
    "aggregate_gates",
]
