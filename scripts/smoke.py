#!/usr/bin/env python3
"""End-to-end smoke test for the ai-rag local stack (stdlib only).

Checks:
1. Qdrant /healthz
2. Postgres reachable through the api container's /healthz
3. Embedding service returns 512-dim L2-normalized vectors
4. Rerank service scores a relevant pair higher than an irrelevant one
5. Qdrant accepts a point with dense + BM25(sparse) in the same collection
and a dense+bm25 prefetch query with RRF fusion returns it

Run with:  python3 scripts/smoke.py            (host ports from .env or
defaults) python3 scripts/smoke.py --in-docker (inside compose network)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request

DEFAULT_DENSE_DIM = 512


def _get(url: str, timeout: float = 10.0) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _post(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _put(url: str, payload: dict, timeout: float = 30.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--in-docker",
        action="store_true",
        help="use compose-internal service names",
    )
    args = ap.parse_args()

    if args.in_docker:
        qdrant = os.getenv("QDRANT_URL", "http://qdrant:6333")
        embedding = os.getenv("EMBEDDING_BASE_URL", "http://embedding:8000/v1")
        rerank = os.getenv("RERANK_BASE_URL", "http://rerank:8000")
        api = "http://api:8000"
        collection = os.getenv("QDRANT_COLLECTION", "kb_chunks")
        dim = int(os.getenv("EMBEDDING_DIM", DEFAULT_DENSE_DIM))
    else:
        qdrant = f"http://127.0.0.1:{os.getenv('QDRANT_HTTP_PORT', '6333')}"
        embedding = f"http://127.0.0.1:{os.getenv('EMBEDDING_PORT', '7997')}/v1"
        rerank = f"http://127.0.0.1:{os.getenv('RERANK_PORT', '7999')}"
        api = f"http://127.0.0.1:{os.getenv('API_PORT', '8000')}"
        collection = os.getenv("QDRANT_COLLECTION", "kb_chunks")
        dim = int(os.getenv("EMBEDDING_DIM", DEFAULT_DENSE_DIM))

    failures: list[str] = []

    # 1. Qdrant
    code, body = _get(f"{qdrant}/healthz")
    print(f"[1] qdrant /healthz -> {code} {body.strip()}")
    if code != 200:
        failures.append("qdrant healthz")

    # 2. api healthz (PG + Qdrant connectivity from inside the app)
    code, body = _get(f"{api}/healthz")
    print(f"[2] api /healthz -> {code} {body.strip()[:200]}")
    if code != 200:
        failures.append("api healthz")

    # 3. embedding
    code, body = _post(
        f"{embedding}/embeddings",
        {"input": ["2025年差旅住宿标准是多少", "差旅报销规定"], "model": "bge"},
    )
    if code != 200:
        print(f"[3] embedding -> {code} {body[:200]}")
        failures.append("embedding")
    else:
        data = json.loads(body)["data"]
        vec = data[0]["embedding"]
        norm = math.sqrt(sum(v * v for v in vec))
        print(f"[3] embedding -> 200 dim={len(vec)} l2norm={norm:.4f}")
        if len(vec) != dim:
            failures.append(f"embedding dim {len(vec)} != {dim}")
        if abs(norm - 1.0) > 0.02:
            failures.append(f"embedding not L2-normalized (norm={norm:.4f})")

    # 4. rerank
    code, body = _post(
        f"{rerank}/rerank",
        {
            "query": "2025年差旅住宿标准是多少",
            "texts": [
                "住宿费每晚上限500元，超标自理。",
                "公司年度营收同比增长12%。",
            ],
        },
    )
    if code != 200:
        print(f"[4] rerank -> {code} {body[:200]}")
        failures.append("rerank")
    else:
        res = json.loads(body)
        scores = [r["score"] for r in res]
        print(f"[4] rerank -> 200 scores={scores}")
        if not (scores and scores[0] > scores[1]):
            failures.append("rerank did not rank the relevant doc first")

    # 5. Qdrant upsert (dense + server-side BM25 sparse) + hybrid RRF query
    if len(failures) == 0:
        point_id = 9000
        dense_vec = json.loads(
            _post(
                f"{embedding}/embeddings",
                {"input": ["住宿费每晚上限500元，超标自理。"]},
            )[1]
        )["data"][0]["embedding"]
        code, body = _put(
            f"{qdrant}/collections/{collection}/points?wait=true",
            {
                "points": [
                    {
                        "id": point_id,
                        "vector": {
                            "dense": dense_vec,
                            "bm25": {
                                "text": "住宿费每晚上限500元，超标自理。",
                                "model": "qdrant/bm25",
                            },
                        },
                        "payload": {
                            "doc_key": "policy/travel-cn",
                            "is_public": True,
                            "effective_year": 2025,
                        },
                    }
                ]
            },
        )
        print(f"[5a] upsert dense+bm25 -> {code} {body.strip()[:160]}")
        if code != 200:
            failures.append("qdrant upsert")
        else:
            query_dense = json.loads(
                _post(f"{embedding}/embeddings", {"input": ["差旅住宿标准"]})[1]
            )["data"][0]["embedding"]
            code, body = _post(
                f"{qdrant}/collections/{collection}/points/query",
                {
                    "prefetch": [
                        {"query": query_dense, "using": "dense", "limit": 5},
                        {
                            "query": {
                                "text": "差旅住宿标准",
                                "model": "qdrant/bm25",
                            },
                            "using": "bm25",
                            "limit": 5,
                        },
                    ],
                    "query": {"fusion": "rrf"},
                    "limit": 3,
                    "with_payload": True,
                },
            )
            if code != 200:
                print(f"[5b] hybrid query -> {code} {body[:200]}")
                failures.append("qdrant hybrid query")
            else:
                pts = json.loads(body)["result"]["points"]
                ids = [p["id"] for p in pts]
                print(f"[5b] hybrid RRF query -> 200 ids={ids}")
                if point_id not in ids:
                    failures.append(
                        "hybrid query did not return the upserted point"
                    )

    print()
    if failures:
        print("SMOKE: FAILED -> " + ", ".join(failures))
        return 1
    print("SMOKE: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
