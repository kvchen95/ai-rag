"""A3 测试夹具：真 PG + 真 Qdrant（每个用例一个独立集合）+ 真 TEI。

三条原则：

1. **不碰共享集合 `kb_chunks`**。每个用例新建 `a3_test_<hex>` 集合，用完删掉。
   B2 的 `test_filter_parity` 用同样的做法——理由一样：测试写真实集合会让
   "跑一次测试"变成"改一次线上数据"。
2. **不碰真实 doc_key**。所有行落在 `a3test/` 命名空间，清理按前缀走。
3. **TEI 不可用时跳过**，不假装跑过。编码服务是本卡的必需依赖（没有它连不出
   dense 向量），但它是外部容器，测试环境里可能没起。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api.clients.pg import normalize_dsn
from api.ingest.vectors import Embedder, QdrantVectorStore

REPO_ROOT = Path(__file__).resolve().parents[3]

QDRANT_URL = os.getenv("AI_RAG_TEST_QDRANT_URL") or os.getenv("QDRANT_URL", "")
EMBEDDING_URL = os.getenv("EMBEDDING_BASE_URL", "")

DENSE_DIM = int(os.getenv("EMBEDDING_DIM", "512"))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")

# 测试命名空间：清理按这个前缀走，绝不碰真实样本（policy/travel-cn 等）
NS = "a3test"


def doc_key(name: str) -> str:
    return f"{NS}/{name}"


def version_of(name: str, tag: str) -> str:
    return f"{NS}/{name}#{tag}"


def fake_hash(tag: str) -> str:
    """64 位小写十六进制（`ck_doc_versions_hash_sha256` 的形状要求）。

    用 tag 派生而不是随机：同一 tag 在同一用例里重跑会撞上
    `uq_doc_versions_doc_hash`，
    这正是我们要的"同一内容不建第二个版本"。
    """
    import hashlib

    return hashlib.sha256(tag.encode()).hexdigest()


@pytest.fixture(scope="session")
def a3_dsn() -> str:
    """真 PG 的 DSN。

    名字不叫 `pg_dsn`：`api/tests/conftest.py` 已经有一个 module 作用域的同名
    fixture，
    重名会让 pytest 按"更内层优先"解析，于是 session 作用域的 fixture 去要一个
    module 作用域的值 → ScopeMismatch（实测）。
    """
    dsn = os.getenv("AI_RAG_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip(
            "integration needs AI_RAG_TEST_DATABASE_URL or DATABASE_URL"
        )
    assert dsn
    return dsn


@pytest.fixture(scope="session")
def migrated(a3_dsn: str) -> str:
    """Schema 走项目唯一的迁移入口（不自己建表）。"""
    import subprocess
    import sys

    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "db_migrate.py"),
            "--dsn",
            a3_dsn,
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"db_migrate.py failed: {proc.stderr[-800:]}"
    with psycopg.connect(normalize_dsn(a3_dsn)) as conn:
        assert conn.execute(
            "SELECT to_regclass('public.vector_sync_tasks')"
        ).fetchone()[0]
    return a3_dsn


@pytest.fixture(scope="session")
def pool(migrated: str) -> ConnectionPool:
    database = ConnectionPool(
        conninfo=normalize_dsn(migrated), min_size=1, max_size=4, open=True
    )
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(scope="session")
def qdrant_reachable() -> str:
    if not QDRANT_URL:
        pytest.skip("QDRANT_URL is not set (integration needs a real Qdrant)")
    try:
        resp = httpx.get(
            f"{QDRANT_URL.rstrip('/')}/collections", timeout=5, trust_env=False
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Qdrant not reachable at {QDRANT_URL}: {exc}")
    return QDRANT_URL


@pytest.fixture(scope="session")
def embedder() -> Embedder:
    if not EMBEDDING_URL:
        pytest.skip(
            "EMBEDDING_BASE_URL is not set (integration needs a real TEI)"
        )
    emb = Embedder(
        base_url=EMBEDDING_URL,
        model=EMBEDDING_MODEL,
        dim=DENSE_DIM,
        api_key=os.getenv("EMBEDDING_API_KEY", "local"),
    )
    try:
        emb.embed(["探活"])
    except Exception as exc:  # noqa: BLE001
        emb.close()
        pytest.skip(f"embedding service not usable at {EMBEDDING_URL}: {exc}")
    try:
        yield emb
    finally:
        emb.close()


@pytest.fixture(scope="function")
def collection(qdrant_reachable: str) -> str:
    """每个用例一个独立集合（dense 512/Cosine + bm25/idf + payload 索引）。

    集合形状不在这里手抄：直接用 `scripts/qdrant_common`（DA1 的唯一真源），
    否则测试集合与真实集合会用两套参数，测过的东西上不了线。
    """
    from scripts import qdrant_common as qc

    name = f"a3_test_{uuid.uuid4().hex[:10]}"
    qc.create_collection(qdrant_reachable, name, DENSE_DIM)
    qc.ensure_payload_indexes(qdrant_reachable, name)
    try:
        yield name
    finally:
        qc.delete_collection(qdrant_reachable, name)


@pytest.fixture(scope="function")
def store(qdrant_reachable: str, collection: str) -> QdrantVectorStore:
    s = QdrantVectorStore(base_url=qdrant_reachable, collection=collection)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _cleanup_rows(pool: ConnectionPool):
    """用例前后清掉 `a3test/` 命名空间。

    顺序：drift 账 -> 同步任务 -> 切片 -> 版本 -> 任务 -> 文档。
    `vector_sync_tasks` 挂了 `ON DELETE CASCADE` 到 doc_versions，但显式删一遍更
    直接（CASCADE 只在版本行真的被删时触发，而这里的清理本来就按 doc_key 删）。
    """
    _purge(pool)
    try:
        yield
    finally:
        _purge(pool)


def _purge(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        with conn.cursor() as cur:
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
        conn.commit()


@pytest.fixture(scope="function")
def conn(pool: ConnectionPool):
    """一条连接（事务由用例自己管）。多用例共享同一个 pool，连接是短借的。"""
    with pool.connection() as connection:
        yield connection


# --- 造数据 -------------------------------------------------------------------


def seed_document(
    conn,
    *,
    key: str,
    version_tag: str,
    chunks: list[dict] | None = None,
    version_status: str = "pending",
    activate: bool = False,
    doc_status: str | None = None,
) -> str:
    """建一个文档 + 一个版本 + 若干切片。返回 version_id。

    `chunks` 里每项可给：content_text（必填）、chunk_type、page_num、is_public、
    allowed_depts、allowed_roles、effective_date、date_confidence、status。
    默认造两条 text 切片。
    """
    version_id = version_of(key, version_tag)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (doc_key, filename, status) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (doc_key) DO UPDATE SET status = EXCLUDED.status",
            (
                doc_key(key),
                f"{key}.pdf",
                doc_status or ("active" if activate else "queued"),
            ),
        )
        # activated_at 与状态在一条语句里定：
        # `ck_doc_versions_active_has_activated_at`
        # 只允许"active 必须带时间"，所以不为 active 时也不能凭空给个 now()。
        cur.execute(
            "INSERT INTO doc_versions "
            "(version_id, doc_key, file_hash, status, activated_at) "
            "VALUES (%s, %s, %s, %s, "
            "CASE WHEN %s = 'active' THEN now() ELSE NULL END)",
            (
                version_id,
                doc_key(key),
                fake_hash(f"{key}:{version_tag}"),
                version_status,
                version_status,
            ),
        )
    for chunk in default_chunks(key, version_tag) if chunks is None else chunks:
        _insert_chunk(conn, version_id=version_id, key=key, **chunk)
    return version_id


def default_chunks(key: str, tag: str) -> list[dict]:
    return [
        {
            "chunk_id": f"ch_{NS}_{key}_{tag}_1".replace("/", "_"),
            "content_text": (
                "住宿费标准：每人每天 500 元。"
                "出差住宿费用按职级限额报销。"
            ),
            "page_num": 1,
            "effective_date": 20260301,
            "date_confidence": "explicit",
            "is_public": True,
        },
        {
            "chunk_id": f"ch_{NS}_{key}_{tag}_2".replace("/", "_"),
            "content_text": "交通费标准：市内交通凭票报销，单次不超过 100 元。",
            "page_num": 2,
            "effective_date": 20260301,
            "date_confidence": "explicit",
            "is_public": True,
        },
    ]


def _insert_chunk(
    conn,
    *,
    version_id: str,
    key: str,
    chunk_id: str,
    content_text: str,
    chunk_type: str = "text",
    page_num: int = 1,
    is_public: bool = True,
    allowed_depts: list[str] | None = None,
    allowed_roles: list[str] | None = None,
    effective_date: int | None = None,
    date_confidence: str = "inferred",
    status: str = "pending",
    parent_id: str | None = None,
) -> None:
    effective_year = effective_date // 10000 if effective_date else None
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
                %(status)s
            )
            """,
            {
                "chunk_id": chunk_id,
                "doc_key": doc_key(key),
                "version_id": version_id,
                "chunk_type": chunk_type,
                "parent_id": parent_id,
                "page_num": page_num,
                "content_text": content_text,
                "effective_date": effective_date,
                "effective_year": effective_year,
                "date_confidence": date_confidence,
                "is_public": is_public,
                "depts": allowed_depts or [],
                "roles": allowed_roles or [],
                "status": status,
            },
        )


def chunk_rows(conn, version_id: str) -> dict[str, dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT chunk_id, status, content_text, embedding_dense "
            "FROM chunks WHERE version_id = %s ORDER BY chunk_id",
            (version_id,),
        )
        return {r["chunk_id"]: r for r in cur.fetchall()}


__all__ = [
    "DENSE_DIM",
    "EMBEDDING_MODEL",
    "NS",
    "a3_dsn",
    "chunk_rows",
    "collection",
    "conn",
    "doc_key",
    "embedder",
    "fake_hash",
    "migrated",
    "pool",
    "qdrant_reachable",
    "seed_document",
    "store",
    "version_of",
]


def insert_chunk(conn, **kwargs) -> None:
    """给需要额外切片的用例（跨版本补片、造孤立切片）直接插一条。

    参数见 `_insert_chunk`：version_id / key / chunk_id / content_text 必填。
    """
    return _insert_chunk(conn, **kwargs)
