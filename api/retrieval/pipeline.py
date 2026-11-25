"""在线检索编排：§6 → §7 → §8 一条链路（B3 的最终出口）。

这个模块只做**编排**：把 `understand()` 的计划、`HybridRetriever` 的两路召回、
`drop_stale()` 的版本兜底、`Reranker` 的精排、`decide()` 的门控按设计文档的顺序
串起来，并把每一段的证据留成一份可审计的快照。

四条顺序约束，每一条都能说清"换了会怎样"：

1. **版本兜底在精排之前**（§7.4）。反过来的话，旧版本切片会占掉精排窗口里的位置，
   而精排是逐对打分（每多一条都是毫秒）：我们花预算给一批注定要丢的候选打分。
2. **父片去重在精排之前**（§7.2）。同一父片的两条子片内容高度重叠，让它们都进
   精排等于用两条预算换一条信息。
3. **门控只在精排之后**（§8.1）。门控看的是精排分：拿融合分判会让"排第一"必然
   过阈（k=1 时第一名恒为 1.0）。
4. **`mode` 可被第一跳推翻**（§6.3 / §8.3）。预判是多跳不代表一定要跑多跳；
   第一跳证据齐了就按单跳收口。这个"改回去"的动作必须在编排层做，
   因为只有它同时知道预判与实际证据。

多跳的每一跳**独立过阈**（卡片的验收原句）：不是"总证据够就继续"，而是每一跳的
最高分都要过 `reject_none`（低于它这一跳什么都没查到，断链）。所以 `run_multihop`
的回调里调的是同一个 `_retrieve_once`，门控判定用的是同一份 `GateConfig`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from api.clients.pg import PgDatabase
from api.errors import ErrorCode
from api.filters import RetrievalFilter, build_filter
from api.identity import IdentitySnapshot
from api.observability import get_logger, log_event
from api.trace import trace as trace_event
from api.query import NeedsClarification, QueryPlan, understand
from api.time_range import select_time_docs
from api.retrieval.gate import (
    GateConfig,
    GateDecision,
    Verdict,
    decide,
)
from api.retrieval.hybrid import Candidate, HybridRetriever
from api.retrieval.multihop import Fact, HopOutcome, MultiHopResult, run_multihop
from api.retrieval.rerank import RerankOutcome, Reranker, rerank_candidates
from api.retrieval.texts import (
    ChunkText,
    all_doc_names,
    dedupe_candidate_texts,
    doc_names,
    drop_header_only_candidates,
    drop_repeated_boilerplate,
    fetch_texts,
    mentioned_doc_keys,
)
from api.version_guard import drop_stale

logger = get_logger("api.retrieval.pipeline")


class EmbeddingUnavailable(RuntimeError):
    """查询编码失败。召回无法开始——这不是"没有证据"，不能落进拒答（§11.3）。"""


@dataclass(slots=True)
class QueryEmbedder:
    """把查询文本编码成稠密向量。

    单独一个类而不是塞进 `HybridRetriever`：编码服务与向量库是两个独立依赖，
    它们各自挂掉时的处置完全不同（编码挂了要 503 `index_stale`/`internal`，
    向量库挂了要重试）。混在一个类里，调用方就只能凭异常消息分辨。
    """

    base_url: str
    model: str
    dim: int
    api_key: str = ""
    timeout_s: float = 10.0

    def embed(self, text: str) -> list[float]:
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with httpx.Client(
                base_url=self.base_url.rstrip("/"),
                timeout=self.timeout_s,
                headers=headers,
                trust_env=False,
            ) as client:
                resp = client.post("/embeddings", json={"input": [text], "model": self.model})
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"embedding service unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise EmbeddingUnavailable(
                f"embedding -> HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = (resp.json() or {}).get("data") or []
        if len(data) != 1:
            raise EmbeddingUnavailable(f"embedding returned {len(data)} vectors for 1 text")
        vector = data[0].get("embedding")
        if not isinstance(vector, list) or len(vector) != self.dim:
            raise EmbeddingUnavailable(
                f"embedding dim mismatch: got {len(vector) if isinstance(vector, list) else 'n/a'}, "
                f"expected {self.dim}"
            )
        return [float(v) for v in vector]


@dataclass(frozen=True, slots=True)
class RetrievalSnapshot:
    """一次检索的审计快照（§11.2 的 `score_snapshot` + `retrieved_ids`）。

    只存编号与分数摘要，**不存原文**——原文在 PG 里，重复存会让审计表变成第二份正文
    （§11.2 原话）。这里的 `filter_json` 会进审计：事后能复现"这次按什么权限搜的"。
    """

    plan: QueryPlan
    decision: GateDecision
    candidates: list[Candidate]
    filter_json: dict[str, Any]
    dense_count: int
    sparse_count: int
    dropped_stale: list[str]
    rerank: RerankOutcome
    elapsed_ms: float
    hop_count: int = 0
    multihop: MultiHopResult | None = None

    @property
    def verdict(self) -> Verdict:
        return self.decision.verdict

    def score_snapshot(self) -> dict[str, Any]:
        """§11.2 的 `score_snapshot`：各路最高分与分布。"""
        dense = [c.dense_score for c in self.candidates if c.dense_score is not None]
        sparse = [c.sparse_score for c in self.candidates if c.sparse_score is not None]
        rerank_scores = [c.rerank_score for c in self.candidates if c.rerank_score is not None]
        return {
            "top1": None
            if self.decision.top1 is None
            else round(self.decision.top1, 6),
            "dense_top1": round(max(dense), 6) if dense else None,
            "sparse_top1": round(max(sparse), 6) if sparse else None,
            # 两个键不能同名：早先这里连写两次 `"rerank"`，后一个（`as_audit()` 的
            # dict）把前一个（分数列表）静默覆盖掉，于是审计里永远看不到分数分布——
            # 而"分怎么分布"正是排查"这次为什么判成弱相关"的第一个依据。
            "rerank_scores": [round(s, 6) for s in rerank_scores],
            "fused": [round(c.fused_score, 6) for c in self.candidates],
            "routes": {"dense": self.dense_count, "sparse": self.sparse_count},
            "after_dedup": len(self.candidates),
            "dropped_stale": list(self.dropped_stale),
            "rerank": self.rerank.as_audit(),
            "verdict": self.verdict,
            "verdict_reason": self.decision.reason,
            "evidence": [c.chunk_id for c in self.decision.evidence],
            "hops": self.hop_count,
            "filter": self.filter_json,
            "mode": self.plan.mode,
            "rewrite_trace": [step.as_text() for step in self.plan.trace],
        }

    def retrieved_ids(self) -> list[str]:
        """§11.2 的 `retrieved_ids`：进精排的切片编号（按上游顺序）。"""
        return [c.chunk_id for c in self.candidates]


@dataclass(slots=True)
class RetrievalPipeline:
    """一条链路的全部依赖。字段就是它的依赖清单，没有隐藏单例。"""

    pg: PgDatabase
    retriever: HybridRetriever
    reranker: Reranker
    embedder: QueryEmbedder
    gate: GateConfig
    top_k: int
    rerank_top_n_min: int
    rerank_top_n_max: int
    rerank_candidates_max: int
    rerank_budget_ms: int
    parent_child_keep: int
    max_hops: int
    evidence_max_chars: int
    rerank_text_max_chars: int | None = None
    multi_entity_threshold: int = 2
    time_llm: Any = None
    _texts: dict[str, ChunkText] = field(default_factory=dict, repr=False)

    # --- 单跳 -----------------------------------------------------------------

    def _retrieve_once(
        self, plan: QueryPlan, flt: RetrievalFilter
    ) -> tuple[list[Candidate], RerankOutcome, dict[str, ChunkText], list[str], int, int]:
        """一次完整召回：两路 → RRF → 父片去重 → 版本兜底 → 取正文 → 精排。

        返回值带上两路各自的条数：排障时"sparse=0"是最有用的一个信号
        （说明 BM25 侧没写进去或查询串没过 `bm25_text()`），丢了它就只剩
        "一条也没召回到"这种无从下手的现象。
        """
        embed_started = time.perf_counter()
        vector = self.embedder.embed(plan.retrieval_text)
        trace_event(
            "retrieval.embed",
            retrieval_text=plan.retrieval_text,
            dim=len(vector),
            elapsed_ms=round((time.perf_counter() - embed_started) * 1000, 1),
        )
        # 预精排要先去重并剔除页眉/样板文本；若两路只取 top_k=30，
        # 大量重复标题会把真正数据表挡在候选池外，后面的过滤再准也救不回来。
        # 因此至少召回 rerank_candidates_max 条，保证过滤后仍有足够候选可打分。
        retrieval_k = max(self.top_k, self.rerank_candidates_max)
        hybrid = self.retriever.retrieve(
            plan.retrieval_text,
            flt,
            dense_query=vector,
            top_k=retrieval_k,
            parent_child_keep=self.parent_child_keep,
        )
        # §7.4：必须在精排前。drop_stale 吃 dict，Candidate 转一下再转回来。
        kept, dropped = drop_stale(self.pg, [_as_mapping(c) for c in hybrid.candidates])
        kept_ids = {row["chunk_id"] for row in kept}
        guarded = [c for c in hybrid.candidates if c.chunk_id in kept_ids]
        dropped_ids = [row["chunk_id"] for row in dropped]
        trace_event(
            "retrieval.version_guard",
            kept_ids=list(kept_ids),
            dropped_ids=dropped_ids,
        )

        requested_ids = [c.chunk_id for c in guarded]
        texts = fetch_texts(self.pg, requested_ids)
        # 取不到正文的候选不能进精排：精排拿空串打分只会给一个 0 分，
        # 然后在证据包里变成一条空引用。它们的正文多半是刚被下线的（版本窗口）。
        guarded = [c for c in guarded if c.chunk_id in texts]
        guarded, deduped = dedupe_candidate_texts(guarded, texts)
        guarded, boilerplate_dropped = drop_repeated_boilerplate(guarded, texts)
        guarded, header_dropped = drop_header_only_candidates(guarded, texts)
        trace_event(
            "retrieval.texts",
            requested=requested_ids,
            fetched=list(texts),
            missing=[cid for cid in requested_ids if cid not in texts],
            dropped_duplicates=deduped,
            dropped_boilerplate=boilerplate_dropped,
            dropped_header_only=header_dropped,
        )

        outcome = rerank_candidates(
            self.reranker,
            plan.retrieval_text,
            guarded,
            {cid: item.text for cid, item in texts.items()},
            budget_ms=self.rerank_budget_ms,
            text_max_chars=self.rerank_text_max_chars,
            top_n_min=self.rerank_top_n_min,
            top_n_max=self.rerank_top_n_max,
            candidates_max=self.rerank_candidates_max,
        )
        return (
            outcome.ordered,
            outcome,
            texts,
            dropped_ids,
            hybrid.dense_count,
            hybrid.sparse_count,
        )

    # --- 对外入口 -------------------------------------------------------------

    def run(
        self,
        question: str,
        identity: IdentitySnapshot,
        *,
        doc_keys: Sequence[str] | None = None,
        previous_entities: Sequence[str] | None = None,
        time_range: Any = None,
    ) -> RetrievalSnapshot:
        """一句问话 → 一个判定（§8.4 的五态之一）。

        `time_range` 显式传进来的优先级高于问句里归一出来的时间窗：前端给的
        `time_range` 是用户点选的结果，比从自然语言里猜出来的更权威。
        """
        started = time.perf_counter()

        try:
            plan = understand(
                question,
                previous_entities=previous_entities,
                time_llm=self.time_llm,
            )
        except NeedsClarification as exc:
            # 指代不清时不检索（§6.1）：此时任何检索结果都是"按原句瞎猜"的产物。
            return self._clarify_without_retrieval(exc, question, started)

        if plan.out_of_scope:
            # §6.3 边界表：明显超出库范围的直接拒答、不检索。
            return self._reject_without_retrieval(
                question, plan, "超出库范围，未检索", started
            )

        effective_range = time_range if time_range is not None else plan.time.range
        effective_doc_keys = tuple(doc_keys) if doc_keys is not None else None
        if not effective_doc_keys and effective_range is not None:
            names = all_doc_names(self.pg)
            selected = select_time_docs(question, names, client=self.time_llm)
            if selected:
                effective_doc_keys = selected
            else:
                named_doc_keys = mentioned_doc_keys(question, names)
                if named_doc_keys:
                    effective_doc_keys = named_doc_keys
        flt = build_filter(identity, time_range=effective_range, doc_keys=effective_doc_keys)
        multi_entity = plan.mode == "multi" or len(plan.entities) >= self.multi_entity_threshold

        candidates, rerank_outcome, texts, dropped, dense_count, sparse_count = self._retrieve_once(
            plan, flt
        )
        decision = decide(
            candidates,
            cfg=self.gate,
            entities=plan.entities,
            texts={cid: item.text for cid, item in texts.items()},
            doc_names={c.doc_key: texts[c.chunk_id].doc_name for c in candidates},
            multi_entity=multi_entity,
            degraded=rerank_outcome.degraded,
            degrade_reason=rerank_outcome.reason,
            evidence_cap=self.rerank_top_n_max,
        )
        trace_event("gate.decided", hop=0, **decision.as_audit())

        multihop: MultiHopResult | None = None
        hop_count = 0
        if decision.verdict == "pass" and plan.mode == "multi" and self.max_hops > 0:
            # §8.3：预判多跳且第一跳已经过阈，才继续找。第一跳没过阈直接收口——
            # 那说明问题本身库里就没有，多跳只会多花三次检索。
            multihop = self._run_multihop(plan, flt, decision, texts)
            hop_count = multihop.hop_count
            if multihop.facts:
                decision = decide(
                    candidates,
                    cfg=self.gate,
                    entities=plan.entities,
                    texts={cid: item.text for cid, item in texts.items()},
                    doc_names={c.doc_key: texts[c.chunk_id].doc_name for c in candidates},
                    multi_entity=multi_entity,
                    degraded=rerank_outcome.degraded,
                    degrade_reason=rerank_outcome.reason,
                    uncovered=multihop.uncovered,
                    evidence_cap=self.rerank_top_n_max,
                )
                trace_event(
                    "gate.decided", hop=hop_count, final=True, **decision.as_audit()
                )

        elapsed_ms = (time.perf_counter() - started) * 1000
        snapshot = RetrievalSnapshot(
            plan=plan,
            decision=decision,
            candidates=candidates,
            filter_json=_filter_json(flt),
            dense_count=dense_count,
            sparse_count=sparse_count,
            dropped_stale=dropped,
            rerank=rerank_outcome,
            elapsed_ms=elapsed_ms,
            hop_count=hop_count,
            multihop=multihop,
        )
        log_event(
            logger,
            "retrieval_finished",
            verdict=decision.verdict,
            candidates=len(candidates),
            evidence=len(decision.evidence),
            hops=hop_count,
            degraded=rerank_outcome.degraded,
            elapsed_ms=round(elapsed_ms, 1),
        )
        trace_event(
            "retrieval.finished",
            verdict=decision.verdict,
            candidates=[item.as_dict() for item in candidates],
            evidence_ids=[item.chunk_id for item in decision.evidence],
            hops=hop_count,
            dense=snapshot.dense_count,
            sparse=snapshot.sparse_count,
            dropped_stale=list(snapshot.dropped_stale),
            elapsed_ms=round(elapsed_ms, 1),
        )
        return snapshot

    # --- 多跳 ----------------------------------------------------------------

    def _run_multihop(
        self,
        plan: QueryPlan,
        flt: RetrievalFilter,
        first: GateDecision,
        first_texts: dict[str, ChunkText],
    ) -> MultiHopResult:
        """带着第一跳已查实的事实在同一套权限条件下继续找（§8.3）。

        每一跳都**重新召回、独立过阈**：不是复用第一跳的候选。复用的后果是
        "第一跳的证据决定了后面所有跳"——那正好是断链时想说清的那件事做不到。
        """
        covered = set(first_texts)
        known_texts = dict(first_texts)

        def _missing() -> tuple[str, ...]:
            """还缺哪些实体（按**实体**归并，而不是按表面写法）。

            实测撞出来的死循环：`coverage_terms` 里同时有 `住宿费限额`（归一后）和
            `住宿费`（原文）。第一跳命中了正文"住宿费标准"——`住宿费` 被覆盖了，
            但 `住宿费限额` 没出现，于是缺口永远非空，多跳空转到 Max Hops。

            正确的口径是"这个**实体**有没有被覆盖"，而同一实体的多种写法是或的关系：
            任一写法出现即算覆盖。所以先把写法按实体分组，再看每组是否有一支命中。
            """
            groups = _entity_groups(plan)
            missing: list[str] = []
            for entity, forms in groups.items():
                if not any(_term_in(form, known_texts) for form in forms):
                    missing.append(entity)
            return tuple(missing)

        remaining = _missing()

        def gap_probe(_board: Any) -> Sequence[str]:
            return _missing()

        def sub_questioner(entity: str) -> str:
            # 子问题只能是"原问 + 这个实体"：拼进改写过的检索串会丢掉时间窗之外
            # 的所有条件（§8.3"带黑板检索"），而拼进原文又会让实体被淹没。
            return f"{entity} {plan.retrieval_text}".strip()

        def runner(hop: int, sub_question: str, gap: str) -> HopOutcome:
            sub_plan = understand(
                sub_question,
                previous_entities=None,
                time_llm=self.time_llm,
            )
            candidates, outcome, texts, _dropped, _dense, _sparse = self._retrieve_once(
                sub_plan, flt
            )
            hop_decision = decide(
                candidates,
                cfg=self.gate,
                entities=(gap,),
                texts={cid: item.text for cid, item in texts.items()},
                doc_names={},
                multi_entity=False,
                degraded=outcome.degraded,
                degrade_reason=outcome.reason,
                evidence_cap=self.rerank_top_n_max,
            )
            passed = hop_decision.verdict == "pass"
            facts: tuple[Fact, ...] = ()
            if passed:
                known_texts.update(texts)
                covered.update(texts)
                facts = tuple(
                    Fact(
                        claim=texts[c.chunk_id].text,
                        chunk_id=c.chunk_id,
                        page_num=texts[c.chunk_id].page_num,
                        hop=hop,
                        doc_key=c.doc_key,
                    )
                    for c in hop_decision.evidence
                    if c.chunk_id in texts
                )
            trace_event(
                "multihop.hop",
                hop=hop,
                sub_question=sub_question,
                gap=gap,
                top1=hop_decision.top1,
                verdict=hop_decision.verdict,
                passed=passed,
                facts=len(facts),
                note=hop_decision.reason,
            )
            return HopOutcome(
                hop=hop,
                sub_question=sub_question,
                gap=gap,
                passed=passed,
                facts=facts,
                top1=hop_decision.top1,
                note=hop_decision.reason,
            )

        result = run_multihop(
            max_hops=self.max_hops,
            runner=runner,
            gap_probe=gap_probe,
            sub_questioner=sub_questioner,
            initial_gap=remaining,
        )
        trace_event("multihop.finished", **result.as_audit())
        _ = covered  # 覆盖集合只用于日志，不参与判定（缺口由 gap_probe 重算）
        return result

    # --- 未检索就收口的两条路 -------------------------------------------------

    def _clarify_without_retrieval(
        self, exc: NeedsClarification, question: str, started: float
    ) -> RetrievalSnapshot:
        """指代不清 → 澄清（§6.1）。

        这里**不能**再调 `understand()`：它会再抛同一个 `NeedsClarification`
        （实测撞过——异常从"被接住"变成"从接住处再抛一次"，最终冒到 HTTP 层）。
        时间归一单独跑一次即可，它跟指代无关，也不会抛。
        """
        from api.time_range import normalise_time

        detail = f"指代「{exc.pronoun}」有 {len(exc.candidates)} 个候选，未检索"
        trace_event("retrieval.short_circuit", mode="clarify", reason=detail)
        plan = QueryPlan(
            retrieval_text=question,
            entities=tuple(exc.candidates),
            time=normalise_time(question),
            mode="single",
            trace=(),
            original=question,
            notes=(detail,),
        )
        decision = GateDecision(
            verdict="clarify",
            top1=None,
            reason=detail,
        )
        return RetrievalSnapshot(
            plan=plan,
            decision=decision,
            candidates=[],
            filter_json={},
            dense_count=0,
            sparse_count=0,
            dropped_stale=[],
            rerank=RerankOutcome(ordered=[]),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def _reject_without_retrieval(
        self, question: str, plan: QueryPlan, reason: str, started: float
    ) -> RetrievalSnapshot:
        trace_event(
            "retrieval.short_circuit", mode="reject_none", reason=reason, question=question
        )
        decision = GateDecision(
            verdict="reject_none",
            top1=None,
            reason=reason,
            reject_code=ErrorCode.NO_EVIDENCE,
        )
        return RetrievalSnapshot(
            plan=plan,
            decision=decision,
            candidates=[],
            filter_json={},
            dense_count=0,
            sparse_count=0,
            dropped_stale=[],
            rerank=RerankOutcome(ordered=[]),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    def close(self) -> None:
        self.retriever.close()
        self.reranker.close()


def _term_in(term: str, texts: dict[str, ChunkText]) -> bool:
    return any(term in item.text for item in texts.values())


def _entity_groups(plan: QueryPlan) -> dict[str, tuple[str, ...]]:
    """把同一实体的多种表面写法归到一组：{实体: (写法, ...)}。

    分组的依据是"谁是谁的前缀"：`住宿费` 是 `住宿费限额` 的前缀，两者是同一个实体
    的两种写法；`交通费` 与 `住宿费` 互不为前缀，是两个实体。

    为什么不用改写痕迹（`trace`）来分组：改写只记录了"被替换掉的词"，而同一实体
    还可能是"没被替换但同义"的写法（比如正文写"住宿费标准"、问句写"住宿费"），
    那些不在 trace 里。前缀分组把两种情况一起覆盖了。
    """
    groups: dict[str, tuple[str, ...]] = {}
    for entity in plan.entities:
        forms = [
            term
            for term in plan.coverage_terms
            if term == entity or entity.startswith(term) or term.startswith(entity)
        ]
        groups[entity] = tuple(dict.fromkeys(forms)) or (entity,)
    return groups


def _entity_in(entity: str, texts: dict[str, ChunkText]) -> bool:
    """兼容旧名（多跳的回调里曾用这个）。"""
    return _term_in(entity, texts)


def _as_mapping(candidate: Candidate) -> dict[str, Any]:
    """`drop_stale` 只要求 chunk_id/doc_key/version_id 三个字段。"""
    return {
        "chunk_id": candidate.chunk_id,
        "doc_key": candidate.doc_key,
        "version_id": candidate.version_id,
    }


def _filter_json(flt: RetrievalFilter) -> dict[str, Any]:
    from api.retrieval.hybrid import qdrant_filter_json

    return qdrant_filter_json(flt)


def build_pipeline(settings: Any, pg: PgDatabase, llm: Any = None) -> RetrievalPipeline:
    """按 `Settings` 造一条链路。装配只在这一处，路由与测试共用。

    为什么要一个工厂而不是让路由自己 new 五个对象：这五个对象的参数全部来自配置，
    散在路由里就会出现"某个路由忘了传 parent_child_keep，于是用默认值 2"——
    而默认值与配置里的值不一致时，表现是"这段召回少了/多了几条"，没有报错。
    """
    return RetrievalPipeline(
        pg=pg,
        retriever=HybridRetriever(
            settings.qdrant_url,
            settings.qdrant_collection,
            api_key=settings.qdrant_api_key.get_secret_value(),
            timeout_s=settings.qdrant_timeout_s,
        ),
        reranker=Reranker(
            settings.rerank_base_url,
            settings.rerank_model,
            api_key=settings.rerank_api_key.get_secret_value(),
            timeout_s=settings.rerank_timeout_s,
        ),
        embedder=QueryEmbedder(
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            api_key=settings.embedding_api_key.get_secret_value(),
            timeout_s=settings.embedding_timeout_s,
        ),
        gate=gate_config(settings),
        top_k=settings.retrieval_top_k,
        rerank_top_n_min=settings.rerank_top_n_min,
        rerank_top_n_max=settings.rerank_top_n_max,
        rerank_candidates_max=settings.rerank_candidates_max,
        rerank_budget_ms=settings.rerank_budget_ms,
        parent_child_keep=settings.parent_child_keep,
        max_hops=settings.max_hops,
        evidence_max_chars=settings.evidence_max_chars,
        rerank_text_max_chars=settings.rerank_text_max_chars,
        time_llm=llm,
    )


def gate_config(settings: Any) -> GateConfig:
    """`Settings` → `GateConfig`。门控的八个数只有这一个转换点。"""
    return GateConfig(
        reject_none=settings.gate_reject_none,
        reject_low=settings.gate_reject_low,
        borderline_low=settings.gate_borderline_low,
        borderline_high=settings.gate_borderline_high,
        clarify_score_delta=settings.clarify_score_delta,
        clarify_options_max=settings.clarify_options_max,
        suspected_docs_max=settings.suspected_docs_max,
        evidence_min_paras=settings.evidence_min_paras,
    )


__all__ = [
    "EmbeddingUnavailable",
    "QueryEmbedder",
    "RetrievalPipeline",
    "RetrievalSnapshot",
    "build_pipeline",
    "gate_config",
]
