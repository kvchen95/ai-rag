"""把开放问句收敛成时序图查询。.

只从已入库事实里取主体/指标/期间；识别不到就返回 None，让原检索链路继续。
这里不做投资分析，也不生成答案。
"""

from __future__ import annotations

from dataclasses import dataclass

from api.resources import finance_terms
from api.temporal.graph import TemporalKnowledgeGraph
from api.temporal.types import TemporalScope

_ALIASES = finance_terms.ALIASES


@dataclass(frozen=True, slots=True)
class FactQuery:
    """FactQuery."""

    subject: str
    metric: str
    period_start: str
    period_end: str
    prior_start: str
    prior_end: str
    metrics: tuple[str, ...] = ()

    @property
    def all_metrics(self) -> tuple[str, ...]:
        """Run all_metrics."""
        return self.metrics or ((self.metric,) if self.metric else ())

    def as_dict(self) -> dict[str, object]:
        """Run as_dict."""
        return {
            "subject": self.subject,
            "metric": self.metric,
            "metrics": list(self.all_metrics),
            "period_start": self.period_start,
            "period_end": self.period_end,
            "prior_start": self.prior_start,
            "prior_end": self.prior_end,
        }


def _prior(start: str, end: str) -> tuple[str, str]:
    """Run _prior."""
    return str(int(start[:4]) - 1) + start[4:], str(int(end[:4]) - 1) + end[4:]


def _previous_quarter(start: str) -> tuple[str, str]:
    """Run _previous_quarter."""
    from datetime import date

    current = date.fromisoformat(start)
    q = (current.month - 1) // 3 + 1
    if q == 1:
        year = current.year - 1
        q = 4
    else:
        year = current.year
        q -= 1
    starts = {
        1: (1, 1, 3, 31),
        2: (4, 1, 6, 30),
        3: (7, 1, 9, 30),
        4: (10, 1, 12, 31),
    }
    sm, sd, em, ed = starts[q]
    return f"{year:04d}-{sm:02d}-{sd:02d}", f"{year:04d}-{em:02d}-{ed:02d}"


def _subject(question: str, graph: TemporalKnowledgeGraph) -> str | None:
    """Run _subject."""
    candidates = sorted(
        {
            fact.subject
            for fact in graph.facts
            if fact.subject and fact.subject in question
        },
        key=len,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _metrics(
    question: str, graph: TemporalKnowledgeGraph, subject: str
) -> tuple[str, ...]:
    """Run _metrics."""
    names = {fact.metric for fact in graph.facts if fact.subject == subject}
    matched: list[str] = []
    for alias, canonical in sorted(
        _ALIASES.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if alias not in question:
            continue
        for name in names:
            if name == canonical or name.startswith(canonical):
                matched.append(name)
    for name in names:
        if name and name in question:
            matched.append(name)
    return tuple(dict.fromkeys(sorted(matched, key=len)))


def resolve_fact_query(
    question: str, graph: TemporalKnowledgeGraph, scope: TemporalScope | None
) -> FactQuery | None:
    """Run resolve_fact_query."""
    subject = _subject(question, graph)
    if subject is None:
        return None
    metrics = _metrics(question, graph, subject)
    if not metrics:
        return None

    series = graph.series(subject, metrics[0])
    if not series:
        return None
    if scope is not None and scope.document_ranges:
        period = scope.document_ranges[0]
        start, end = str(period.date_from), str(period.date_to)
        start = f"{start[:4]}-{start[4:6]}-{start[6:]}"
        end = f"{end[:4]}-{end[4:6]}-{end[6:]}"
    else:
        # 未写时间时取最新一期；这是“取最近披露”，不是放宽时间范围。
        latest = series[-1]
        start, end = latest.period_start, latest.period_end

    prior_start, prior_end = (
        _previous_quarter(start)
        if any(
            marker in question
            for marker in finance_terms.PREVIOUS_QUARTER_MARKERS
        )
        else _prior(start, end)
    )
    has_any = any(
        graph.at(subject, metric, start, end) is not None
        or graph.at(subject, metric, prior_start, prior_end) is not None
        for metric in metrics
    )
    if not has_any:
        return None
    return FactQuery(
        subject,
        metrics[0],
        start,
        end,
        prior_start,
        prior_end,
        metrics=metrics,
    )


__all__ = ["FactQuery", "resolve_fact_query"]
