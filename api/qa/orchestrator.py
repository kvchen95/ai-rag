"""问答编排：§5–§10 一条链路，出口是 SSE 事件（详细设计.md §9 / §10 / 附录 C）。

这个模块只做**编排**与**接口适配**，不实现任何一步的规则：检索归 `retrieval.pipeline`，
证据包归 `generate.evidence`，五道校验归 `verify.checks`。它的职责是决定"哪一步的结果
该走哪个出口"，以及把结果翻译成附录 C.2 的六类事件与一条 `qa_log`。

出口映射（`decision.verdict` → 事件序列）是这一层的核心，写死在这里的理由是：
它是**跨模块的契约**，散在两个地方写迟早出现"verdict=reject_low 但走了 fallback 的
system 档"这种组合。

    pass      → step… → token* → citations → done(pass)
    partial   → step… → token* + 未覆盖说明 → citations → done(partial)
    clarify   → step… → clarify → done(clarify)
    reject_*  → step… → fallback(no_evidence|low_confidence) → done(reject_*)
    校验不过   → step… → fallback(unverifiable) + 原文清单 → done(unverifiable)
    系统异常   → step… → fallback(model_timeout|internal) → done(...)

四条取舍值得写下来：

1. **`token` 在校验之后**。这不是放弃流式，而是 §10 的要求："答案要等下一章校验
   通过才算数"。代价是首字延迟包含生成与校验；收益是屏幕上不会出现一句后来被撤掉的
   结论。生成本身是严格 JSON 的非流式调用（§9.2 的四个字段必须先拿到才能校验）。
2. **系统陈述与模型主张分开**。未覆盖清单（§8.3）、证据超长说明（§9.3）、补年份
   假设（§5.3）都是**系统**说的话，它们不需要引用。写进被校验正文会让第二道
   （每句都要引用）判它们失败，而它们本来就不该被引用。所以它们作为 `token` 尾巴
   发出，且不参与五道校验。
3. **系统档（模型不可用/超时）不给原文**。§10.3 的原话是"一句稍后重试，不暴露内部
   细节"：那一次我们连"证据是否相关"都没验证完，把原文推给用户等于用噪声掩盖故障。
   档一（校验不过）才给原文——那时证据已经过门控且被精排选出来了。
4. **一条流只有一个收尾**。`done` 在 `finally` 里发，异常路径也发：前端没有别的
   "结束"信号，缺它就一直转圈。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from api.audit import QaLogEntry
from api.errors import ErrorCode, spec_for
from api.generate.evidence import EvidencePack, build_evidence_pack
from api.generate.judge import JudgeResult
from api.generate.llm import (
    Draft,
    DraftFormatError,
    GenerationUnavailable,
    LlmClient,
    generate_draft,
)
from api.identity import IdentitySnapshot
from api.observability import get_logger, log_event
from api.qa import events as ev
from api.qa.events import SseEvent
from api.retrieval.pipeline import RetrievalPipeline, RetrievalSnapshot
from api.retrieval.texts import doc_names, fetch_texts
from api.verify.checks import VerificationReport, verify_draft

logger = get_logger("api.qa.orchestrator")

# 最终判定词：§8.4 的门控五态 + 校验层的第六种出口（见 `api/qa/events.py::done`）。
VERDICT_UNVERIFIABLE = "unverifiable"

# 用户文案一律取 §11.3 的表：文案与告警口径必须同源，这里不自己写一套。
FALLBACK_MESSAGES: dict[ErrorCode, str] = {
    code: spec_for(code).user_message
    for code in (
        ErrorCode.NO_EVIDENCE,
        ErrorCode.LOW_CONFIDENCE,
        ErrorCode.UNVERIFIABLE,
        ErrorCode.MODEL_TIMEOUT,
        ErrorCode.INTERNAL,
    )
}


@dataclass
class QaOutcome:
    """一次问答的终态。`audit` 是给 `AuditWriter` 的那一行。"""

    question: str
    verdict: str
    identity: IdentitySnapshot | None = None
    retrieved_ids: tuple[str, ...] = ()
    score_snapshot: dict[str, Any] = field(default_factory=dict)
    error_code: ErrorCode | None = None
    audit: QaLogEntry | None = None
    latency_ms: float = 0.0
    hop_count: int = 0
    answer: str = ""
    notices: tuple[str, ...] = ()
    evidence: EvidencePack | None = None
    verification: VerificationReport | None = None
    judge: JudgeResult | None = None
    events_sent: int = 0
    detail: str = ""

    @property
    def refused(self) -> bool:
        return self.error_code is not None

    @property
    def gate1_verdict(self) -> str:
        """`qa_log.gate1_verdict` 的四取值（DB 的 CHECK 只认这四个）。

        `partial` → `pass`：它过了门控（§8.4 说 partial 要带证据给下一步），未覆盖
        清单进 `score_snapshot.uncovered`。
        `unverifiable` → `pass`：门控放行了、校验拦住了。写成 reject_low 会污染
        "低置信"的统计，而两者处置完全不同（一个有疑似文档、一个连答案都生成了）。
        """
        return self.verdict if self.verdict in ("reject_none", "reject_low", "clarify") else "pass"


@dataclass(slots=True)
class QaService:
    """一条问答链路的全部依赖。字段就是依赖清单，没有隐藏单例。"""

    pipeline: RetrievalPipeline
    llm: LlmClient | None
    judge_client: LlmClient | None
    evidence_max_chars: int
    token_chunk_chars: int = 2
    qa_budget_s: float = 30.0
    model_name: str = ""

    # --- 对外入口 -------------------------------------------------------------

    def run(
        self,
        question: str,
        identity: IdentitySnapshot,
        *,
        doc_keys: Sequence[str] | None = None,
        time_range: Any = None,
        previous_entities: Sequence[str] | None = None,
        emit: Callable[[SseEvent], None] | None = None,
    ) -> QaOutcome:
        """跑完一次问答，边跑边 `emit` 事件。

        为什么用回调而不是返回事件列表：`step` 必须在**进入阶段时**发出去
        （附录 C.2"进入阶段或跳次"），而列表要等全部跑完才能返回——那样前端在
        生成与校验的几秒里只能看到转圈，SSE 就退化成一条慢速 HTTP 请求。
        """
        started = time.perf_counter()
        sent = 0

        def send(event: SseEvent) -> None:
            nonlocal sent
            sent += 1
            if emit is not None:
                emit(event)

        outcome = QaOutcome(question=question, verdict="internal", identity=identity)
        try:
            self._run(
                question,
                identity,
                send,
                outcome,
                started,
                doc_keys=doc_keys,
                time_range=time_range,
                previous_entities=previous_entities,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：缺 done 的流会让前端永远转圈
            log_event(
                logger,
                "qa_unhandled",
                level=40,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            self._system_fallback(send, outcome, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
        finally:
            latency = (time.perf_counter() - started) * 1000
            outcome.latency_ms = latency
            uncovered = tuple(outcome.score_snapshot.get("uncovered") or ())
            # `done` 永远最后一条（附录 C.2），异常路径也发。
            send(
                ev.done(
                    latency_ms=latency,
                    hop_count=outcome.hop_count,
                    verdict=outcome.verdict,
                    code=None if outcome.error_code is None else outcome.error_code.value,
                    uncovered=uncovered,
                )
            )
            outcome.events_sent = sent
            outcome.audit = _audit_entry(outcome, latency, self.model_name)
        return outcome

    # --- 主流程 ---------------------------------------------------------------

    def _run(
        self,
        question: str,
        identity: IdentitySnapshot,
        send: Callable[[SseEvent], None],
        outcome: QaOutcome,
        started: float,
        *,
        doc_keys: Sequence[str] | None,
        time_range: Any,
        previous_entities: Sequence[str] | None,
    ) -> None:
        send(ev.step(ev.PHASE_RETRIEVAL, elapsed_ms=_ms(started)))
        snapshot = self.pipeline.run(
            question,
            identity,
            doc_keys=doc_keys,
            previous_entities=previous_entities,
            time_range=time_range,
        )
        outcome.hop_count = snapshot.hop_count
        outcome.retrieved_ids = tuple(snapshot.retrieved_ids())
        outcome.score_snapshot = dict(snapshot.score_snapshot())
        outcome.score_snapshot["uncovered"] = list(snapshot.decision.uncovered)

        send(
            ev.step(
                ev.PHASE_RERANK,
                hop=snapshot.hop_count,
                elapsed_ms=_ms(started),
                note=snapshot.decision.reason,
            )
        )

        if snapshot.verdict == "clarify":
            self._emit_clarify(send, outcome, snapshot)
            return
        if snapshot.verdict in ("reject_none", "reject_low"):
            self._emit_reject(send, outcome, snapshot)
            return

        # --- pass / partial：进生成 ---
        pack = self._build_pack(snapshot)
        outcome.evidence = pack
        if not pack:
            # 门控放行了但证据包是空的（候选都取不到正文）。这不是"模型没说好"，
            # 是"我们没东西给它"，所以按无证据收口，不去调模型烧一次 token。
            self._emit_reject(send, outcome, snapshot, force_code=ErrorCode.NO_EVIDENCE)
            return

        notices = self._notices(pack, snapshot)
        send(ev.step(ev.PHASE_GENERATE, hop=snapshot.hop_count, elapsed_ms=_ms(started)))
        if self.llm is None:
            self._system_fallback(
                send, outcome, ErrorCode.MODEL_TIMEOUT, "未配置生成模型（LLM_MODEL 为空）"
            )
            return

        try:
            draft = generate_draft(self.llm, question, pack.render(), model=self.model_name)
        except DraftFormatError as exc:
            # §9.2："非 JSON / 缺字段 / answered 但空答案 → 降级"。落档一（只给原文）：
            # 证据在手，用户读原文比读"稍后重试"有用。档三留给真正的基础设施故障。
            log_event(logger, "draft_format_error", level=30, reason=exc.reason)
            self._emit_verification_failure(
                send,
                outcome,
                pack,
                notices,
                code=ErrorCode.UNVERIFIABLE,
                detail=f"模型输出不可用（{exc.reason}）",
            )
            return
        except GenerationUnavailable as exc:
            self._system_fallback(send, outcome, ErrorCode.MODEL_TIMEOUT, str(exc))
            return

        send(ev.step(ev.PHASE_VERIFY, hop=snapshot.hop_count, elapsed_ms=_ms(started)))
        report = verify_draft(draft, pack, question=question, judge_client=self.judge_client)
        outcome.verification = report
        outcome.judge = report.judge
        if not report.passed:
            self._emit_verification_failure(
                send,
                outcome,
                pack,
                notices,
                code=report.error_code or ErrorCode.UNVERIFIABLE,
                detail=report.failure.detail if report.failure else "校验未通过",
                report=report,
            )
            return

        self._emit_answer(send, outcome, snapshot, pack, report, notices, draft)

    # --- 出口 -----------------------------------------------------------------

    def _emit_answer(
        self,
        send: Callable[[SseEvent], None],
        outcome: QaOutcome,
        snapshot: RetrievalSnapshot,
        pack: EvidencePack,
        report: VerificationReport,
        notices: tuple[str, ...],
        draft: Draft,
    ) -> None:
        """校验通过：逐字 → 系统陈述 → 引用（附录 C.2 的 token* → citations）。"""
        answer = report.answer
        outcome.answer = answer
        outcome.notices = notices
        outcome.verdict = "partial" if snapshot.verdict == "partial" else "pass"
        for piece in ev.chunk_text(answer, self.token_chunk_chars):
            send(ev.token(piece))
        for notice in notices:
            send(ev.token(notice))
        send(ev.citations([item.as_citation() for item in report.cited_evidence]))
        outcome.detail = (
            f"checks={len(report.checks_run)} judge={report.gate2_verdict} "
            f"evidence={pack.n_items} claims={len(draft.claims)}"
        )
        log_event(
            logger,
            "qa_answered",
            verdict=outcome.verdict,
            evidence=pack.n_items,
            checks=len(report.checks_run),
            judge=report.gate2_verdict,
        )

    def _emit_clarify(
        self,
        send: Callable[[SseEvent], None],
        outcome: QaOutcome,
        snapshot: RetrievalSnapshot,
    ) -> None:
        decision = snapshot.decision
        options = [opt.as_dict() for opt in decision.clarify_options]
        question = (
            "请确认要查的是哪一份资料：" if options else "请补充查询条件（例如所属部门或年份）："
        )
        outcome.verdict = "clarify"
        outcome.detail = decision.reason
        send(ev.clarify(question, options))

    def _emit_reject(
        self,
        send: Callable[[SseEvent], None],
        outcome: QaOutcome,
        snapshot: RetrievalSnapshot,
        *,
        force_code: ErrorCode | None = None,
    ) -> None:
        """门控拒答：不调模型、不发 token（§8.1 / §10.4）。

        疑似文档只来自 `decision.suspected_docs`——它由门控从**已过前置过滤**的候选
        聚合而来（§8.1"≤3 份且都有权限"），所以这里不再筛一遍。
        """
        decision = snapshot.decision
        code = force_code or decision.reject_code or ErrorCode.NO_EVIDENCE
        outcome.verdict = snapshot.verdict
        outcome.error_code = code
        outcome.detail = decision.reason
        names = doc_names(self.pipeline.pg, [doc.doc_key for doc in decision.suspected_docs])
        docs = [
            {
                "doc_key": doc.doc_key,
                "doc_name": names.get(doc.doc_key, doc.doc_key),
                "best_score": round(doc.best_score, 4),
            }
            for doc in decision.suspected_docs
        ]
        send(ev.fallback(code.value, FALLBACK_MESSAGES[code], docs))
        log_event(
            logger,
            "qa_rejected",
            verdict=outcome.verdict,
            code=code.value,
            docs=len(docs),
            reason=decision.reason,
        )

    def _emit_verification_failure(
        self,
        send: Callable[[SseEvent], None],
        outcome: QaOutcome,
        pack: EvidencePack,
        notices: tuple[str, ...],
        *,
        code: ErrorCode,
        detail: str,
        report: VerificationReport | None = None,
    ) -> None:
        """校验不过 / 输出不可用：档一"只给原文"，不发 token（§10.3）。

        档一与档三的区别就在 `docs`：这里有原文可给（证据已过门控并被选出来），
        档三（系统异常）没有。
        """
        outcome.error_code = code
        outcome.verdict = VERDICT_UNVERIFIABLE if code is ErrorCode.UNVERIFIABLE else "reject_none"
        outcome.detail = detail
        outcome.notices = notices
        if report is not None:
            outcome.verification = report
            outcome.judge = report.judge
        evidence = report.cited_evidence if report is not None else ()
        if not evidence:
            evidence = pack.items[: min(3, pack.n_items)]
        docs = [
            {
                "doc_key": item.doc_key,
                "doc_name": item.doc_name,
                "page_num": item.page_num,
                "snippet": item.snippet,
            }
            for item in evidence[:3]
        ]
        send(ev.fallback(code.value, FALLBACK_MESSAGES[code], docs))
        log_event(
            logger,
            "qa_unverifiable",
            level=30,
            code=code.value,
            detail=detail[:200],
            docs=len(docs),
        )

    def _system_fallback(
        self,
        send: Callable[[SseEvent], None],
        outcome: QaOutcome,
        code: ErrorCode,
        detail: str,
    ) -> None:
        """档三：系统异常。**不给原文**（§10.3"不暴露内部细节"）。"""
        outcome.error_code = code
        if outcome.verdict in ("internal", "pass", "partial"):
            outcome.verdict = "internal" if code is ErrorCode.INTERNAL else code.value
        outcome.detail = detail
        send(ev.fallback(code.value, FALLBACK_MESSAGES[code], []))

    # --- 取数 -----------------------------------------------------------------

    def _build_pack(self, snapshot: RetrievalSnapshot) -> EvidencePack:
        """证据包：候选 → 按类型取文本 → 同父片合并 → 封顶 6000 字 → 编号 1..n。

        正文从 PG 再取一次（`pipeline` 内部的 `texts` 不外露）。多一次主键查询换
        一个干净的模块边界：证据包不该知道精排内部怎么存正文。
        """
        candidates = list(snapshot.decision.evidence)
        texts = fetch_texts(self.pipeline.pg, [c.chunk_id for c in candidates])
        return build_evidence_pack(
            candidates,
            texts,
            max_chars=self.evidence_max_chars,
            cap=self.pipeline.rerank_top_n_max,
        )

    def _notices(self, pack: EvidencePack, snapshot: RetrievalSnapshot) -> tuple[str, ...]:
        """系统陈述，写在被校验正文之外（见模块头第 2 条）。

        三句话各有硬要求：
        - 证据超长（§9.3"在答案里说明仅依据前 N 条"）
        - 多跳未覆盖（§8.3 / §8.4：partial 必须给"未覆盖清单"）
        - 时间补了年份（§5.3"并在回答里注明假设"）
        """
        notices: list[str] = []
        truncation = pack.notice()
        if truncation:
            notices.append(truncation)
        uncovered = tuple(snapshot.decision.uncovered)
        if uncovered:
            notices.append(f"（以下问题本次未查到可验证依据：{'、'.join(uncovered)}）")
        time_note = getattr(snapshot.plan.time, "note", None)
        if time_note:
            notices.append(f"（时间口径：{time_note}）")
        return tuple(notices)


def _audit_entry(outcome: QaOutcome, latency_ms: float, model: str) -> QaLogEntry:
    """把一次问答翻译成 `qa_log` 的一行（附录 A.4 + §11.2）。

    四条配对口径都在这里落地（`QaLogEntry` 的模型校验会再拦一遍）：
    - 拒答（reject_*）必须带 `reject_reason`；
    - `no_evidence` / `low_confidence` 只能配拒答判定；
    - `unverifiable` 不能与 `gate2=supported` 并存；
    - 只有校验通过的才写 `citations`（草稿被撤掉时那条映射不能留在审计里当"引用"）。
    """
    identity = outcome.identity
    if identity is None:  # pragma: no cover - `run()` 一定传了身份
        raise ValueError("QaOutcome 缺身份快照：审计无法回答「谁看了什么」")

    score = dict(outcome.score_snapshot)
    if outcome.verification is not None:
        score["verify"] = outcome.verification.as_audit()
    if outcome.evidence is not None:
        score["evidence_pack"] = outcome.evidence.as_audit()
    if outcome.notices:
        score["notices"] = list(outcome.notices)
    if outcome.detail:
        score["detail"] = outcome.detail

    passthrough = (
        None
        if outcome.verification is None or not outcome.verification.passed
        else [item.as_citation() for item in outcome.verification.cited_evidence]
    )
    return QaLogEntry(
        **identity.to_audit(),
        question=outcome.question,
        retrieved_ids=list(outcome.retrieved_ids),
        score_snapshot=score,
        gate1_verdict=outcome.gate1_verdict,  # type: ignore[arg-type]
        gate2_verdict=(
            None if outcome.verification is None else outcome.verification.gate2_verdict  # type: ignore[arg-type]
        ),
        hop_count=outcome.hop_count,
        answer=outcome.answer or None,
        citations=passthrough,
        reject_reason=outcome.error_code,
        latency_ms=int(round(latency_ms)),
        model=model or None,
    )


def _ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


__all__ = [
    "FALLBACK_MESSAGES",
    "QaOutcome",
    "QaService",
    "VERDICT_UNVERIFIABLE",
]
