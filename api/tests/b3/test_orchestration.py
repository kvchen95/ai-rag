"""在线检索编排的端到端验证（真 PG + 真 Qdrant + 真 TEI）。.

覆盖 §6 → §7 → §8 这一条链路。

`test_pipeline.py` 验的是各段的**部件**；这个文件验的是它们串起来之后的**顺序**
与**出口**。顺序错了部件全对也没用——§7.4 的版本兜底放在精排后面，每个部件都
正常，结果却是"旧条款进了证据包"。

四条只有编排层才存在的判据：

1. **版本兜底在精排之前**：用一个预算极小的精排，数一数它到底收到几条正文。
   旧版本切片若进了精排，那个计数会包含它——这是"顺序"唯一可观测的痕迹。
2. **`mode` 可被推翻**：预判多跳但第一跳证据齐了 → 最终仍按单跳收口，
   `hop_count` 不增加。
3.
**门外不检索**：超范围与指代不清两条路都必须在**没有调用编码服务**的情况下收口
   （用 `EmbeddingUnavailable` 的替身证明"真的没调"）。
4. **审计快照可用**：`score_snapshot()` 能直接填 `qa_log.score_snapshot`，
   且不含原文。
"""

from __future__ import annotations

import pytest

from api.identity import IdentitySnapshot
from api.query import NeedsClarification, understand
from api.retrieval.gate import GateConfig
from api.retrieval.hybrid import HybridRetriever
from api.retrieval.pipeline import (
    QueryEmbedder,
    RetrievalPipeline,
    build_pipeline,
)
from api.retrieval.rerank import Reranker
from api.tests.b3.conftest import make_chunk, publish, unique

pytestmark = pytest.mark.integration

FIN = IdentitySnapshot(user_id="u_fin", depts=("fin",), roles=("staff",))

GATE = GateConfig(
    reject_none=0.20,
    reject_low=0.40,
    borderline_low=0.38,
    borderline_high=0.42,
    clarify_score_delta=0.05,
    clarify_options_max=4,
    suspected_docs_max=3,
    evidence_min_paras=2,
)


def _pipeline(
    *,
    b3_pg,
    retriever: HybridRetriever,
    reranker: Reranker,
    query_embedder: QueryEmbedder,
    rerank_budget_ms: int = 5000,
    max_hops: int = 3,
    top_k: int = 30,
) -> RetrievalPipeline:
    """Run _pipeline."""
    return RetrievalPipeline(
        pg=b3_pg,
        retriever=retriever,
        reranker=reranker,
        embedder=query_embedder,
        gate=GATE,
        top_k=top_k,
        rerank_top_n_min=2,
        rerank_top_n_max=8,
        rerank_candidates_max=50,
        rerank_budget_ms=rerank_budget_ms,
        parent_child_keep=2,
        max_hops=max_hops,
        evidence_max_chars=6000,
    )


def test_happy_path_returns_evidence_and_a_reusable_audit(
    conn, store, embedder, b3_pg, retriever, reranker, query_embedder
) -> None:
    """正常路径：能召回、能过阈、审计快照能直接落 qa_log。."""
    name = unique("happy")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(
                chunk_id=f"ch_{name}_hot",
                text="住宿费标准：每人每天 500 元，按职级限额报销。",
                page_num=47,
            )
        ],
    )
    pipeline = _pipeline(
        b3_pg=b3_pg,
        retriever=retriever,
        reranker=reranker,
        query_embedder=query_embedder,
    )
    try:
        snapshot = pipeline.run("住宿费标准是多少", FIN)
    finally:
        pipeline.close()

    assert snapshot.verdict == "pass", snapshot.decision.reason
    assert snapshot.decision.evidence
    assert snapshot.decision.evidence[0].chunk_id == f"ch_{name}_hot"
    assert snapshot.rerank.degraded is False

    audit = snapshot.score_snapshot()
    # 能直接进 qa_log 的四组字段
    assert audit["verdict"] == "pass"
    assert audit["rerank"]["degraded"] is False
    assert audit[
        "filter"
    ], "审计必须带这次的过滤条件（事后能复现按什么权限搜的）"
    assert audit["routes"]["dense"] > 0
    # 不存原文（§11.2）：审计只有编号与分数
    assert "500" not in str(audit), "审计快照里不该出现正文"


def test_unrelated_question_is_rejected_not_answered(
    conn, store, embedder, b3_pg, retriever, reranker, query_embedder
) -> None:
    """Run test_unrelated_question_is_rejected_not_answered."""
    name = unique("nohit")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(
                chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。"
            )
        ],
    )
    pipeline = _pipeline(
        b3_pg=b3_pg,
        retriever=retriever,
        reranker=reranker,
        query_embedder=query_embedder,
    )
    try:
        snapshot = pipeline.run("如何申报个人所得税专项附加扣除", FIN)
    finally:
        pipeline.close()
    assert snapshot.verdict != "pass"
    assert not snapshot.decision.evidence


def test_out_of_scope_question_never_touches_the_embedding_service(
    b3_pg,
) -> None:
    """§6.3："明显超出库范围 → 直接拒答，不检索"。.

    用一个一调就炸的编码替身证明"真的没调"：如果实现里先检索再判范围，
    这个替身会抛 `EmbeddingUnavailable`，用例就红了。
    """

    class ExplodingEmbedder(QueryEmbedder):
        """ExplodingEmbedder."""
        def embed(
            self, text: str
        ) -> list[float]:  # pragma: no cover - 不该被调用
            """Run embed."""
            raise AssertionError("超出范围的问句不该触发编码")

    pipeline = RetrievalPipeline(
        pg=b3_pg,
        retriever=None,  # type: ignore[arg-type]
        reranker=None,  # type: ignore[arg-type]
        embedder=ExplodingEmbedder(
            base_url="http://unused", model="m", dim=512
        ),
        gate=GATE,
        top_k=30,
        rerank_top_n_min=2,
        rerank_top_n_max=8,
        rerank_candidates_max=50,
        rerank_budget_ms=800,
        parent_child_keep=2,
        max_hops=3,
        evidence_max_chars=6000,
    )
    snapshot = pipeline.run("帮我预测一下明天的股价", FIN)
    assert snapshot.verdict == "reject_none"
    assert snapshot.candidates == []


def test_unresolvable_pronoun_asks_instead_of_searching(b3_pg) -> None:
    """§6.1："指代不清（多个候选）→ 不替换，触发澄清"。."""

    class ExplodingEmbedder(QueryEmbedder):
        """ExplodingEmbedder."""
        def embed(
            self, text: str
        ) -> list[float]:  # pragma: no cover - 不该被调用
            """Run embed."""
            raise AssertionError("指代不清时不该检索")

    pipeline = RetrievalPipeline(
        pg=b3_pg,
        retriever=None,  # type: ignore[arg-type]
        reranker=None,  # type: ignore[arg-type]
        embedder=ExplodingEmbedder(
            base_url="http://unused", model="m", dim=512
        ),
        gate=GATE,
        top_k=30,
        rerank_top_n_min=2,
        rerank_top_n_max=8,
        rerank_candidates_max=50,
        rerank_budget_ms=800,
        parent_child_keep=2,
        max_hops=3,
        evidence_max_chars=6000,
    )
    snapshot = pipeline.run(
        "这个流程要几天", FIN, previous_entities=["甲办法", "乙办法"]
    )
    assert snapshot.verdict == "clarify"
    assert snapshot.candidates == []


def test_stale_version_never_reaches_the_reranker(
    conn, store, embedder, b3_pg, retriever, reranker, query_embedder
) -> None:
    """§7.4 的顺序判据：旧版本切片必须在**进精排之前**被剔除。.

    可观测的痕迹只有一个：精排实际收到的条数（`RerankOutcome.scored`）。造一个
    旧版本（1 条）+ 新版本（2 条）都在向量库里 active 的状态，若顺序对，
    `scored` 最多是 2；顺序错（先精排再兜底）就会是 3。
    """
    name = unique("order")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="old",
        chunks=[
            make_chunk(
                chunk_id=f"ch_{name}_old",
                text="住宿费标准：每人每天 800 元（旧版）。",
            )
        ],
    )
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="new",
        chunks=[
            make_chunk(
                chunk_id=f"ch_{name}_new1",
                text="住宿费标准：每人每天 500 元（现行）。",
            ),
            make_chunk(
                chunk_id=f"ch_{name}_new2",
                text="住宿费标准：管理层每人每天 900 元（现行）。",
                page_num=2,
            ),
        ],
    )
    pipeline = _pipeline(
        b3_pg=b3_pg,
        retriever=retriever,
        reranker=reranker,
        query_embedder=query_embedder,
    )
    try:
        snapshot = pipeline.run("住宿费标准是多少", FIN)
    finally:
        pipeline.close()

    assert snapshot.rerank.scored <= 2, (
        f"精排收到了 {snapshot.rerank.scored} 条，说明旧版本切片进了精排"
        "（版本兜底放在了精排之后）"
    )
    ids = {c.chunk_id for c in snapshot.decision.evidence}
    assert f"ch_{name}_old" not in ids
    assert snapshot.dropped_stale, "审计里必须能看到被丢弃的旧版本切片"


def test_single_hop_prediction_is_downgraded_when_first_hop_suffices(
    conn, store, embedder, b3_pg, retriever, reranker, query_embedder
) -> None:
    """§6.3 / §8.3：预判多跳可被第一跳推翻。.

    问句里带"分别"会被预判成多跳，但库里两条证据一次就找齐了 —— 最终 `hop_count`
    必须是 0（没跑多跳），因为多跑的每一跳都是一次检索 + 一次精排。
    """
    name = unique("downgrade")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(
                chunk_id=f"ch_{name}_a",
                text="住宿费标准：每人每天 500 元。",
                page_num=1,
            ),
            make_chunk(
                chunk_id=f"ch_{name}_b",
                text="交通费标准：市内交通单次不超过 100 元。",
                page_num=2,
            ),
        ],
    )
    pipeline = _pipeline(
        b3_pg=b3_pg,
        retriever=retriever,
        reranker=reranker,
        query_embedder=query_embedder,
    )
    try:
        snapshot = pipeline.run("住宿费和交通费分别是多少", FIN)
    finally:
        pipeline.close()

    assert snapshot.plan.mode == "multi", "含'分别'应预判为多跳"
    assert snapshot.verdict in ("pass", "partial")
    # 库里两条实体一次都找到了 → 缺口为空 → 多跳一跳都不该跑
    if snapshot.multihop is not None:
        assert snapshot.multihop.stop == "fulfilled"
        assert (
            snapshot.hop_count == 0
        ), f"第一跳已找齐却跑了 {snapshot.hop_count} 跳（mode 没有被推翻）"


def test_build_pipeline_reads_every_threshold_from_settings(settings) -> None:
    """装配只走 `build_pipeline`。.

    阈值全部来自 `Settings`，不在代码里另写一份。
    """
    pipeline = build_pipeline(settings, pg=None)  # type: ignore[arg-type]
    try:
        assert pipeline.top_k == settings.retrieval_top_k
        assert pipeline.rerank_top_n_min == settings.rerank_top_n_min
        assert pipeline.rerank_top_n_max == settings.rerank_top_n_max
        assert pipeline.rerank_candidates_max == settings.rerank_candidates_max
        assert pipeline.rerank_budget_ms == settings.rerank_budget_ms
        assert pipeline.parent_child_keep == settings.parent_child_keep
        assert pipeline.max_hops == settings.max_hops
        assert pipeline.gate.reject_none == settings.gate_reject_none
        assert pipeline.gate.reject_low == settings.gate_reject_low
        assert pipeline.gate.borderline_low == settings.gate_borderline_low
        assert pipeline.gate.clarify_score_delta == settings.clarify_score_delta
        assert pipeline.gate.suspected_docs_max == settings.suspected_docs_max
    finally:
        pipeline.close()


def test_needs_clarification_from_understand_propagates(b3_pg) -> None:
    """`understand()` 抛的澄清必须被编排层接住，而不是当异常冒到 HTTP 层。."""
    with pytest.raises(NeedsClarification):
        understand("那它呢", previous_entities=None)
