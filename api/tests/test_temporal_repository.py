"""TemporalFactRepository 的行映射与 upsert 参数形状。."""

from __future__ import annotations

from decimal import Decimal

from api.temporal.facts import TemporalFact
from api.temporal.repository import TemporalFactRepository


class _Cursor:
    """_Cursor."""
    def __init__(self) -> None:
        """Initialize the instance."""
        self.rows = []
        self.executed = []

    def executemany(self, sql, rows):
        """Run executemany."""
        self.executed.append((sql, list(rows)))

    def __enter__(self):
        """Enter the context manager."""
        return self

    def __exit__(self, *args):
        """Exit the context manager."""
        return


class _Conn:
    """_Conn."""
    def __init__(self, cursor):
        """Initialize the instance."""
        self._cursor = cursor

    def cursor(self):
        """Run cursor."""
        return self._cursor

    def __enter__(self):
        """Enter the context manager."""
        return self

    def __exit__(self, *args):
        """Exit the context manager."""
        return


class _Pg:
    """_Pg."""
    def __init__(self, cursor=None, rows=None):
        """Initialize the instance."""
        self._cursor = cursor or _Cursor()
        self._rows = rows or []

    def connection(self):
        """Run connection."""
        return _Conn(self._cursor)

    def fetch_all(self, sql, params):
        """Run fetch_all."""
        return self._rows


def _fact() -> TemporalFact:
    """Run _fact."""
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
    """Run test_upsert_writes_one_row_per_fact."""
    pg = _Pg()
    assert TemporalFactRepository(pg).upsert([_fact()]) == 1
    sql, rows = pg._cursor.executed[0]
    assert "INSERT INTO temporal_facts" in sql
    assert rows[0][0:3] == ("tf_1", "寒武纪", "营业收入")


def test_load_maps_numeric_and_json_columns() -> None:
    """Run test_load_maps_numeric_and_json_columns."""
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
