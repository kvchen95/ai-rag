"""三段门控（详细设计.md §8.1 / §8.2）。.

只看精排后的最高分，分三段处置。中间那一档的存在理由在文档里写得很清楚：
"有一点像"和"完全没有"在用户那里的感受完全不同——前者该告诉他"可能有这几份"，
后者只能说"库里没有"。

五态出口（§8.4）：
`pass` / `clarify` / `reject_none` / `reject_low` / `partial`。

为什么 `partial` 不在 `gate1` 上看分数：它只由多跳收敛情况产生（§8.3 三跳后仍缺
一部分）。所以门控接受一个显式的 `partial_reason`，由编排层在多跳结束时传进来；
门控自己不猜"是不是没答全"。

三条容易写错的边界，逐条落地在下面：

1. **0.38–0.42 之间不单靠分数**（§8.1）。这个带由配置给（`gate_borderline_*`），
   落进带里必须看辅助条件：有效证据条数（复杂问题 ≥2 条且分属不同段落）、
   关键实体是否在证据里出现。
2. **分数刚好低于 0.20 但关键词强命中 → 仍拒答**（§8.1 边界表）。关键词命中
   不构成证据，所以这里**不看** BM25 分数——只有精排分能决定 verdict。这条最
   容易被"优化召回"的人改坏，所以用一条 BM25 分数极高的候选钉住它
   （`test_bm25_strength_never_overrides_gate`）。
3. **最高分高但只有一条证据**：单事实放行，多实体拒答（§8.1 边界表）。
   "多实体"的判定来自 `QueryPlan.mode == "multi"` 或实体数 > 1。

降级时的阈值不变，但 `top1` 的来源变了（按名次取的前 N 条的**精排分**仍然可能
是 None）。落进 None 时按 0 处理会直接拒答——这正是 §7.3 想要的保守方向，
但必须在审计里能看出"这是降级导致的拒答"，所以 `GateDecision` 带 `degraded`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from api.errors import ErrorCode
from api.observability import get_logger, log_event
from api.resources import messages as msg
from api.retrieval.hybrid import Candidate

logger = get_logger("api.retrieval.gate")

Verdict = Literal["pass", "clarify", "reject_none", "reject_low", "partial"]

# §8.4 五态，写全了才能被穷举检查（测试里对枚举做断言，加一态不写测试会红）。
VERDICTS: tuple[Verdict, ...] = (
    "pass",
    "clarify",
    "reject_none",
    "reject_low",
    "partial",
)


@dataclass(frozen=True, slots=True)
class GateConfig:
    """门控阈值。全部来自 `Settings`，这里不设默认值。.

    做成独立 dataclass 而不是直接吃 `Settings`：门控的单测要能在没有完整配置
    （没有 PG/Qdrant/模型名）的情况下跑，而 `Settings` 是"缺一项就启动失败"的。
    """

    reject_none: float
    reject_low: float
    borderline_low: float
    borderline_high: float
    clarify_score_delta: float
    clarify_options_max: int
    suspected_docs_max: int
    evidence_min_paras: int

    def __post_init__(self) -> None:
        """Run __post_init__."""
        if not (0 < self.reject_none < self.reject_low <= 1):
            raise ValueError(
                msg.GATE_THRESHOLD_INVALID.format(
                    reject_none=self.reject_none, reject_low=self.reject_low
                )
            )
        if self.borderline_low >= self.borderline_high:
            raise ValueError(
                msg.GATE_BORDERLINE_INVALID.format(
                    low=self.borderline_low, high=self.borderline_high
                )
            )

    @property
    def effective_borderline(self) -> tuple[float, float] | None:
        """裁到门控区间内的临界带；裁剪后为空则返回 None（= 不做临界带判定）。.

        为什么裁剪而不是拒绝：门控阈值随模型与语料重标定（§12.2 波谷法），
        `0.38–0.42` 只是当前模型下的一个位置，不是真理。把三个数硬绑在一起会让
        "把阈值调到 0.10/0.55 重新标定"启动失败——那正好堵死了标定这件事。

        返回 None 时门控**退化成纯阈值判定**（这正是"没配临界带"的语义），
        而不是"临界带永远不命中、辅助条件永远不生效"——后者是一种静默失效。
        """
        low = max(self.borderline_low, self.reject_none)
        high = min(self.borderline_high, self.reject_low)
        if low > high:
            return None
        return (low, high)


@dataclass(frozen=True, slots=True)
class ClarifyOption:
    """§8.2：选项只能从检索到的事实里取，每个带一个可回填的参数。."""

    label: str
    doc_key: str
    # 回填参数：文档编号或时间范围。这里统一成"并进 scope"的形态，
    # 前端不用为两种参数写两套逻辑。
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "label": self.label,
            "doc_key": self.doc_key,
            "params": dict(self.params),
        }


@dataclass(frozen=True, slots=True)
class SuspectedDoc:
    """拒答时给的疑似文档（§8.1：≤3 份，只含有权限文档）。."""

    doc_key: str
    best_score: float

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "doc_key": self.doc_key,
            "best_score": round(self.best_score, 6),
        }


@dataclass(frozen=True, slots=True)
class GateDecision:
    """GateDecision."""

    verdict: Verdict
    # 进证据包的候选。只有 pass / partial 有；其余为空。
    evidence: tuple[Candidate, ...] = ()
    clarify_options: tuple[ClarifyOption, ...] = ()
    suspected_docs: tuple[SuspectedDoc, ...] = ()
    top1: float | None = None
    # 为什么落到这一态。审计与排障的第一句话。
    reason: str = ""
    # 辅助条件命中了哪一条（§8.1 的"只在最高分刚过线时用"）。
    borderline_used: str | None = None
    # 精排降级（§7.3）。降级导致的拒答必须在审计里可辨。
    degraded: bool = False
    degrade_reason: str | None = None
    # 多跳未覆盖全部（§8.4 的 partial 带什么给下一步）。
    uncovered: tuple[str, ...] = ()
    # 拒答原因码（§11.3）。由 `decide()` 按 verdict 填，不由调用方各写一份：
    # 两处各写迟早出现"verdict=reject_low 但 reason=no_evidence"的组合。
    reject_code: ErrorCode | None = None

    def as_audit(self) -> dict[str, Any]:
        """Run as_audit."""
        return {
            "verdict": self.verdict,
            "top1": None if self.top1 is None else round(self.top1, 6),
            "reason": self.reason,
            "borderline_used": self.borderline_used,
            "evidence_ids": [item.chunk_id for item in self.evidence],
            "clarify_options": [opt.as_dict() for opt in self.clarify_options],
            "suspected_docs": [doc.as_dict() for doc in self.suspected_docs],
            "degraded": self.degraded,
            "degrade": self.degrade_reason,
            "uncovered": list(self.uncovered),
        }


def effective_score(candidate: Candidate) -> float:
    """门控看的分数：精排分。没有精排分时是 0.0（不是融合分）。.

    为什么不用融合分兜底：融合分是名次的函数（k=1 时第一名恒为 1.0），拿它和
    0.20/0.40 比没有意义——任何候选只要排第一就"过阈"，门控等于不存在。
    降级时没有精排分 → 0.0 → 拒答，这是保守方向，且审计里 `degraded=True` 可辨。
    """
    return candidate.rerank_score if candidate.rerank_score is not None else 0.0


def _distinct_paragraphs(evidence: Sequence[Candidate]) -> int:
    """分属不同段落"的可操作口径：(doc_key, page_num) 的去重数。.

    页是最接近"段落"的稳定标识：切片没有段落编号，而相邻子片往往同页；
    用 chunk_type 或 parent_id 都会把"同一页的两段"算成两段。
    """
    return len({(item.doc_key, item.page_num) for item in evidence})


def _entity_present(
    entities: Sequence[str],
    evidence: Sequence[Candidate],
    texts: dict[str, str],
) -> bool:
    """关键实体是否在证据里出现（§8.1 的第二个辅助条件）。.

    实体一个都没抽到时返回 True——"没有可检查的实体"不等于"实体缺失"，
    顺手判 False 会让所有没抽到实体的问句一律拒答。
    """
    if not entities:
        return True
    blob = " ".join(texts.get(item.chunk_id, "") for item in evidence)
    return any(entity in blob for entity in entities)


def decorate_clarify_options(
    candidates: Sequence[Candidate],
    *,
    doc_names: dict[str, str],
    max_options: int,
    reasons: Sequence[str] = (),
) -> tuple[ClarifyOption, ...]:
    """从证据里生成澄清选项（§8.2）。.

    选项的两个字段都必须来自检索结果：
    - `label` = 文档标题（有就给）+ 命中页；不编造章节名——章节名在 payload 里
      没有，编一个出来就是"凭空的选项"；
    - `params` = 回填的 `doc_key`，用户点选后并进 `scope` 重查一次。

    两份候选来自同一文档时不重复出选项（同一份文档选两次没有意义）。
    """
    options: list[ClarifyOption] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.doc_key in seen:
            continue
        seen.add(candidate.doc_key)
        name = doc_names.get(candidate.doc_key) or candidate.doc_key
        label = (
            msg.GATE_CLARIFY_LABEL.format(
                name=name, page=candidate.page_num
            )
            if candidate.page_num
            else name
        )
        options.append(
            ClarifyOption(
                label=label,
                doc_key=candidate.doc_key,
                params={
                    "doc_keys": [candidate.doc_key],
                    **({"reasons": list(reasons)} if reasons else {}),
                },
            )
        )
        if len(options) >= max_options:
            break
    return tuple(options)


def _needs_clarify(
    candidates: Sequence[Candidate], cfg: GateConfig
) -> tuple[bool, str]:
    """条件缺失或多文档同分（§8.2 的两种触发情形）。.

    实现上只判第二种（"候选来自两份以上文档且分数接近"）：第一种（没说部门/年份）
    在检索前就已经由调用方处理了（缺条件该在 `understand()` 那侧触发澄清），
    到这里再说"缺条件"会把一个已经检索过的问句退回起点，用户会以为没生效。

    分数接近用**精排分**判：融合分是名次的函数，两路名次相邻的候选分数必然接近，
    拿它判"同分"等于每两份文档都触发澄清。
    """
    by_doc: dict[str, float] = {}
    for candidate in candidates:
        score = effective_score(candidate)
        if score > by_doc.get(candidate.doc_key, 0.0):
            by_doc[candidate.doc_key] = score
    if len(by_doc) < 2:
        return False, ""
    ordered = sorted(by_doc.values(), reverse=True)
    delta = ordered[0] - ordered[1]
    if delta < cfg.clarify_score_delta:
        return (
            True,
            msg.GATE_CLARIFY_CLOSE.format(
                delta=delta, threshold=cfg.clarify_score_delta
            ),
        )
    return False, ""


def decide(
    candidates: Sequence[Candidate],
    *,
    cfg: GateConfig,
    entities: Sequence[str] = (),
    texts: dict[str, str] | None = None,
    doc_names: dict[str, str] | None = None,
    multi_entity: bool = False,
    degraded: bool = False,
    degrade_reason: str | None = None,
    uncovered: Sequence[str] = (),
    evidence_cap: int | None = None,
) -> GateDecision:
    """一条判定（§8.1 / §8.2 / §8.4）。.

    `candidates` 必须已按精排分降序（`rerank_candidates()` 的产物）。这里不
    重排：重排会让"取前 N"和"最高分"两个口径分裂成两处实现。

    `uncovered` 非空表示多跳已收敛但未覆盖全部 → `partial`。它优先于分数
    判定，因为部分结论是有价值的（§8.3"断链时交出去的是已经查实的部分 +
    断在哪一步"），把它降级成 `reject_low` 会让用户看不到任何已查实的东西。
    """
    texts = texts or {}
    doc_names = doc_names or {}
    items = list(candidates)

    if not items:
        return GateDecision(
            verdict="reject_none",
            top1=None,
            reason=msg.GATE_NO_CANDIDATES,
            degraded=degraded,
            degrade_reason=degrade_reason,
            reject_code=ErrorCode.NO_EVIDENCE,
        )

    top1 = effective_score(items[0])
    # 有精排分才算"跑过精排"；全为 None 说明整段降级了。
    any_scored = any(item.rerank_score is not None for item in items)

    # --- 第一段：无相关 ---
    if top1 < cfg.reject_none:
        reason = f"top1={top1:.3f} < {cfg.reject_none}" + (
            msg.GATE_RERANK_DEGRADED if not any_scored else ""
        )
        return GateDecision(
            verdict="reject_none",
            top1=top1 if any_scored else None,
            reason=reason,
            degraded=degraded,
            degrade_reason=degrade_reason,
            reject_code=ErrorCode.NO_EVIDENCE,
        )

    # --- 临界带：用辅助条件**替代**分数判定（§8.1）---
    # §8.1 原话："最高分在 0.38–0.42 之间 → 按辅助条件决定，不单独靠分数"。
    # 所以这一带里辅助条件说了算：满足就当作过线，不满足就落 reject_low。
    # 早先的写法是"判完辅助条件再继续走 `top1 < reject_low`"——那样 0.39
    # 会因为没到 0.40 被拒，辅助条件等于白判。"替代"与"补充"是两种语义，
    # 这里必须是替代。
    #
    # 带宽由 `effective_borderline` 给出：门控阈值重标定后带宽会跟着裁，
    # 裁空了（None）就退化成纯阈值判定。
    borderline_used: str | None = None
    band = cfg.effective_borderline
    in_borderline = band is not None and band[0] <= top1 <= band[1]
    if in_borderline:
        paragraphs = _distinct_paragraphs(items)
        if multi_entity and paragraphs < cfg.evidence_min_paras:
            borderline_used = msg.GATE_BORDERLINE_FEW_PARAS.format(
                top1=top1, paragraphs=paragraphs
            )
            return GateDecision(
                verdict="reject_low",
                top1=top1,
                reason=borderline_used,
                borderline_used=borderline_used,
                suspected_docs=_suspects(items, cfg.suspected_docs_max),
                degraded=degraded,
                degrade_reason=degrade_reason,
                reject_code=ErrorCode.LOW_CONFIDENCE,
            )
        if not _entity_present(entities, items, texts):
            borderline_used = msg.GATE_BORDERLINE_NO_ENTITY.format(top1=top1)
            return GateDecision(
                verdict="reject_low",
                top1=top1,
                reason=borderline_used,
                borderline_used=borderline_used,
                suspected_docs=_suspects(items, cfg.suspected_docs_max),
                degraded=degraded,
                degrade_reason=degrade_reason,
                reject_code=ErrorCode.LOW_CONFIDENCE,
            )
        borderline_used = msg.GATE_BORDERLINE_PASSED.format(top1=top1)

    # --- 第二段：弱相关（临界带已由上面判过，不再用分数复核）---
    if not in_borderline and top1 < cfg.reject_low:
        return GateDecision(
            verdict="reject_low",
            top1=top1,
            reason=msg.GATE_BELOW_LOW.format(
                top1=top1, low=cfg.reject_none, high=cfg.reject_low
            ),
            borderline_used=borderline_used,
            suspected_docs=_suspects(items, cfg.suspected_docs_max),
            degraded=degraded,
            degrade_reason=degrade_reason,
            reject_code=ErrorCode.LOW_CONFIDENCE,
        )

    # --- 第三段：有支撑，进歧义与覆盖检查 ---
    # §8.1 边界表："最高分高但只有一条证据 → 单事实问题放行；多实体问题拒答"。
    distinct = _distinct_paragraphs(items)
    if multi_entity and distinct < cfg.evidence_min_paras:
        return GateDecision(
            verdict="reject_low",
            top1=top1,
            reason=msg.GATE_FEW_PARAS.format(
                paragraphs=distinct, required=cfg.evidence_min_paras
            ),
            suspected_docs=_suspects(items, cfg.suspected_docs_max),
            degraded=degraded,
            degrade_reason=degrade_reason,
            reject_code=ErrorCode.LOW_CONFIDENCE,
        )

    # partial 优先于 clarify：已经查到一部分时再问用户"你要哪个"是本末倒置，
    # 该先把已查实的部分给出去（§8.3）。
    if uncovered:
        return GateDecision(
            verdict="partial",
            evidence=tuple(items[: evidence_cap or len(items)]),
            top1=top1,
            reason=msg.GATE_UNCOVERED.format(items=", ".join(uncovered)),
            uncovered=tuple(uncovered),
            degraded=degraded,
            degrade_reason=degrade_reason,
        )

    needs, why = _needs_clarify(items, cfg)
    if needs and not degraded:
        options = decorate_clarify_options(
            items, doc_names=doc_names, max_options=cfg.clarify_options_max
        )
        if len(options) >= 2:
            return GateDecision(
                verdict="clarify",
                clarify_options=options,
                top1=top1,
                reason=why,
                borderline_used=borderline_used,
                degraded=degraded,
                degrade_reason=degrade_reason,
            )

    window = items[: evidence_cap or len(items)]
    decision = GateDecision(
        verdict="pass",
        evidence=tuple(window),
        top1=top1,
        reason=f"top1={top1:.3f} >= {cfg.reject_low}",
        borderline_used=borderline_used,
        degraded=degraded,
        degrade_reason=degrade_reason,
    )
    log_event(
        logger,
        "gate_decided",
        verdict=decision.verdict,
        top1=None if decision.top1 is None else round(decision.top1, 4),
        evidence=len(decision.evidence),
        degraded=decision.degraded,
        borderline=decision.borderline_used,
    )
    return decision


def _suspects(
    candidates: Sequence[Candidate], limit: int
) -> tuple[SuspectedDoc, ...]:
    """拒答时给的疑似文档：按文档聚合取最高分，最多 `limit` 份（§8.1）。.

    只含"有权限的文档"这件事由上游保证——进到这里的候选全都经过了前置过滤
    （§5.2 / §7.1），所以这里不再过滤一次。多过滤一次会让"列表为空"有两种原因
    （没权限 / 没命中），审计就分不清了。
    """
    best: dict[str, float] = {}
    for candidate in candidates:
        if not candidate.doc_key:
            continue
        score = effective_score(candidate)
        if score > best.get(candidate.doc_key, -1.0):
            best[candidate.doc_key] = score
    ordered = sorted(best.items(), key=lambda kv: -kv[1])[:limit]
    return tuple(
        SuspectedDoc(doc_key=key, best_score=score) for key, score in ordered
    )


__all__ = [
    "VERDICTS",
    "ClarifyOption",
    "GateConfig",
    "GateDecision",
    "SuspectedDoc",
    "Verdict",
    "decide",
    "decorate_clarify_options",
    "effective_score",
]
