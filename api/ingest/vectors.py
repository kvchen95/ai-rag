"""双向量写入：一个切片同时写稠密（BGE-small-zh-v1.5 / 512）+ 稀疏（BM25），进同一个 point。

口径来源：详细设计.md §3.6、§7.1、附录 B；集合形状与 point id 规则由
`scripts/qdrant_common.py`（DA1）唯一持有，这里不复制常量——两份定义迟早分叉，
而分叉的表现是"检索少召回几条"，功能测试看不出来。

四件必须一起成立的事
--------------------

1. **dense 与 bm25 在同一个 point 的同一个 vector 字段里**（命名向量 `dense` /
   `bm25`）。写成两条 point 会让 §7.2 的 RRF 融合失去意义：名次来自两路，条目必须
   是同一份切片。实测：读回来 `vector.keys() == ['bm25','dense']`。

2. **payload 带齐过滤字段**。`status` 恒等于切片状态——新版本写进来是 `pending`，
   检索侧过滤恒带 `status=active`（§7.1），所以 pending 版本天然不可见，不需要
   "先写后藏"的额外动作。

3. **向量先写、PG 后写（回填 embedding_dense）**。顺序不能反：PG 里存了向量但
   Qdrant 没有点，对账会认为"点丢了"并尝试修复；反过来（Qdrant 有点、PG 没向量）
   则是一份无法从库内重建的冗余。所以先 upsert 再回填，回填失败只是让 PG 那一列
   留空，不影响可检索性。

4. **按批编码**。TEI 的 `--max-client-batch-size 32` 是硬上限，一次塞 200 条会被
   直接拒（不是截断）。批大小默认 32 且可配（`--batch-size`）。

为什么 embedding 走 httpx 而不是复用 scripts/qdrant_common.embed_texts
--------------------------------------------------------------------
后者是 urllib，会继承宿主 `HTTP(S)_PROXY`；本机 ALL_PROXY 指向已死的 7890，
容器/宿主行为不一致（B2 踩过同一类坑）。这里统一用 `trust_env=False` 的 httpx，
并在响应上强校验维度与 L2 范数——维度错会让 Cosine 分数变成垃圾，范数没归一
会让"余弦相似度"退化成点积，两者都不会报错，只会让检索质量悄悄变差。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import httpx
from psycopg import Connection
from psycopg.rows import dict_row

from api.observability import get_logger, log_event
from scripts.qdrant_common import (
    DENSE_VECTOR,
    SPARSE_VECTOR,
    bm25_text,
    bm25_vector,
    build_payload,
    point_id_for,
)

logger = get_logger("api.ingest.vectors")

# TEI 的 max-client-batch-size 默认是 32：超过它服务端直接 4xx，不会自动切片。
DEFAULT_EMBED_BATCH = 32
# 单次 HTTP 的尝试次数（含首次）。与 §4.2「同步失败重试三次」不是同一个口径：
# 这是"一次调用内的传输层重试"，那一条是"整件事跨时间的重试"。
HTTP_ATTEMPTS = 3
HTTP_BACKOFF_S = 0.5


class VectorStoreError(RuntimeError):
    """向量库不可用/拒绝请求。调用方据此决定"重试"还是"进对账"。"""


class EmbeddingError(RuntimeError):
    """编码服务不可用或返回了不合规的向量（维度错 / 未归一 / 条数对不上）。"""


# --------------------------------------------------------------------- embedder


@dataclass(slots=True)
class Embedder:
    """TEI（OpenAI 兼容 /embeddings）的薄客户端 + 输出校验。"""

    base_url: str
    model: str
    dim: int
    api_key: str = ""
    timeout_s: float = 30.0
    batch_size: int = DEFAULT_EMBED_BATCH
    _client: httpx.Client | None = field(default=None, repr=False)

    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            # trust_env=False：容器内访问 http://embedding:8000 绝不能走宿主代理。
            self._client = httpx.Client(
                base_url=self.base_url.rstrip("/"),
                timeout=self.timeout_s,
                headers=headers,
                trust_env=False,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "Embedder":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量编码。返回顺序与入参严格一致（Qdrant 的 upsert 靠位置配对）。"""
        if not texts:
            return []
        payload: dict[str, Any] = {"input": list(texts), "model": self.model}
        last: Exception | None = None
        for attempt in range(HTTP_ATTEMPTS):
            try:
                resp = self._http().post("/embeddings", json=payload)
            except httpx.HTTPError as exc:
                last = exc
                if attempt + 1 < HTTP_ATTEMPTS:
                    time.sleep(HTTP_BACKOFF_S * (attempt + 1))
                    continue
                raise EmbeddingError(f"embedding service unreachable: {exc}") from exc
            if resp.status_code >= 500 and attempt + 1 < HTTP_ATTEMPTS:
                last = EmbeddingError(f"HTTP {resp.status_code}")
                time.sleep(HTTP_BACKOFF_S * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise EmbeddingError(
                    f"embedding service rejected the request: HTTP {resp.status_code} "
                    f"{resp.text[:300]}"
                )
            return self._validate(resp.json().get("data") or [], len(texts))
        raise EmbeddingError(f"embedding failed after {HTTP_ATTEMPTS} attempts: {last}")

    def _validate(self, data: list[dict[str, Any]], expected: int) -> list[list[float]]:
        """条数、维度、L2 范数三项都校验。

        三项里任何一项不对都不会让请求失败——服务会老老实实返回向量，然后检索质量
        悄悄变差。所以宁可在这里炸，也不要让一份坏向量进库。
        """
        if len(data) != expected:
            raise EmbeddingError(
                f"embedding returned {len(data)} vectors for {expected} texts"
            )
        out: list[list[float]] = []
        for index, item in enumerate(data):
            vec = item.get("embedding")
            if not isinstance(vec, list) or not vec:
                raise EmbeddingError(f"embedding #{index} has no vector")
            if len(vec) != self.dim:
                raise EmbeddingError(
                    f"embedding dim={len(vec)} != configured EMBEDDING_DIM={self.dim} "
                    "(model and collection must agree)"
                )
            norm = sum(float(v) * float(v) for v in vec) ** 0.5
            if abs(norm - 1.0) > 0.02:
                raise EmbeddingError(
                    f"embedding #{index} is not L2-normalised (norm={norm:.4f}); "
                    "Cosine similarity would be distorted"
                )
            out.append([float(v) for v in vec])
        return out


# ----------------------------------------------------------------- vector store


@dataclass(slots=True)
class QdrantVectorStore:
    """只做本卡需要的五件事：upsert / 按版本改状态 / 按版本删点 / 按版本计数 / 按版本滚动。"""

    base_url: str
    collection: str
    api_key: str = ""
    timeout_s: float = 10.0
    _client: httpx.Client | None = field(default=None, repr=False)

    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {"api-key": self.api_key} if self.api_key else {}
            self._client = httpx.Client(
                base_url=self.base_url.rstrip("/"),
                timeout=self.timeout_s,
                headers=headers,
                trust_env=False,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "QdrantVectorStore":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """带重试的 REST 调用。5xx / 连接错误重试，4xx 直接抛（重试也是白试）。"""
        last: Exception | None = None
        for attempt in range(HTTP_ATTEMPTS):
            try:
                resp = self._http().request(
                    method, path, json=body, timeout=timeout or self.timeout_s
                )
            except httpx.HTTPError as exc:
                last = exc
                if attempt + 1 < HTTP_ATTEMPTS:
                    time.sleep(HTTP_BACKOFF_S * (attempt + 1))
                    continue
                raise VectorStoreError(f"{method} {path} failed: {exc}") from exc
            if resp.status_code >= 500 and attempt + 1 < HTTP_ATTEMPTS:
                last = VectorStoreError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                time.sleep(HTTP_BACKOFF_S * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise VectorStoreError(
                    f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:400]}"
                )
            return resp.json() if resp.content else None
        raise VectorStoreError(f"{method} {path} failed after retries: {last}")

    # --- 写 ---

    def upsert(self, points: Sequence[dict[str, Any]], *, timeout: float = 60.0) -> int:
        """`wait=true`：写完立刻可查。

        不等的话，翻牌后的"同步校验"（按版本计数）会在自己的写入生效前跑，
        于是把一次成功的写入判成失败并触发无意义的重试。
        """
        if not points:
            return 0
        self._call(
            "PUT",
            f"/collections/{self.collection}/points?wait=true",
            {"points": list(points)},
            timeout=timeout,
        )
        return len(points)

    def delete_version_points(self, version_id: str) -> None:
        """按 version_id 删点（幂等：没有匹配点时 Qdrant 返回 completed）。"""
        self._call(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            {"filter": _version_filter(version_id)},
        )

    def set_status_by_version(self, version_id: str, status: str) -> None:
        """按 version_id 一次性改整批点的 payload.status（§4.2「不逐条改」）。

        实测：`POST /points/payload` 的 filter 形式一次调用就完成；逐条改在几十万点
        上要跑很久，而翻牌后的同步窗口只有一分钟。
        """
        self._call(
            "POST",
            f"/collections/{self.collection}/points/payload?wait=true",
            {"payload": {"status": status}, "filter": _version_filter(version_id)},
        )

    def set_status_by_chunk_ids(self, chunk_ids: Sequence[str], status: str) -> None:
        """按 chunk_id 列表改 payload.status（对账逐点纠偏用）。"""
        if not chunk_ids:
            return
        self._call(
            "POST",
            f"/collections/{self.collection}/points/payload?wait=true",
            {
                "payload": {"status": status},
                "points": [point_id_for(chunk_id) for chunk_id in chunk_ids],
            },
        )

    def delete_points(self, point_ids: Sequence[str]) -> None:
        """按 point id 删点（对账清孤儿点用）。空列表是 no-op。"""
        if not point_ids:
            return
        self._call(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            {"points": list(point_ids)},
        )

    def retrieve(self, point_ids: Sequence[str]) -> list[dict[str, Any]]:
        """按 id 取回点（含 payload，不含向量）。不存在的 id 不会出现在结果里。"""
        if not point_ids:
            return []
        out: list[dict[str, Any]] = []
        ids = list(point_ids)
        for start in range(0, len(ids), 256):
            body = self._call(
                "POST",
                f"/collections/{self.collection}/points",
                {"ids": ids[start : start + 256], "with_payload": True, "with_vector": False},
            )
            out.extend(((body or {}).get("result") or []) if isinstance(body, dict) else [])
        return out

    # --- 读 ---

    def count_version(self, version_id: str, *, status: str | None = None) -> int:
        flt = _version_filter(version_id)
        if status is not None:
            flt = {"must": flt["must"] + [{"key": "status", "match": {"value": status}}]}
        body = self._call(
            "POST",
            f"/collections/{self.collection}/points/count",
            {"exact": True, "filter": flt},
        )
        return int(((body or {}).get("result") or {}).get("count", 0))

    def scroll_version(self, version_id: str, *, page: int = 256) -> list[dict[str, Any]]:
        """该版本名下所有点的 payload（对账用）。"""
        points: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            body: dict[str, Any] = {
                "limit": page,
                "with_payload": True,
                "with_vector": False,
                "filter": _version_filter(version_id),
            }
            if offset is not None:
                body["offset"] = offset
            resp = self._call(
                "POST", f"/collections/{self.collection}/points/scroll", body
            )
            result = ((resp or {}).get("result") or {}) if isinstance(resp, dict) else {}
            page_points = result.get("points") or []
            points.extend(page_points)
            offset = result.get("next_page_offset")
            if not offset or not page_points:
                return points

    def scroll_doc(self, doc_key: str, *, page: int = 256) -> list[dict[str, Any]]:
        points: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            body: dict[str, Any] = {
                "limit": page,
                "with_payload": True,
                "with_vector": False,
                "filter": {"must": [{"key": "doc_key", "match": {"value": doc_key}}]},
            }
            if offset is not None:
                body["offset"] = offset
            resp = self._call(
                "POST", f"/collections/{self.collection}/points/scroll", body
            )
            result = ((resp or {}).get("result") or {}) if isinstance(resp, dict) else {}
            page_points = result.get("points") or []
            points.extend(page_points)
            offset = result.get("next_page_offset")
            if not offset or not page_points:
                return points

    def collection_points(self) -> int:
        body = self._call("GET", f"/collections/{self.collection}")
        return int(((body or {}).get("result") or {}).get("points_count", 0))


def _version_filter(version_id: str) -> dict[str, Any]:
    return {"must": [{"key": "version_id", "match": {"value": version_id}}]}


# ------------------------------------------------------------------ index a version

SELECT_CHUNKS_SQL = """
SELECT chunk_id, doc_key, version_id, chunk_type, parent_id, page_num,
       content_text, effective_date, effective_year, date_confidence,
       report_period_start, report_period_end, publication_date,
       is_public, allowed_depts, allowed_roles, status
FROM chunks
WHERE version_id = %s
ORDER BY page_num, chunk_id
"""

UPDATE_DENSE_SQL = "UPDATE chunks SET embedding_dense = %s::vector WHERE chunk_id = %s"


@dataclass(slots=True)
class IndexReport:
    version_id: str
    rows: int = 0
    points: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    dense_written: int = 0
    statuses: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """完整入库：每一条切片都进了库。

        跳过**不算成功**。被跳过的切片在关键词路与语义路上都永远召回不到，把它算成
        "跑完了"会让一次丢内容的写入看起来正常——那正是这张卡最该防的静默失效。
        跳过本身不是错误（零宽字符的切片确实没法索引），所以它单独计数、由调用方
        决定"带病转正"还是"先修解析"。
        """
        return self.rows > 0 and not self.skipped and self.points == self.rows

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "rows": self.rows,
            "points": self.points,
            "skipped": len(self.skipped),
            "dense_written": self.dense_written,
            "statuses": self.statuses,
        }


def index_version(
    conn: Connection[Any],
    store: QdrantVectorStore,
    embedder: Embedder,
    version_id: str,
    *,
    batch_size: int | None = None,
    write_dense_back: bool = True,
) -> IndexReport:
    """把一个版本的切片写成 Qdrant 点（dense + bm25 + 完整 payload）。

    在**调用方的事务里**工作：不自己 commit。翻牌前的写入通常是"解析完成 → 写切片
    → 写向量 → 提交"这样一条链，中间提交会出现"切片已可见、向量还没有"的窗口。

    正文为空的切片跳过并记进 `skipped`：库里 `ck_chunks_text_nonempty` 已经拦了空白
    正文，这里防的是"只有空白字符以外的零宽字符"这种漏网的极端值——空 BM25 稀疏
    向量会被 Qdrant 拒，而那时整批都写不进去。
    """
    batch = max(1, batch_size or embedder.batch_size)
    report = IndexReport(version_id=version_id)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SELECT_CHUNKS_SQL, (version_id,))
        rows = list(cur.fetchall())
    report.rows = len(rows)

    pending: list[dict[str, Any]] = []
    for row in rows:
        text = row.get("content_text") or ""
        # `str.strip()` 挡不住 U+200B/200D/FEFF 这类零宽字符（实测：它们 strip 之后
        # 还在），而 Qdrant 会把它们切成 0 个 token → 稀疏向量为空。空稀疏向量本身
        # 能被 Qdrant 收下（实测 200），但那条切片在关键词路上永远召回不到，属于
        # 静默失效。所以判据是"有没有可检索字符"，不是"strip 之后非空"。
        if not bm25_text(text):
            report.skipped.append((row["chunk_id"], "no searchable characters in content_text"))
            continue
        row["status"] = str(row["status"])
        report.statuses[row["status"]] = report.statuses.get(row["status"], 0) + 1
        pending.append(row)

    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        vectors = embedder.embed([row["content_text"] for row in chunk])
        points = [
            {
                "id": point_id_for(row["chunk_id"]),
                "vector": {
                    DENSE_VECTOR: vector,
                    # BM25 交给 Qdrant 服务端推理（modifier=idf），正文先过 `bm25_text()`
                    # 做中文 bigram 切分——不切的话整句中文只有 1 个 token，关键词路
                    # 等于废掉（实测见 scripts/qdrant_common.py 的数字）。词频仍由服务
                    # 端算，所以稀疏向量永远可以从 PG 重算，不需要另存。
                    SPARSE_VECTOR: bm25_vector(row["content_text"]),
                },
                "payload": build_payload(row, status=row["status"]),
            }
            for row, vector in zip(chunk, vectors, strict=True)
        ]
        report.points += store.upsert(points)

        if write_dense_back:
            # 回填 PG 的 embedding_dense：让"点丢了"可以只靠库内数据修复，
            # 不必重新编码（对账修 missing_point 就是走这条路）。
            with conn.cursor() as cur:
                cur.executemany(
                    UPDATE_DENSE_SQL,
                    [(_vector_literal(vector), row["chunk_id"]) for row, vector in zip(chunk, vectors, strict=True)],
                )
            report.dense_written += len(chunk)

    log_event(
        logger,
        "version_indexed",
        version_id=version_id,
        rows=report.rows,
        points=report.points,
        skipped=len(report.skipped),
    )
    return report


def _vector_literal(vector: Iterable[float]) -> str:
    """pgvector 的文本输入格式：`[v1,v2,...]`。

    不引 pgvector-python 是因为它只为了一个类型注册；`%s::vector` 用文本输入等价，
    且少了"装不上 pgvector 包就跑不了"的额外依赖。
    """
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def points_from_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """从**已带 embedding_dense**的 PG 行拼点（不需要编码服务）。

    给对账修 `missing_point` 用：PG 里存了稠密向量，缺的点可以直接补，不必调用
    编码服务——编码服务挂了的时候，"向量库丢了点"仍然可修。
    """
    points: list[dict[str, Any]] = []
    for row in rows:
        dense = row.get("embedding_dense")
        if dense is None:
            raise EmbeddingError(
                f"chunk {row['chunk_id']} has no embedding_dense; re-index it instead"
            )
        if isinstance(dense, str):
            dense = [float(part) for part in dense.strip("[]").split(",") if part]
        points.append(
            {
                "id": point_id_for(row["chunk_id"]),
                "vector": {
                    DENSE_VECTOR: [float(v) for v in dense],
                    SPARSE_VECTOR: bm25_vector(row["content_text"]),
                },
                "payload": build_payload(row, status=str(row["status"])),
            }
        )
    return points


__all__ = [
    "Embedder",
    "EmbeddingError",
    "IndexReport",
    "QdrantVectorStore",
    "VectorStoreError",
    "index_version",
    "points_from_rows",
]
