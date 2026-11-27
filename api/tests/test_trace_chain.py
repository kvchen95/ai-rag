"""链路追踪的端到端切片：从问题理解到检索、精排的事件都落到本地文件。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.trace import reset_trace


@pytest.fixture
def trace_path(monkeypatch, tmp_path) -> Path:
    path = tmp_path / "chain.jsonl"
    monkeypatch.setenv("AI_RAG_TRACE_FILE", str(path))
    monkeypatch.delenv("AI_RAG_TRACE_ENABLED", raising=False)
    reset_trace()
    return path


def events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_understand_writes_query_stage_with_rewrites(trace_path):
    from api.query import understand

    plan = understand("2025年差旅住宿标准是多少")

    rows = [
        row for row in events(trace_path) if row["event"] == "query.understood"
    ]
    assert len(rows) == 1
    assert rows[0]["retrieval_text"] == plan.retrieval_text
    assert rows[0]["rewrite_trace"] == ["住宿标准→住宿费限额"]
    assert rows[0]["time"]["range"]["from"] == 20250101


def test_hybrid_retrieve_traces_both_routes_fusion_and_dedup(trace_path):
    from api.filters import RetrievalFilter
    from api.identity import IdentitySnapshot
    from api.retrieval.hybrid import HybridRetriever

    def point(chunk_id: str, score: float) -> dict:
        return {
            "score": score,
            "payload": {
                "chunk_id": chunk_id,
                "doc_key": "policy/travel-cn",
                "version_id": "v1",
                "chunk_type": "text",
                "page_num": 3,
            },
        }

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "result": [
                    {"points": [point("ch_dense", 0.91)]},
                    {
                        "points": [
                            point("ch_sparse", 7.5),
                            point("ch_dense", 3.0),
                        ]
                    },
                ]
            }

    class FakeClient:
        def post(self, _path, **_kwargs):
            return FakeResponse()

        def close(self):
            return None

    flt = RetrievalFilter(
        identity=IdentitySnapshot(user_id="u", depts=("fin",), roles=("staff",))
    )
    with HybridRetriever("http://qdrant:6333", "kb_chunks") as retriever:
        retriever._client = FakeClient()  # type: ignore[assignment]
        result = retriever.retrieve(
            "住宿费限额", flt, dense_query=[0.1] * 512, top_k=30
        )

    got = {row["event"]: row for row in events(trace_path)}
    assert [item["chunk_id"] for item in got["hybrid.raw_routes"]["dense"]] == [
        "ch_dense"
    ]
    assert [
        item["chunk_id"] for item in got["hybrid.raw_routes"]["sparse"]
    ] == [
        "ch_sparse",
        "ch_dense",
    ]
    assert {item["chunk_id"] for item in got["hybrid.fused"]["candidates"]} == {
        "ch_dense",
        "ch_sparse",
    }
    assert got["hybrid.dedup"]["kept_ids"] == [
        item.chunk_id for item in result.candidates
    ]


def test_rerank_traces_batch_scores_and_selected(trace_path):
    from api.retrieval import rerank
    from api.retrieval.hybrid import Candidate

    candidates = [
        Candidate(
            chunk_id=f"ch_{index}",
            doc_key="policy/travel-cn",
            version_id="v1",
            chunk_type="text",
            page_num=index + 1,
            fused_rank=index + 1,
            fused_score=1.0 / (index + 1),
        )
        for index in range(3)
    ]
    texts = {item.chunk_id: f"证据文本 {item.chunk_id}" for item in candidates}

    class FakeRerankResponse:
        status_code = 200

        def json(self):
            return [
                {"index": index, "score": score}
                for index, score in enumerate([0.9, 0.5, 0.1])
            ]

    class FakeRerankClient:
        def post(self, _path, **_kwargs):
            return FakeRerankResponse()

        def close(self):
            return None

    with rerank.Reranker("http://rerank:8000", "m") as reranker:
        reranker._client = FakeRerankClient()  # type: ignore[assignment]
        outcome = rerank.rerank_candidates(
            reranker,
            "住宿费限额",
            candidates,
            texts,
            budget_ms=1000,
            top_n_min=2,
            top_n_max=2,
            candidates_max=10,
        )

    got = {row["event"]: row for row in events(trace_path)}
    assert got["rerank.batch"]["scores"] == [0.9, 0.5, 0.1]
    assert len(got["rerank.batch"]["text_heads"]) == 3
    assert [item["chunk_id"] for item in got["rerank.selected"]["ordered"]] == [
        item.chunk_id for item in outcome.ordered
    ]
    assert got["rerank.selected"]["top1"] == 0.9
    assert got["rerank.selected"]["degraded"] is False


def test_pipeline_run_traces_full_retrieval_path(trace_path, monkeypatch):
    from api.identity import IdentitySnapshot
    from api.observability import request_id_var
    from api.retrieval import pipeline as pipeline_mod
    from api.retrieval.gate import GateConfig
    from api.retrieval.hybrid import Candidate, HybridResult
    from api.retrieval.texts import ChunkText

    candidate = Candidate(
        chunk_id="ch_1",
        doc_key="policy/travel-cn",
        version_id="v1",
        chunk_type="text",
        page_num=3,
        dense_rank=1,
        sparse_rank=2,
        dense_score=0.91,
        sparse_score=6.5,
        fused_score=1.5,
        fused_rank=1,
    )

    class FakeRetriever:
        def retrieve(
            self, _query, _flt, *, dense_query, top_k, parent_child_keep
        ):
            return HybridResult(
                candidates=[candidate],
                dense_count=1,
                sparse_count=1,
                filter_json={"scope": "trace-test"},
            )

        def close(self):
            return None

    class FakeEmbedder:
        def embed(self, _text):
            return [0.1] * 512

    class FakeReranker:
        max_batch = 32

        def score(self, _query, _texts, *, timeout_s):
            return [0.88]

        def close(self):
            return None

    monkeypatch.setattr(pipeline_mod, "drop_stale", lambda pg, rows: (rows, []))
    monkeypatch.setattr(
        pipeline_mod,
        "fetch_texts",
        lambda pg, ids: {
            candidate.chunk_id: ChunkText(
                chunk_id=candidate.chunk_id,
                doc_key=candidate.doc_key,
                doc_name="差旅办法",
                page_num=3,
                bbox=[],
                text="住宿费标准每人每天 500 元",
                chunk_type="text",
            )
        },
    )
    monkeypatch.setattr(
        pipeline_mod,
        "all_doc_names",
        lambda pg: {"policy/travel-cn": "差旅办法"},
    )

    pipeline = pipeline_mod.RetrievalPipeline(
        pg=object(),  # type: ignore[arg-type]
        retriever=FakeRetriever(),  # type: ignore[arg-type]
        reranker=FakeReranker(),  # type: ignore[arg-type]
        embedder=FakeEmbedder(),  # type: ignore[arg-type]
        gate=GateConfig(
            reject_none=0.2,
            reject_low=0.4,
            borderline_low=0.38,
            borderline_high=0.42,
            clarify_score_delta=0.05,
            clarify_options_max=4,
            suspected_docs_max=3,
            evidence_min_paras=2,
        ),
        top_k=30,
        rerank_top_n_min=1,
        rerank_top_n_max=1,
        rerank_candidates_max=10,
        rerank_budget_ms=1000,
        parent_child_keep=2,
        max_hops=0,
        evidence_max_chars=6000,
    )
    token = request_id_var.set("rid-pipeline")
    try:
        snapshot = pipeline.run(
            "住宿费限额是多少",
            IdentitySnapshot(user_id="u", depts=("fin",), roles=("staff",)),
        )
    finally:
        request_id_var.reset(token)

    rows = events(trace_path)
    names = [row["event"] for row in rows]
    assert snapshot.decision.top1 == 0.88
    assert "filter.built" in names
    assert "retrieval.embed" in names
    assert "retrieval.version_guard" in names
    assert "retrieval.texts" in names
    assert "gate.decided" in names
    assert "retrieval.finished" in names
    assert names.index("retrieval.embed") < names.index(
        "retrieval.version_guard"
    )
    assert names.index("retrieval.version_guard") < names.index(
        "retrieval.texts"
    )
    assert names.index("retrieval.texts") < names.index("rerank.selected")
    assert names.index("rerank.selected") < names.index("gate.decided")
    assert {row["request_id"] for row in rows} == {"rid-pipeline"}


def test_qa_service_traces_generation_verification_and_exit(
    trace_path, monkeypatch
):
    from api.generate.llm import Draft, Fact
    from api.identity import IdentitySnapshot
    from api.qa import orchestrator
    from api.query import understand
    from api.retrieval.gate import GateDecision
    from api.retrieval.hybrid import Candidate
    from api.retrieval.pipeline import RetrievalSnapshot
    from api.retrieval.rerank import RerankOutcome
    from api.retrieval.texts import ChunkText
    from api.verify.checks import VerificationReport

    candidate = Candidate(
        chunk_id="ch_1",
        doc_key="policy/travel-cn",
        version_id="v1",
        chunk_type="text",
        page_num=3,
        fused_rank=1,
        rerank_score=0.9,
    )
    snapshot = RetrievalSnapshot(
        plan=understand("住宿费限额是多少"),
        decision=GateDecision(
            verdict="pass", evidence=(candidate,), top1=0.9, reason="过阈"
        ),
        candidates=[candidate],
        filter_json={},
        dense_count=1,
        sparse_count=1,
        dropped_stale=[],
        rerank=RerankOutcome(ordered=[candidate]),
        elapsed_ms=1.0,
    )

    class FakePipeline:
        pg = object()
        rerank_top_n_max = 1

        def run(self, *_args, **_kwargs):
            return snapshot

        def close(self):
            return None

    class FakePack:
        n_items = 1
        items: tuple = ()

        def render(self):
            return "[1] 住宿费标准每人每天 500 元"

        def notice(self):
            return ""

        def as_audit(self):
            return {"items": [], "truncated": False}

    monkeypatch.setattr(
        orchestrator,
        "fetch_texts",
        lambda pg, ids: {
            "ch_1": ChunkText(
                chunk_id="ch_1",
                doc_key="policy/travel-cn",
                doc_name="差旅办法",
                page_num=3,
                bbox=[],
                text="住宿费标准每人每天 500 元",
                chunk_type="text",
            )
        },
    )
    monkeypatch.setattr(
        orchestrator, "build_evidence_pack", lambda *a, **k: FakePack()
    )
    monkeypatch.setattr(
        orchestrator,
        "generate_draft",
        lambda *a, **k: Draft(
            status="answered",
            facts=(Fact(text="住宿费每人每天 500 元", evidence_ids=("1",)),),
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "verify_draft",
        lambda *a, **k: VerificationReport(
            passed=True,
            checks_run=("citation_range",),
            answer="住宿费每人每天 500 元",
            citations=(1,),
        ),
    )

    service = orchestrator.QaService(
        pipeline=FakePipeline(),  # type: ignore[arg-type]
        llm=object(),  # type: ignore[arg-type]
        judge_client=None,
        evidence_max_chars=6000,
        model_name="test-model",
    )
    outcome = service.run(
        "住宿费限额是多少",
        IdentitySnapshot(user_id="u", depts=("fin",), roles=("staff",)),
    )

    got = {row["event"]: row for row in events(trace_path)}
    assert outcome.verdict == "pass"
    assert got["qa.retrieval_done"]["verdict"] == "pass"
    assert got["qa.evidence_pack"]["n_items"] == 1
    # fact 审计口径固定六字段（§9.4）：kind/name/expression/result 对普通事实为
    # 空串。
    assert got["qa.draft"]["facts"] == [
        {
            "text": "住宿费每人每天 500 元",
            "evidence_ids": ["1"],
            "kind": "fact",
            "name": "",
            "expression": "",
            "result": "",
        }
    ]
    assert got["qa.verify"]["passed"] is True
    assert got["qa.exit"]["verdict"] == "pass"
    assert got["qa.exit"]["answer"] == "住宿费每人每天 500 元"


def test_qa_stream_traces_route_entry(trace_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from api.identity import IdentitySnapshot
    from api.observability import request_id_var
    from api.qa.events import done
    from api.qa.orchestrator import QaOutcome
    from api.routers import qa as qa_router

    settings = SimpleNamespace(max_question_chars=500, audit_retention_days=365)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, pg=object())
        )
    )
    identity = IdentitySnapshot(
        user_id="u_route", depts=("fin",), roles=("staff",)
    )

    class FakeService:
        pipeline = SimpleNamespace(close=lambda: None)

        def run(self, question, _identity, *, emit=None, **_kwargs):
            emit(done(latency_ms=1.0, hop_count=0, verdict="pass"))
            return QaOutcome(
                question=question, verdict="pass", identity=_identity
            )

        def close(self):
            return None

    monkeypatch.setattr(
        qa_router, "build_service", lambda *a, **k: FakeService()
    )
    monkeypatch.setattr(qa_router, "write_audit", lambda *a, **k: None)

    token = request_id_var.set("rid-route")
    try:

        async def call_and_drain():
            response = await qa_router.qa_stream(
                qa_router.QaStreamRequest(
                    question="住宿费限额是多少",
                    scope=qa_router.ScopeIn(doc_keys=["policy/travel-cn"]),
                ),
                request,  # type: ignore[arg-type]
                identity,
            )
            return b"".join([chunk async for chunk in response.body_iterator])

        body = asyncio.run(call_and_drain())
    finally:
        request_id_var.reset(token)

    assert b"event: done" in body
    rows = [
        row for row in events(trace_path) if row["event"] == "qa.route_enter"
    ]
    assert len(rows) == 1
    assert rows[0]["request_id"] == "rid-route"
    assert rows[0]["question"] == "住宿费限额是多少"
    assert rows[0]["doc_keys"] == ["policy/travel-cn"]
    assert rows[0]["user_id"] == "u_route"


def test_qa_stream_traces_worker_failure(trace_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from api.identity import IdentitySnapshot
    from api.routers import qa as qa_router

    settings = SimpleNamespace(max_question_chars=500, audit_retention_days=365)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, pg=object())
        )
    )
    identity = IdentitySnapshot(user_id="u", depts=("fin",), roles=("staff",))

    def boom(*_args, **_kwargs):
        raise RuntimeError("装配失败")

    monkeypatch.setattr(qa_router, "build_service", boom)

    async def call_and_drain():
        response = await qa_router.qa_stream(
            qa_router.QaStreamRequest(question="坏请求"),
            request,  # type: ignore[arg-type]
            identity,
        )
        return b"".join([chunk async for chunk in response.body_iterator])

    asyncio.run(call_and_drain())
    rows = [
        row for row in events(trace_path) if row["event"] == "qa.worker_failed"
    ]
    assert len(rows) == 1
    assert "装配失败" in rows[0]["error"]
