"""TemporalFactRepository 的行映射与 upsert 参数形状。"""

from __future__ import annotations

from decimal import Decimal

from api.temporal.facts import TemporalFact
from api.temporal.repository import TemporalFactRepository


class _Cursor:
    def __init__(self) -> None:
        self.rows = []
        self.executed = []

    def executemany(self, sql, rows):
        self.executed.append((sql, list(rows)))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _Pg:
    def __init__(self, cursor=None, rows=None):
        self._cursor = cursor or _Cursor()
        self._rows = rows or []

    def connection(self):
        return _Conn(self._cursor)

    def fetch_all(self, sql, params):
        return self._rows


def _fact() -> TemporalFact:
    return TemporalFact(
        fact_id="tf_1",
        subject="寒武纪",
        metric="营业收入",
        value=Decimal("5995573619.61"),
        unit="元",
        period_start="2026-01-01",
        period_end="2026-06-30",
        period_granularity="half_year",
        source_chunk_id="ch_1",
        doc_key="reports/han-wu-ji-2026-h1",
    )


def test_upsert_writes_one_row_per_fact() -> None:
    pg = _Pg()
    assert TemporalFactRepository(pg).upsert([_fact()]) == 1
    sql, rows = pg._cursor.executed[0]
    assert "INSERT INTO temporal_facts" in sql
    assert rows[0][0:3] == ("tf_1", "寒武纪", "营业收入")


def test_load_maps_numeric_and_json_columns() -> None:
    pg = _Pg(
        rows=[
            {
                "fact_id": "tf_1",
                "subject": "寒武纪",
                "metric": "营业收入",
                "value": "5995573619.61",
                "unit": "元",
                "period_start": "2026-01-01",
                "period_end": "2026-06-30",
                "period_granularity": "half_year",
                "source_chunk_id": "ch_1",
                "doc_key": "reports/han-wu-ji-2026-h1",
                "role": "reported",
                "confidence": "explicit",
                "provenance": {"column": "本期数"},
            }
        ]
    )
    graph = TemporalFactRepository(pg).load(subject="寒武纪", metric="营业收入")
    assert len(graph.facts) == 1
    assert graph.facts[0].value == Decimal("5995573619.61")
    assert graph.facts[0].provenance["column"] == "本期数"
