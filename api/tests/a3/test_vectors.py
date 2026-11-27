"""双向量入库：一个 point 同时带 dense 与 bm25（详细设计.md §3.6 / §7.1）。

本卡验收"点同时含 dense+bm25"在这里逐项断言。四类断言，缺一不可：

1. **结构**：读回来 `vector` 字典里 `dense` 与 `bm25` 两个键都在，维度对。
   写成两条 point 也会让"两路检索都能命中"，所以光看检索结果分不出来。
2. **payload**：过滤字段齐全（否则检索侧静默不匹配，见 filters.py 的注释）。
3. **中文 bm25 真的切碎了**：整句中文在服务端只切出 1 个 token（实测），
   所以写入前必须过 `bm25_text()`。这一条盯的是"预处理没做/做漏"。
4. **两路都能命中**：同一份文本用 dense 查、用 bm25 查都能召回它——证明两个向量
   真的编码了这段正文，而不是"字段存在但内容是空的"。
"""

from __future__ import annotations

import pytest

from api.ingest.vectors import EmbeddingError, index_version
from api.tests.a3.conftest import (  # 只 import 辅助函数；fixtures 自动发现
    DENSE_DIM,
    chunk_rows,
    doc_key,
    seed_document,
)

pytestmark = pytest.mark.integration


# --- 1. 结构 ------------------------------------------------------------------


def test_point_carries_dense_and_bm25_together(conn, store, embedder) -> None:
    """核心断言：一个 point 里两个向量都在（本卡的验收原句）。"""
    from scripts.qdrant_common import DENSE_VECTOR, SPARSE_VECTOR

    version_id = seed_document(conn, key="dual", version_tag="v1")
    conn.commit()

    report = index_version(conn, store, embedder, version_id)
    conn.commit()

    assert report.rows == 2
    assert report.points == 2
    assert report.ok

    points = store.scroll_version(version_id)
    assert len(points) == 2
    assert {p["payload"]["chunk_id"] for p in points} == set(
        chunk_rows(conn, version_id)
    )

    # with_vector=True 才有向量本身：scroll / retrieve 都只取 payload（对账用不
    # 着向量）
    resp = store._call(
        "POST",
        f"/collections/{store.collection}/points",
        {
            "ids": [p["id"] for p in points],
            "with_vector": True,
            "with_payload": True,
        },
    )
    for point in resp["result"]:
        vectors = point["vector"]
        assert set(vectors) == {
            DENSE_VECTOR,
            SPARSE_VECTOR,
        }, f"point must carry both dense and bm25, got {sorted(vectors)}"
        assert len(vectors[DENSE_VECTOR]) == DENSE_DIM
        # bm25 由服务端推理：存的是 indices/values。indices 为空 = 正文没进词法
        # 索引，
        # 那条切片在关键词路上永远召回不到（静默失效）。
        assert vectors[SPARSE_VECTOR]["indices"], "bm25 sparse vector is empty"


def test_payload_has_every_filter_field(conn, store, embedder) -> None:
    """Payload 必须带齐过滤字段。少一个，检索侧就是"静默不匹配"。"""
    version_id = seed_document(
        conn,
        key="payload",
        version_tag="v1",
        chunks=[
            {
                "chunk_id": "ch_a3test_payload_1",
                "content_text": "差旅住宿标准 500 元",
                "page_num": 3,
                "effective_date": 20260301,
                "date_confidence": "explicit",
                "is_public": False,
                "allowed_depts": ["fin", "hr"],
                "allowed_roles": ["staff"],
            }
        ],
    )
    conn.commit()
    index_version(conn, store, embedder, version_id)
    conn.commit()

    (point,) = store.scroll_version(version_id)
    payload = point["payload"]

    # `parent_id` 是唯一允许缺席的字段：只有子片才有父片编号，顶层父片本来就是
    # NULL。
    # 其余索引字段缺席就是 bug——检索侧不会报错，只会静默不匹配。
    required = {
        "chunk_id",
        "doc_key",
        "version_id",
        "chunk_type",
        "status",
        "is_public",
        "allowed_depts",
        "allowed_roles",
        "effective_year",
        "effective_date",
        "date_confidence",
        "page_num",
    }
    missing = required - set(payload)
    assert (
        not missing
    ), f"payload is missing indexed field(s): {sorted(missing)}"
    assert "parent_id" not in payload  # 顶层切片没有父片，不该写个 null 进去

    assert payload["status"] == "pending"  # 切片是 pending，点也必须写 pending
    assert payload["version_id"] == version_id
    assert payload["doc_key"] == doc_key("payload")
    assert payload["is_public"] is False
    assert payload["allowed_depts"] == ["fin", "hr"]
    assert payload["allowed_roles"] == ["staff"]
    assert payload["effective_date"] == 20260301
    assert payload["effective_year"] == 2026
    assert payload["date_confidence"] == "explicit"
    assert payload["page_num"] == 3
    assert payload["chunk_id"] == "ch_a3test_payload_1"


def test_child_chunk_writes_parent_id(conn, store, embedder) -> None:
    """子片必须带 parent_id（§3.5 父子两级：检索命中子片，作答用父片）。"""
    version_id = seed_document(
        conn,
        key="parent",
        version_tag="v1",
        chunks=[
            {
                "chunk_id": "ch_a3test_parent_1",
                "content_text": (
                    "第二章 差旅住宿：住宿费按职级限额，总监 800 元。"
                ),
                "parent_id": None,
                "is_public": True,
            },
            {
                "chunk_id": "ch_a3test_child_1",
                "content_text": "总监级住宿费上限为每人每天 800 元。",
                "parent_id": "ch_a3test_parent_1",
                "is_public": True,
            },
        ],
    )
    conn.commit()
    index_version(conn, store, embedder, version_id)
    conn.commit()

    payloads = {
        p["payload"]["chunk_id"]: p["payload"]
        for p in store.scroll_version(version_id)
    }
    assert payloads["ch_a3test_parent_1"].get("parent_id") is None
    assert payloads["ch_a3test_child_1"]["parent_id"] == "ch_a3test_parent_1"


# --- 2. 中文 bm25 预处理 ------------------------------------------------------


def test_chinese_text_is_segmented_before_bm25(conn, store, embedder) -> None:
    """整句中文必须被切成多个 token。

    实测过这条：原文直接入库时整句只产出 1 个 token（服务端按空白/标点切词），
    于是 `{"text": "住宿费"}` 这种查询一条也召回不到。这条用例盯的就是那个回归。
    """
    from scripts.qdrant_common import SPARSE_VECTOR

    text = "住宿费标准每人每天500元出差住宿费用按职级限额报销"
    version_id = seed_document(
        conn,
        key="segment",
        version_tag="v1",
        chunks=[
            {
                "chunk_id": "ch_a3test_seg_1",
                "content_text": text,
                "is_public": True,
            }
        ],
    )
    conn.commit()
    index_version(conn, store, embedder, version_id)
    conn.commit()

    (point,) = store.scroll_version(version_id)
    resp = store._call(
        "POST",
        f"/collections/{store.collection}/points",
        {"ids": [point["id"]], "with_vector": True},
    )
    tokens = len(resp["result"][0]["vector"][SPARSE_VECTOR]["indices"])
    assert tokens > 10, (
        f"chinese text produced only {tokens} bm25 token(s); it must be "
        "bigram-segmented before indexing (see qdrant_common.bm25_text)"
    )


# --- 3. 两路都真的能命中 ------------------------------------------------------


def test_both_vectors_recall_the_text(conn, store, embedder) -> None:
    """Dense 与 bm25 各自独立都能召回同一条切片。

    只断言“字段存在”不够：把 bm25 的 text 传空、或把 dense 写成全零，
    结构断言照样过。
    """
    from scripts.qdrant_common import bm25_vector

    version_id = seed_document(
        conn,
        key="recall",
        version_tag="v1",
        chunks=[
            {
                "chunk_id": "ch_a3test_recall_1",
                "content_text": "住宿费标准：每人每天 500 元",
                "page_num": 1,
                "is_public": True,
            },
            {
                "chunk_id": "ch_a3test_recall_2",
                "content_text": "年假天数为 15 天，需提前申请",
                "page_num": 2,
                "is_public": True,
            },
        ],
    )
    conn.commit()
    index_version(conn, store, embedder, version_id)
    conn.commit()

    flt = {"must": [{"key": "version_id", "match": {"value": version_id}}]}

    # 稠密路：问句与目标切片语义相近（换了个说法）
    query_vec = embedder.embed(["住宿费限额多少"])[0]
    dense_hits = store._call(
        "POST",
        f"/collections/{store.collection}/points/query",
        {
            "query": query_vec,
            "using": "dense",
            "filter": flt,
            "limit": 5,
            "with_payload": True,
        },
    )
    dense_ids = [
        p["payload"]["chunk_id"] for p in dense_hits["result"]["points"]
    ]
    assert "ch_a3test_recall_1" in dense_ids

    # 稀疏路：靠字面词命中（"年假"只在第二片里出现）。查询侧也必须过
    # bm25_vector，否则两侧 token 空间对不上（实测 top1 命中率从 3/4 掉到
    # 2/4）。
    bm25_hits = store._call(
        "POST",
        f"/collections/{store.collection}/points/query",
        {
            "query": bm25_vector("年假"),
            "using": "bm25",
            "filter": flt,
            "limit": 5,
            "with_payload": True,
        },
    )
    bm25_ids = [p["payload"]["chunk_id"] for p in bm25_hits["result"]["points"]]
    assert "ch_a3test_recall_2" in bm25_ids


# --- 4. 写入的其它口径 --------------------------------------------------------


def test_dense_is_backfilled_into_pg(conn, store, embedder) -> None:
    """PG 的 embedding_dense 要回填：它让"点丢了"可以只靠库内数据修复。"""
    version_id = seed_document(conn, key="backfill", version_tag="v1")
    conn.commit()
    index_version(conn, store, embedder, version_id)
    conn.commit()

    rows = chunk_rows(conn, version_id)
    for chunk_id, row in rows.items():
        assert (
            row["embedding_dense"] is not None
        ), f"{chunk_id} has no embedding_dense"
    sample = next(iter(rows.values()))["embedding_dense"]
    assert str(sample).count(",") == DENSE_DIM - 1


def test_reindexing_is_idempotent(conn, store, embedder) -> None:
    """重放同一版本不产生重复点（point id = uuid5(chunk_id)）。"""
    version_id = seed_document(conn, key="idem", version_tag="v1")
    conn.commit()
    first = index_version(conn, store, embedder, version_id)
    conn.commit()
    before = store.count_version(version_id)

    second = index_version(conn, store, embedder, version_id)
    conn.commit()

    assert first.points == second.points
    assert store.count_version(version_id) == before


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("zero-width space", "\u200b"),
        ("zero-width joiner", "\u200d"),
        ("BOM", "\ufeff"),
        # "   "（纯空格）不放进来：`ck_chunks_text_nonempty` 用 btrim 判定，它在
        #  DB 层
        # 就已经被拒，压根到不了写入侧（实测 CheckViolation）。真正漏网的是零宽
        # 字符
        # ——它们 strip 之后仍然存在，能过 CHECK，所以必须由写入侧挡。
    ],
)
def test_unsearchable_text_is_skipped_not_fatal(
    conn, store, embedder, label, text
) -> None:
    """没有可检索字符的切片跳过并计数，不让整批失败。

    DB 的 `ck_chunks_text_nonempty` 只拦 `btrim` 之后为空——零宽字符能过，而它们
    在 BM25 里是 0 个 token。空稀疏向量 Qdrant 会收（实测 200），但那条切片永远
    召回不到，属于静默失效，所以在写入侧挡掉。
    """
    version_id = seed_document(
        conn,
        key=f"blank{label.replace(' ', '')}",
        version_tag="v1",
        chunks=[
            {
                "chunk_id": "ch_a3test_blank_1",
                "content_text": "正常正文",
                "is_public": True,
            },
            {
                "chunk_id": "ch_a3test_blank_2",
                "content_text": text,
                "is_public": True,
            },
        ],
    )
    conn.commit()
    report = index_version(conn, store, embedder, version_id)
    conn.commit()

    assert report.rows == 2
    assert report.points == 1
    assert [c for c, _ in report.skipped] == ["ch_a3test_blank_2"]
    # `ok` 的定义是"每条切片都进了库"。这里故意不是 ok：跳过的那条在大盘上是个
    # 需要解释的数字（要么解析产出了垃圾，要么正文清洗出了空片），不能算
    # 静默成功。
    assert not report.ok


def test_embedding_dim_mismatch_is_rejected_before_writing(
    conn, store, embedder
) -> None:
    """维度与配置不一致必须在写前炸掉，不能把坏向量写进库。"""
    from api.ingest.vectors import Embedder as EmbedderCls

    version_id = seed_document(conn, key="dimmismatch", version_tag="v1")
    conn.commit()

    # 参数从 fixture 上取，不读 os.environ：`api/tests/conftest.py` 的 autouse
    # `_clean_env` 会把 Settings 的每个字段名从环境里删掉（它为了"测试不依赖本机
    # .env"而设计），在这里读 EMBEDDING_BASE_URL 只会拿到 KeyError（实测）。
    wrong = EmbedderCls(
        base_url=embedder.base_url,
        model=embedder.model,
        dim=DENSE_DIM + 1,  # 故意配错
        batch_size=8,
    )
    try:
        with pytest.raises(EmbeddingError, match="dim="):
            index_version(conn, store, wrong, version_id)
    finally:
        wrong.close()
    conn.rollback()
    assert store.count_version(version_id) == 0
