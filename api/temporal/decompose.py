"""查询单元模型。.

这里刻意**不做复合问拆分**：没有可靠的中文/英文句法分析器时，按“的/同比/原因”
这些词切分只会产生错误子问题。当前契约是：

- 一句用户问题 = 一个检索单元；
- 需要多子问题时，由上层 Planner 显式生成 `SubQuestion`，并保证每个单元都有
  subject/metric/temporal；本模块不提供任何推测性拆分或回退。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from api.resources import messages as msg
from api.temporal.types import TemporalScope
from api.time_range import normalise_time


@dataclass(frozen=True, slots=True)
class SubQuestion:
    """独立检索单元。`subject/metric` 为空表示整问检索，不是待猜项。."""

    id: str
    question: str
    temporal: TemporalScope
    subject: str = ""
    metric: str = ""
    source: str = "original"

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "id": self.id,
            "question": self.question,
            "subject": self.subject,
            "metric": self.metric,
            "time_scope": self.temporal.as_dict(),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class Decomposition:
    """Decomposition."""

    original: str
    subquestions: tuple[SubQuestion, ...]
    decomposed: bool
    notes: tuple[str, ...] = ()

    @property
    def is_compound(self) -> bool:
        """Run is_compound."""
        return self.decomposed and len(self.subquestions) > 1

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "decomposed": self.decomposed,
            "notes": list(self.notes),
            "subquestions": [item.as_dict() for item in self.subquestions],
        }


def _scope(question: str, *, now: date | None) -> TemporalScope:
    """Run _scope."""
    from api.temporal.normalize import build_temporal_scope

    return build_temporal_scope(normalise_time(question, now=now))


def decompose_question(
    question: str,
    *,
    now: date | None = None,
) -> Decomposition:
    """返回单检索单元；不拆分、不猜主体/指标、不提供回退。."""
    original = (question or "").strip()
    if not original:
        return Decomposition("", (), False, (msg.EMPTY_QUESTION,))
    return Decomposition(
        original=original,
        subquestions=(
            SubQuestion(
                id="sq1",
                question=original,
                temporal=_scope(original, now=now),
            ),
        ),
        decomposed=False,
        notes=(
            "complex decomposition disabled: "
            "whole question as one retrieval unit",
        ),
    )


decompose = decompose_question
split_compound = decompose_question

__all__ = [
    "Decomposition",
    "SubQuestion",
    "decompose",
    "decompose_question",
    "split_compound",
]
