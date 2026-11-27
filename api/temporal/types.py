from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from api.time_range import TimeRange

TimeSpan = TimeRange
TimeRole = Literal["document", "target"]
Confidence = Literal["high", "medium", "low"]
TemporalBasis = Literal["explicit", "relative", "assumed_year", "inferred"]

# Back-compat alias expected by some callers/tests.
TimeRoleType = TimeRole


@dataclass(frozen=True, slots=True)
class TemporalScope:
    """结构化时间范围：文档有效时间与答案目标时间分离。"""

    question: str
    document_ranges: tuple[TimeRange, ...] = ()
    target_times: tuple[str, ...] = ()
    basis: str = "explicit"
    confidence: Literal["high", "medium", "low"] = "high"
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def range(self) -> TimeRange | None:
        """兼容旧调用方：取第一个文档时间范围。"""
        return self.document_ranges[0] if self.document_ranges else None

    @property
    def target_time(self) -> str | None:
        return self.target_times[0] if self.target_times else None

    @property
    def has_window(self) -> bool:
        return bool(self.document_ranges)

    def as_dict(self) -> dict[str, object]:
        return {
            "document_ranges": [item.as_dict() for item in self.document_ranges],
            "target_times": list(self.target_times),
            "basis": self.basis,
            "confidence": self.confidence,
            "notes": list(self.notes),
        }
