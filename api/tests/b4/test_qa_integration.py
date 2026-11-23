"""B4 集成验收：真 PG + Qdrant + TEI 的检索，加可注入的生成替身。

**这一组能证明什么、不能证明什么**（读之前先看这段）：

能证明：证据包是从**真库**组装的——citations 里的 `doc_key` / `page_num` / `bbox` /
`snippet` / `chunk_id` 全部来自 `chunks` 表的真实行；门控五态在这批真数据上给出的是它
该给的那一态；事件顺序符合附录 C.2；拒答路径没有 `token`；审计行能插进 `qa_log`
（真表的 CHECK 约束会拦下口径不一致的行）。

不能证明：**生成质量**。这台机器没有配置 LLM 网关（`LLM_BASE_URL` / `LLM_MODEL` 为空），
所以生成与判别都是脚本化替身。也就是说，"五道校验能拦住幻觉"这件事在这里验的是
"拦住的动作与出口正确"，不是"模型真的会说错话并且被拦住"——后者要在配了网关的
环境里跑 `eval/run_eval.py`（黄金 50 题）才算数。这一点不能含糊过去。

跳过策略：缺 PG / Qdrant / TEI / rerank 任一项就 skip，绝不假装跑过。
"""

from __future__ import annotations

import json

import pytest

from api.errors import ErrorCode
from api.qa.orchestrator import QaService
from api.tests.b4.conftest import (
    FIN,
    ScriptedLlm,
    build_qa_service,
    doc_key,
    make_chunk,
    publish,
    unique,
)

pytestmark = pytest.mark.integration


def _events(names):  # 小工具：只留事件名，断言读起来短
    return [event.name for event in names]


def _delta_text(events) -> str:
    return "".join(event.payload["delta"] for event in events if event.name == "token")


def _fallback(events):
    return next((event for event in events if event.name == "fallback"), None)


# --- 1. 正常路径：证据包字段来自真库 -----------------------------------------


def test_pass_path_citations_come_from_the_real_chunk_rows(
    conn, store, embedder, retriever, reranker, b3_pg, settings
) -> None:
    """验收原句："citations 含 doc/page/bbox/snippet/chunk_id"。

    造一条真切片（页码 47、真 bbox），让它过门控与校验，断言 citations 里的五个字段
    与 `chunks` 表里的行**逐字一致**——不是与测试里写的字面量一致。
    """
    name = unique("pass")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(
                chunk_id=f"ch_{name}_hot",
                text="住宿费标准：每人每天 500 元，按职级限额报销。",
                page_num=47,
            )
        ],
    )
    llm = ScriptedLlm(
        generation=(
            '{"status":"answered","answer":"住宿费标准为每人每天 500 元[1]。",'
            '"citations":[1],"claims":[{"text":"住宿费标准为每人每天 500 元","citations":[1]}]}'
        )
    )
    service = build_qa_service(
        b3_pg=b3_pg, settings=settings, embedder=embedder, retriever_client=retriever, reranker_client=reranker, llm=llm
    )
    sent = []
    try:
        outcome = service.run("住宿费标准是多少", FIN, emit=sent.append)
    finally:
        service.pipeline.close()

    assert outcome.verdict == "pass", outcome.detail
    names = _events(sent)
    assert names[0] == "step" and names[-1] == "done"
    assert "citations" in names

    items = next(event.payload["items"] for event in sent if event.name == "citations")
    assert items
    item = items[0]
    assert set(item) == {"n", "doc_key", "doc_name", "page_num", "bbox", "snippet", "chunk_id"}
    assert item["chunk_id"] == f"ch_{name}_hot"
    assert item["doc_key"] == doc_key(name)
    # 与真库对拍：页码与 bbox 必须就是那一行
    with conn.cursor() as cur:
        cur.execute(
            "SELECT page_num, bbox FROM chunks WHERE chunk_id = %s", (f"ch_{name}_hot",)
        )
        page_num, bbox = cur.fetchone()
    assert item["page_num"] == page_num
    assert item["bbox"] == bbox
    assert item["snippet"].startswith("住宿费标准")
    assert llm.temperatures and set(llm.temperatures) == {0.0}


def test_pass_path_audit_row_passes_the_real_table_constraints(
    conn, store, embedder, retriever, reranker, b3_pg, settings
) -> None:
    """审计行必须能插进真 `qa_log`：DB 的 CHECK 是最后一道口径闸。"""
    from api.audit import AuditWriter

    name = unique("audit")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。", page_num=47)
        ],
    )
    llm = ScriptedLlm(
        generation=(
            '{"status":"answered","answer":"住宿费标准为每人每天 500 元[1]。",'
            '"citations":[1],"claims":[{"text":"住宿费标准为每人每天 500 元","citations":[1]}]}'
        )
    )
    service = build_qa_service(
        b3_pg=b3_pg, settings=settings, embedder=embedder, retriever_client=retriever, reranker_client=reranker, llm=llm
    )
    try:
        outcome = service.run("住宿费标准是多少", FIN)
    finally:
        service.pipeline.close()

    assert outcome.audit is not None
    result = AuditWriter(b3_pg, retention_days=1).record(outcome.audit)
    assert result.ok is True, result.error
    # 读回来确认落的是这一条（不只看 ok：`record` 失败也会返回 ok=False，但
    # 一条写错库的审计能返回 ok=True 吗？不能——这里再查一遍是为了验"内容对"）
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gate1_verdict, gate2_verdict, answer, reject_reason, model "
            "FROM qa_log WHERE id = %s",
            (result.audit_id,),
        )
        gate1, gate2, answer, reject_reason, model = cur.fetchone()
    assert gate1 == "pass"
    assert gate2 == "supported"
    assert answer and "500" in answer
    assert reject_reason is None
    assert model == "scripted"


# --- 2. 拒答路径：不调模型、没有 token ---------------------------------------


def test_irrelevant_question_rejects_without_calling_the_model(
    conn, store, embedder, retriever, reranker, b3_pg, settings
) -> None:
    """验收原句："无证据不输出结论性幻觉"——最直接的做法是根本不调模型。"""
    name = unique("reject")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。", page_num=47)
        ],
    )
    llm = ScriptedLlm(
        generation='{"status":"answered","answer":"不应被调用[1]","citations":[1],"claims":[]}'
    )
    service = build_qa_service(
        b3_pg=b3_pg, settings=settings, embedder=embedder, retriever_client=retriever, reranker_client=reranker, llm=llm
    )
    sent = []
    try:
        outcome = service.run("如何申报个人所得税专项附加扣除", FIN, emit=sent.append)
    finally:
        service.pipeline.close()

    assert outcome.verdict in ("reject_none", "reject_low"), outcome.detail
    assert llm.prompts == [], "拒答路径不该把证据发给模型"
    names = _events(sent)
    assert "token" not in names, "拒答路径不允许有正文"
    assert "citations" not in names
    fallback = _fallback(sent)
    assert fallback is not None
    assert fallback.payload["code"] in ("no_evidence", "low_confidence")
    assert outcome.audit is not None and outcome.audit.reject_reason is not None


def test_rejected_audit_row_also_satisfies_the_table_constraints(
    conn, store, embedder, retriever, reranker, b3_pg, settings
) -> None:
    """拒答的审计行必须带 `reject_reason`（§11.3 的配对口径，DB 只拦单列）。"""
    from api.audit import AuditWriter

    name = unique("audit_reject")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。")],
    )
    service = build_qa_service(
        b3_pg=b3_pg, settings=settings, embedder=embedder, retriever_client=retriever, reranker_client=reranker, llm=None
    )
    try:
        outcome = service.run("如何申报个人所得税专项附加扣除", FIN)
    finally:
        service.pipeline.close()
    assert outcome.audit is not None
    result = AuditWriter(b3_pg, retention_days=1).record(outcome.audit)
    assert result.ok is True, result.error
    with conn.cursor() as cur:
        cur.execute("SELECT reject_reason FROM qa_log WHERE id = %s", (result.audit_id,))
        (reason,) = cur.fetchone()
    assert reason in ("no_evidence", "low_confidence")


# --- 3. 校验不过：真证据 + 编造数字 → 只给原文 -------------------------------


def test_hallucinated_number_degrades_to_original_text(
    conn, store, embedder, retriever, reranker, b3_pg, settings
) -> None:
    """验收原句："不可验证不输出结论性幻觉"。

    真库里只有 500 元，模型说 400 元：第三道必须拦住，出口是"只给原文"，
    而且原文必须是**库里的那一行**（页码与片段都对得上）。
    """
    name = unique("hallucinate")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(
                chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。", page_num=47
            )
        ],
    )
    llm = ScriptedLlm(
        generation=(
            '{"status":"answered","answer":"住宿费标准一般是 400 元[1]。",'
            '"citations":[1],"claims":[{"text":"住宿费标准一般是 400 元","citations":[1]}]}'
        )
    )
    service = build_qa_service(
        b3_pg=b3_pg, settings=settings, embedder=embedder, retriever_client=retriever, reranker_client=reranker, llm=llm
    )
    sent = []
    try:
        outcome = service.run("住宿费标准是多少", FIN, emit=sent.append)
    finally:
        service.pipeline.close()

    names = _events(sent)
    assert "token" not in names, "校验不过时一个字都不能发"
    fallback = _fallback(sent)
    assert fallback is not None
    assert fallback.payload["code"] == "unverifiable"
    docs = fallback.payload["docs"]
    assert docs, "档一必须给原文"
    assert docs[0]["page_num"] == 47
    assert "500" in docs[0]["snippet"]
    assert outcome.verdict == "unverifiable"
    assert outcome.audit is not None
    assert outcome.audit.reject_reason is ErrorCode.UNVERIFIABLE
    # 审计里不能出现草稿正文（它没通过校验）
    assert outcome.audit.answer is None


# --- 4. 模型不可用：档三"稍后重试"，不给原文 --------------------------------


def test_no_llm_configured_is_the_system_tier(
    conn, store, embedder, retriever, reranker, b3_pg, settings
) -> None:
    """本机真实状态：没有配置 LLM 网关。有证据、过了门控，但生成不可用。"""
    name = unique("nollm")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。")],
    )
    service = build_qa_service(
        b3_pg=b3_pg, settings=settings, embedder=embedder, retriever_client=retriever, reranker_client=reranker, llm=None
    )
    sent = []
    try:
        outcome = service.run("住宿费标准是多少", FIN, emit=sent.append)
    finally:
        service.pipeline.close()

    fallback = _fallback(sent)
    if fallback is None:
        # 门控没放行（例如这批数据上 PR 分没过阈）→ 那是拒答路径，也算"没输出幻觉"。
        assert outcome.verdict in ("reject_none", "reject_low")
        return
    assert fallback.payload["code"] == "model_timeout"
    assert fallback.payload["docs"] == [], "档三不给原文：连相关性都没验完"
    assert outcome.error_code is ErrorCode.MODEL_TIMEOUT


# --- 5. 事件帧的端到端字节形状 -----------------------------------------------


def test_every_frame_is_valid_sse_and_done_is_last(
    conn, store, embedder, retriever, reranker, b3_pg, settings
) -> None:
    name = unique("frames")
    publish(
        conn,
        store,
        embedder,
        name=name,
        tag="v1",
        chunks=[
            make_chunk(chunk_id=f"ch_{name}_a", text="住宿费标准：每人每天 500 元。", page_num=47)
        ],
    )
    llm = ScriptedLlm(
        generation=(
            '{"status":"answered","answer":"住宿费标准为每人每天 500 元[1]。",'
            '"citations":[1],"claims":[{"text":"住宿费标准为每人每天 500 元","citations":[1]}]}'
        )
    )
    service = build_qa_service(
        b3_pg=b3_pg, settings=settings, embedder=embedder, retriever_client=retriever, reranker_client=reranker, llm=llm
    )
    sent = []
    try:
        service.run("住宿费标准是多少", FIN, emit=sent.append)
    finally:
        service.pipeline.close()

    for event in sent:
        frame = event.frame()
        assert frame.startswith(f"event: {event.name}\n"), frame
        assert frame.endswith("\n\n")
        data = json.loads(frame.split("data: ", 1)[1].strip())
        assert data["type"] == event.name
    assert sent[-1].name == "done"
    # 附录 C.2 的顺序：token 必须在 citations 之前，done 最后
    names = _events(sent)
    if "token" in names and "citations" in names:
        assert names.index("token") < names.index("citations")


# --- 6. 澄清路径（用真数据造两份同分文档不划算，这里只验"编排不篡改选项"）----


def test_clarify_event_shape_when_the_gate_asks_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """澄清由门控触发，编排层只负责把它翻译成事件。

    真数据上造"两份文档分数差 < 0.05"需要两份几乎同文的制度，成本远大于收益
    （B3 的 `test_retrieval_logic` 已穷举过那半段）。这里验的是另一半：
    **选项原样透传、带 value 供前端回填**。
    """
    from api.retrieval.gate import ClarifyOption, GateDecision
    from api.retrieval.pipeline import RetrievalSnapshot
    from api.retrieval.rerank import RerankOutcome
    from api.query import understand

    snapshot = RetrievalSnapshot(
        plan=understand("住宿标准是多少"),
        decision=GateDecision(
            verdict="clarify",
            clarify_options=(
                ClarifyOption(
                    label="差旅管理办法-2025版.pdf（第 47 页）",
                    doc_key="policy/travel",
                    params={"doc_keys": ["policy/travel"]},
                ),
                ClarifyOption(
                    label="费用报销管理办法-2025版.pdf（第 7 页）",
                    doc_key="policy/expense",
                    params={"doc_keys": ["policy/expense"]},
                ),
            ),
            top1=0.61,
            reason="两份文档分数接近",
        ),
        candidates=[],
        filter_json={},
        dense_count=2,
        sparse_count=2,
        dropped_stale=[],
        rerank=RerankOutcome(ordered=[]),
        elapsed_ms=5.0,
    )

    class _Pipeline:
        pg = b3_pg_stub = object()

        def close(self) -> None:
            return None

        def run(self, *args, **kwargs):
            return snapshot

    service = QaService(
        pipeline=_Pipeline(),  # type: ignore[arg-type]
        llm=None,
        judge_client=None,
        evidence_max_chars=6000,
    )
    sent = []
    service.run("住宿标准是多少", FIN, emit=sent.append)
    names = _events(sent)
    assert "clarify" in names and "token" not in names
    options = next(event.payload["options"] for event in sent if event.name == "clarify")
    assert [opt["value"] for opt in options] == ["policy/travel", "policy/expense"]
    assert sent[-1].payload["verdict"] == "clarify"
