"""集成测试夹具：真 PG + 真 Qdrant（独立集合）+ 真 TEI。.

复用 A3 的 `api/tests/a3/conftest.py` 里那套造数函数（`seed_document` /
`index_version` / `flip`）与 fixture——理由很直接：两条链路对同一份数据必须给
同一套结果，如果 B3 自己再写一份造数，两边数据形状一漂移，"检索不对"就分不清
是检索逻辑错了还是造数错了。

与 A3 的两点差别：

1. **命名空间 `b3test/`**：A3 的 `_cleanup_rows` 只清 `a3test/%`，B3 的用例
   混进同一个库里就必须有自己的前缀，否则它的残留行会被 A3 的下一次跑当成
   "别人的数据"留在地上。
2. **额外提供 `index_chunks()`**：B3 需要"某个版本完全上线"的状态（PG 双 active
   + 向量库点 status=active），一步到位。A3 的 `flip()` 只改 PG，向量库要靠
   `drain()` 消费期望状态——测试里多一步就多一个可能失败的地方，所以这里提供
   一个"直接对齐"的辅助函数，`test_pipeline` 里也用 `drain()` 至少走一遍真路。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest

from api.clients.pg import PgDatabase
from api.retrieval.hybrid import HybridRetriever
from api.retrieval.pipeline import QueryEmbedder
from api.retrieval.rerank import Reranker
from api.tests.a3.conftest import (  # noqa: F401 - fixtures 靠 conftest 发现
    DENSE_DIM,
    EMBEDDING_MODEL,
    a3_dsn,
    chunk_rows,
    collection,
    conn,
    embedder,
    insert_chunk,
    migrated,
    pool,
    qdrant_reachable,
    seed_document,
    store,
    version_of,
)

QDRANT_URL = os.getenv("AI_RAG_TEST_QDRANT_URL") or os.getenv("QDRANT_URL", "")
EMBEDDING_URL = os.getenv("EMBEDDING_BASE_URL", "")
RERANK_URL = os.getenv("RERANK_BASE_URL", "http://127.0.0.1:7999")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-base")

# B3 自己的命名空间：与 a3test 分开，清理互不干扰。
NS = "b3test"


def doc_key(name: str) -> str:
    """Run doc_key."""
    return f"{NS}/{name}"


@pytest.fixture(scope="session")
def b3_pg(request: pytest.FixtureRequest) -> PgDatabase:
    """B3 专用的真 PG 连接池，迁移由 a3 的 `migrated` 夹具完成。.

    通过 fixture 名取值而不是同名形参：形参会遮蔽上面 import 进来的
    `migrated`（F811），而那个 import 是让本目录也能发现该夹具的。
    """
    database = PgDatabase(
        request.getfixturevalue("migrated"),
        min_size=1,
        max_size=4,
        connect_timeout_s=5,
    )
    database.open()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(autouse=True)
def _cleanup_b3_rows(pool) -> None:
    """用例前后清掉 `b3test/` 命名空间。."""
    _purge(pool)
    try:
        yield
    finally:
        _purge(pool)


def _purge(pool) -> None:
    """Run _purge."""
    with pool.connection() as connection:
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM vector_drift_log WHERE doc_key LIKE %s",
                (f"{NS}/%",),
            )
            cur.execute(
                "DELETE FROM vector_sync_tasks WHERE doc_key LIKE %s",
                (f"{NS}/%",),
            )
            cur.execute(
                "DELETE FROM chunks WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
            cur.execute(
                "DELETE FROM doc_versions WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
            cur.execute(
                "DELETE FROM ingest_jobs WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
            cur.execute(
                "DELETE FROM documents WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
        connection.commit()


@pytest.fixture(scope="session")
def rerank_reachable() -> str:
    """Run rerank_reachable."""
    if not RERANK_URL:
        pytest.skip("RERANK_BASE_URL 未设置（集成需要真精排服务）")
    try:
        resp = httpx.get(
            f"{RERANK_URL.rstrip('/')}/health", timeout=5, trust_env=False
        )
        # TEI 的 /health 成功时返回空 body 和 200。
        assert resp.status_code < 400
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"rerank 不可达 {RERANK_URL}: {exc}")
    return RERANK_URL


@pytest.fixture
def retriever(qdrant_reachable: str, collection: str) -> HybridRetriever:
    """Run retriever."""
    r = HybridRetriever(qdrant_reachable, collection, timeout_s=10)
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def reranker(rerank_reachable: str) -> Reranker:
    """Run reranker."""
    r = Reranker(rerank_reachable, RERANK_MODEL, timeout_s=30)
    try:
        yield r
    finally:
        r.close()


@pytest.fixture
def query_embedder(embedder) -> QueryEmbedder:  # noqa: ARG001 - 复用它的 skip 判定
    """Run query_embedder."""
    return QueryEmbedder(
        base_url=EMBEDDING_URL,
        model=EMBEDDING_MODEL,
        dim=DENSE_DIM,
        timeout_s=30,
    )


# --- 造数：一个"完全上线"的版本 ----------------------------------------------


def publish(
    conn,
    store,
    embedder,
    *,
    name: str,
    tag: str,
    chunks: list[dict],
) -> str:
    """造一个已上线的版本：PG 双 active + 向量库点 status=active。.

    B3 的用例要么验"能召回"、要么验"旧版本被剔"，两种都需要"这个版本现在是
    线上版本"这个事实。分成三步做（写 PG → 写向量 → 翻状态）而不是一步到位，
    是为了让每一步都能单独断言——`publish()` 只是把它们串起来。

    **不直接用 A3 的 `seed_document()`**：它把 `doc_key` 写成 `a3test/<name>`，
    而 B3 的清理只扫 `b3test/%`。混用会留下 200+ 行残留在 `a3test/` 命名空间里，
    而 `test_filter_parity` 做的是**全库**对拍（它的 WHERE 里没有 doc_key
    条件），于是下一轮跑它就会看到一堆陌生切片并全部报红——实测撞过：11 个
    parity 用例一起红，原因全在别人的测试数据上。

    所以这里自己写一份最小的建行逻辑，命名空间跟着 `NS`（= `b3test`）走。
    """
    from api.ingest.flip import flip
    from api.ingest.vectors import index_version

    version_id = version_of(name, tag)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (doc_key, filename, status) "
            "VALUES (%s, %s, 'active') "
            "ON CONFLICT (doc_key) DO UPDATE SET status = 'active'",
            (doc_key(name), f"{name}.pdf"),
        )
        cur.execute(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status) "
            "VALUES (%s, %s, %s, 'pending')",
            (version_id, doc_key(name), _hash(f"{name}:{tag}")),
        )
    for chunk in chunks:
        _insert_chunk(conn, version_id=version_id, name=name, **chunk)
    conn.commit()
    report = index_version(conn, store, embedder, version_id)
    conn.commit()
    assert report.ok, f"index_version 未完成: {report.as_dict()}"
    flip(conn, version_id)
    conn.commit()
    # 向量库侧：翻牌后点仍是 pending（point 的 status 恒等于切片状态，翻牌改了
    # PG 的切片状态），要对齐必须走一次状态改写——这就是生产里 worker 做的事。
    store.set_status_by_version(version_id, "active")
    return version_id


def _hash(tag: str) -> str:
    """Run _hash."""
    import hashlib

    return hashlib.sha256(tag.encode()).hexdigest()


def _insert_chunk(
    conn,
    *,
    version_id: str,
    name: str,
    chunk_id: str,
    content_text: str,
    page_num: int = 1,
    is_public: bool = True,
    allowed_depts: list[str] | None = None,
    allowed_roles: list[str] | None = None,
    effective_date: int | None = 20260301,
    date_confidence: str = "explicit",
    parent_id: str | None = None,
    chunk_type: str = "text",
) -> None:
    """插一条切片（status 先 pending，翻牌时才变 active）。."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chunks (
                chunk_id, doc_key, version_id, chunk_type, parent_id, page_num,
                bbox, content_text, effective_date, effective_year,
                date_confidence, is_public, allowed_depts, allowed_roles, status
            )
            VALUES (
                %(chunk_id)s, %(doc_key)s, %(version_id)s, %(chunk_type)s,
                %(parent_id)s, %(page_num)s, '[[0.1,0.1,0.9,0.2]]'::jsonb,
                %(content_text)s, %(effective_date)s, %(effective_year)s,
                %(date_confidence)s, %(is_public)s, %(depts)s, %(roles)s,
                'pending'
            )
            """,
            {
                "chunk_id": chunk_id,
                "doc_key": doc_key(name),
                "version_id": version_id,
                "chunk_type": chunk_type,
                "parent_id": parent_id,
                "page_num": page_num,
                "content_text": content_text,
                "effective_date": effective_date,
                "effective_year": effective_date // 10000
                if effective_date
                else None,
                "date_confidence": date_confidence,
                "is_public": is_public,
                "depts": allowed_depts or [],
                "roles": allowed_roles or [],
            },
        )


def make_chunk(
    *,
    chunk_id: str,
    text: str,
    page_num: int = 1,
    is_public: bool = True,
    depts: list[str] | None = None,
    roles: list[str] | None = None,
    effective_date: int = 20260301,
    date_confidence: str = "explicit",
    parent_id: str | None = None,
    chunk_type: str = "text",
) -> dict[str, Any]:
    """Run make_chunk."""
    return {
        "chunk_id": chunk_id,
        "content_text": text,
        "page_num": page_num,
        "is_public": is_public,
        "allowed_depts": depts or [],
        "allowed_roles": roles or [],
        "effective_date": effective_date,
        "date_confidence": date_confidence,
        "parent_id": parent_id,
        "chunk_type": chunk_type,
    }


def unique(name: str) -> str:
    """同一份代码里多个用例共用一张表，名字必须唯一。.

    不唯一就撞 chunk_id 主键。
    """
    return f"{name}_{uuid.uuid4().hex[:6]}"


__all__ = [
    "DENSE_DIM",
    "EMBEDDING_MODEL",
    "NS",
    "RERANK_MODEL",
    "b3_pg",
    "chunk_rows",
    "collection",
    "conn",
    "doc_key",
    "embedder",
    "insert_chunk",
    "make_chunk",
    "pool",
    "publish",
    "qdrant_reachable",
    "query_embedder",
    "rerank_reachable",
    "reranker",
    "retriever",
    "seed_document",
    "store",
    "unique",
    "version_of",
]
