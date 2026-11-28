#!/usr/bin/env python3
"""Shared Qdrant helpers for ai-rag (stdlib only: urllib).

Everything that must agree between ingest (A3), the collection bootstrap and
the full rebuild lives here, so there is exactly one definition of:

* the collection shape (named dense "dense" 512/Cosine + sparse "bm25" IDF)
* the point id mapping (chunk_id -> deterministic UUIDv5)
* the payload fields + indexes used by retrieval filters
* the HTTP calls (PUT collection / PUT index / upsert / scroll)

Point id mapping — why it is not chunk_id verbatim
--------------------------------------------------
Qdrant point ids must be an unsigned integer or a UUID; ``ch_9f2c31`` is
neither, so 附录 B's "id=chunk_id" cannot be stored as-is. We derive a stable
UUIDv5 from the chunk_id and keep the real id in ``payload.chunk_id``:

point_id = uuid5(NAMESPACE, chunk_id)          # deterministic, replay-safe

Re-running an upsert overwrites the same point instead of duplicating it,
which is what makes the rebuild script idempotent.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"
BM25_MODEL = "qdrant/bm25"
DEFAULT_DIM = 512
DEFAULT_COLLECTION = "kb_chunks"

# Fixed namespace: changing it re-keys every point (= full rebuild). Do
# not change.
POINT_NAMESPACE = uuid.UUID("6f3d3c1e-2b8a-5a0e-9c7d-1a2b3c4d5e6f")

# ---------------------------------------------------------------------------
# 中文 BM25 的必要预处理（§7.1 边界表："BM25 分词对中文切分不理想"）
#
# 实测（Qdrant 1.15.3 + modifier=idf，2026-11-24）：服务端的 `qdrant/bm25` 只按
# **空白和标点**切词，不认中文词边界。于是「住宿费标准每人每天500元」整句只产出
# **1 个 token**，而 `{"text": "住宿费"}` 的查询产出的 token 与它对不上——
# 关键词路
# 对中文基本召回不到东西。实测数字：
#
#   原文入库 + 原文查询          top1 命中 1/4（只有 SKU 编号那种带空格的能中）
#   bigram 入库 + bigram 查询    top1 命中 3/4
#   bigram 入库 + 原文查询        top1 命中 2/4（查询侧漏改就白做）
#
# 所以入库与查询**两侧都必须**先用 `bm25_text()` 处理。为什么要 bigram 而不是接
# 一个分词器：分词器带来词典与版本（换一版 jieba 就换一套切法，历史索引与查询不
# 再
# 对齐，且重建前后不一致）；bigram 无参数，同一段文字永远切出同一串 token。
# 代价是索引变大（中文按 2-gram 展开，约字符数 ×1），可接受。
#
# 拉丁字母/数字（型号、条款号、SKU）整体保留——它们本来就是"整词命中"
# 的最佳场景，
# 拆成 bigram 反而会让 `SKU-99213` 命中 `SKU-99214` 这种近似编号
# （危险的假命中）。
# ---------------------------------------------------------------------------

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-/]*")


def bm25_text(text: str) -> str:
    """把正文/查询转成 BM25 要吃的 token 串（中文 bigram + 拉丁整词）。.

    入库侧与查询侧必须调用同一个函数，否则两侧的 token 空间对不上
    （见上面实测表）。
    """
    out: list[str] = []
    pos = 0
    for match in _CJK_RUN.finditer(text):
        out.extend(_WORD.findall(text[pos : match.start()]))
        run = match.group()
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
        pos = match.end()
    out.extend(_WORD.findall(text[pos:]))
    return " ".join(out)


def bm25_vector(text: str) -> dict[str, Any]:
    """Qdrant 稀疏向量字段（服务端推理）。.

    入库与查询的查询体都用它，保证形状一致。.
    """
    return {"text": bm25_text(text), "model": BM25_MODEL}


# Payload indexes required by 附录 B: doc_key, version_id, is_public,
# allowed_depts,
# allowed_roles, effective_year, chunk_type, status. chunk_id / parent_id
# / page_num
# are not filter fields but are indexed/kept for joins back to PG and citations.
#
# effective_date / date_confidence 是 §5.3 时间过滤必需的：只按年份过滤会把
# 「三月」这种月度窗口退化成整年，而且会把 date_confidence=inferred 的切片一起召
# 回
# （§5.2 明确"只认显式时间"）。两者缺一个，Qdrant 侧与 PG 侧的过滤语义就对不上。
PAYLOAD_INDEXES: dict[str, str] = {
    "doc_key": "keyword",
    "version_id": "keyword",
    "chunk_id": "keyword",
    "chunk_type": "keyword",
    "status": "keyword",
    "parent_id": "keyword",
    "is_public": "bool",
    "allowed_depts": "keyword",  # array<keyword>: Qdrant matches "any element"
    "allowed_roles": "keyword",
    "effective_year": "integer",
    "effective_date": "integer",
    "date_confidence": "keyword",
    "report_period_start": "integer",
    "report_period_end": "integer",
    "publication_date": "integer",
    "page_num": "integer",
}


def point_id_for(chunk_id: str) -> str:
    """Deterministic Qdrant point id for a chunk_id."""
    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


def collection_spec(dim: int = DEFAULT_DIM) -> dict[str, Any]:
    """PUT /collections/{name} body — 方案B。.

    dense 512 COSINE + bm25 sparse IDF.
    """
    return {
        "vectors": {DENSE_VECTOR: {"size": dim, "distance": "Cosine"}},
        "sparse_vectors": {SPARSE_VECTOR: {"modifier": "idf"}},
        "on_disk_payload": True,
        "hnsw_config": {"m": 16, "ef_construct": 100},
        "optimizers_config": {"indexing_threshold": 20000},
    }


def build_payload(
row: dict[str, Any], status: str | None = None
) -> dict[str, Any]:
    """Filter fields + identifiers stored with each point (正文不进向量库).

    时间三件套一起写：`effective_date` 决定区间过滤精度（月/日窗口），
    `date_confidence` 决定这条切片能不能在时间过滤里被召回（§5.2 只认
    explicit），
    `effective_year` 保留给年份粒度与旧查询。少写任一项，检索侧的时间条件就会
    静默变成"永远不匹配"或"匹配整年"，而这类错在功能测试里看不出来。
    """
    payload: dict[str, Any] = {
        "chunk_id": row["chunk_id"],
        "doc_key": row["doc_key"],
        "version_id": row["version_id"],
        "status": status if status is not None else row.get("status", "active"),
        "is_public": bool(row["is_public"]),
        "allowed_depts": list(row.get("allowed_depts") or []),
        "allowed_roles": list(row.get("allowed_roles") or []),
        "chunk_type": row["chunk_type"],
        "page_num": int(row["page_num"]),
        "date_confidence": row.get("date_confidence") or "inferred",
    }
    if row.get("effective_year") is not None:
        payload["effective_year"] = int(row["effective_year"])
    if row.get("effective_date") is not None:
        payload["effective_date"] = int(row["effective_date"])
    if row.get("report_period_start") is not None:
        payload["report_period_start"] = int(
            str(row["report_period_start"]).replace("-", "")
        )
    if row.get("report_period_end") is not None:
        payload["report_period_end"] = int(
            str(row["report_period_end"]).replace("-", "")
        )
    if row.get("publication_date") is not None:
        payload["publication_date"] = int(
            str(row["publication_date"]).replace("-", "")
        )
    if row.get("parent_id"):
        payload["parent_id"] = row["parent_id"]
    return payload


# ---------------------------------------------------------------------------
# HTTP


class HttpError(RuntimeError):
    """HttpError."""
    def __init__(self, method: str, url: str, code: int, body: str):
        """Initialize the instance."""
        super().__init__(f"{method} {url} -> HTTP {code}: {body[:400]}")
        self.code = code
        self.body = body


def request(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 60.0,
    retries: int = 2,
) -> tuple[int, dict[str, Any] | list[Any] | None]:
    """Minimal JSON request with retry on 5xx / connection errors."""
    data = (
        None
        if payload is None
        else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    )
    headers = {"Content-Type": "application/json"}
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code >= 500 and attempt < retries:
                last = HttpError(method, url, exc.code, body)
                time.sleep(1.5 * (attempt + 1))
                continue
            raise HttpError(method, url, exc.code, body) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"{method} {url} failed after retries: {last}")


# ---------------------------------------------------------------------
# collection


def collection_info(base_url: str, name: str) -> dict[str, Any] | None:
    """Return the collection's ``result`` block, or None when missing。."""
    try:
        _, body = request("GET", f"{base_url.rstrip('/')}/collections/{name}")
    except HttpError as exc:
        if exc.code == 404:
            return None
        raise
    return (body or {}).get("result") if isinstance(body, dict) else None


def spec_mismatches(info: dict[str, Any], dim: int = DEFAULT_DIM) -> list[str]:
    """Compare a live collection against 方案B. Empty list == compatible."""
    params = (info.get("config") or {}).get("params") or {}
    dense = (params.get("vectors") or {}).get(DENSE_VECTOR)
    sparse = (params.get("sparse_vectors") or {}).get(SPARSE_VECTOR)
    problems: list[str] = []
    if dense is None:
        problems.append(
            f"missing dense vector '{DENSE_VECTOR}' "
            f"(found: {sorted(params.get('vectors') or {})})"
        )
    else:
        if int(dense.get("size", -1)) != dim:
            problems.append(
                f"dense size={dense.get('size')} != {dim} "
                "(cannot mix dims; full rebuild needed)"
            )
        if str(dense.get("distance", "")).lower() != "cosine":
            problems.append(f"dense distance={dense.get('distance')} != Cosine")
    if sparse is None:
        problems.append(f"missing sparse vector '{SPARSE_VECTOR}'")
    else:
        modifier = str(sparse.get("modifier") or "").lower()
        if modifier != "idf":
            problems.append(f"sparse modifier={sparse.get('modifier')} != idf")
    return problems


def create_collection(base_url: str, name: str, dim: int = DEFAULT_DIM) -> None:
    """Run create_collection."""
    request(
        "PUT",
        f"{base_url.rstrip('/')}/collections/{name}",
        collection_spec(dim),
    )


def delete_collection(base_url: str, name: str) -> None:
    """Run delete_collection."""
    try:
        request("DELETE", f"{base_url.rstrip('/')}/collections/{name}")
    except HttpError as exc:
        if exc.code != 404:
            raise


def ensure_payload_indexes(
    base_url: str, name: str, indexes: dict[str, str] | None = None
) -> list[str]:
    """Create missing payload indexes; returns the fields created."""
    indexes = indexes or PAYLOAD_INDEXES
    info = collection_info(base_url, name)
    if info is None:
        raise RuntimeError(f"collection {name} does not exist")
    existing = set((info.get("payload_schema") or {}).keys())
    created = []
    for field, schema in indexes.items():
        if field in existing:
            continue
        request(
            "PUT",
            f"{base_url.rstrip('/')}/collections/{name}/index?wait=true",
            {"field_name": field, "field_schema": schema},
        )
        created.append(field)
    return created


def count_points(
    base_url: str, name: str, flt: dict[str, Any] | None = None
) -> int:
    """Run count_points."""
    payload: dict[str, Any] = {"exact": True}
    if flt:
        payload["filter"] = flt
    _, body = request(
        "POST",
        f"{base_url.rstrip('/')}/collections/{name}/points/count",
        payload,
    )
    return (
        int(((body or {}).get("result") or {}).get("count", 0))
        if isinstance(body, dict)
        else 0
    )


def scroll_points(
    base_url: str,
    name: str,
    with_payload: Sequence[str] | bool = True,
    page: int = 512,
) -> Iterable[dict[str, Any]]:
    """Yield every point (id + payload) via the scroll API."""
    offset = None
    while True:
        body: dict[str, Any] = {
            "limit": page,
            "with_payload": with_payload,
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        _, resp = request(
            "POST",
            f"{base_url.rstrip('/')}/collections/{name}/points/scroll",
            body,
        )
        result = (
            ((resp or {}).get("result") or {}) if isinstance(resp, dict) else {}
        )
        points = result.get("points") or []
        yield from points
        offset = result.get("next_page_offset")
        if not offset or not points:
            return


# ------------------------------------------------------------------- embeddings


def embed_texts(
    embedding_url: str,
    texts: Sequence[str],
    model: str | None = None,
    timeout: float = 120.0,
) -> list[list[float]]:
    """OpenAI-compatible /embeddings call against the TEI container."""
    url = embedding_url.rstrip("/") + "/embeddings"
    body: dict[str, Any] = {"input": list(texts)}
    if model:
        body["model"] = model
    _, resp = request("POST", url, body, timeout=timeout, retries=2)
    data = (resp or {}).get("data") if isinstance(resp, dict) else None
    if not data:
        raise RuntimeError(
            f"embedding service returned no data: {str(resp)[:300]}"
        )
    return [item["embedding"] for item in data]
