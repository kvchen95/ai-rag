#!/usr/bin/env python3
"""Shared Qdrant helpers for ai-rag (stdlib only: urllib).

Everything that must agree between ingest (A3), the collection bootstrap and the
full rebuild lives here, so there is exactly one definition of:

  * the collection shape        (named dense "dense" 512/Cosine + sparse "bm25" IDF)
  * the point id mapping        (chunk_id -> deterministic UUIDv5)
  * the payload fields + indexes used by retrieval filters
  * the HTTP calls              (PUT collection / PUT index / upsert / scroll)

Point id mapping — why it is not chunk_id verbatim
--------------------------------------------------
Qdrant point ids must be an unsigned integer or a UUID; ``ch_9f2c31`` is neither,
so 附录 B's "id=chunk_id" cannot be stored as-is. We derive a stable UUIDv5 from
the chunk_id and keep the real id in ``payload.chunk_id``:

    point_id = uuid5(NAMESPACE, chunk_id)          # deterministic, replay-safe

Re-running an upsert overwrites the same point instead of duplicating it, which is
what makes the rebuild script idempotent.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Iterable, Sequence

DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"
BM25_MODEL = "qdrant/bm25"
DEFAULT_DIM = 512
DEFAULT_COLLECTION = "kb_chunks"

# Fixed namespace: changing it re-keys every point (= full rebuild). Do not change.
POINT_NAMESPACE = uuid.UUID("6f3d3c1e-2b8a-5a0e-9c7d-1a2b3c4d5e6f")

# Payload indexes required by 附录 B: doc_key, version_id, is_public, allowed_depts,
# allowed_roles, effective_year, chunk_type, status. chunk_id / parent_id / page_num
# are not filter fields but are indexed/kept for joins back to PG and citations.
PAYLOAD_INDEXES: dict[str, str] = {
    "doc_key": "keyword",
    "version_id": "keyword",
    "chunk_id": "keyword",
    "chunk_type": "keyword",
    "status": "keyword",
    "parent_id": "keyword",
    "is_public": "bool",
    "allowed_depts": "keyword",   # array<keyword>: Qdrant matches "any element"
    "allowed_roles": "keyword",
    "effective_year": "integer",
    "page_num": "integer",
}


def point_id_for(chunk_id: str) -> str:
    """Deterministic Qdrant point id for a chunk_id."""
    return str(uuid.uuid5(POINT_NAMESPACE, chunk_id))


def collection_spec(dim: int = DEFAULT_DIM) -> dict[str, Any]:
    """PUT /collections/{name} body — 方案B：dense 512 COSINE + bm25 sparse IDF."""
    return {
        "vectors": {DENSE_VECTOR: {"size": dim, "distance": "Cosine"}},
        "sparse_vectors": {SPARSE_VECTOR: {"modifier": "idf"}},
        "on_disk_payload": True,
        "hnsw_config": {"m": 16, "ef_construct": 100},
        "optimizers_config": {"indexing_threshold": 20000},
    }


def build_payload(row: dict[str, Any], status: str | None = None) -> dict[str, Any]:
    """Filter fields + identifiers stored with each point (正文不进向量库)."""
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
    }
    if row.get("effective_year") is not None:
        payload["effective_year"] = int(row["effective_year"])
    if row.get("parent_id"):
        payload["parent_id"] = row["parent_id"]
    return payload


# --------------------------------------------------------------------------- HTTP


class HttpError(RuntimeError):
    def __init__(self, method: str, url: str, code: int, body: str):
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
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
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


# --------------------------------------------------------------------- collection


def collection_info(base_url: str, name: str) -> dict[str, Any] | None:
    """Return the collection's ``result`` block, or None when it does not exist."""
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
        problems.append(f"missing dense vector '{DENSE_VECTOR}' (found: {sorted((params.get('vectors') or {}))})")
    else:
        if int(dense.get("size", -1)) != dim:
            problems.append(f"dense size={dense.get('size')} != {dim} (cannot mix dims; full rebuild needed)")
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
    request("PUT", f"{base_url.rstrip('/')}/collections/{name}", collection_spec(dim))


def delete_collection(base_url: str, name: str) -> None:
    try:
        request("DELETE", f"{base_url.rstrip('/')}/collections/{name}")
    except HttpError as exc:
        if exc.code != 404:
            raise


def ensure_payload_indexes(base_url: str, name: str, indexes: dict[str, str] | None = None) -> list[str]:
    """Create missing payload indexes. Returns the list of fields that were created."""
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


def count_points(base_url: str, name: str, flt: dict[str, Any] | None = None) -> int:
    payload: dict[str, Any] = {"exact": True}
    if flt:
        payload["filter"] = flt
    _, body = request("POST", f"{base_url.rstrip('/')}/collections/{name}/points/count", payload)
    return int(((body or {}).get("result") or {}).get("count", 0)) if isinstance(body, dict) else 0


def scroll_points(
    base_url: str,
    name: str,
    with_payload: Sequence[str] | bool = True,
    page: int = 512,
) -> Iterable[dict[str, Any]]:
    """Yield every point (id + payload) via the scroll API."""
    offset = None
    while True:
        body: dict[str, Any] = {"limit": page, "with_payload": with_payload, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        _, resp = request("POST", f"{base_url.rstrip('/')}/collections/{name}/points/scroll", body)
        result = ((resp or {}).get("result") or {}) if isinstance(resp, dict) else {}
        points = result.get("points") or []
        for pt in points:
            yield pt
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
        raise RuntimeError(f"embedding service returned no data: {str(resp)[:300]}")
    return [item["embedding"] for item in data]
