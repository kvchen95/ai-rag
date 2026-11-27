#!/usr/bin/env python3
"""B4 端到端验收演练：一次问答从检索到 SSE 帧，六条判据逐条打印实测结果。.

**为什么单独一个脚本，而不是只靠 pytest**：pytest 的绿只说明断言成立，不说明走的
是
哪条分支。这套链路里有几处"两种结果都能算过"的地方（门控可能放行也可能拒答、
模型可能被调也可能被跳过），只给一个 passed 就是在含糊。这里把每一问的
**实际出口**打出来：verdict、事件序列、fallback 的 code、citations 的五个字段。

**它测什么**：真 PG + 真 Qdrant + 真 TEI（embedding / rerank）。
**它不测什么**：生成质量。本机没有 LLM 网关
（`LLM_BASE_URL` / `LLM_MODEL` 为空），
所以生成与判别是脚本替身。这一条写在输出里，不靠读者自己推断。

用法（容器内，接进 compose 网络）：

docker run --rm --network ai-rag_default       -e
AI_RAG_TEST_DATABASE_URL='postgresql+psycopg://ai_rag:***@postgres:
5432/ai_rag'       -e QDRANT_URL='http://qdrant:
6333'       -e EMBEDDING_BASE_URL='http://embedding:
8000/v1' -e EMBEDDING_API_KEY=local       -e RERANK_BASE_URL='http:
//rerank:8000' -e RERANK_API_KEY=local       ai-rag-api:
dev python scripts/verify_b4_qa.py

退出码：全部判据成立为 0；任一不成立为 1 并打印是哪一条。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 造数走 A3/B3 那套已验证的 `publish()`：它一步做到"PG 双 active + 向量库点
# active"，
# 自己再写一份会让"检索不到"分不清是数据没上线还是检索有问题。
NS = "b4verify"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    dsn = os.getenv("AI_RAG_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        print("需要 AI_RAG_TEST_DATABASE_URL 或 DATABASE_URL", file=sys.stderr)
        return 2
    for var in ("QDRANT_URL", "EMBEDDING_BASE_URL", "RERANK_BASE_URL"):
        if not os.getenv(var):
            print(f"需要 {var}（本脚本要真检索链路）", file=sys.stderr)
            return 2

    from api.clients.pg import PgDatabase
    from api.config import Settings
    from api.generate.llm import LlmClient
    from api.identity import IdentitySnapshot
    from api.ingest.flip import flip
    from api.ingest.vectors import Embedder, QdrantVectorStore, index_version
    from api.qa.orchestrator import QaService
    from api.retrieval.pipeline import build_pipeline

    settings = Settings()
    pg = PgDatabase(dsn, min_size=1, max_size=4, connect_timeout_s=5)
    pg.open()

    store = QdrantVectorStore(
        base_url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout_s=settings.qdrant_timeout_s,
    )
    embedder = Embedder(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout_s=settings.embedding_timeout_s,
    )

    identity = IdentitySnapshot(
        user_id="u_verify", depts=("fin",), roles=("staff",)
    )
    tag = uuid.uuid4().hex[:6]

    class Scripted(LlmClient):
        """生成与判别的脚本替身（本机没有网关，见模块头）。."""

        def __init__(self, generation: str, verdict: str = "SUPPORTED") -> None:
            self.generation = generation
            self.verdict = verdict
            self.temps: list[float] = []
            self.called = 0

        def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
            self.temps.append(temperature)
            self.called += 1
            body = "\n".join(m.get("content", "") for m in messages)
            if "SUPPORTED" in body:
                return json.dumps(
                    {"verdict": self.verdict, "reason": "scripted"}
                )
            return self.generation

    def publish(name: str, chunks: list[dict]) -> str:
        """造一个已上线版本（PG 双 active + 向量库点 active）。.

        与 `api/tests/b3/conftest.py::publish` 同一套动作：写行 → 索引 → 翻牌 →
        对齐向量库状态。自己另写一份会让"检索不到"分不清是数据没上线还是检索坏了。
        """
        version_id = f"{NS}/{name}#{tag}"
        with pg.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO documents (doc_key, filename, status) VALUES"
                    " (%s, %s, 'active') "
                    "ON CONFLICT (doc_key) DO UPDATE SET status = 'active'",
                    (f"{NS}/{name}", f"{name}.pdf"),
                )
                cur.execute(
                    "INSERT INTO doc_versions (version_id, doc_key, file_hash,"
                    " status) "
                    "VALUES (%s, %s, %s, 'pending')",
                    (
                        version_id,
                        f"{NS}/{name}",
                        uuid.uuid5(uuid.NAMESPACE_DNS, tag + name).hex
                        + uuid.uuid5(uuid.NAMESPACE_DNS, name + tag).hex,
                    ),
                )
                for chunk in chunks:
                    cur.execute(
                        (
                            "\n"
                            "                        INSERT INTO chunks "
                            "(chunk_id, doc_key, version_id, chunk_type, "
                            "page_num, bbox,\n"
                            "                                            "
                            "content_text, effective_date, effective_year,\n"
                            "                                            "
                            "date_confidence, is_public, status)\n"
                            "                        VALUES (%(chunk_id)s, "
                            "%(doc_key)s, %(version_id)s, 'text', "
                            "%(page_num)s,\n"
                            "                                "
                            "'[[0.12,0.34,0.88,0.51]]'::jsonb, %(text)s, "
                            "20260301, 2026,\n"
                            "                                'explicit', true, "
                            "'pending')\n"
                            "                        "
                        ),
                        {
                            "chunk_id": chunk["chunk_id"],
                            "doc_key": f"{NS}/{name}",
                            "version_id": version_id,
                            "page_num": chunk.get("page_num", 1),
                            "text": chunk["text"],
                        },
                    )
            report = index_version(conn, store, embedder, version_id)
            assert report.ok, report.as_dict()
            flip(conn, version_id)
        store.set_status_by_version(version_id, "active")
        return version_id

    def service(llm: LlmClient | None) -> QaService:
        pipeline = build_pipeline(settings, pg)
        return QaService(
            pipeline=pipeline,
            llm=llm,
            judge_client=llm,
            evidence_max_chars=settings.evidence_max_chars,
            token_chunk_chars=settings.sse_token_chunk_chars,
            model_name="scripted",
        )

    def run(
        question: str, llm: LlmClient | None
    ):  # -> (QaOutcome, list[SseEvent])
        sent: list = []
        svc = service(llm)
        try:
            outcome = svc.run(question, identity, emit=sent.append)
        finally:
            svc.pipeline.close()
        print(f"\n问：{question}")
        print(
            f"  verdict={outcome.verdict} code={outcome.error_code} "
            f"events={[e.name for e in sent]}"
        )
        for event in sent:
            if event.name == "fallback":
                print(
                    f"  fallback.code={event.payload['code']} "
                    f"docs={[d.get('doc_key') for d in event.payload['docs']]}"
                )
            if event.name == "citations":
                items = json.dumps(
                    event.payload["items"], ensure_ascii=False
                )
                print(f"  citations={items[:400]}")
            if event.name == "done":
                print(f"  done={json.dumps(event.payload, ensure_ascii=False)}")
        return outcome, sent

    print("=" * 78)
    print("B4 端到端验收（真 PG + Qdrant + TEI；生成/判别为脚本替身）")
    print("=" * 78)

    # --- 造数 ---------------------------------------------------------------
    good = f"vr_good_{tag}"
    publish(
        good,
        [
            {
                "chunk_id": f"ch_{good}_a",
                "text": "住宿费标准：每人每天 500 元，按职级限额报销。",
                "page_num": 47,
            },
        ],
    )

    # --- 判据 1：正常路径，citations 五个字段来自真库 -----------------------
    accept = Scripted(
        json.dumps(
            {
                "status": "answered",
                "answer": "住宿费标准为每人每天 500 元[1]。",
                "citations": [1],
                "claims": [
                    {"text": "住宿费标准为每人每天 500 元", "citations": [1]}
                ],
            },
            ensure_ascii=False,
        )
    )
    print("\n--- 判据 1：正常答案与引用 ---")
    outcome, sent = run("住宿费标准是多少", accept)
    names = [e.name for e in sent]
    check(
        "有 token 与 citations，事件以 done 收尾",
        "token" in names and names[-1] == "done",
        f"events={names}",
    )
    items = next(
        (e.payload["items"] for e in sent if e.name == "citations"), []
    )
    ok_fields = bool(items) and set(items[0]) == {
        "n",
        "doc_key",
        "doc_name",
        "page_num",
        "bbox",
        "snippet",
        "chunk_id",
    }
    check(
        "citations 含 doc/page/bbox/snippet/chunk_id",
        ok_fields,
        json.dumps(items[:1], ensure_ascii=False)[:200],
    )
    with pg.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT page_num, bbox FROM chunks WHERE chunk_id = %s",
            (f"ch_{good}_a",),
        )
        row = cur.fetchone()
    check(
        "citations 的页码与 bbox 与库里的行一致",
        bool(items)
        and row is not None
        and items[0]["page_num"] == row[0]
        and items[0]["bbox"] == row[1],
        f"db={row}",
    )
    check(
        "生成与判别都在 Temperature=0 调用",
        bool(accept.temps) and set(accept.temps) == {0.0},
        f"temps={accept.temps}",
    )

    # --- 判据 2：编造数字 → 只给原文，不发 token ---------------------------
    hallu = Scripted(
        json.dumps(
            {
                "status": "answered",
                "answer": "住宿费标准一般是 400 元[1]。",
                "citations": [1],
                "claims": [
                    {"text": "住宿费标准一般是 400 元", "citations": [1]}
                ],
            },
            ensure_ascii=False,
        )
    )
    print("\n--- 判据 2：不可验证不输出结论 ---")
    outcome2, sent2 = run("住宿费标准是多少", hallu)
    names2 = [e.name for e in sent2]
    fb2 = next((e for e in sent2 if e.name == "fallback"), None)
    check("校验不过时不发 token", "token" not in names2, f"events={names2}")
    check(
        "降级码是 unverifiable",
        bool(fb2) and fb2.payload["code"] == "unverifiable",
        fb2.payload["code"] if fb2 else "无 fallback",
    )
    check(
        "档一给了原文片段",
        bool(fb2) and bool(fb2.payload["docs"]),
        json.dumps(fb2.payload["docs"][:1], ensure_ascii=False)[:200]
        if fb2
        else "",
    )

    # --- 判据 3：无关问题 → 不调模型 --------------------------------------
    garbage = Scripted("不应被调用")
    print("\n--- 判据 3：无证据不硬答 ---")
    outcome3, sent3 = run("如何申报个人所得税专项附加扣除", garbage)
    names3 = [e.name for e in sent3]
    check("拒答路径没有 token", "token" not in names3, f"events={names3}")
    check("拒答路径没有调模型", garbage.called == 0, f"calls={garbage.called}")
    check(
        "verdict 是拒答两态之一",
        outcome3.verdict in ("reject_none", "reject_low"),
        outcome3.verdict,
    )

    # --- 判据 4：模型不可用 → 系统档，不给原文 ----------------------------
    print("\n--- 判据 4：系统异常不暴露内部细节 ---")
    outcome4, sent4 = run("住宿费标准是多少", None)
    fb4 = next((e for e in sent4 if e.name == "fallback"), None)
    if fb4 is None:
        check(
            "有证据但模型不可用 → 系统档",
            False,
            f"events={[e.name for e in sent4]}",
        )
    else:
        check(
            "降级码是 model_timeout",
            fb4.payload["code"] == "model_timeout",
            fb4.payload["code"],
        )
        check(
            "系统档不给原文",
            fb4.payload["docs"] == [],
            json.dumps(fb4.payload["docs"], ensure_ascii=False),
        )
        check(
            "系统档文案是稍后重试",
            fb4.payload["message"] == "稍后重试",
            fb4.payload["message"],
        )

    # --- 判据 5：审计行能插进真表 ----------------------------------------
    print("\n--- 判据 5：审计落库 ---")
    from api.audit import AuditWriter

    outcome5, _ = run(
        "住宿费标准是多少",
        Scripted(
            json.dumps(
                {
                    "status": "answered",
                    "answer": "住宿费标准为每人每天 500 元[1]。",
                    "citations": [1],
                    "claims": [
                        {
                            "text": "住宿费标准为每人每天 500 元",
                            "citations": [1],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ),
    )
    assert outcome5.audit is not None
    result = AuditWriter(pg, retention_days=1).record(outcome5.audit)
    check(
        "qa_log 插入成功（真表 CHECK 通过）",
        result.ok,
        result.error or f"id={result.audit_id}",
    )
    if result.audit_id:
        with pg.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT gate1_verdict, gate2_verdict, model FROM qa_log WHERE"
                " id = %s",
                (result.audit_id,),
            )
            row5 = cur.fetchone()
        print(f"  qa_log 行：gate1={row5[0]} gate2={row5[1]} model={row5[2]}")

    # --- 判据 6：SSE 帧的字节形状 ----------------------------------------
    print("\n--- 判据 6：SSE 帧形状 ---")
    frame_ok = True
    for event in sent:
        frame = event.frame()
        if not (
            frame.startswith(f"event: {event.name}\n")
            and frame.endswith("\n\n")
        ):
            frame_ok = False
            break
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        if payload.get("type") != event.name:
            frame_ok = False
            break
    check(
        "每帧都是 event:/data: 且 data.type 与事件名一致",
        frame_ok,
        f"checked={len(sent)}",
    )

    # --- 清理 --------------------------------------------------------------
    with pg.connection() as conn, conn.cursor() as cur:
        for table in (
            "vector_drift_log",
            "vector_sync_tasks",
            "chunks",
            "doc_versions",
            "ingest_jobs",
            "documents",
        ):
            cur.execute(
                f"DELETE FROM {table} WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
    store.close()
    embedder.close()
    pg.close()

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n" + "=" * 78)
    if failed:
        print(f"{len(failed)} 条判据不成立：")
        for name in failed:
            print(f"  - {name}")
        return 1
    print(f"ALL {len(CHECKS)} CHECKS PASSED")
    print(
        "注意：生成与判别是脚本替身。生成质量（模型真的会说错并被拦住）"
        "需要配了 LLM 网关后跑 eval/run_eval.py。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
