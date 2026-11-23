"""SSE 事件顺序与出口（详细设计.md §10.4 / 附录 C.2）与三档降级。

这一组用**整条编排层**（`QaService`）跑，检索与生成都换成替身：要验的不是
"检索对不对"（那是 B3 的用例），而是"拿到判定之后，发出去的是哪几个事件、顺序对不对、
拒答路径有没有 token"。这些判据只有把整条链路串起来才成立。

六条验收（对应卡片）：
1. 无证据 / 不可验证 / 异常 都不输出结论性幻觉；
2. 事件顺序符合附录 C.2（step* → token* | clarify | fallback → citations? → done）；
3. citations 含 doc/page/bbox/snippet/chunk_id；
4. 拒答无 token；
5. `done` 永远最后一条，异常路径也有；
6. 三档降级的用户文案不同（§10.3）。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Sequence

import pytest

from api.errors import ErrorCode
from api.generate.llm import DraftFormatError, GenerationUnavailable, LlmClient
from api.identity import IdentitySnapshot
from api.qa import events as ev
from api.qa.events import PHASES, SseEvent, chunk_text
from api.qa.orchestrator import QaOutcome, QaService
from api.retrieval.gate import ClarifyOption, GateDecision, SuspectedDoc
from api.retrieval.hybrid import Candidate
from api.retrieval.pipeline import RetrievalSnapshot
from api.retrieval.rerank import RerankOutcome
from api.retrieval.texts import ChunkText
from api.verify.checks import CHECKS

FIN = IdentitySnapshot(user_id="u_fin", depts=("fin",), roles=("staff",))


# --- 替身 ---------------------------------------------------------------------


class _TextRepo:
    """`fetch_texts` / `doc_names` 的替身（编排层只用 PG 做这两件事）。"""

    def __init__(self, texts: dict[str, ChunkText], names: dict[str, str] | None = None) -> None:
        self.texts = texts
        self.names = names or {}

    def fetch(self, _pg: Any, chunk_ids: Sequence[str]) -> dict[str, ChunkText]:
        return {cid: self.texts[cid] for cid in chunk_ids if cid in self.texts}

    def names_for(self, _pg: Any, doc_keys: Sequence[str]) -> dict[str, str]:
        return {key: self.names.get(key, key) for key in doc_keys}


class _StubPipeline:
    """只实现编排层用到的那几个成员：`run`、`pg`、`rerank_top_n_max`、`close`。"""

    def __init__(self, snapshot: RetrievalSnapshot) -> None:
        self.snapshot = snapshot
        self.pg = object()
        self.rerank_top_n_max = 8
        self.closed = False

    def run(self, question: str, identity: Any, **_: Any) -> RetrievalSnapshot:
        _ = (question, identity)
        return self.snapshot

    def close(self) -> None:
        self.closed = True


class _StubLlm(LlmClient):
    def __init__(self, payload: str | Exception) -> None:  # noqa: D107
        self.payload = payload
        self.calls: list[float] = []

    def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
        _ = messages
        self.calls.append(temperature)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _cand(chunk_id: str, *, doc_key: str = "policy/travel", page_num: int = 47, score: float = 0.62) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=f"{doc_key}#v1",
        chunk_type="text",
        page_num=page_num,
        rerank_score=score,
        fused_rank=1,
    )


def _text(chunk_id: str, body: str, *, doc_key: str = "policy/travel", page_num: int = 47) -> ChunkText:
    return ChunkText(
        chunk_id=chunk_id,
        doc_key=doc_key,
        doc_name="差旅管理办法-2025版.pdf",
        page_num=page_num,
        bbox=[[0.12, 0.34, 0.88, 0.51]],
        text=body,
        chunk_type="text",
    )


TEXTS = {
    "ch_a": _text("ch_a", "住宿费标准：每人每天 500 元，按职级限额报销。"),
    "ch_b": _text("ch_b", "市内交通费单次不超过 100 元。", page_num=48),
}
NAMES = {"policy/travel": "差旅管理办法-2025版.pdf", "policy/hr": "员工手册.pdf"}


def _snapshot(
    verdict: str,
    *,
    evidence: tuple[Candidate, ...] = (),
    reason: str = "",
    reject_code: ErrorCode | None = None,
    clarify_options: tuple[ClarifyOption, ...] = (),
    suspected: tuple[SuspectedDoc, ...] = (),
    uncovered: tuple[str, ...] = (),
    hop_count: int = 0,
    time_note: str | None = None,
) -> RetrievalSnapshot:
    """造一个检索快照。

    `plan` 用**真的** `understand()` 产物而不是手写替身：`score_snapshot()` 会读
    `plan.mode` / `plan.trace`，手写一个只有 `time.note` 的对象会在编排层炸掉——
    而那正是"替身与真对象形状不一致"造成的假失败，与生产代码无关。
    时间口径用 `time_note` 覆盖（正常路径下由 `normalise_time` 给出）。
    """
    from api.query import understand

    plan = understand("住宿标准是多少")
    if time_note is not None:
        plan = replace(
            plan,
            time=replace(plan.time, note=time_note),
        )
    decision = GateDecision(
        verdict=verdict,  # type: ignore[arg-type]
        evidence=evidence,
        clarify_options=clarify_options,
        suspected_docs=suspected,
        top1=0.62,
        reason=reason,
        reject_code=reject_code,
        uncovered=uncovered,
    )
    return RetrievalSnapshot(
        plan=plan,
        decision=decision,
        candidates=list(evidence),
        filter_json={"must": []},
        dense_count=3,
        sparse_count=2,
        dropped_stale=[],
        rerank=RerankOutcome(ordered=list(evidence)),
        elapsed_ms=12.0,
        hop_count=hop_count,
    )


def _service(snapshot: RetrievalSnapshot, llm: Any = None, judge: Any = None) -> QaService:
    _patch_text_repo()
    return QaService(
        pipeline=_StubPipeline(snapshot),  # type: ignore[arg-type]
        llm=llm,
        judge_client=judge,
        evidence_max_chars=6000,
        token_chunk_chars=3,
        model_name="gw-qwen-max",
    )


_ORIGINAL_FETCH = None
_ORIGINAL_NAMES = None


def _patch_text_repo() -> None:
    """把 `fetch_texts` / `doc_names` 换成本文件的替身（编排层每次调用都重新 import）。"""
    global _ORIGINAL_FETCH, _ORIGINAL_NAMES
    import api.qa.orchestrator as mod
    import api.retrieval.texts as texts_mod

    if _ORIGINAL_FETCH is None:
        _ORIGINAL_FETCH = mod.fetch_texts
        _ORIGINAL_NAMES = mod.doc_names
    mod.fetch_texts = lambda _pg, ids: {cid: TEXTS[cid] for cid in ids if cid in TEXTS}
    mod.doc_names = lambda _pg, keys: {key: NAMES.get(key, key) for key in keys}
    _ = texts_mod


@pytest.fixture(autouse=True)
def _restore_text_repo():
    yield
    if _ORIGINAL_FETCH is not None:
        import api.qa.orchestrator as mod

        mod.fetch_texts = _ORIGINAL_FETCH
        mod.doc_names = _ORIGINAL_NAMES


def _run(service: QaService, question: str = "2025年差旅住宿标准是多少") -> tuple[QaOutcome, list[SseEvent]]:
    sent: list[SseEvent] = []
    outcome = service.run(question, FIN, emit=sent.append)
    return outcome, sent


def _names(events: Sequence[SseEvent]) -> list[str]:
    return [event.name for event in events]


PASS_DRAFT = (
    '{"status":"answered","answer":"住宿费标准为每人每天 500 元[1]。",'
    '"citations":[1],"claims":[{"text":"住宿费标准为每人每天 500 元","citations":[1]}]}'
)


class _Supported(LlmClient):
    def __init__(self) -> None:  # noqa: D107
        pass

    def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
        return '{"verdict":"SUPPORTED","reason":"stub"}'


# --- 事件帧格式（附录 C.2）----------------------------------------------------


def test_frame_has_both_event_name_and_type() -> None:
    """`event:` 行给 `addEventListener`，`data.type` 给落盘重放——两条都要有。"""
    frame = ev.token("你好").frame()
    assert frame.startswith("event: token\n")
    assert frame.endswith("\n\n")
    data = json.loads(frame.split("data: ", 1)[1].strip())
    assert data == {"type": "token", "delta": "你好"}


def test_step_payload_has_hop_phase_elapsed() -> None:
    """附录 C.2：`step` 的载荷是 hop / phase / elapsed_ms。"""
    frame = ev.step(ev.PHASE_RETRIEVAL, hop=2, elapsed_ms=123.4).frame()
    data = json.loads(frame.split("data: ", 1)[1].strip())
    assert data["hop"] == 2
    assert data["phase"] == "retrieval"
    assert data["elapsed_ms"] == 123.4


def test_phases_are_the_four_from_appendix_c() -> None:
    assert PHASES == ("retrieval", "rerank", "generate", "verify")


def test_citations_payload_is_the_items_array() -> None:
    frame = ev.citations([{"n": 1, "chunk_id": "ch_a"}]).frame()
    data = json.loads(frame.split("data: ", 1)[1].strip())
    assert data["items"] == [{"n": 1, "chunk_id": "ch_a"}]


def test_fallback_payload_has_code_message_docs() -> None:
    """附录 C.2：`fallback` 的载荷是 code / message / docs。"""
    data = json.loads(ev.fallback("no_evidence", "未找到可验证依据", []).frame().split("data: ", 1)[1])
    assert data["code"] == "no_evidence"
    assert data["message"] == "未找到可验证依据"
    assert data["docs"] == []


def test_done_carries_latency_hops_verdict() -> None:
    data = json.loads(
        ev.done(latency_ms=1840.4, hop_count=2, verdict="pass").frame().split("data: ", 1)[1]
    )
    assert data["latency_ms"] == 1840
    assert data["hop_count"] == 2
    assert data["verdict"] == "pass"


def test_clarify_options_expose_value_for_the_frontend() -> None:
    """前端 F1 的 `ClarifyOption` 只有 `{label, value}`：`value` 要是可回填的 doc_key。"""
    data = json.loads(
        ev.clarify(
            "请确认",
            [{"label": "差旅管理办法（第 47 页）", "doc_key": "policy/travel", "params": {"doc_keys": ["policy/travel"]}}],
        )
        .frame()
        .split("data: ", 1)[1]
    )
    option = data["options"][0]
    assert option["label"].startswith("差旅管理办法")
    assert option["value"] == "policy/travel"
    assert option["params"]["doc_keys"] == ["policy/travel"]


def test_chunk_text_splits_into_fixed_size_pieces() -> None:
    assert chunk_text("abcdefg", 3) == ("abc", "def", "g")
    assert chunk_text("", 3) == ()


def test_chunk_text_rejects_non_positive_size() -> None:
    with pytest.raises(ValueError):
        chunk_text("abc", 0)


# --- 正常回答：step* → token* → citations → done ------------------------------


def test_happy_path_event_order() -> None:
    """§10.4 时序图的正常路径：进度 → 逐字 → 引用 → 收尾。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),), reason="top1=0.62"),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    outcome, sent = _run(service)
    names = _names(sent)
    assert outcome.verdict == "pass"
    assert names[0] == "step"
    assert names[-1] == "done"
    assert "citations" in names
    # token 只出现在 citations 之前、generate/verify 之后
    assert names.index("token") > names.index("step")
    assert names.index("token") < names.index("citations")
    assert names.count("done") == 1


def test_answer_tokens_join_back_to_the_verified_text() -> None:
    """逐字增量拼起来必须等于校验通过的正文，一个字都不能多不能少。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(PASS_DRAFT), judge=_Supported()
    )
    outcome, sent = _run(service)
    joined = "".join(event.payload["delta"] for event in sent if event.name == "token")
    assert joined == "住宿费标准为每人每天 500 元[1]。"
    assert joined == outcome.answer


def test_citations_carry_the_full_field_set() -> None:
    """验收原句：citations 含 doc/page/bbox/snippet/chunk_id（附录 C.2 的 items[]）。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(PASS_DRAFT), judge=_Supported()
    )
    _, sent = _run(service)
    items = next(event.payload["items"] for event in sent if event.name == "citations")
    assert items
    item = items[0]
    assert item["n"] == 1
    assert item["doc_key"] == "policy/travel"
    assert item["doc_name"] == "差旅管理办法-2025版.pdf"
    assert item["page_num"] == 47
    assert item["bbox"] == [[0.12, 0.34, 0.88, 0.51]]
    assert item["snippet"]
    assert item["chunk_id"] == "ch_a"


def test_llm_is_called_at_temperature_zero() -> None:
    """§9.2：解码参数固定 Temperature = 0。"""
    llm = _StubLlm(PASS_DRAFT)
    service = _service(_snapshot("pass", evidence=(_cand("ch_a"),)), llm=llm, judge=_Supported())
    _run(service)
    assert llm.calls == [0.0]


# --- 拒答路径：没有 token -----------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "code"),
    [("reject_none", ErrorCode.NO_EVIDENCE), ("reject_low", ErrorCode.LOW_CONFIDENCE)],
)
def test_gate_rejection_has_no_token(verdict: str, code: ErrorCode) -> None:
    """§10.4："拒答路径只有进度与收尾两类，没有逐字"。"""
    service = _service(
        _snapshot(verdict, reason="top1 太低", reject_code=code),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    outcome, sent = _run(service)
    names = _names(sent)
    assert "token" not in names
    assert "citations" not in names
    assert "fallback" in names
    assert names[-1] == "done"
    assert outcome.verdict == verdict
    assert outcome.error_code is code


def test_gate_rejection_does_not_call_the_model() -> None:
    """§8.1："< 0.20 直接返回拒答，不调模型"。省一次生成也避免模型看到空证据胡说。"""
    llm = _StubLlm(PASS_DRAFT)
    service = _service(
        _snapshot("reject_none", reject_code=ErrorCode.NO_EVIDENCE), llm=llm, judge=_Supported()
    )
    _run(service)
    assert llm.calls == []


def test_reject_low_fallback_lists_only_permitted_suspects() -> None:
    """§8.1："拒答 + 有权限的疑似文档清单（≤3 份）"。

    疑似文档来自 `decision.suspected_docs`，而它们已经过前置过滤——这里验的是
    "编排层不在这一步再筛一遍、也不自己造文档"。
    """
    service = _service(
        _snapshot(
            "reject_low",
            reason="0.32 落在弱相关",
            reject_code=ErrorCode.LOW_CONFIDENCE,
            suspected=(SuspectedDoc("policy/hr", 0.32),),
        ),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    _, sent = _run(service)
    docs = next(event.payload["docs"] for event in sent if event.name == "fallback")
    assert [doc["doc_key"] for doc in docs] == ["policy/hr"]
    assert docs[0]["doc_name"] == "员工手册.pdf", "展示名要来自 documents.filename，不是内部编号"


# --- 澄清路径 ----------------------------------------------------------------


def test_clarify_path_has_no_token_and_no_fallback() -> None:
    service = _service(
        _snapshot(
            "clarify",
            reason="两份文档分数接近",
            clarify_options=(
                ClarifyOption(label="差旅管理办法（第 47 页）", doc_key="policy/travel", params={"doc_keys": ["policy/travel"]}),
                ClarifyOption(label="员工手册（第 3 页）", doc_key="policy/hr", params={"doc_keys": ["policy/hr"]}),
            ),
        ),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    outcome, sent = _run(service)
    names = _names(sent)
    assert "clarify" in names
    assert "token" not in names
    assert "fallback" not in names
    assert names[-1] == "done"
    assert outcome.verdict == "clarify"


def test_clarify_options_are_passed_through_from_the_gate() -> None:
    """选项只能来自证据（§8.2）：编排层不做筛选、不补文档。"""
    service = _service(
        _snapshot(
            "clarify",
            clarify_options=(
                ClarifyOption(label="A", doc_key="policy/travel", params={"doc_keys": ["policy/travel"]}),
            ),
        )
    )
    _, sent = _run(service)
    options = next(event.payload["options"] for event in sent if event.name == "clarify")
    assert [option["value"] for option in options] == ["policy/travel"]


# --- 校验不过：档一"只给原文" ------------------------------------------------


def test_verification_failure_degrades_to_original_text_without_token() -> None:
    """§10.3 档一：数字或判别不过 → 原文片段 + 页码，明确"未形成结论"。"""
    hallucinated = (
        '{"status":"answered","answer":"住宿费标准一般是 400 元[1]。",'
        '"citations":[1],"claims":[{"text":"住宿费标准一般是 400 元","citations":[1]}]}'
    )
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(hallucinated), judge=_Supported()
    )
    outcome, sent = _run(service)
    names = _names(sent)
    assert "token" not in names, "校验不过绝不能把草稿发出去，一个字都不行"
    assert "citations" not in names
    fallback = next(event for event in sent if event.name == "fallback")
    assert fallback.payload["code"] == "unverifiable"
    assert fallback.payload["docs"], "档一必须带原文片段"
    doc = fallback.payload["docs"][0]
    assert doc["doc_key"] == "policy/travel"
    assert doc["page_num"] == 47
    assert doc["snippet"]
    assert outcome.verdict == "unverifiable"
    assert outcome.answer == ""


def test_judge_not_supported_degrades_to_original_text() -> None:
    class _NotSupported(LlmClient):
        def __init__(self) -> None:  # noqa: D107
            pass

        def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
            return '{"verdict":"NOT_SUPPORTED","reason":"stub"}'

    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)),
        llm=_StubLlm(PASS_DRAFT),
        judge=_NotSupported(),
    )
    outcome, sent = _run(service)
    assert "token" not in _names(sent)
    assert any(event.name == "fallback" and event.payload["code"] == "unverifiable" for event in sent)
    assert outcome.audit is not None and outcome.audit.gate2_verdict == "not_supported"


def test_missing_judge_model_degrades_but_is_audited_as_skipped() -> None:
    """没配判别模型时也走档一，但审计里必须是 skipped（少了一道闸要看得见）。"""
    service = _service(_snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(PASS_DRAFT))
    outcome, sent = _run(service)
    assert "token" not in _names(sent)
    assert outcome.audit is not None
    assert outcome.audit.gate2_verdict == "skipped"
    assert outcome.audit.reject_reason is ErrorCode.UNVERIFIABLE


# --- 系统异常：档三"稍后重试"，不给原文 --------------------------------------


def test_model_unavailable_is_the_system_tier_and_gives_no_docs() -> None:
    """§10.3 档三："一句稍后重试，不暴露内部细节"——也不能把原文推出去当安慰。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)),
        llm=_StubLlm(GenerationUnavailable("gateway down")),
        judge=_Supported(),
    )
    outcome, sent = _run(service)
    names = _names(sent)
    assert "token" not in names
    fallback = next(event for event in sent if event.name == "fallback")
    assert fallback.payload["code"] == "model_timeout"
    assert fallback.payload["docs"] == []
    assert fallback.payload["message"] == "稍后重试"
    # 判定词用错误码本身（`model_timeout`）而不是笼统的 internal：
    # 前端与评测都按它区分"模型超时"与"代码炸了"。
    assert outcome.verdict == "model_timeout"
    assert outcome.audit is not None and outcome.audit.gate1_verdict == "pass"


def test_model_output_format_error_is_tier_one_not_system() -> None:
    """§9.2 的降级（非 JSON / 缺字段）落档一：证据在手，用户读原文比读"稍后重试"有用。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)),
        llm=_StubLlm("我认为是 500 元。"),
        judge=_Supported(),
    )
    outcome, sent = _run(service)
    fallback = next(event for event in sent if event.name == "fallback")
    assert fallback.payload["code"] == "unverifiable"
    assert fallback.payload["docs"], "档一要给原文"
    assert outcome.error_code is ErrorCode.UNVERIFIABLE


def test_no_llm_configured_is_the_system_tier() -> None:
    service = _service(_snapshot("pass", evidence=(_cand("ch_a"),)), llm=None)
    outcome, sent = _run(service)
    fallback = next(event for event in sent if event.name == "fallback")
    assert fallback.payload["code"] == "model_timeout"
    assert fallback.payload["docs"] == []
    assert outcome.error_code is ErrorCode.MODEL_TIMEOUT


def test_unhandled_exception_still_emits_done() -> None:
    """没有 done 的流会让前端永远转圈：异常路径也必须有收尾。"""

    class _Exploding(_StubLlm):
        def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
            raise RuntimeError("boom")

    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_Exploding(""), judge=_Supported()
    )
    outcome, sent = _run(service)
    assert _names(sent)[-1] == "done"
    assert outcome.error_code is ErrorCode.INTERNAL
    # 内部异常的细节不进 fallback 的用户文案
    fallback = next(event for event in sent if event.name == "fallback")
    assert fallback.payload["message"] == "稍后重试"
    assert "boom" not in fallback.payload["message"]


# --- partial：已查实部分 + 未覆盖清单 ----------------------------------------


def test_partial_emits_answer_tokens_plus_the_uncovered_notice() -> None:
    """§8.3 / §8.4：partial 要给"已查实部分 + 未覆盖清单"，不能只给一半答案。"""
    service = _service(
        _snapshot(
            "partial",
            evidence=(_cand("ch_a"),),
            reason="多跳未覆盖",
            uncovered=("交通费标准",),
            hop_count=3,
        ),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    outcome, sent = _run(service)
    joined = "".join(event.payload["delta"] for event in sent if event.name == "token")
    assert "500 元" in joined
    assert "交通费标准" in joined, "未覆盖清单必须出现在给用户的文本里"
    assert outcome.verdict == "partial"
    done = sent[-1]
    assert done.payload["uncovered"] == ["交通费标准"]
    assert done.payload["hop_count"] == 3


def test_time_assumption_note_reaches_the_user() -> None:
    """§5.3："补了年份并在回答里注明假设"——不注明的话用户会以为那是原文说的。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),), time_note="问句未写年份，已按 2026 年理解"),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    _, sent = _run(service)
    joined = "".join(event.payload["delta"] for event in sent if event.name == "token")
    assert "2026 年" in joined


def test_truncated_evidence_notice_reaches_the_user() -> None:
    """§9.3："证据超长被截 → 在答案里说明仅依据前 N 条"。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"), _cand("ch_b", score=0.55))),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    service.evidence_max_chars = 100  # 只容得下一条
    _, sent = _run(service)
    joined = "".join(event.payload["delta"] for event in sent if event.name == "token")
    assert "仅依据前" in joined


# --- 审计 ---------------------------------------------------------------------


def test_audit_row_maps_pass_to_gate1_pass() -> None:
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(PASS_DRAFT), judge=_Supported()
    )
    outcome, _ = _run(service)
    assert outcome.audit is not None
    assert outcome.audit.gate1_verdict == "pass"
    assert outcome.audit.gate2_verdict == "supported"
    assert outcome.audit.reject_reason is None
    assert outcome.audit.answer == "住宿费标准为每人每天 500 元[1]。"
    assert outcome.audit.citations


def test_audit_row_maps_partial_to_gate1_pass_with_uncovered_in_snapshot() -> None:
    """`partial` 过了门控（§8.4 说它要带证据给下一步），未覆盖清单进 score_snapshot。"""
    service = _service(
        _snapshot("partial", evidence=(_cand("ch_a"),), uncovered=("交通费标准",)),
        llm=_StubLlm(PASS_DRAFT),
        judge=_Supported(),
    )
    outcome, _ = _run(service)
    assert outcome.audit is not None
    assert outcome.audit.gate1_verdict == "pass"
    assert outcome.audit.score_snapshot["uncovered"] == ["交通费标准"]


def test_audit_row_maps_unverifiable_to_pass_plus_reject_reason() -> None:
    """校验不过是"门控放行、校验拦住"：写进 reject_low 会污染低置信的统计。"""
    hallucinated = (
        '{"status":"answered","answer":"一般是 400 元[1]。","citations":[1],'
        '"claims":[{"text":"一般是 400 元","citations":[1]}]}'
    )
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(hallucinated), judge=_Supported()
    )
    outcome, _ = _run(service)
    assert outcome.audit is not None
    assert outcome.audit.gate1_verdict == "pass"
    assert outcome.audit.reject_reason is ErrorCode.UNVERIFIABLE
    assert outcome.audit.answer is None, "校验不过的草稿不能进审计的 answer 字段"
    assert outcome.audit.citations is None


def test_audit_row_for_rejection_carries_the_reason_code() -> None:
    service = _service(_snapshot("reject_none", reject_code=ErrorCode.NO_EVIDENCE))
    outcome, _ = _run(service)
    assert outcome.audit is not None
    assert outcome.audit.gate1_verdict == "reject_none"
    assert outcome.audit.reject_reason is ErrorCode.NO_EVIDENCE
    assert outcome.audit.gate2_verdict is None


def test_audit_row_carries_identity_snapshot() -> None:
    """§11.2：身份快照是必填，否则审计回答不了"谁在什么时候看了什么"。"""
    service = _service(_snapshot("reject_none", reject_code=ErrorCode.NO_EVIDENCE))
    outcome, _ = _run(service)
    assert outcome.audit is not None
    assert outcome.audit.user_id == "u_fin"
    assert outcome.audit.user_depts == ["fin"]


def test_audit_row_has_no_full_text() -> None:
    """§11.2：审计只存编号与分数摘要，不存完整原文。"""
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(PASS_DRAFT), judge=_Supported()
    )
    outcome, _ = _run(service)
    assert outcome.audit is not None
    dumped = json.dumps(outcome.audit.score_snapshot, ensure_ascii=False)
    assert "按职级限额报销" not in dumped, "证据正文不能进审计"


def test_verification_report_records_all_five_checks_on_success() -> None:
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)), llm=_StubLlm(PASS_DRAFT), judge=_Supported()
    )
    outcome, _ = _run(service)
    assert outcome.verification is not None
    assert outcome.verification.checks_run == CHECKS


# --- 一个容易漏的组合：DraftFormatError 是 Exception 子类吗 -------------------


def test_format_error_is_not_treated_as_a_system_outage() -> None:
    """`DraftFormatError` 与 `GenerationUnavailable` 是两类：前者档一、后者档三。

    合成一个类会让"模型说了但不算数"变成"系统挂了"——用户收到"稍后重试"，
    而真相是这次查询本来就有证据可看。
    """
    service = _service(
        _snapshot("pass", evidence=(_cand("ch_a"),)),
        llm=_StubLlm(DraftFormatError("缺 citations")),
        judge=_Supported(),
    )
    outcome, sent = _run(service)
    fallback = next(event for event in sent if event.name == "fallback")
    assert fallback.payload["code"] == "unverifiable"
    assert fallback.payload["docs"]
    assert outcome.verdict == "unverifiable"
