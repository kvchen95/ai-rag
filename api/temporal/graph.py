"""时序知识图谱：实体—指标—期间—来源。

这一层不负责从自然语言猜时间，也不负责生成答案。它只回答：
某个主体在哪些期间披露了哪些指标、相邻/同比期间是什么、能否比较。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from api.temporal.facts import TemporalFact


@dataclass(frozen=True, slots=True)
class TemporalComparison:
    subject: str
    metric: str
    current: TemporalFact
    prior: TemporalFact
    delta: Decimal
    pct_change: Decimal | None

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "metric": self.metric,
            "current": self.current.as_dict(),
            "prior": self.prior.as_dict(),
            "delta": str(self.delta),
            "pct_change": None if self.pct_change is None else str(self.pct_change),
        }


class TemporalKnowledgeGraph:
    """内存图；持久化由 repository 负责，查询逻辑只依赖已加载事实。"""

    def __init__(self, facts: Iterable[TemporalFact] = ()) -> None:
        seen: dict[str, TemporalFact] = {}
        for fact in facts:
            seen[fact.fact_id] = fact
        self._facts = tuple(seen.values())

    @property
    def facts(self) -> tuple[TemporalFact, ...]:
        return self._facts

    def for_subject(self, subject: str) -> tuple[TemporalFact, ...]:
        return tuple(fact for fact in self._facts if fact.subject == subject)

    def series(self, subject: str, metric: str) -> tuple[TemporalFact, ...]:
        return tuple(
            sorted(
                (
                    fact
                    for fact in self._facts
                    if fact.subject == subject and fact.metric == metric
                ),
                key=lambda fact: (fact.period_start, fact.period_end, fact.fact_id),
            )
        )

    def at(
        self, subject: str, metric: str, period_start: str, period_end: str
    ) -> TemporalFact | None:
        for fact in self.series(subject, metric):
            if fact.period_start == period_start and fact.period_end == period_end:
                return fact
        return None

    def compare(
        self, subject: str, metric: str, period_start: str, period_end: str
    ) -> TemporalComparison | None:
        current = self.at(subject, metric, period_start, period_end)
        if current is None:
            return None
        prior_year = str(int(period_start[:4]) - 1) + period_start[4:]
        prior_end = str(int(period_end[:4]) - 1) + period_end[4:]
        prior = self.at(subject, metric, prior_year, prior_end)
        if prior is None or prior.unit != current.unit:
            return None
        delta = current.value - prior.value
        pct = None if prior.value == 0 else delta / prior.value * Decimal(100)
        return TemporalComparison(subject, metric, current, prior, delta, pct)

    def periods(self, subject: str) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                {
                    (fact.period_start, fact.period_end)
                    for fact in self._facts
                    if fact.subject == subject
                }
            )
        )


__all__ = ["TemporalComparison", "TemporalKnowledgeGraph"]
