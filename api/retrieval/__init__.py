"""在线检索（详细设计.md §7 / §8）。

三段职责，分开是因为它们的失败模式完全不同：

| 模块 | 干什么 | 挂了会怎样 |
|---|---|---|
| `hybrid`   | 两路召回 + RRF | 一条候选都没有 → 落进 `reject_none` |
| `rerank`   | 逐对打分 + 降级 | 降级成按名次取，审计留 `degrade` |
| `gate`     | 三段门控 + 澄清 | 判定不成立 → 保守拒答 |
| `multihop` | 黑板 + 停止条件 | 断链 → 已查实部分 + 断链点 |

`pipeline.py` 把它们按 §7.4 要求的顺序串起来（**版本兜底必须在精排前**）。
"""

from __future__ import annotations

from api.retrieval.gate import (
    VERDICTS,
    ClarifyOption,
    GateConfig,
    GateDecision,
    SuspectedDoc,
    Verdict,
    decide,
    effective_score,
)
from api.retrieval.hybrid import (
    RRF_K,
    Candidate,
    HybridResult,
    HybridRetrievalError,
    HybridRetriever,
    dedupe_parent_children,
    qdrant_filter_json,
    rrf_fuse,
)
from api.retrieval.multihop import (
    Blackboard,
    Fact,
    HopOutcome,
    MultiHopResult,
    run_multihop,
)
from api.retrieval.pipeline import (
    EmbeddingUnavailable,
    QueryEmbedder,
    RetrievalPipeline,
    RetrievalSnapshot,
)
from api.retrieval.rerank import (
    RerankOutcome,
    RerankTimeout,
    RerankUnavailable,
    Reranker,
    rerank_candidates,
)
from api.retrieval.texts import ChunkText, doc_names, fetch_texts

__all__ = [
    "RRF_K",
    "VERDICTS",
    "Blackboard",
    "Candidate",
    "ChunkText",
    "ClarifyOption",
    "EmbeddingUnavailable",
    "Fact",
    "GateConfig",
    "GateDecision",
    "HopOutcome",
    "HybridResult",
    "HybridRetrievalError",
    "HybridRetriever",
    "MultiHopResult",
    "QueryEmbedder",
    "RerankOutcome",
    "RerankTimeout",
    "RerankUnavailable",
    "Reranker",
    "RetrievalPipeline",
    "RetrievalSnapshot",
    "SuspectedDoc",
    "Verdict",
    "decide",
    "dedupe_parent_children",
    "doc_names",
    "effective_score",
    "fetch_texts",
    "qdrant_filter_json",
    "rerank_candidates",
    "rrf_fuse",
    "run_multihop",
]
