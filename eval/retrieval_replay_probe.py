"""Read-only replay of one QA retrieval, with hybrid/rerank instrumented.

Run INSIDE api container: docker compose -f infra/docker-compose.yml exec -T api python eval/retrieval_replay_probe.py "东方证券给中芯国际的目标价是多少？"

Captures the full 30+30 hybrid candidate list and every rerank batch score, to
answer: was the golden fact-bearing chunk ever a candidate, and if so why did
it not make top-8?
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from api.clients.pg import PgDatabase
from api.config import get_settings
from api.identity import IdentitySnapshot
from api.retrieval.hybrid import HybridRetriever, HybridResult
from api.retrieval.pipeline import build_pipeline
from api.retrieval.rerank import Reranker

QUESTION = sys.argv[1] if len(sys.argv) > 1 else "东方证券给中芯国际的目标价是多少？"
GOLDEN_CHUNK = os.environ.get("TARGET_CHUNK", "ch_f7237808607b")

captured: dict[str, Any] = {"hybrid": [], "rerank_batches": []}

_orig_retrieve = HybridRetriever.retrieve
_orig_score = Reranker.score


def traced_retrieve(self, query_text, flt, *, dense_query, top_k, parent_child_keep=2):
    result: HybridResult = _orig_retrieve(
        self, query_text, flt, dense_query=dense_query, top_k=top_k,
        parent_child_keep=parent_child_keep,
    )
    captured["hybrid"] = [
        {
            "chunk_id": c.chunk_id, "doc_key": c.doc_key, "chunk_type": c.chunk_type,
            "page_num": c.page_num,
            "dense_rank": c.dense_rank, "sparse_rank": c.sparse_rank,
            "dense_score": c.dense_score, "sparse_score": c.sparse_score,
            "fused_score": round(c.fused_score, 6),
        }
        for c in result.candidates
    ]
    return result


def traced_score(self, query, texts, *, timeout_s):
    scores = _orig_score(self, query, texts, timeout_s=timeout_s)
    captured["rerank_batches"].append(
        {"query": query, "n_texts": len(texts), "scores": [round(s, 6) for s in scores],
         "text_heads": [t[:120] for t in texts]}
    )
    return scores


HybridRetriever.retrieve = traced_retrieve
Reranker.score = traced_score

import api.retrieval.hybrid as H

_orig_rrf = H.rrf_fuse
_orig_dedupe = H.dedupe_parent_children


def traced_rrf(dense, sparse, *, top_k, rrf_k=H.RRF_K):
    captured["raw_routes"] = [
        [{"chunk_id": cid, "score": round(s, 6), "rank": i + 1} for i, (cid, s) in enumerate(route)]
        for route in (dense, sparse)
    ]
    return _orig_rrf(dense, sparse, top_k=top_k, rrf_k=rrf_k)


def traced_dedupe(candidates, *, keep):
    kept, dropped = _orig_dedupe(candidates, keep=keep)
    captured["dedup_dropped"] = [c.chunk_id for c in dropped]
    return kept, dropped


H.rrf_fuse = traced_rrf
H.dedupe_parent_children = traced_dedupe

settings = get_settings()
pg = PgDatabase(settings.database_url)
pg.open()
try:
    pipeline = build_pipeline(settings, pg)
    snap = pipeline.run(
        QUESTION,
        IdentitySnapshot(user_id="probe", depts=("fin",), roles=("staff",)),
    )
finally:
    pg.close()

hybrid = captured["hybrid"]
target = [c for c in hybrid if c["chunk_id"] == GOLDEN_CHUNK]
print(json.dumps({
    "question": QUESTION,
    "hybrid_candidate_n": len(hybrid),
    "dense_count": snap.dense_count,
    "sparse_count": snap.sparse_count,
    "rerank_scored": snap.rerank.scored,
    "hybrid_candidates": hybrid,
    "golden_chunk_in_hybrid": bool(target),
    "golden_chunk_detail": target,
    "golden_chunk_in_raw_routes": [
        [r for r in route if r["chunk_id"] == GOLDEN_CHUNK]
        for route in captured.get("raw_routes", [])
    ],
    "golden_chunk_dropped_by_dedup": GOLDEN_CHUNK in captured.get("dedup_dropped", []),
    "dedup_dropped_count": len(captured.get("dedup_dropped", [])),
    "final_top8": snap.score_snapshot()["evidence"],
    "top1": snap.score_snapshot()["top1"],
    "rerank_batches": [
        {"n_texts": b["n_texts"], "scores": b["scores"]}
        for b in captured["rerank_batches"]
    ],
}, ensure_ascii=False, indent=2))
