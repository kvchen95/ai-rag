"""时序事实与文档期间图：从表格提取、按期间检索、同比比较。"""

from __future__ import annotations

from decimal import Decimal

from api.temporal.facts import extract_table_facts
from api.temporal.graph import TemporalKnowledgeGraph

TABLE = """| 项目 | 本期数 | 上年同期数 | 变化幅度（%） |
| --- | --- | --- | --- |
| 营业收入 | 5,995,573,619.61 | 2,880,643,471.09 | 108.13 |
| 研发投入合计 | 702,466,071.26 | 541,895,478.65 | 29.63 |
| 资本化研发投入 | - | - | / |"""


def test_table_facts_keep_current_and_prior_periods() -> None:
    facts = extract_table_facts(
        chunk_id="ch_fin",
        doc_key="reports/han-wu-ji-2026-h1",
        subject="寒武纪",
        report_period_start="2026-01-01",
        report_period_end="2026-06-30",
        table_md=TABLE,
        unit="元",
    )
    by_period = {(fact.metric, fact.period_start): fact for fact in facts}
    current = by_period[("营业收入", "2026-01-01")]
    prior = by_period[("营业收入", "2025-01-01")]
    assert current.value == Decimal("5995573619.61")
    assert current.period_end == "2026-06-30"
    assert prior.value == Decimal("2880643471.09")
    assert prior.period_end == "2025-06-30"
    assert current.source_chunk_id == "ch_fin"


def test_graph_series_is_sorted_and_comparable() -> None:
    facts = extract_table_facts(
        chunk_id="ch_fin",
        doc_key="reports/han-wu-ji-2026-h1",
        subject="寒武纪",
        report_period_start="2026-01-01",
        report_period_end="2026-06-30",
        table_md=TABLE,
        unit="元",
    )
    graph = TemporalKnowledgeGraph(facts)
    series = graph.series("寒武纪", "营业收入")
    assert [item.period_start for item in series] == ["2025-01-01", "2026-01-01"]

    comparison = graph.compare("寒武纪", "营业收入", "2026-01-01", "2026-06-30")
    assert comparison is not None
    assert comparison.current.value == Decimal("5995573619.61")
    assert comparison.prior is not None
    assert comparison.prior.value == Decimal("2880643471.09")
    assert comparison.delta == Decimal("3114930148.52")
    assert comparison.pct_change is not None
    assert comparison.pct_change.quantize(Decimal("0.01")) == Decimal("108.13")


def test_graph_rejects_cross_unit_comparison() -> None:
    graph = TemporalKnowledgeGraph(
        extract_table_facts(
            chunk_id="ch_a",
            doc_key="a",
            subject="寒武纪",
            report_period_start="2026-01-01",
            report_period_end="2026-06-30",
            table_md="| 项目 | 本期数 |\n| --- | --- |\n| 研发投入 | 70,246.61 |",
            unit="元",
        )
        + extract_table_facts(
            chunk_id="ch_b",
            doc_key="b",
            subject="寒武纪",
            report_period_start="2026-01-01",
            report_period_end="2026-06-30",
            table_md="| 项目 | 本期数 |\n| --- | --- |\n| 研发投入 | 11.72 |",
            unit="%",
        )
    )
    assert graph.compare("寒武纪", "研发投入", "2026-01-01", "2026-06-30") is None


def test_change_rate_column_is_not_treated_as_prior_period() -> None:
    table = """| 项目 | 本报告期 | 本报告期比上年同期增减变动幅度（%） | 年初至报告期末 |
| --- | --- | --- | --- |
| 营业收入 | 665,044,497.90 | 69.93 | 1,817,290,048.48 |"""
    facts = extract_table_facts(
        chunk_id="ch_q3",
        doc_key="unitree-q3",
        subject="宇树科技",
        report_period_start="2026-07-01",
        report_period_end="2026-09-30",
        table_md=table,
        unit="元",
    )
    assert {(fact.metric, fact.period_start, fact.value) for fact in facts} == {
        ("营业收入", "2026-07-01", Decimal("665044497.90")),
        ("营业收入", "2026-01-01", Decimal("1817290048.48")),
    }
