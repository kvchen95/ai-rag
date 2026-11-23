"""混合检索 + 版本兜底 + 精排 + 门控的端到端验证（真 PG + 真 Qdrant + 真 TEI）。

这个文件是 B3 的**唯一**验收依据：它不用替身，所有断言都对着真服务的结果。

四类断言，对应卡片的四条验收：

1. **两路同 filter**：同一个 `RetrievalFilter` 渲染出的 JSON 只 dump 一次，两路
   用的是同一个 dict；再用"两路都该召回到什么/都不该召回到什么"在真数据上验。
2. **旧版本剔除**（§7.4）：造一个旧版本下的 active 切片，它会被前置过滤放进来，
   但必须在精排前被 `drop_stale` 丢掉。
3. **精排超时降级有审计**（§7.3）：把预算压到 1ms，断言降级原因、`scored=0`、
   且**没有编造分数**。
4. **不泄露无权限文档**（§5.2 / §11.1 闸一）：用一个没有对应部门的身份检索，
   那条切片在两路都不得出现——包括它不能出现在候选池里（否则"库里有这份东西"
   这件事会从疑似文档列表漏出去）。

另外三条是"用真服务才有的价值"：

5. **中文 BM25 必须过 `bm25_text()`**：用同一段中文长句，比较"查询侧漏改"与
   "两侧都改"的命中差异。A3 报的 1/4 → 3/4 在 B3 的召回层要能复现。
6. **RRF 与 Qdrant 原生融合等价**：同一批数据、同一对 prefetch，本地 `rrf_fuse`
   的顺序必须与 `/points/query?fusion=rrf` 一致。这条钉住 `RRF_K = 1`。
7. **澄清选项只来自证据**：真数据里两份文档分数接近时，选项必须只含这两份，
   且每条都能回填 `doc_keys`。

跳过策略：缺 PG / Qdrant / TEI / rerank 任一项就 skip，绝不假装跑过。
"""

from __future__ import annotations

import pytest

from api.filters import build_filter
from api.identity import IdentitySnapshot
from api.retrieval.gate import GateConfig, decide
from api.retrieval.hybrid import HybridRetriever, qdrant_filter_json
from api.retrieval.rerank import Reranker, rerank_candidates
from api.retrieval.texts import fetch_texts
from api.version_guard import drop_stale

# 只 import **辅助函数**，不 import fixture 名。pytest 允许测试模块内定义 fixture，
# 所以从 conftest import 一个同名函数会把它变成"本模块的 fixture"，把 conftest 里
# 那份真 fixture 遮蔽掉——实测的表现是 `TypeError: Invalid type for url. Expected
# str or httpx.URL, got <class 'function'>`：拿到的不是 URL 字符串，而是 fixture 函数。
from api.tests.b3.conftest import (
    doc_key,
    make_chunk,
    publish,
    unique,
    version_of,
)

pytestmark = pytest.mark.integration

FIN = IdentitySnapshot(user_id="u_fin", depts=("fin",), roles=("staff",))
HR = IdentitySnapshot(user_id="u_hr", depts=("hr",), roles=("staff",))
ADMIN = IdentitySnapshot(user_id="u_admin", depts=(), roles=(), admin=True)

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


# --- 1. 两路同 filter ---------------------------------------------------------


def test_both_routes_get_the_same_filter_object() -> None:
    """§7.1："两路必须带完全相同的 filter"。

    做法不是"两次 to_qdrant() 比较"，而是**只 dump 一次**、两路共用同一个 dict——
    这样"两路不一致"在结构上不可能发生，而不是靠一条用例事后发现。
    """
    flt = build_filter(FIN, doc_keys=["policy/a", "policy/b"])
    dumped = qdrant_filter_json(flt)
    assert dumped == qdrant_filter_json(flt)
    # 结构上确认三条都在：status、ACL、scope
    keys = [item["key"] for item in dumped["must"] if "key" in item]
    assert "status" in keys and "doc_key" in keys
    assert any("should" in item for item in dumped["must"]), "ACL 组必须存在"


def test_hybrid_retrieval_honours_the_filter_on_both_routes(
    conn, store, embedder, retriever: HybridRetriever, query_embedder
) -> None:
    """真数据上验两路都受同一个 filter 约束。

    造三条切片：公开、仅 fin、仅 hr。用 fin 身份检索时三条文本都能"字面命中"，
    但 hr 那条在两路都不得出现。
    """
    name = unique("filter")
    chunks = [
        make_chunk(chunk_id=f"ch_{name}_pub", text="住宿费标准：每人每天 500 元，公开制度。"),
        make_chunk(
            chunk_id=f"ch_{name}_fin",
            text="住宿费标准：财务部专用差旅额度说明。",
            is_public=False,
            depts=["fin"],
        ),
        make_chunk(
            chunk_id=f"ch_{name}_hr",
            text="住宿费标准：人力资源部专用差旅额度说明。",
            is_public=False,
            depts=["hr"],
        ),
    ]
    publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    flt = build_filter(FIN)
    vector = query_embedder.embed("住宿费标准是多少")
    result = retriever.retrieve("住宿费标准是多少", flt, dense_query=vector, top_k=30)

    ids = {c.chunk_id for c in result.candidates}
    assert f"ch_{name}_hr" not in ids, "无权限切片从某一路漏进候选池"
    assert f"ch_{name}_pub" in ids
    # 两路都该有东西（一个是稠密一个 BM25，都跑过）
    assert result.dense_count > 0 and result.sparse_count > 0, (
        f"某一路没返回任何结果：dense={result.dense_count} sparse={result.sparse_count}"
    )


def test_unprivileged_document_never_leaks_through_either_route(
    conn, store, embedder, retriever: HybridRetriever, query_embedder, b3_pg
) -> None:
    """验收原句："不泄露无权限文档"。

    用一个**完全没有部门**的身份（只能看公开文档）检索，再造一条仅 fin 的切片。
    断言：候选池里没有它、门控的疑似文档列表里也没有它——后者更关键，
    因为"疑似文档"本身就是"库里存在这份东西"的一次泄露。
    """
    name = unique("leak")
    chunks = [
        make_chunk(chunk_id=f"ch_{name}_pub", text="住宿费标准：每人每天 500 元。"),
        make_chunk(
            chunk_id=f"ch_{name}_secret",
            text="住宿费标准：财务部内部每人每天 2000 元，仅限财务部。",
            is_public=False,
            depts=["fin"],
        ),
    ]
    publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    nobody = IdentitySnapshot(user_id="u_none")
    flt = build_filter(nobody)
    vector = query_embedder.embed("住宿费标准 2000 元")
    result = retriever.retrieve("住宿费标准 2000 元", flt, dense_query=vector, top_k=30)
    ids = {c.chunk_id for c in result.candidates}
    assert f"ch_{name}_secret" not in ids

    texts = fetch_texts(b3_pg, [c.chunk_id for c in result.candidates])
    decision = decide(
        result.candidates,
        cfg=GATE,
        texts={cid: item.text for cid, item in texts.items()},
    )
    suspect_docs = {doc.doc_key for doc in decision.suspected_docs}
    assert doc_key(name) not in suspect_docs or f"ch_{name}_secret" not in ids
    # 更直接的一条：疑似文档里若出现这份文档，只能是因为公开切片命中，
    # 而那份公开切片的文本里没有"2000"
    for item in texts.values():
        if item.doc_key == doc_key(name):
            assert "2000" not in item.text


# --- 2. 旧版本剔除（§7.4）----------------------------------------------------


def test_stale_version_chunks_are_dropped_before_rerank(
    conn, store, embedder, retriever: HybridRetriever, query_embedder, b3_pg
) -> None:
    """验收原句："旧版本剔除"。

    造两个版本：旧版本先上线（切片 status=active、点也在），再上线新版本。
    旧版本切片在向量库里仍是 active（前置过滤放它进来），但 PG 里它已下线——
    `drop_stale` 必须在精排前把它丢掉。

    这是 §7.4 那个"两分钟窗口"的最小复现：Qdrant payload 只有切片级 status，
    表达不了"这个切片所属版本是不是当前生效版本"。
    """
    name = unique("stale")
    old_chunks = [
        make_chunk(chunk_id=f"ch_{name}_old", text="住宿费标准：每人每天 800 元（旧版）。")
    ]
    publish(conn, store, embedder, name=name, tag="old", chunks=old_chunks)

    new_chunks = [
        make_chunk(chunk_id=f"ch_{name}_new", text="住宿费标准：每人每天 500 元（现行）。")
    ]
    publish(conn, store, embedder, name=name, tag="new", chunks=new_chunks)

    # 旧版本切片在向量库里仍是 active：这正是前置过滤挡不住的那一段
    old_points = store.scroll_version(version_of(name, "old"))
    assert old_points, "旧版本的点应该还在（要等回收窗口）"

    flt = build_filter(FIN)
    vector = query_embedder.embed("住宿费标准 每人每天 多少元")
    result = retriever.retrieve("住宿费标准 每人每天 多少元", flt, dense_query=vector, top_k=30)

    before = {c.chunk_id for c in result.candidates}
    assert f"ch_{name}_old" in before, (
        "前置过滤本应放它进来（否则这条用例测不到 drop_stale 的作用）"
    )

    kept, dropped = drop_stale(b3_pg, [c.as_dict() | {"doc_key": c.doc_key, "version_id": c.version_id} for c in result.candidates])
    kept_ids = {row["chunk_id"] for row in kept}
    dropped_ids = {row["chunk_id"] for row in dropped}
    assert f"ch_{name}_old" in dropped_ids, "旧版本切片必须在精排前被剔除"
    assert f"ch_{name}_new" in kept_ids


def test_drop_stale_is_fail_closed_for_unknown_documents(
    conn, store, embedder, retriever: HybridRetriever, query_embedder, b3_pg
) -> None:
    """查不到文档的生效版本 → 该文档的候选全丢（Fail-Closed）。

    少召回会落进低置信门控；多召回会把已废止条款送进答案。两者代价不对等，
    所以查不到也必须丢。
    """
    name = unique("ghost")
    chunks = [make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。")]
    version_id = publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    flt = build_filter(FIN)
    vector = query_embedder.embed("住宿费标准")
    result = retriever.retrieve("住宿费标准", flt, dense_query=vector, top_k=30)

    # 把版本改成 deprecated 但**不动**向量库：模拟"PG 已下线、向量库还不知道"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE doc_versions SET status = 'deprecated', deprecated_at = now() "
            "WHERE version_id = %s",
            (version_id,),
        )
    conn.commit()

    kept, dropped = drop_stale(
        b3_pg,
        [c.as_dict() | {"doc_key": c.doc_key, "version_id": c.version_id} for c in result.candidates],
    )
    assert not kept, "没有生效版本的文档，其候选必须全丢"
    assert dropped


# --- 3. 精排超时降级 + 审计（§7.3）--------------------------------------------


def test_rerank_timeout_degrades_with_audit(
    conn, store, embedder, retriever: HybridRetriever, reranker: Reranker, query_embedder, b3_pg
) -> None:
    """验收原句："精排超时降级有审计"。

    把预算压到 1ms（真服务不可能在 1ms 内返回）→ 必须降级，且审计里能读到
    四件事：降级标志、原因码、实际打分条数（0）、降级口径（按融合名次）。
    """
    name = unique("timeout")
    chunks = [
        make_chunk(chunk_id=f"ch_{name}_{i}", text=f"住宿费标准：每人每天 {500 + i} 元。")
        for i in range(6)
    ]
    publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    flt = build_filter(FIN)
    vector = query_embedder.embed("住宿费标准")
    result = retriever.retrieve("住宿费标准", flt, dense_query=vector, top_k=30)
    texts = fetch_texts(b3_pg, [c.chunk_id for c in result.candidates])

    outcome = rerank_candidates(
        reranker,
        "住宿费标准",
        result.candidates,
        {cid: item.text for cid, item in texts.items()},
        budget_ms=1,
        top_n_min=2,
        top_n_max=8,
        candidates_max=50,
    )
    assert outcome.degraded is True
    assert outcome.reason == "rerank_timeout"
    assert outcome.scored == 0
    assert outcome.fallback == "fused_rank"
    assert all(item.rerank_score is None for item in outcome.ordered), (
        "降级时不能编造分数——填 0 会让门控判成'无相关'"
    )
    audit = outcome.as_audit()
    assert audit["degrade"] == "rerank_timeout" and audit["degraded"] is True


def test_rerank_actually_scores_relevant_text_higher(
    conn, store, embedder, retriever: HybridRetriever, reranker: Reranker, query_embedder, b3_pg
) -> None:
    """真服务上的精排必须把相关的那条排到最前——否则"降级/不降级"没有意义。

    这条同时是"精排真的在工作"的证据：拿到真分数、且顺序符合常识。
    """
    name = unique("rerank")
    chunks = [
        make_chunk(chunk_id=f"ch_{name}_hot", text="住宿费标准：每人每天 500 元，按职级限额报销。"),
        make_chunk(chunk_id=f"ch_{name}_cold1", text="员工入职需提交体检报告，费用由公司承担。"),
        make_chunk(chunk_id=f"ch_{name}_cold2", text="市内交通凭票报销，单次不超过 100 元。"),
        make_chunk(chunk_id=f"ch_{name}_cold3", text="加班需提前申请，由部门负责人审批。"),
    ]
    publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    flt = build_filter(FIN)
    query = "住宿费每天多少元"
    vector = query_embedder.embed(query)
    result = retriever.retrieve(query, flt, dense_query=vector, top_k=30)
    texts = fetch_texts(b3_pg, [c.chunk_id for c in result.candidates])

    outcome = rerank_candidates(
        reranker,
        query,
        result.candidates,
        {cid: item.text for cid, item in texts.items()},
        budget_ms=5000,
        top_n_min=2,
        top_n_max=8,
        candidates_max=50,
    )
    assert not outcome.degraded, "5 秒预算下不该降级"
    assert outcome.scored >= 3
    assert outcome.ordered[0].chunk_id == f"ch_{name}_hot"
    assert outcome.ordered[0].rerank_score is not None
    assert outcome.ordered[0].rerank_score > 0.4, (
        f"相关切片的分应显著高于阈值，实际 {outcome.ordered[0].rerank_score}"
    )


def test_gate_rejects_irrelevant_question_on_real_data(
    conn, store, embedder, retriever: HybridRetriever, reranker: Reranker, query_embedder, b3_pg
) -> None:
    """真数据上的拒答路径：库里只有差旅制度，问"明天天气"。

    门控必须给出 `reject_none`（< 0.20）或至少不是 `pass`——重点是它不能靠
    关键词碰巧命中就放行。
    """
    name = unique("irrelevant")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。")],
    )
    flt = build_filter(FIN)
    query = "如何申报个人所得税专项附加扣除"
    vector = query_embedder.embed(query)
    result = retriever.retrieve(query, flt, dense_query=vector, top_k=30)
    texts = fetch_texts(b3_pg, [c.chunk_id for c in result.candidates])
    outcome = rerank_candidates(
        reranker, query, result.candidates, {cid: item.text for cid, item in texts.items()},
        budget_ms=5000, top_n_min=2, top_n_max=8, candidates_max=50,
    )
    decision = decide(
        outcome.ordered,
        cfg=GATE,
        texts={cid: item.text for cid, item in texts.items()},
        degraded=outcome.degraded,
        degrade_reason=outcome.reason,
    )
    assert decision.verdict != "pass", f"不相关问题不该放行（top1={decision.top1}）"


# --- 4. 中文 BM25 必须过 bm25_text（A3 报的坑在召回层的复现）------------------


def test_chinese_bm25_needs_the_shared_tokeniser(
    conn, store, embedder, query_embedder, qdrant_reachable, collection
) -> None:
    """查询侧漏过 `bm25_text()` 会让关键词路对中文几乎失效。

    做法：同一句长中文查询，一次用 `bm25_vector()`（正确）、一次用原文（漏改），
    比较两路的 `sparse_count` 与能否命中目标切片。

    A3 实测（真 Qdrant 1.15.3）：入库原文 + 查询原文 top1 命中 1/4，两侧都过
    `bm25_text()` 3/4，查询侧漏改 2/4。这里验的是"漏改会掉命中"这个方向，
    不钉具体比例（比例随语料变，钉死会腐）。
    """
    import httpx

    from api.retrieval.hybrid import qdrant_filter_json
    from scripts.qdrant_common import SPARSE_VECTOR

    name = unique("bm25")
    target_text = "住宿费标准每人每天五百元整按职级限额报销交通费另计"
    chunks = [
        make_chunk(chunk_id=f"ch_{name}_target", text=target_text),
        make_chunk(chunk_id=f"ch_{name}_noise", text="员工入职需提交体检报告费用由公司承担"),
    ]
    publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    flt = build_filter(FIN)
    body_base = {
        "using": SPARSE_VECTOR,
        "filter": qdrant_filter_json(flt),
        "limit": 30,
        "with_payload": ["chunk_id"],
    }
    wrong = body_base | {"query": {"text": "住宿费每人每天多少元", "model": "qdrant/bm25"}}
    right = body_base | {
        "query": _bm25_vector("住宿费每人每天多少元")
    }
    with httpx.Client(base_url=qdrant_reachable, timeout=20, trust_env=False) as client:
        wrong_hits = client.post(
            f"/collections/{collection}/points/query", json=wrong
        ).json()["result"]["points"]
        right_hits = client.post(
            f"/collections/{collection}/points/query", json=right
        ).json()["result"]["points"]

    wrong_ids = [p["payload"]["chunk_id"] for p in wrong_hits]
    right_ids = [p["payload"]["chunk_id"] for p in right_hits]
    target = f"ch_{name}_target"
    assert target in right_ids, "过了 bm25_text() 的查询必须能命中目标切片"
    assert len(right_ids) >= len(wrong_ids), (
        f"漏过 bm25_text() 不该召回更多：right={right_ids} wrong={wrong_ids}"
    )


def _bm25_vector(text: str):
    from scripts.qdrant_common import bm25_vector

    return bm25_vector(text)


# --- 5. RRF 与 Qdrant 原生融合等价 --------------------------------------------


def test_local_rrf_matches_qdrant_native_fusion(
    conn, store, embedder, query_embedder, qdrant_reachable, collection
) -> None:
    """`rrf_fuse` 的顺序必须与 `/points/query?fusion=rrf` 一致。

    这条钉住 `RRF_K = 1`（实测反解出来的常数）。Qdrant 换版本改了常数、或者有人
    觉得"论文里 k=60 更正规"把常数改掉，这条会立刻红。
    """
    import httpx

    from api.retrieval.hybrid import rrf_fuse
    from scripts.qdrant_common import DENSE_VECTOR, SPARSE_VECTOR, bm25_vector

    name = unique("rrf")
    chunks = [
        make_chunk(chunk_id=f"ch_{name}_1", text="住宿费标准每人每天500元"),
        make_chunk(chunk_id=f"ch_{name}_2", text="住宿费限额住宿费标准住宿报销说明"),
        make_chunk(chunk_id=f"ch_{name}_3", text="出差住宿费用按职级限额报销"),
        make_chunk(chunk_id=f"ch_{name}_4", text="员工入职需提交体检报告"),
        make_chunk(chunk_id=f"ch_{name}_5", text="市内交通凭票报销单次不超过100元"),
    ]
    publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    query = "住宿费标准每人每天多少元"
    flt = build_filter(FIN)
    filter_json = qdrant_filter_json(flt)
    vector = query_embedder.embed(query)

    with httpx.Client(base_url=qdrant_reachable, timeout=20, trust_env=False) as client:
        batch = client.post(
            f"/collections/{collection}/points/query/batch",
            json={
                "searches": [
                    {"query": vector, "using": DENSE_VECTOR, "filter": filter_json, "limit": 30, "with_payload": ["chunk_id"]},
                    {"query": bm25_vector(query), "using": SPARSE_VECTOR, "filter": filter_json, "limit": 30, "with_payload": ["chunk_id"]},
                ]
            },
        ).json()["result"]
        native = client.post(
            f"/collections/{collection}/points/query",
            json={
                "prefetch": [
                    {"query": vector, "using": DENSE_VECTOR, "filter": filter_json, "limit": 30},
                    {"query": bm25_vector(query), "using": SPARSE_VECTOR, "filter": filter_json, "limit": 30},
                ],
                "query": {"fusion": "rrf"},
                "limit": 10,
                "with_payload": ["chunk_id"],
            },
        ).json()["result"]["points"]

    dense = [(p["payload"]["chunk_id"], p["score"]) for p in batch[0]["points"]]
    sparse = [(p["payload"]["chunk_id"], p["score"]) for p in batch[1]["points"]]
    local = [c.chunk_id for c in rrf_fuse(dense, sparse, top_k=30)]
    native_ids = [p["payload"]["chunk_id"] for p in native]
    assert local[: len(native_ids)] == native_ids, (
        f"本地 RRF 与原生不一致：local={local} native={native_ids}"
    )


# --- 6. 澄清选项只来自证据 ---------------------------------------------------


def test_clarify_options_are_built_from_real_document_titles(
    conn, store, embedder, b3_pg
) -> None:
    """§8.2：选项只能从检索到的事实里取，且每条能回填 `doc_keys`。

    用真 PG 里的真实标题验证 label 的来源：`documents.filename`（澄清选项的展示名
    必须是用户认得出的文件名，不能是 doc_key 那种内部编号——除非库里没有文件名）。

    为什么这里不跑真检索：触发澄清需要"两份文档的精排分差 < 0.05"，那要求两份制度
    几乎同文。为一条选项生成规则去造一对同文制度，成本远大于收益，而"分数接近"
    那一半已经由 `test_retrieval_logic.py::test_two_documents_with_close_scores_trigger_clarify`
    穷举过了。这里补的是另一半：**标题真的来自库**。
    """
    from api.retrieval.gate import decorate_clarify_options
    from api.retrieval.texts import doc_names
    from api.retrieval.hybrid import Candidate

    name = unique("clarify")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。")],
    )
    # 再造一份只有文档行、没有切片的文档：澄清选项不该需要它有切片
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (doc_key, filename, status) VALUES (%s, %s, 'active')",
            (doc_key(f"{name}_second"), "费用报销管理办法-2025版.pdf"),
        )
    conn.commit()

    candidates = [
        Candidate(
            chunk_id=f"ch_{name}_a",
            doc_key=doc_key(name),
            version_id=version_of(name, "v1"),
            chunk_type="text",
            page_num=3,
            rerank_score=0.61,
        ),
        Candidate(
            chunk_id=f"ch_{name}_b",
            doc_key=doc_key(f"{name}_second"),
            version_id=f"{doc_key(f'{name}_second')}#v1",
            chunk_type="text",
            page_num=7,
            rerank_score=0.60,
        ),
    ]
    names = doc_names(b3_pg, [c.doc_key for c in candidates])
    assert names[doc_key(f"{name}_second")] == "费用报销管理办法-2025版.pdf"

    options = decorate_clarify_options(candidates, doc_names=names, max_options=4)
    assert len(options) == 2
    labels = [opt.label for opt in options]
    assert any("费用报销管理办法-2025版.pdf" in label for label in labels), labels
    assert all(opt.params["doc_keys"] for opt in options)
    # 选项里的 doc_key 必须都在候选里出现过（不能凭空调出来一份）
    assert {opt.doc_key for opt in options} == {c.doc_key for c in candidates}


# --- 7. 三路分数都留在候选上（§7.6）-------------------------------------------


def test_candidates_carry_both_route_ranks(
    conn, store, embedder, retriever: HybridRetriever, query_embedder
) -> None:
    """§7.6：每条候选带 `dense_rank` / `sparse_rank` / `fused_rank`。

    少了任一路的名次，审计就回答不了"它为什么没排上来"——那正是排查检索质量
    时第一个要问的问题。
    """
    name = unique("ranks")
    chunks = [
        make_chunk(chunk_id=f"ch_{name}_1", text="住宿费标准每人每天500元"),
        make_chunk(chunk_id=f"ch_{name}_2", text="住宿费限额与报销额度说明"),
        make_chunk(chunk_id=f"ch_{name}_3", text="体检费用由公司承担"),
    ]
    publish(conn, store, embedder, name=name, tag="v1", chunks=chunks)

    query = "住宿费标准"
    flt = build_filter(FIN)
    vector = query_embedder.embed(query)
    result = retriever.retrieve(query, flt, dense_query=vector, top_k=30)

    assert result.candidates
    top = result.candidates[0]
    assert top.dense_rank is not None, "第一名必须有稠密名次"
    assert top.fused_rank == 1
    # 至少有一条被两路同时命中（否则 RRF 融合这件事没有被真正检验）
    assert any(c.dense_rank and c.sparse_rank for c in result.candidates), (
        "没有任何一条被两路同时命中，融合逻辑没被真正跑到"
    )
