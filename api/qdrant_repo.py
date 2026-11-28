"""向量库侧的"按过滤条件取候选"。.

存在的理由和 `chunks_repo.select_ids` 一样：让"两边过滤语义一致"这件事可以被
对拍证明。真正的检索（稠密 + BM25 两路 prefetch + RRF）属于 B3 卡，这里刻意
只做最笨的一件事——scroll，不排序、不打分。

用 scroll 而不是 search：对拍要的是**全集**，而相似度检索天然带 limit，
两边 limit 不同就会得出"漏了"的假结论。

`scroll_rows` 返回 payload（含 `version_id`），因为 §7.4 的版本兜底需要它；
`scroll_ids` 是它上面的一层薄封装，只给调用方要编号的场景用。
"""

from __future__ import annotations

from typing import Any

from api.filters import RetrievalFilter

DEFAULT_SCROLL_LIMIT = 5000
PAGE_SIZE = 256


def scroll_rows(
    client: Any,
    collection: str,
    flt: RetrievalFilter,
    *,
    limit: int = DEFAULT_SCROLL_LIMIT,
) -> list[dict[str, Any]]:
    """返回满足前置过滤的切片 payload（去重后按 chunk_id 排序）。."""
    qdrant_filter = flt.to_qdrant()
    rows: dict[str, dict[str, Any]] = {}
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=qdrant_filter,
            limit=PAGE_SIZE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in points:
            payload = dict(point.payload if point.payload else {})
            chunk_id = payload.get("chunk_id")
            if chunk_id:
                rows[str(chunk_id)] = payload
        if offset is None or len(rows) >= limit:
            break
    return [rows[key] for key in sorted(rows)]


def scroll_ids(
    client: Any,
    collection: str,
    flt: RetrievalFilter,
    *,
    limit: int = DEFAULT_SCROLL_LIMIT,
) -> list[str]:
    """Run scroll_ids."""
    return [
        row["chunk_id"]
        for row in scroll_rows(client, collection, flt, limit=limit)
    ]
