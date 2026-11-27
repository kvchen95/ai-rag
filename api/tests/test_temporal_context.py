"""时序图查询解析与证据合并。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from api.generate.evidence import Evidence, EvidencePack
from api.qa.orchestrator import QaService
from api.temporal.context import merge_evidence, temporal_evidence
from api.temporal.facts import TemporalFact
from api.temporal.graph import TemporalKnowledgeGraph
from api.temporal.query import resolve_fact_query
from api.temporal.types import TemporalScope
from api.time_range import TimeRange


def _fact(metric: str, year: int, value: str, unit: str = "元") -> TemporalFact:
    return TemporalFact(
        fact_id=f"tf_{metric}_{year}",
        subject="寒武纪",
        metric=metric,
        value=Decimal(value),
        unit=unit,
        period_start=f"{year}-01-01",
        period_end=f"{year}-06-30",
        period_granularity="half_year",
        source_chunk_id="ch_shared",
        doc_key=f"reports/cambricon-{year}-h1",
    )


def test_resolve_query_from_scope() -> None:
    graph = TemporalKnowledgeGraph(
        [
            _fact("研发投入合计", 2026, "702466071.26"),
            _fact("研发投入合计", 2025, "541895478.65"),
            _fact("研发投入总额占营业收入比例（%）", 2026, "11.72", "%"),
            _fact("研发投入总额占营业收入比例（%）", 2025, "18.81", "%"),
        ]
    )
    scope = TemporalScope(
        question="寒武纪 2026 上半年研发投入",
        document_ranges=(TimeRange(20260101, 20260630, "half_year", "2026上半年", "explicit"),),
    )
    query = resolve_fact_query("寒武纪 2026 上半年研发投入同比和占营业收入比例", graph, scope)
    assert query is not None
    assert query.subject == "寒武纪"
    assert query.metric == "研发投入合计"
    assert "研发投入总额占营业收入比例（%）" in query.all_metrics
    assert (query.period_start, query.period_end) == ("2026-01-01", "2026-06-30")
    assert (query.prior_start, query.prior_end) == ("2025-01-01", "2025-06-30")


def test_revenue_query_does_not_pull_rd_ratio() -> None:
    graph = TemporalKnowledgeGraph(
        [
            replace(
                _fact("营业收入", 2026, "5855689454.32"),
                fact_id="tf_rev_2026q3",
                period_start="2026-07-01",
                period_end="2026-09-30",
            ),
            _fact("研发投入占营业收入的比例（%）", 2026, "31.08", "%"),
        ]
    )
    scope = TemporalScope(
        question="",
        document_ranges=(TimeRange(20260701, 20260930, "quarter", "2026Q3", "explicit"),),
    )
    query = resolve_fact_query("寒武纪 2026 年第三季度营业收入是多少", graph, scope)
    assert query is not None
    assert query.all_metrics == ("营业收入",)


def test_resolve_query_returns_none_without_metric() -> None:
    graph = TemporalKnowledgeGraph([_fact("研发投入合计", 2026, "702466071.26")])
    assert resolve_fact_query("寒武纪经营质量如何", graph, None) is None


def test_resolve_query_uses_previous_quarter_for_qoq() -> None:
    graph = TemporalKnowledgeGraph(
        [
            replace(
                _fact("毛利率", 2026, "35.0", "%"),
                fact_id="tf_gm_2026q3",
                period_start="2026-07-01",
                period_end="2026-09-30",
            ),
            replace(
                _fact("毛利率", 2026, "34.0", "%"),
                fact_id="tf_gm_2026q2",
                period_start="2026-04-01",
                period_end="2026-06-30",
            ),
        ]
    )
    scope = TemporalScope(
        question="",
        document_ranges=(TimeRange(20260701, 20260930, "quarter", "2026Q3", "explicit"),),
    )
    query = resolve_fact_query("寒武纪 2026 年第三季度毛利率较上一季度变化多少", graph, scope)
    assert query is not None
    assert (query.prior_start, query.prior_end) == ("2026-04-01", "2026-06-30")


def test_temporal_evidence_merges_periods_from_same_chunk(monkeypatch) -> None:
    graph = TemporalKnowledgeGraph(
        [_fact("研发投入合计", 2026, "702466071.26"), _fact("研发投入合计", 2025, "541895478.65")]
    )
    query = resolve_fact_query(
        "寒武纪 2026 上半年研发投入",
        graph,
        TemporalScope(
            question="",
            document_ranges=(TimeRange(20260101, 20260630, "half_year", "2026上半年", "explicit"),),
        ),
    )
    assert query is not None

    class _Chunk:
        chunk_id = "ch_shared"
        doc_key = "reports/cambricon-2026-h1"
        doc_name = "寒武纪2026半年报.pdf"
        page_num = 18
        bbox = []
        chunk_type = "table"

    def _fetch(_pg, _ids):
        return {"ch_shared": _Chunk()}

    monkeypatch.setattr("api.temporal.context.fetch_texts", _fetch)
    items = temporal_evidence(graph, query, object(), start_n=1)
    assert len(items) == 1
    assert "702466071.26" in items[0].text or "702,466,071.26" in items[0].text
    assert "541895478.65" in items[0].text or "541,895,478.65" in items[0].text


def test_merge_evidence_renumbers_and_deduplicates() -> None:
    base = EvidencePack(items=(Evidence(1, "ch_a", ("ch_a",), "d", "n", 1, [], "x", "text"),))
    extra = (
        Evidence(1, "ch_b", ("ch_b",), "d", "n", 1, [], "y", "text"),
        Evidence(2, "ch_a", ("ch_a",), "d", "n", 1, [], "z", "text"),
    )
    merged = merge_evidence(base, extra)
    assert [item.n for item in merged.items] == [1, 2]
    assert merged.items[1].chunk_id == "ch_b"


def test_qa_service_appends_graph_evidence_when_query_matches(monkeypatch) -> None:
    graph = TemporalKnowledgeGraph(
        [_fact("研发投入合计", 2026, "702466071.26"), _fact("研发投入合计", 2025, "541895478.65")]
    )

    class _Repo:
        def load(self):
            return graph

    service = QaService(
        pipeline=SimpleNamespace(pg=object()),  # type: ignore[arg-type]
        llm=None,
        judge_client=None,
        evidence_max_chars=6000,
        temporal_repo=_Repo(),  # type: ignore[arg-type]
    )
    base = EvidencePack(items=(Evidence(1, "ch_base", ("ch_base",), "d", "n", 1, [], "x", "text"),))
    snapshot = SimpleNamespace(
        plan=SimpleNamespace(
            temporal=TemporalScope(
                question="",
                document_ranges=(TimeRange(20260101, 20260630, "half_year", "2026上半年", "explicit"),),
            )
        )
    )
    monkeypatch.setattr(
        "api.qa.orchestrator.temporal_evidence",
        lambda *args, **kwargs: (
            Evidence(2, "ch_graph", ("ch_graph",), "d", "n", 1, [], "graph", "text"),
        ),
    )
    merged = service._augment_temporal_graph("寒武纪 2026 上半年研发投入", snapshot, base)
    assert len(merged.items) == 2
    assert merged.items[1].chunk_id == "ch_graph"
