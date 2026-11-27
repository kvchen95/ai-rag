"""TemporalFact 的 PostgreSQL 仓储。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from api.temporal.facts import TemporalFact
from api.temporal.graph import TemporalKnowledgeGraph

_UPSERT = """
INSERT INTO temporal_facts (
    fact_id, subject, metric, value, unit,
    period_start, period_end, period_granularity,
    source_chunk_id, doc_key, role, confidence, provenance
) VALUES (
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s
)
ON CONFLICT (fact_id) DO UPDATE SET
    value = EXCLUDED.value,
    unit = EXCLUDED.unit,
    period_start = EXCLUDED.period_start,
    period_end = EXCLUDED.period_end,
    period_granularity = EXCLUDED.period_granularity,
    confidence = EXCLUDED.confidence,
    provenance = EXCLUDED.provenance
"""

_SELECT = """
SELECT fact_id, subject, metric, value::text AS value, unit,
       period_start::text AS period_start, period_end::text AS period_end,
       period_granularity, source_chunk_id, doc_key, role, confidence, provenance
FROM temporal_facts
WHERE (%s::text IS NULL OR subject = %s)
  AND (%s::text IS NULL OR metric = %s)
  AND (%s::date IS NULL OR period_end >= %s::date)
  AND (%s::date IS NULL OR period_start <= %s::date)
ORDER BY period_start, period_end, metric
"""


def _row_to_fact(row: dict[str, Any]) -> TemporalFact:
    return TemporalFact(
        fact_id=str(row["fact_id"]),
        subject=str(row["subject"]),
        metric=str(row["metric"]),
        value=Decimal(str(row["value"])),
        unit=str(row.get("unit") or ""),
        period_start=str(row["period_start"]),
        period_end=str(row["period_end"]),
        period_granularity=str(row.get("period_granularity") or "range"),
        source_chunk_id=str(row["source_chunk_id"]),
        doc_key=str(row["doc_key"]),
        role=str(row.get("role") or "reported"),
        confidence=str(row.get("confidence") or "explicit"),
        provenance=dict(row.get("provenance") or {}),
    )


class TemporalFactRepository:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    def upsert(self, facts: Iterable[TemporalFact]) -> int:
        rows = []
        for fact in facts:
            rows.append(
                (
                    fact.fact_id,
                    fact.subject,
                    fact.metric,
                    fact.value,
                    fact.unit,
                    fact.period_start,
                    fact.period_end,
                    fact.period_granularity,
                    fact.source_chunk_id,
                    fact.doc_key,
                    fact.role,
                    fact.confidence,
                    Jsonb(fact.provenance),
                )
            )
        if not rows:
            return 0
        with self.pg.connection() as conn, conn.cursor() as cur:
            cur.executemany(_UPSERT, rows)
            return len(rows)

    def clear_all(self) -> int:
        """清空事实表；回填是全量重建，避免旧规则留下的错误事实残留。"""
        return self.pg.execute("DELETE FROM temporal_facts")

    def load(
        self,
        *,
        subject: str | None = None,
        metric: str | None = None,
        date_from: str | date | None = None,
        date_to: str | date | None = None,
    ) -> TemporalKnowledgeGraph:
        params = (subject, subject, metric, metric, date_from, date_from, date_to, date_to)
        rows = self.pg.fetch_all(_SELECT, params)
        return TemporalKnowledgeGraph(_row_to_fact(row) for row in rows)


__all__ = ["TemporalFactRepository"]
