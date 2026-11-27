"""混合检索：两路 prefetch + RRF 融合（详细设计.md §7.1 / §7.2 / §7.6）。.

为什么走 REST 而不是 qdrant-client
----------------------------------
实测（1.15.1）：`client.query_batch_points()` 传
`query=models.Document(text=..., model="qdrant/bm25")` 会走进 fastembed 分支
并抛 `ImportError: fastembed is not installed`。为了发一句查询去装一个 ~100MB
的推理依赖不划算，而 REST 的 `{"text": <预分词串>, "model": "qdrant/bm25"}`
服务端自己推理、一次 HTTP 拿回两路（实测通过，`/points/query/batch` 的
`searches` 数组）。这也与 A3 的 `QdrantVectorStore`（同样是 REST）保持一致：
两条链路对同一个 Qdrant 用同一种客户端，排障时不用先分辨"这个方法走 SDK 还是
走 REST"。

为什么用 batch 而不是原生 `fusion=rrf`
--------------------------------------
§7.2 说"两种方式结果等价时优先用 Qdrant 原生，少一跳网络"。这条在这里不成立：

1. `/points/query/batch` 与 `/points/query`（带 prefetch+fusion）**都是一次
   HTTP**，"少一跳"没有收益；
2. §7.6 要求每条候选带 `dense_rank` / `sparse_rank`，原生融合只返回合成分数，
   拿不到两路各自的名次——审计要回答"它为什么没排上来"就缺了依据；
3. §7.2 的并列规则（名次和相同看语义名次）必须是**我们能控制**的，原生实现里的
   并列顺序不是我们的契约。

代价是我们要自己算一遍 RRF。为了不"自造一套语义"，常数 k 不是猜的：实测
（真 Qdrant 1.15.3）原生 RRF 的输出逐值等于 `Σ 1/(1 + 名次)`（1-based）——
`1.0 / 0.666667 / 0.5 / 0.2 / 0.166667 / 0.142857` 六个值全部对上。所以下面
`RRF_K = 1` 是**对齐实现的常数**，不是调参旋钮；`test_hybrid_parity` 里有一条
用例拿同一批数据跑原生融合与本地融合，顺序必须一致，改坏这个常数会立刻红。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from api.filters import RetrievalFilter
from api.observability import get_logger, log_event
from api.trace import trace as trace_event
from scripts.qdrant_common import DENSE_VECTOR, SPARSE_VECTOR, bm25_vector

logger = get_logger("api.retrieval.hybrid")

# 与 Qdrant 原生 RRF 对齐的常数（实测反解，见模块 docstring）。改它等于换一套
# 融合语义，必须先重跑 parity 用例。
RRF_K = 1

# 一路没命中时候的名次（§7.2 表格："一条只在一路出现，另一路名次记为 31"）。
# 取 top_k + 1 而不是"不参与计算"：不参与等于把"只在一路出现"当成"两路都弱"，
# 而 §7.2 明确要给一个确认过的名次。
MISSING_RANK_OFFSET = 1

# Qdrant payload 里我们真正需要的字段。正文不进向量库（附录 B），所以候选只有
# 编号、定位与分数；正文要回 PG 取（§2.4「证据包」的正文来自库）。
CANDIDATE_PAYLOAD_FIELDS = (
    "chunk_id",
    "doc_key",
    "version_id",
    "chunk_type",
    "parent_id",
    "page_num",
    "effective_date",
    "effective_year",
)


class HybridRetrievalError(RuntimeError):
    """向量库不可用或返回了无法解释的形状。调用方据此决定重试还是拒绝。."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条候选：三路分数与出处（§7.6「这一章交给下一步什么」）。."""

    chunk_id: str
    doc_key: str
    version_id: str
    chunk_type: str
    page_num: int
    parent_id: str | None = None
    effective_date: int | None = None
    effective_year: int | None = None
    # 名次一律 1-based；None 表示该路没召回它（区别于"排在第 31 名"）。
    dense_rank: int | None = None
    sparse_rank: int | None = None
    dense_score: float | None = None
    sparse_score: float | None = None
    fused_score: float = 0.0
    fused_rank: int = 0
    # 精排分数（0..1，sigmoid 后的交叉编码器输出）。None = 没跑或降级。
    rerank_score: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "chunk_id": self.chunk_id,
            "doc_key": self.doc_key,
            "version_id": self.version_id,
            "chunk_type": self.chunk_type,
            "page_num": self.page_num,
            "parent_id": self.parent_id,
            "dense_rank": self.dense_rank,
            "sparse_rank": self.sparse_rank,
            "fused_rank": self.fused_rank,
            "fused_score": round(self.fused_score, 6),
            "rerank_score": None
            if self.rerank_score is None
            else round(self.rerank_score, 6),
        }

    def with_rerank(self, score: float | None) -> Candidate:
        """Run with_rerank."""
        return Candidate(
            chunk_id=self.chunk_id,
            doc_key=self.doc_key,
            version_id=self.version_id,
            chunk_type=self.chunk_type,
            page_num=self.page_num,
            parent_id=self.parent_id,
            effective_date=self.effective_date,
            effective_year=self.effective_year,
            dense_rank=self.dense_rank,
            sparse_rank=self.sparse_rank,
            dense_score=self.dense_score,
            sparse_score=self.sparse_score,
            fused_score=self.fused_score,
            fused_rank=self.fused_rank,
            rerank_score=score,
        )


@dataclass(frozen=True, slots=True)
class HybridResult:
    """HybridResult."""

    candidates: list[Candidate]
    # 两路各自返回的条数（排障用：一路恒为 0 通常意味着 sparse 侧没写进去）。
    dense_count: int
    sparse_count: int
    # 实际发给 Qdrant 的 filter JSON。审计里存一份，事后能复现
    # "这次按什么条件搜的"。
    filter_json: dict[str, Any] = field(default_factory=dict)


def qdrant_filter_json(flt: RetrievalFilter) -> dict[str, Any]:
    """把过滤条件渲染成**一份** JSON，两路共用同一份。.

    §7.1 原话："两路必须带完全相同的 filter"。这里的做法比"各自 to_qdrant()"
    更硬：只 dump 一次，两路拿到的是同一个 dict 对象。测试里对它做逐字节比较
    （`test_both_routes_get_the_same_filter`），谁想把其中一路改成自建 filter，
    必须先把这条用例删掉——而删用例比改错难被忽略。

    `exclude_none=True` 不是美化：Qdrant 对 `{"range": null}` 这类显式 None 的
    容忍度没有承诺，而两条路一旦一条带 None 一条不带，字节比较就会红——那正好是
    我们想要的红。
    """
    return flt.to_qdrant().model_dump(mode="json", exclude_none=True)


def rrf_fuse(
    dense: Sequence[tuple[str, float]],
    sparse: Sequence[tuple[str, float]],
    *,
    top_k: int,
    rrf_k: int = RRF_K,
) -> list[Candidate]:
    """把两路 `[(chunk_id, score)]` 按名次融合（§7.2）。.

    分数不可比，名次可比：稠密是余弦（0..1），BM25 是无上界的词频加权
    （实测同一句查询能到 13.29）。所以这里**完全不看分数值**，只用位置。

    三条规则逐条对应 §7.2 的边界表：
    - 一条只在一路出现 → 另一路名次记 `top_k + 1`；
    - 名次和相同 → 语义（稠密）名次靠前者优先；
    - 同一父片的第 3 条及以后 → 丢（由 `dedupe_parent_children` 做，见调用方）。

    并列的第二级退回 `chunk_id` 字典序：没有第二级的话，同分候选的顺序取决于
    dict 插入顺序，同一份数据两次查询可能给出不同证据包——排查时会以为是
    检索漂移。
    """
    dense_rank = {
        chunk_id: index + 1 for index, (chunk_id, _) in enumerate(dense)
    }
    sparse_rank = {
        chunk_id: index + 1 for index, (chunk_id, _) in enumerate(sparse)
    }
    dense_score = dict(dense)
    sparse_score = dict(sparse)
    missing = top_k + MISSING_RANK_OFFSET

    scored: list[tuple[float, int, str]] = []
    for chunk_id in set(dense_rank) | set(sparse_rank):
        d_rank = dense_rank.get(chunk_id, missing)
        s_rank = sparse_rank.get(chunk_id, missing)
        score = 1.0 / (rrf_k + d_rank) + 1.0 / (rrf_k + s_rank)
        scored.append((score, d_rank, chunk_id))

    # 分数降序 → 语义名次升序（§7.2）→ chunk_id 升序（确定性兜底）。
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    # 排序用的 key 里已经带了语义名次，所以同分时稠密靠前的自然在前。
    return [
        Candidate(
            chunk_id=chunk_id,
            doc_key="",  # 由调用方从 payload 补齐
            version_id="",
            chunk_type="",
            page_num=0,
            dense_rank=dense_rank.get(chunk_id),
            sparse_rank=sparse_rank.get(chunk_id),
            dense_score=dense_score.get(chunk_id),
            sparse_score=sparse_score.get(chunk_id),
            fused_score=score,
            fused_rank=index + 1,
        )
        for index, (score, _d_rank, chunk_id) in enumerate(scored)
    ]


def dedupe_parent_children(
    candidates: Sequence[Candidate], *, keep: int = 2
) -> tuple[list[Candidate], list[Candidate]]:
    """同一父片最多留 `keep` 条子片（§7.2 边界表）。.

    父子切片内容高度重叠，全留下会把精排预算花在重复信息上（精排是逐对打分，
    每多一条都是实打实的毫秒）。返回 (保留, 丢弃)，丢弃的进审计。

    `keep=0` 是合法输入（等于关闭去重），负数是配置错误——用 `max(0, keep)` 会
    把配置错误静默变成"全留"，所以直接抛。
    """
    if keep < 0:
        raise ValueError(f"parent_child_keep 必须 >= 0，实际 {keep}")
    seen: dict[str, int] = {}
    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    for candidate in candidates:
        parent = candidate.parent_id
        if not parent:
            # 父片本身（parent_id 为空）不参与这个规则：规则说的是"同父片的
            # 第 3 条子片及以后"，父片没有父片。
            kept.append(candidate)
            continue
        count = seen.get(parent, 0)
        if count < keep:
            seen[parent] = count + 1
            kept.append(candidate)
        else:
            dropped.append(candidate)
    return kept, dropped


def build_batch_body(
    query_text: str,
    dense_query: Sequence[float],
    flt: RetrievalFilter,
    *,
    top_k: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """构造两路请求体，返回 (请求体, 这次用的 filter JSON)。.

    抽成纯函数不是为了好看：**"两路同 filter"这条判据必须能在没有 Qdrant 的
    情况下被断言**。放在 `retrieve()` 里面的话，它只能靠集成测试验，而集成
    测试又贵又容易被 skip——一条只在"恰好装了 Qdrant"时才跑的判据，等于没有
    判据。

    filter 只 dump 一次，两路拿到的是**同一个 dict 对象**（不是两份内容相同的
    dict）：内容相同的两份仍然可能在未来某次改动里漂移，同一个对象不会。
    """
    if not query_text.strip():
        raise ValueError(
            "query_text 不能为空：空的 BM25 查询会退化成'全库等权'"
        )
    if not dense_query:
        raise ValueError("dense_query 不能为空")

    filter_json = qdrant_filter_json(flt)
    body = {
        "searches": [
            {
                "query": list(dense_query),
                "using": DENSE_VECTOR,
                "filter": filter_json,
                "limit": top_k,
                "with_payload": list(CANDIDATE_PAYLOAD_FIELDS),
                "with_vector": False,
            },
            {
                # 必须过 bm25_vector()：服务端只按空白/标点切词，中文整句只产出
                # 1 个 token（A3 实测 top1 命中率 1/4 → 3/4）。
                "query": bm25_vector(query_text),
                "using": SPARSE_VECTOR,
                "filter": filter_json,
                "limit": top_k,
                "with_payload": list(CANDIDATE_PAYLOAD_FIELDS),
                "with_vector": False,
            },
        ]
    }
    return body, filter_json


class HybridRetriever:
    """一次混合召回的完整动作：两路取数 → RRF → 父片去重。.

    只做召回，不做版本兜底与精排（那两个在 pipeline 里，顺序不能换：§7.4 的版本
    校验必须在精排前，理由见 `api/version_guard.py`）。
    """

    def __init__(
        self,
        base_url: str,
        collection: str,
        *,
        api_key: str = "",
        timeout_s: float = 5.0,
    ) -> None:
        """Initialize the instance."""
        self.base_url = base_url.rstrip("/")
        self.collection = collection
        headers = {"api-key": api_key} if api_key else {}
        # trust_env=False：容器内访问 http://qdrant:6333 绝不能走宿主
        # HTTP(S)_PROXY（B2/A3 都踩过，宿主 ALL_PROXY 指向一个已死的端口）。
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            headers=headers,
            trust_env=False,
        )

    def close(self) -> None:
        """Run close."""
        self._client.close()

    def __enter__(self) -> HybridRetriever:
        """Enter the context manager."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Exit the context manager."""
        self.close()

    def retrieve(
        self,
        query_text: str,
        flt: RetrievalFilter,
        *,
        dense_query: Sequence[float],
        top_k: int,
        parent_child_keep: int = 2,
    ) -> HybridResult:
        """两路各取 `top_k` 条，同 filter，RRF 融合后按父片去重。.

        `query_text` 与 `dense_query` 必须是同一次改写、同一句话的两种编码：
        稠密向量由调用方事先算好（编码服务是独立组件，召回层不该带一个编码器），
        稀疏侧在这里现场生成——`bm25_vector()` 是纯字符串操作，没有 IO。
        """
        if not query_text.strip():
            raise ValueError(
                "query_text 不能为空：空的 BM25 查询会退化成'全库等权'"
            )
        if not dense_query:
            raise ValueError("dense_query 不能为空")

        body, filter_json = build_batch_body(
            query_text, dense_query, flt, top_k=top_k
        )
        payloads: dict[str, dict[str, Any]] = {}
        routes: list[list[tuple[str, float]]] = []
        try:
            response = self._client.post(
                f"/collections/{self.collection}/points/query/batch", json=body
            )
            if response.status_code >= 400:
                raise HybridRetrievalError(
                    f"Qdrant batch query -> HTTP {response.status_code}: "
                    f"{response.text[:400]}"
                )
            result = (response.json() or {}).get("result") or []
        except httpx.HTTPError as exc:
            raise HybridRetrievalError(f"Qdrant unreachable: {exc}") from exc

        if len(result) != 2:
            raise HybridRetrievalError(
                f"batch query returned {len(result)} responses for 2 searches"
            )
        for route in result:
            rows: list[tuple[str, float]] = []
            for point in route.get("points") or []:
                payload = point.get("payload") or {}
                chunk_id = payload.get("chunk_id")
                if not chunk_id:
                    # payload 缺 chunk_id 的点无法回 PG、无法引用，收进来只会让
                    # 后续步骤拿到一个空编号。跳过并计数（下面日志里能看出来）。
                    continue
                payloads[chunk_id] = payload
                rows.append((chunk_id, float(point.get("score") or 0.0)))
            routes.append(rows)

        # 本地链路：两路原始名单先落盘，再看融合/去重。排障时“正确切片是否进过
        # 候选池”只能靠这一步回答，融合后的名单已经把两路出处压成了名次。
        trace_event(
            "hybrid.raw_routes",
            dense=[
                {"chunk_id": cid, "score": round(score, 6), "rank": index + 1}
                for index, (cid, score) in enumerate(routes[0])
            ],
            sparse=[
                {"chunk_id": cid, "score": round(score, 6), "rank": index + 1}
                for index, (cid, score) in enumerate(routes[1])
            ],
        )

        fused = rrf_fuse(routes[0], routes[1], top_k=top_k)
        # 融合结果是"裸编号 + 名次"，payload 在这里补回去：RRF 只看名次，
        # 不该知道 payload 长什么样（否则测试里没法用两个字符串构造它）。
        enriched = [
            _with_payload(candidate, payloads.get(candidate.chunk_id, {}))
            for candidate in fused
        ]
        kept, dropped = dedupe_parent_children(enriched, keep=parent_child_keep)
        trace_event(
            "hybrid.fused",
            query_text=query_text,
            top_k=top_k,
            candidates=[
                {
                    **item.as_dict(),
                    "dense_score": None
                    if item.dense_score is None
                    else round(item.dense_score, 6),
                    "sparse_score": None
                    if item.sparse_score is None
                    else round(item.sparse_score, 6),
                }
                for item in fused
            ],
        )
        trace_event(
            "hybrid.dedup",
            keep=parent_child_keep,
            kept_ids=[item.chunk_id for item in kept],
            dropped_ids=[item.chunk_id for item in dropped],
        )
        if dropped:
            log_event(
                logger,
                "parent_child_deduped",
                count=len(dropped),
                chunk_ids=[item.chunk_id for item in dropped][:20],
                keep=parent_child_keep,
            )
        log_event(
            logger,
            "hybrid_retrieved",
            dense=len(routes[0]),
            sparse=len(routes[1]),
            fused=len(fused),
            after_dedup=len(kept),
            top_k=top_k,
        )
        return HybridResult(
            candidates=kept,
            dense_count=len(routes[0]),
            sparse_count=len(routes[1]),
            filter_json=filter_json,
        )


def _with_payload(candidate: Candidate, payload: dict[str, Any]) -> Candidate:
    """把 payload 里的字段补到候选上。payload 缺失时保留空值而不是抛：.

    点存在但 payload 不全，说明有人绕过 `build_payload()` 直接写过向量库——
    这是数据事故，但它的表现应该是"这条候选内容为空、进不了证据包"，
    而不是整个请求 500（一次坏数据不该打挂在线链路）。
    """

    def _pick(key: str, default: Any) -> Any:
        """Run _pick."""
        value = payload.get(key)
        return default if value is None else value

    return Candidate(
        chunk_id=candidate.chunk_id,
        doc_key=_pick("doc_key", ""),
        version_id=_pick("version_id", ""),
        chunk_type=_pick("chunk_type", ""),
        page_num=int(_pick("page_num", 0)),
        parent_id=payload.get("parent_id"),
        effective_date=payload.get("effective_date"),
        effective_year=payload.get("effective_year"),
        dense_rank=candidate.dense_rank,
        sparse_rank=candidate.sparse_rank,
        dense_score=candidate.dense_score,
        sparse_score=candidate.sparse_score,
        fused_score=candidate.fused_score,
        fused_rank=candidate.fused_rank,
    )


__all__ = [
    "MISSING_RANK_OFFSET",
    "RRF_K",
    "Candidate",
    "HybridResult",
    "HybridRetrievalError",
    "HybridRetriever",
    "build_batch_body",
    "dedupe_parent_children",
    "qdrant_filter_json",
    "rrf_fuse",
]
