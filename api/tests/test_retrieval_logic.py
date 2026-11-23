"""检索纯逻辑（§7.1–§7.6、§8.1–§8.4）：RRF、父片去重、精排降级、门控五态、多跳。

这个文件不碰任何外部服务——两路召回、精排、门控、多跳全都通过**可注入的替身**
驱动。理由不是"图快"，而是这些判据必须能被穷举：

- 门控有五态、各自的边界各有一个"刚好跨过"的值；
- 精排有四种结局（正常 / 超时 / 不可用 / 部分失败）；
- 多跳有五个停止原因。

拿真服务跑这些组合，要么造不出（比如让精排刚好 799ms 返回），要么得改服务端。
所以这里的做法是：**纯逻辑穷举在这里，真服务的行为在 `test_b3_integration.py`**。
两层缺一不可——只有前者会漏掉"API 形状不对"，只有后者会漏掉边界组合。
"""

from __future__ import annotations

import pytest

from api.retrieval.gate import (
    VERDICTS,
    GateConfig,
    decide,
    decorate_clarify_options,
    effective_score,
)
from api.retrieval.hybrid import (
    MISSING_RANK_OFFSET,
    RRF_K,
    Candidate,
    dedupe_parent_children,
    rrf_fuse,
)
from api.retrieval.multihop import (
    Blackboard,
    Fact,
    HopOutcome,
    run_multihop,
)
from api.retrieval.rerank import (
    TEI_MAX_BATCH,
    RerankTimeout,
    RerankUnavailable,
    rerank_candidates,
)

# 一份与 .env.example 一致的门控配置，只在测试里出现。
CFG = GateConfig(
    reject_none=0.20,
    reject_low=0.40,
    borderline_low=0.38,
    borderline_high=0.42,
    clarify_score_delta=0.05,
    clarify_options_max=4,
    suspected_docs_max=3,
    evidence_min_paras=2,
)


def cand(
    chunk_id: str,
    *,
    score: float | None,
    doc_key: str = "policy/a",
    page_num: int = 1,
    parent_id: str | None = None,
    fused_rank: int = 1,
    dense_rank: int | None = 1,
    sparse_rank: int | None = None,
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=f"{doc_key}#v1",
        chunk_type="text",
        page_num=page_num,
        parent_id=parent_id,
        dense_rank=dense_rank,
        sparse_rank=sparse_rank,
        fused_rank=fused_rank,
        rerank_score=score,
    )


# --- RRF（§7.2）--------------------------------------------------------------


def test_rrf_k_matches_qdrant_native_constant() -> None:
    """k=1 不是拍的：实测 Qdrant 1.15.3 原生 RRF 的分数逐值等于 Σ1/(1+名次)。

    （探针记录：两路各返回 3 条时原生给 1.0 / 0.666667 / 0.5 / 0.2 / …
    全部与 `1/(1+rank)` 归一后一致。）改这个常数等于换一套融合语义，
    所以用一条用例把它钉死。
    """
    assert RRF_K == 1


def test_rrf_uses_ranks_not_scores() -> None:
    """分数不可比：稠密是 0..1 余弦，BM25 无上界（实测同一句查询 13.29）。
    融合只看位置，所以把分数换成数量级完全不同的一组，结果必须不变。"""
    dense_a = [("x", 0.9), ("y", 0.8)]
    sparse_a = [("y", 12.0), ("z", 3.0)]
    dense_b = [("x", 0.09), ("y", 0.08)]
    sparse_b = [("y", 120.0), ("z", 30.0)]
    order_a = [c.chunk_id for c in rrf_fuse(dense_a, sparse_a, top_k=30)]
    order_b = [c.chunk_id for c in rrf_fuse(dense_b, sparse_b, top_k=30)]
    assert order_a == order_b


def test_both_routes_hit_ranks_first() -> None:
    """两路都命中的候选排在最前（§7.2 的意图：多路印证更可信）。"""
    fused = rrf_fuse([("a", 0.9), ("b", 0.8)], [("b", 9.0)], top_k=30)
    assert fused[0].chunk_id == "b"


def test_missing_route_gets_top_k_plus_one() -> None:
    """§7.2 边界表："一条只在一路出现，另一路名次记为 31"。"""
    fused = rrf_fuse([("a", 0.9)], [("a", 9.0), ("b", 1.0)], top_k=30)
    by_id = {item.chunk_id: item for item in fused}
    assert by_id["b"].dense_rank is None  # 名次仍是 None（"没命中"是可区分的）
    # 但融合分数用的是 30+1=31 这个位置
    expected = 1.0 / (RRF_K + 31) + 1.0 / (RRF_K + 2)
    assert by_id["b"].fused_score == pytest.approx(expected)
    assert MISSING_RANK_OFFSET == 1


def test_tie_breaks_by_dense_rank() -> None:
    """§7.2 边界表："两路名次和相同，语义名次靠前者优先"。"""
    # a: dense 1 / sparse 3；b: dense 3 / sparse 1 → 名次和相同，a 该在前
    fused = rrf_fuse([("a", 0.9), ("m1", 0.8), ("b", 0.7)], [("b", 9.0), ("m2", 8.0), ("a", 7.0)], top_k=30)
    assert fused[0].chunk_id == "a"


def test_same_input_gives_same_order() -> None:
    """确定性：同一份数据两次融合顺序一致，否则排障时会以为检索在漂移。"""
    dense = [("a", 0.9), ("b", 0.8)]
    sparse = [("c", 9.0), ("d", 8.0)]
    assert [c.chunk_id for c in rrf_fuse(dense, sparse, top_k=30)] == [
        c.chunk_id for c in rrf_fuse(dense, sparse, top_k=30)
    ]


def test_fused_rank_is_one_based_and_contiguous() -> None:
    fused = rrf_fuse([("a", 1.0), ("b", 0.9)], [("c", 9.0)], top_k=30)
    assert [item.fused_rank for item in fused] == [1, 2, 3]


# --- 父片去重（§7.2 边界表）-------------------------------------------------


def test_third_child_of_same_parent_is_dropped() -> None:
    items = [
        cand("c1", score=0.9, parent_id="p1"),
        cand("c2", score=0.8, parent_id="p1"),
        cand("c3", score=0.7, parent_id="p1"),
    ]
    kept, dropped = dedupe_parent_children(items, keep=2)
    assert [item.chunk_id for item in kept] == ["c1", "c2"]
    assert [item.chunk_id for item in dropped] == ["c3"]


def test_parent_rows_are_not_affected() -> None:
    """父片（parent_id 为空）不参与这条规则——规则说的是"同父片的子片"。"""
    items = [cand("p1", score=0.9), cand("p2", score=0.8), cand("p3", score=0.7)]
    kept, dropped = dedupe_parent_children(items, keep=2)
    assert len(kept) == 3 and not dropped


def test_keep_zero_disables_dedup_explicitly() -> None:
    items = [cand(f"c{i}", score=1.0 - i / 10, parent_id="p1") for i in range(3)]
    kept, dropped = dedupe_parent_children(items, keep=0)
    assert not kept and len(dropped) == 3


def test_negative_keep_is_rejected() -> None:
    """配置错误不能静默变成"全留"——那会让一段本该短的召回悄悄变长。"""
    with pytest.raises(ValueError, match=">= 0"):
        dedupe_parent_children([cand("c1", score=0.9, parent_id="p1")], keep=-1)


# --- 精排（§7.3）-------------------------------------------------------------


class FakeReranker:
    """可编排结局的精排替身：正常、超时、不可用、部分失败。

    故意**不继承** `Reranker`：继承会带来"替身与真实实现共享方法体"的风险——
    那样测的就不再是编排逻辑，而是替身自己。用 `# type: ignore[arg-type]` 换来
    "两者只共享 `score()` 这一条契约"，比为了让类型检查闭嘴去继承更安全。
    """

    max_batch = TEI_MAX_BATCH

    def __init__(
        self,
        *,
        scores: dict[str, float] | None = None,
        fail_on_call: int | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self.scores = scores or {}
        self.fail_on_call = fail_on_call
        self.delay_s = delay_s
        self.calls: list[list[str]] = []

    def score(self, query: str, texts, *, timeout_s: float):
        import time

        self.calls.append(list(texts))
        if self.delay_s:
            # 服务端"需要" delay_s 才返回：给的时间不够就超时抛错，够就正常返回。
            # 这才是超时的定义（"没在给的时间里返回"），拿"睡满 timeout"演会把
            # 每一批都演成超时，测出来的其实是"必超时"而不是"预算共享"。
            if self.delay_s > timeout_s:
                time.sleep(max(timeout_s, 0.0))
                raise RerankTimeout(f"scripted timeout after {timeout_s*1000:.0f}ms")
            time.sleep(self.delay_s)
        if self.fail_on_call is not None and len(self.calls) - 1 >= self.fail_on_call:
            raise RerankUnavailable("scripted failure")
        return [self.scores.get(text[:20], 0.5) for text in texts]


def _texts_for(items: list[Candidate]) -> dict[str, str]:
    return {item.chunk_id: f"{item.chunk_id} 的正文" for item in items}


def test_rerank_orders_by_score_and_windows() -> None:
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(6)]
    texts = _texts_for(items)
    scores = {f"c{i} 的正文": (6 - i) / 10 for i in range(6)}
    outcome = rerank_candidates(
        FakeReranker(scores=scores),
        "q",
        items,
        texts,
        budget_ms=800,
        top_n_min=2,
        top_n_max=3,
        candidates_max=50,
    )
    assert not outcome.degraded
    assert len(outcome.ordered) == 3
    assert outcome.ordered[0].chunk_id == "c0"


def test_unscored_candidates_never_get_a_zero_filled_score() -> None:
    """降级时不能给候选填 0 分：0 分会让门控判成"无相关"，
    而真相是"我们没打分"。填 0 等于伪造一个结论。"""
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(4)]
    outcome = rerank_candidates(
        FakeReranker(fail_on_call=0),
        "q",
        items,
        _texts_for(items),
        budget_ms=800,
        top_n_min=2,
        top_n_max=3,
        candidates_max=50,
    )
    assert outcome.degraded is True
    assert all(item.rerank_score is None for item in outcome.ordered)


def test_rerank_unavailable_degrades_to_fused_order_and_records_reason() -> None:
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(4)]
    outcome = rerank_candidates(
        FakeReranker(fail_on_call=0),
        "q",
        items,
        _texts_for(items),
        budget_ms=800,
        top_n_min=2,
        top_n_max=3,
        candidates_max=50,
    )
    assert outcome.scored == 0
    assert outcome.reason == "rerank_unavailable"
    assert outcome.fallback == "fused_rank"
    assert [item.chunk_id for item in outcome.ordered] == ["c0", "c1", "c2"]
    audit = outcome.as_audit()
    assert audit["degraded"] is True and audit["degrade"] == "rerank_unavailable"


def test_partial_failure_keeps_scored_batches_and_marks_partial() -> None:
    """部分失败：已打分的照用，未打分的按融合名次垫后，原因必须是 `rerank_partial`。

    把这种情况记成 `rerank_unavailable` 会让运维以为"精排整个挂了"，
    而实际上是"候选太多、第二批超时"——两者的处理完全不同。
    """
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(TEI_MAX_BATCH + 5)]
    texts = _texts_for(items)
    scores = {texts[f"c{i}"]: 0.9 - i * 0.01 for i in range(TEI_MAX_BATCH)}
    outcome = rerank_candidates(
        FakeReranker(scores=scores, fail_on_call=1),
        "q",
        items,
        texts,
        budget_ms=10_000,
        top_n_min=2,
        top_n_max=8,
        candidates_max=50,
    )
    assert outcome.degraded is True
    assert outcome.reason == "rerank_partial"
    assert outcome.scored == TEI_MAX_BATCH
    # 打过分的那批按分数排，没打分的不出现在前 8 里
    assert all(item.rerank_score is not None for item in outcome.ordered)


def test_timeout_and_unavailable_are_distinguishable() -> None:
    """超时与服务不可用必须是两个码（§7.3 的审计要能指向不同的处置）。

    实测撞过：`httpx.ReadTimeout` 被统一包成 `RerankUnavailable`，于是"预算 1ms"
    与"精排挂了"在审计里是同一个 `degrade`，事后根本不知道该调预算还是看容器。
    """
    assert issubclass(RerankTimeout, RerankUnavailable), (
        "超时是不可用的特例，所以继承；但两者的 reason 必须分开"
    )
    assert RerankTimeout is not RerankUnavailable


def test_budget_expiry_is_reported_as_timeout_not_unavailable() -> None:
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(4)]
    outcome = rerank_candidates(
        FakeReranker(delay_s=1.0),
        "q",
        items,
        _texts_for(items),
        budget_ms=20,
        top_n_min=2,
        top_n_max=3,
        candidates_max=50,
    )
    assert outcome.degraded is True
    assert outcome.reason == "rerank_timeout"


def test_budget_is_shared_across_batches_not_per_batch() -> None:
    """deadline 是整段精排共享的：两批各 800ms 会变成 1600ms，预算就没有意义了。

    替身在"给定超时"之后抛 `RerankUnavailable`（`delay_s` 模拟服务端慢）。
    两批各用掉全部预算 → 总耗时约等于一个预算，而不是两个。
    """
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(TEI_MAX_BATCH + 5)]
    outcome = rerank_candidates(
        FakeReranker(delay_s=0.08),
        "q",
        items,
        _texts_for(items),
        budget_ms=200,
        top_n_min=2,
        top_n_max=8,
        candidates_max=50,
    )
    # 两批各 ~80ms 仍在 200ms 内 → 都该跑完，不降级
    assert outcome.elapsed_ms < 200
    assert outcome.degraded is False
    assert outcome.scored == TEI_MAX_BATCH + 5


def test_budget_expiry_degrades_and_does_not_start_another_batch() -> None:
    """预算用完就降级，不再发起下一批：发起出去的那一批只能超时白等。"""
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(TEI_MAX_BATCH * 3)]
    reranker = FakeReranker(delay_s=0.1)
    outcome = rerank_candidates(
        reranker,
        "q",
        items,
        _texts_for(items),
        budget_ms=120,
        top_n_min=2,
        top_n_max=8,
        candidates_max=50,
    )
    assert outcome.degraded is True
    assert len(reranker.calls) <= 2, f"预算已用完，不该再发第 3 批（实发 {len(reranker.calls)} 批）"


def test_candidates_are_capped_before_batching() -> None:
    """§7.5："候选上限 50，超了按名次截"。先截再分批，预算才是可估的。"""
    items = [cand(f"c{i}", score=None, fused_rank=i + 1) for i in range(80)]
    reranker = FakeReranker()
    rerank_candidates(
        reranker,
        "q",
        items,
        _texts_for(items),
        budget_ms=10_000,
        top_n_min=2,
        top_n_max=8,
        candidates_max=40,
    )
    assert sum(len(call) for call in reranker.calls) == 40


def test_empty_candidates_do_not_call_the_model() -> None:
    reranker = FakeReranker()
    outcome = rerank_candidates(
        reranker, "q", [], {}, budget_ms=800, top_n_min=2, top_n_max=8, candidates_max=50
    )
    assert reranker.calls == [] and outcome.ordered == [] and not outcome.degraded


# --- 两路同 filter（§7.1）----------------------------------------------------


def test_both_routes_get_the_same_filter_object() -> None:
    """§7.1："两路必须带完全相同的 filter"。

    断言的是**同一个 dict 对象**（`is`），不是"内容相等"。两份内容相同的 dict
    仍然可以在未来某次改动里漂移，同一个对象不会。

    这条用例在纯逻辑层就能跑，所以"两路 filter 分裂"这个变异一定会被它抓到——
    不需要真 Qdrant。这是刻意把判据搬到能廉价穷举的一层。
    """
    from api.filters import build_filter
    from api.identity import IdentitySnapshot
    from api.retrieval.hybrid import build_batch_body

    user = IdentitySnapshot(user_id="u_fin", depts=("fin",), roles=("staff",))
    body, filter_json = build_batch_body(
        "住宿费标准", [0.1] * 8, build_filter(user), top_k=30
    )
    dense, sparse = body["searches"][0], body["searches"][1]
    assert dense["filter"] is sparse["filter"], "两路的 filter 必须是同一个对象"
    assert dense["filter"] is filter_json
    # ACL 组真的在里面（不是只有 status）
    assert any("should" in item for item in filter_json["must"])
    assert dense["using"] == "dense" and sparse["using"] == "bm25"


def test_sparse_query_goes_through_the_shared_tokeniser() -> None:
    """关键词路的查询体必须过 `bm25_text()`（A3 实测：漏改会掉命中）。

    断言"查询串 != 原文"这件事：中文整句若原样发出，服务端只切 1 个 token。
    """
    from api.filters import build_filter
    from api.identity import IdentitySnapshot
    from api.retrieval.hybrid import build_batch_body

    user = IdentitySnapshot(user_id="u1")
    text = "住宿费标准每人每天多少元"
    body, _ = build_batch_body(text, [0.1] * 8, build_filter(user), top_k=30)
    sparse_query = body["searches"][1]["query"]
    assert sparse_query["text"] != text, "稀疏路必须用 bm25_text() 预处理过的串"
    assert " " in sparse_query["text"], "bigram 切分后的串应该含空格"
    assert sparse_query["model"] == "qdrant/bm25"


def test_dense_route_is_not_tokenised() -> None:
    """稠密路发的是向量，不是文本——两路的"查询"形态本就不同，容易写反。"""
    from api.filters import build_filter
    from api.identity import IdentitySnapshot
    from api.retrieval.hybrid import build_batch_body

    user = IdentitySnapshot(user_id="u1")
    body, _ = build_batch_body("住宿费标准", [0.5] * 8, build_filter(user), top_k=30)
    dense_query = body["searches"][0]["query"]
    assert isinstance(dense_query, list) and len(dense_query) == 8


def test_empty_query_is_rejected_before_calling_qdrant() -> None:
    from api.filters import build_filter
    from api.identity import IdentitySnapshot
    from api.retrieval.hybrid import build_batch_body

    with pytest.raises(ValueError, match="不能为空"):
        build_batch_body("  ", [0.1] * 8, build_filter(IdentitySnapshot(user_id="u1")), top_k=30)


# --- 版本兜底（§7.4）---------------------------------------------------------


class _FakePg:
    """只回答 `active_version_map` 那一条 SQL 的替身。

    用它而不是真 PG：`drop_stale` 的判据是**纯逻辑**（"候选的版本 == 该文档的生效
    版本"），把它绑在真库上会让"去掉版本兜底"这个变异只能在集成测试里被发现，
    而集成测试在 CI 里通常被 skip —— 那样的判据等于没有。
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[list[str]] = []

    def fetch_all(self, sql: str, params=None):
        keys = list((params or [[]])[0])
        self.calls.append(keys)
        return [
            {"doc_key": key, "version_id": self.mapping[key]}
            for key in keys
            if key in self.mapping
        ]


def test_drop_stale_keeps_only_the_active_version() -> None:
    from api.version_guard import drop_stale

    pg = _FakePg({"policy/a": "policy/a#v2"})
    kept, dropped = drop_stale(
        pg,
        [
            {"chunk_id": "ch_new", "doc_key": "policy/a", "version_id": "policy/a#v2"},
            {"chunk_id": "ch_old", "doc_key": "policy/a", "version_id": "policy/a#v1"},
        ],
    )
    assert [item["chunk_id"] for item in kept] == ["ch_new"]
    assert [item["chunk_id"] for item in dropped] == ["ch_old"]
    assert dropped[0]["expected_version_id"] == "policy/a#v2"


def test_drop_stale_is_fail_closed_for_documents_without_active_version() -> None:
    """查不到生效版本 → 该文档的候选全丢（Fail-Closed）。

    少召回会落进低置信门控；多召回会把已废止条款送进答案，两者代价不对等。
    """
    from api.version_guard import drop_stale

    pg = _FakePg({})  # 一个生效版本都没有
    kept, dropped = drop_stale(
        pg, [{"chunk_id": "ch_x", "doc_key": "policy/ghost", "version_id": "policy/ghost#v1"}]
    )
    assert not kept
    assert dropped and dropped[0]["expected_version_id"] is None


def test_drop_stale_queries_each_document_once() -> None:
    """一次 IN 查询把所有文档的生效版本取回来，不是 N 次主键查询。"""
    from api.version_guard import drop_stale

    pg = _FakePg({"policy/a": "policy/a#v1", "policy/b": "policy/b#v1"})
    drop_stale(
        pg,
        [
            {"chunk_id": "c1", "doc_key": "policy/a", "version_id": "policy/a#v1"},
            {"chunk_id": "c2", "doc_key": "policy/a", "version_id": "policy/a#v1"},
            {"chunk_id": "c3", "doc_key": "policy/b", "version_id": "policy/b#v1"},
        ],
    )
    assert len(pg.calls) == 1
    assert sorted(pg.calls[0]) == ["policy/a", "policy/b"]


# --- 门控（§8.1 / §8.2 / §8.4）-----------------------------------------------


def test_verdict_enum_is_exactly_five_states() -> None:
    """§8.4 五态。少一态意味着某个分支没法表达；多一态意味着有人加了没写文档的出口。"""
    assert set(VERDICTS) == {"pass", "clarify", "reject_none", "reject_low", "partial"}


def test_fused_score_is_never_used_as_the_gate_score() -> None:
    """融合分不进门槛判定：k=1 时第一名恒为 1.0，拿它比 0.20 等于门控不存在。"""
    item = Candidate(
        chunk_id="a", doc_key="d", version_id="v", chunk_type="text", page_num=1,
        fused_score=1.0, rerank_score=None,
    )
    assert effective_score(item) == 0.0
    assert decide([item], cfg=CFG).verdict == "reject_none"


def test_below_reject_none_is_hard_reject() -> None:
    decision = decide([cand("a", score=0.19)], cfg=CFG)
    assert decision.verdict == "reject_none"
    assert decision.reject_code is not None and decision.reject_code.value == "no_evidence"


def test_between_thresholds_is_low_confidence_with_suspects() -> None:
    items = [
        cand("a", score=0.31, doc_key="policy/x"),
        cand("b", score=0.25, doc_key="policy/y"),
    ]
    decision = decide(items, cfg=CFG)
    assert decision.verdict == "reject_low"
    assert decision.reject_code is not None and decision.reject_code.value == "low_confidence"
    assert [doc.doc_key for doc in decision.suspected_docs] == ["policy/x", "policy/y"]


def test_suspected_docs_are_capped() -> None:
    items = [cand(f"c{i}", score=0.3 - i * 0.01, doc_key=f"policy/{i}") for i in range(5)]
    decision = decide(items, cfg=CFG)
    assert len(decision.suspected_docs) == CFG.suspected_docs_max


def test_at_or_above_reject_low_passes() -> None:
    decision = decide([cand("a", score=0.40)], cfg=CFG)
    assert decision.verdict == "pass"
    assert decision.evidence


def test_bm25_strength_never_overrides_the_gate() -> None:
    """§8.1 边界表："分数刚好低于 0.20 但关键词强命中 → 仍拒答"。

    BM25 分数再高也不构成证据。这条用一条 sparse_score 极高的候选钉住：
    谁把门控改成"看最大值"，这条会立刻红。
    """
    item = Candidate(
        chunk_id="a", doc_key="d", version_id="v", chunk_type="text", page_num=1,
        sparse_score=999.0, dense_score=0.99, rerank_score=0.19,
    )
    assert decide([item], cfg=CFG).verdict == "reject_none"


def test_borderline_band_uses_evidence_count_for_multi_entity() -> None:
    """临界带里多实体问题只有一段证据 → 不单靠分数放行（§8.1）。"""
    items = [cand("a", score=0.39)]
    decision = decide(items, cfg=CFG, multi_entity=True)
    assert decision.verdict == "reject_low"
    assert decision.borderline_used and "段证据" in decision.borderline_used


def test_borderline_band_lets_single_fact_through() -> None:
    decision = decide([cand("a", score=0.39)], cfg=CFG, multi_entity=False)
    assert decision.verdict == "pass"


def test_borderline_band_checks_entity_presence() -> None:
    items = [cand("a", score=0.39), cand("b", score=0.38, page_num=2)]
    texts = {"a": "交通费标准", "b": "体检费用"}
    decision = decide(
        items, cfg=CFG, entities=("住宿费",), texts=texts, multi_entity=False
    )
    assert decision.verdict == "reject_low"
    assert decision.borderline_used and "实体" in decision.borderline_used


def test_borderline_band_passes_when_entity_is_present() -> None:
    items = [cand("a", score=0.39), cand("b", score=0.38, page_num=2)]
    decision = decide(
        items, cfg=CFG, entities=("住宿费",), texts={"a": "住宿费限额 500 元", "b": "x"}
    )
    assert decision.verdict == "pass"


def test_multi_entity_with_one_paragraph_is_rejected_even_when_scored_high() -> None:
    """§8.1 边界表："最高分高但只有一条证据 → 多实体问题拒答"。"""
    decision = decide([cand("a", score=0.85)], cfg=CFG, multi_entity=True)
    assert decision.verdict == "reject_low"


def test_single_fact_with_one_evidence_passes() -> None:
    decision = decide([cand("a", score=0.85)], cfg=CFG, multi_entity=False)
    assert decision.verdict == "pass"


def test_two_documents_with_close_scores_trigger_clarify() -> None:
    """§8.2："候选来自两份以上文档且分数接近（差小于 0.05）"→ 澄清。"""
    items = [cand("a", score=0.62, doc_key="policy/x"), cand("b", score=0.60, doc_key="policy/y")]
    decision = decide(items, cfg=CFG, doc_names={"policy/x": "X 办法", "policy/y": "Y 办法"})
    assert decision.verdict == "clarify"
    assert {opt.doc_key for opt in decision.clarify_options} == {"policy/x", "policy/y"}


def test_two_documents_with_clear_winner_both_pass_through() -> None:
    items = [cand("a", score=0.72, doc_key="policy/x"), cand("b", score=0.45, doc_key="policy/y")]
    assert decide(items, cfg=CFG).verdict == "pass"


def test_clarify_options_come_from_evidence_only() -> None:
    """§8.2："选项不是凭空编的，只能从检索到的事实里取"。"""
    items = [cand("a", score=0.6, doc_key="policy/x", page_num=47)]
    options = decorate_clarify_options(items, doc_names={"policy/x": "差旅管理办法"}, max_options=4)
    assert len(options) == 1
    assert options[0].doc_key == "policy/x"
    assert "差旅管理办法" in options[0].label and "47" in options[0].label
    assert options[0].params["doc_keys"] == ["policy/x"]


def test_clarify_options_are_capped_and_deduped_by_document() -> None:
    items = [cand(f"c{i}", score=0.6, doc_key="policy/same") for i in range(3)]
    items += [cand(f"d{i}", score=0.5, doc_key=f"policy/{i}") for i in range(6)]
    options = decorate_clarify_options(items, doc_names={}, max_options=4)
    assert len(options) == 4
    assert len({opt.doc_key for opt in options}) == 4


def test_partial_wins_over_clarify() -> None:
    """已经查到一部分时再问"你要哪个"是本末倒置（§8.3）：先把已查实的给出去。"""
    items = [cand("a", score=0.62, doc_key="policy/x"), cand("b", score=0.61, doc_key="policy/y")]
    decision = decide(items, cfg=CFG, uncovered=("交通费",))
    assert decision.verdict == "partial"
    assert decision.uncovered == ("交通费",)
    assert decision.evidence


def test_degraded_rerank_cannot_be_clarified() -> None:
    """降级时分数不可用 → 不拿"分数接近"去问用户（那个判断本身没有依据）。"""
    items = [cand("a", score=0.62, doc_key="policy/x"), cand("b", score=0.61, doc_key="policy/y")]
    decision = decide(items, cfg=CFG, degraded=True, degrade_reason="rerank_timeout")
    assert decision.verdict == "pass"
    assert decision.degraded is True


def test_empty_candidates_is_reject_none() -> None:
    decision = decide([], cfg=CFG)
    assert decision.verdict == "reject_none" and decision.top1 is None


def test_gate_config_rejects_inverted_thresholds() -> None:
    with pytest.raises(ValueError, match="reject_none"):
        GateConfig(
            reject_none=0.5, reject_low=0.4, borderline_low=0.38, borderline_high=0.42,
            clarify_score_delta=0.05, clarify_options_max=4, suspected_docs_max=3,
            evidence_min_paras=2,
        )


# --- 多跳（§8.3）-------------------------------------------------------------


def _fact(chunk_id: str, hop: int, claim: str = "原文摘录") -> Fact:
    return Fact(claim=claim, chunk_id=chunk_id, page_num=1, hop=hop)


def test_blackboard_dedupes_by_chunk() -> None:
    board = Blackboard()
    assert board.add(_fact("c1", 1)) is True
    assert board.add(_fact("c1", 2, "换个说法")) is False
    assert len(board) == 1


def test_max_hops_is_respected() -> None:
    """§8.3：跳数到 Max Hops = 3 就停。"""
    calls: list[int] = []

    def runner(hop: int, sub: str, gap: str) -> HopOutcome:
        calls.append(hop)
        return HopOutcome(hop=hop, sub_question=sub, gap=gap, passed=True, facts=(_fact(f"c{hop}", hop),))

    result = run_multihop(
        max_hops=3,
        runner=runner,
        # 永远填不满：每个实体都缺
        gap_probe=lambda _b: ("a", "b", "c", "d", "e"),
        sub_questioner=lambda gap: gap,
        initial_gap=("a",),
    )
    assert calls == [1, 2, 3]
    assert result.stop == "max_hops"
    assert result.uncovered  # 三跳后仍缺一部分 → 输出未覆盖清单


def test_stops_early_when_gap_is_filled() -> None:
    calls: list[int] = []

    def runner(hop: int, sub: str, gap: str) -> HopOutcome:
        calls.append(hop)
        return HopOutcome(hop=hop, sub_question=sub, gap=gap, passed=True, facts=(_fact(f"c{hop}", hop),))

    result = run_multihop(
        max_hops=3,
        runner=runner,
        gap_probe=lambda _b: (),
        sub_questioner=lambda gap: gap,
        initial_gap=("a",),
    )
    assert calls == []  # 一开始就没有缺口 → 一跳都不跑
    assert result.stop == "fulfilled"


def test_broken_first_hop_stops_without_facts() -> None:
    """§8.3 边界表："第一跳就断链 → 直接拒答，不进多跳"。"""
    result = run_multihop(
        max_hops=3,
        runner=lambda hop, sub, gap: HopOutcome(
            hop=hop, sub_question=sub, gap=gap, passed=False, top1=0.05
        ),
        gap_probe=lambda _b: ("a",),
        sub_questioner=lambda gap: gap,
        initial_gap=("a",),
    )
    assert result.stop == "empty"
    assert result.facts == ()
    assert result.broken_at == 1


def test_broken_later_hop_keeps_earlier_facts() -> None:
    """断链时交出去的是"已经查实的部分 + 断在哪一步"（§8.3）。"""

    def runner(hop: int, sub: str, gap: str) -> HopOutcome:
        if hop == 2:
            return HopOutcome(hop=hop, sub_question=sub, gap=gap, passed=False)
        return HopOutcome(hop=hop, sub_question=sub, gap=gap, passed=True, facts=(_fact(f"c{hop}", hop),))

    result = run_multihop(
        max_hops=3,
        runner=runner,
        # 永远填不满（缺口始终存在），所以第二跳一定会被执行
        gap_probe=lambda _board: ("still-missing",),
        sub_questioner=lambda gap: gap,
        initial_gap=("a", "b"),
    )
    assert result.stop == "broken"
    assert result.broken_at == 2
    assert len(result.facts) == 1  # 第一跳的事实保住了


def test_conflicting_reading_of_the_same_chunk_stops_the_loop() -> None:
    """§8.3 边界表："子问题与已查实事实冲突 → 停止，输出冲突说明，不强行统一"。"""

    def runner(hop: int, sub: str, gap: str) -> HopOutcome:
        claim = "500 元" if hop == 1 else "800 元"
        return HopOutcome(
            hop=hop, sub_question=sub, gap=gap, passed=True, facts=(_fact("same", hop, claim),)
        )

    result = run_multihop(
        max_hops=3,
        runner=runner,
        gap_probe=lambda _b: ("a", "b"),
        sub_questioner=lambda gap: gap,
        initial_gap=("a",),
    )
    assert result.stop == "conflict"
    assert result.broken_at == 2


def test_each_hop_must_pass_its_own_threshold() -> None:
    """卡片的验收原句："多跳每跳独立过阈"。

    第一跳过阈、第二跳不过 → 整体是 broken，不是"总证据够就继续"。
    """
    seen: list[int] = []

    def runner(hop: int, sub: str, gap: str) -> HopOutcome:
        seen.append(hop)
        passed = hop == 1
        return HopOutcome(
            hop=hop, sub_question=sub, gap=gap, passed=passed,
            top1=0.7 if passed else 0.05,
            facts=(_fact(f"c{hop}", hop),) if passed else (),
        )

    result = run_multihop(
        max_hops=3,
        runner=runner,
        gap_probe=lambda board: () if len(board) >= 2 else ("a", "b"),
        sub_questioner=lambda gap: gap,
        initial_gap=("a", "b"),
    )
    assert seen == [1, 2]
    assert result.stop == "broken" and result.broken_at == 2


def test_max_hops_zero_means_no_multihop() -> None:
    result = run_multihop(
        max_hops=0,
        runner=lambda *_: pytest.fail("不该跑任何一跳"),
        gap_probe=lambda _b: ("a",),
        sub_questioner=lambda gap: gap,
        initial_gap=("a",),
    )
    assert result.stop == "max_hops" and result.hops == ()
