"""五道校验与降级决策（详细设计.md §10.1 / §10.3）。

五道按成本从低到高排，任何一道不过就**停**，不必跑完（§10.1 首句）。这个"停"不是
优化，是判据的一部分：第五道的"同实体同单位两个值"只有在前四道都过了才有意义——
给一个引用越界的草稿检查内部一致性，得出的结论无从解释。

| 道次 | 规则 | 不过时 |
|---|---|---|
| 一 | 引用编号存在且在范围内 | 拒答 |
| 二 | 每条主张至少一个引用 | 拒答 |
| 三 | 数字与专名可在被引证据定位 | 拒答 |
| 四 | 判别器判"支持" | 拒答 |
| 五 | 同实体同单位无两个值 | 拒答 |

三档降级（§10.3）不是按"第几道失败"分的，而是按**用户接下来要做什么**分的：

| 档位 | 触发 | 给用户什么 |
|---|---|---|
| 只给原文 | 数字或判别不过 | 原文片段 + 页码，明确"未形成结论" |
| 部分结论 | 多跳未覆盖全部 | 已查实部分 + 未覆盖清单 |
| 系统异常 | 模型不可用、超时 | 一句"稍后重试"，不暴露内部细节 |

所以这里的映射是显式的、可测的：每一道失败都自带 `tier` 与 `code`，不由调用方
按"第几道"再推一次。第一道（引用越界）与第二道（无引用主张）落"只给原文"而不是
"系统异常"——它们都是**内容**问题，用户拿到原文比拿到"稍后重试"有用。

三条容易写错的边界：

1. **`status=unknown` 不是校验失败**（§9.2 规则 3）。模型自己说"证据不足"时
   不该再跑五道校验，也不该报 `unverifiable`——那是它按提示词正确行事。走
   `no_evidence`，因为那正是 §11.3 里"未找到可验证依据"这一档。
2. **判别器 `skipped` 与 `NOT_SUPPORTED` 处置相同但审计要能分辨**。没配判别模型
   意味着这套系统少了一道闸，这件事必须被看见，不能被"答案反正拒了"掩盖。
3. **`partial` 档不经过五道校验**：多跳未覆盖全部时，输出的是"已查实事实 +
   未覆盖清单"，它不是一段待校验的结论。所以那一档由调用方（编排层）决定，
   这里的 `FALLBACK_PARTIAL` 只提供给它的构造函数。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from api.errors import ErrorCode
from api.generate.evidence import Evidence, EvidencePack
from api.generate.judge import JudgeResult, judge
from api.generate.llm import Draft, LlmClient
from api.observability import get_logger, log_event
from api.verify.numbers import (
    find_value_conflicts,
    locate_proper_nouns,
    locate_quantities,
)
from api.verify.sentences import Sentence, merge_claim_sentences, parse_citations

logger = get_logger("api.verify.checks")

# 五道的名字。写成常量是因为它们会进审计与日志，拼错就看不出是哪一道失败的。
CHECK_CITATION_RANGE = "citation_range"
CHECK_EVERY_CLAIM = "every_claim_cited"
CHECK_GROUNDING = "numeric_and_named"
CHECK_NLI = "nli_support"
CHECK_CONSISTENCY = "internal_consistency"
# 不是第六道：`status=unknown` 在五道之前就收口了，但它必须有一个可命名的失败原因。
CHECK_DRAFT_STATUS = "draft_status"

CHECKS: tuple[str, ...] = (
    CHECK_CITATION_RANGE,
    CHECK_EVERY_CLAIM,
    CHECK_GROUNDING,
    CHECK_NLI,
    CHECK_CONSISTENCY,
)

# 三档降级。名字即用户看到的东西（§10.3 的"给用户什么"那一列）。
FALLBACK_ORIGINAL_ONLY = "original_only"  # 档一：只给原文
FALLBACK_PARTIAL = "partial"  # 档二：部分结论
FALLBACK_SYSTEM = "system"  # 档三：系统异常

DegradeTier = Literal["original_only", "partial", "system"]

# 各道失败落在哪一档。写成表而不是函数里的 if：新增一道校验时，
# 忘了给它定档会在这里缺一个键，而缺键比走错分支更容易被发现。
TIER_BY_CHECK: dict[str, DegradeTier] = {
    CHECK_DRAFT_STATUS: FALLBACK_ORIGINAL_ONLY,
    CHECK_CITATION_RANGE: FALLBACK_ORIGINAL_ONLY,
    CHECK_EVERY_CLAIM: FALLBACK_ORIGINAL_ONLY,
    CHECK_GROUNDING: FALLBACK_ORIGINAL_ONLY,
    CHECK_NLI: FALLBACK_ORIGINAL_ONLY,
    CHECK_CONSISTENCY: FALLBACK_ORIGINAL_ONLY,
}

# 各道失败对应的 §11.3 错误码。用户看到的文案都一样（"本次不作结论，附原文"），
# 细分只进 detail 与审计——那是给运维的。
CODE_BY_CHECK: dict[str, ErrorCode] = {
    CHECK_DRAFT_STATUS: ErrorCode.NO_EVIDENCE,
    CHECK_CITATION_RANGE: ErrorCode.UNVERIFIABLE,
    CHECK_EVERY_CLAIM: ErrorCode.UNVERIFIABLE,
    CHECK_GROUNDING: ErrorCode.UNVERIFIABLE,
    CHECK_NLI: ErrorCode.UNVERIFIABLE,
    CHECK_CONSISTENCY: ErrorCode.UNVERIFIABLE,
}


@dataclass(frozen=True, slots=True)
class CheckFailure:
    """一道不过。`check` 是道次名，`detail` 是给人看的一句话。"""

    check: str
    detail: str
    # 具体哪几句 / 哪些编号出的问题。报告里要能指到内容。
    offenders: tuple[str, ...] = ()

    @property
    def tier(self) -> DegradeTier:
        return TIER_BY_CHECK[self.check]

    @property
    def code(self) -> ErrorCode:
        return CODE_BY_CHECK[self.check]

    def as_audit(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "detail": self.detail,
            "offenders": list(self.offenders),
            "tier": self.tier,
            "code": self.code.value,
        }


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """五道校验的结论 + 降级决策。"""

    passed: bool
    # 实际跑到第几道：pass 时是全五道，失败时含失败的那一道。
    checks_run: tuple[str, ...]
    failure: CheckFailure | None = None
    judge: JudgeResult | None = None
    # 校验通过时给用户的正文（= 草稿 answer）。
    answer: str = ""
    # 校验通过时的引用编号（按出现顺序）。
    citations: tuple[int, ...] = ()
    elapsed_ms: float = 0.0
    # 被引证据（附录 C.2 的 citations 事件要用它填 doc/page/bbox/snippet）。
    cited_evidence: tuple[Evidence, ...] = ()

    @property
    def gate2_verdict(self) -> str:
        """`qa_log.gate2_verdict` 的三个取值：supported / not_supported / skipped。

        `skipped` 的判定必须看 `judge.skipped` 而不是 `judge is None`：没配判别器时
        `verify_draft` 也会建一个 `verdict="skipped"` 的结果（它要进审计），
        只看 `None` 会把这个结果读成 `not_supported`——于是"少了一道闸"在审计里
        长得像"判别器说答案没据"，两种处置完全不同。
        """
        if self.judge is None:
            return "skipped"
        if self.judge.skipped:
            return "skipped"
        return "supported" if self.judge.supported else "not_supported"

    @property
    def fallback_tier(self) -> DegradeTier | None:
        return None if self.passed else (self.failure.tier if self.failure else FALLBACK_SYSTEM)

    @property
    def error_code(self) -> ErrorCode | None:
        return None if self.passed else (self.failure.code if self.failure else ErrorCode.INTERNAL)

    def as_audit(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks_run": list(self.checks_run),
            "failure": None if self.failure is None else self.failure.as_audit(),
            "judge": None if self.judge is None else self.judge.as_audit(),
            "fallback": self.fallback_tier,
            "gate2": self.gate2_verdict,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def referable_evidence(pack: EvidencePack, numbers: Sequence[int]) -> tuple[Evidence, ...]:
    """把编号翻成证据。未知编号忽略（第一道已经把越界的拦掉了）。"""
    by_n = pack.by_number()
    return tuple(
        item for item in (by_n.get(number) for number in dict.fromkeys(numbers)) if item is not None
    )


def _evidence_text(items: Sequence[Evidence]) -> str:
    return "\n".join(item.text for item in items)


def _clip(text: str, limit: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def check_citation_range(draft: Draft, pack: EvidencePack) -> CheckFailure | None:
    """第一道：每个 `[n]` 必须落在证据编号范围内（纯字符串比对）。

    "编号解析失败按不存在处理"（§10.1 表格）在这里具体化成：`citations` 字段与
    `answer` 正文两处都扫。只扫一处会漏——模型完全可能把字段改对、正文留着旧的
    编号，而用户看到的是正文里的角标。
    """
    numbers = set(pack.numbers())
    offenders: list[str] = []
    out_of_range = [n for n in draft.citations if n not in numbers]
    offenders.extend(f"[{n}]" for n in sorted(set(out_of_range)))
    for number in parse_citations(draft.answer):
        if number not in numbers and f"[{number}]" not in offenders:
            offenders.append(f"[{number}]")
    if offenders:
        return CheckFailure(
            check=CHECK_CITATION_RANGE,
            detail=f"引用编号不在证据范围内（证据编号 1..{len(numbers)}）",
            offenders=tuple(offenders[:8]),
        )
    _ = pack
    return None


def _draft_sentences(draft: Draft) -> tuple[Sentence, ...]:
    return merge_claim_sentences(draft.answer, tuple(claim.text for claim in draft.claims))


def check_every_claim_cited(draft: Draft, _pack: EvidencePack) -> CheckFailure | None:
    """第二道：每条主张至少一个引用，"主张为空也算失败"（§10.1 表格）。

    校验单位是**句子**（见 `api/verify/sentences.py` 的说明）：`claims` 只是模型
    自报的切法，answer 里可能还有它没报出来的句子。两条路取并集。
    """
    sentences = _draft_sentences(draft)
    if not sentences:
        return CheckFailure(
            check=CHECK_EVERY_CLAIM, detail="草稿没有任何可校验的主张（主张为空）"
        )
    naked = [item.text for item in sentences if item.empty or not item.citations]
    if naked:
        return CheckFailure(
            check=CHECK_EVERY_CLAIM,
            detail=f"{len(naked)} 句没有引用编号",
            offenders=tuple(naked[:5]),
        )
    return None


def check_grounding(draft: Draft, pack: EvidencePack) -> CheckFailure | None:
    """第三道：数字与专名能在**被引**证据里定位（§10.1 / §10.2）。

    只查被引证据，不查整包：一句只引了 [1] 的话，在 [2] 里找到的"500 元"不算
    "有据"——那正是"引错编号"的情形，而它的后果是用户点角标跳到一段不相干原文。
    """
    offenders: list[str] = []
    for sentence in _draft_sentences(draft):
        cited = referable_evidence(pack, sentence.citations)
        if not cited:
            # 没有可查的证据只可能因为编号越界，而第一道已经拦了。
            continue
        blob = _evidence_text(cited)
        missing_numbers, _ = locate_quantities(sentence.bare, blob)
        missing_names, _ = locate_proper_nouns(sentence.bare, blob)
        parts: list[str] = []
        if missing_numbers:
            parts.append("数字 " + "、".join(item.raw for item in missing_numbers))
        if missing_names:
            parts.append("专名 " + "、".join(missing_names))
        if parts:
            offenders.append(f"「{_clip(sentence.text)}」里的{'、'.join(parts)}在被引证据里定位不到")
    if offenders:
        return CheckFailure(
            check=CHECK_GROUNDING,
            detail=f"{len(offenders)} 句的数字或专名无法在被引证据里定位",
            offenders=tuple(offenders[:5]),
        )
    return None


def check_nli(
    client: LlmClient | None, question: str, draft: Draft, pack: EvidencePack
) -> tuple[CheckFailure | None, JudgeResult]:
    """第四道：判别器判"支持"（附录 D.4）。

    送进去的"证据"只含**被引**的那几条，按编号升序拼接：判别器看到的东西必须与
    我们声称的引用一致，多给它几条会让它替模型辩护（"别的证据里好像有"）。
    """
    result = judge(client, question, _evidence_text(referable_evidence(pack, draft.citations)), draft.answer)
    if result.supported:
        return None, result
    return (
        CheckFailure(
            check=CHECK_NLI,
            detail="判别器判不支持" if not result.skipped else "判别器未运行（未配置），按不支持处理",
            offenders=(result.reason,) if result.reason else (),
        ),
        result,
    )


def check_consistency(draft: Draft, _pack: EvidencePack) -> CheckFailure | None:
    """第五道：同实体同单位不出现两个值（§10.1 / 纯规则，不调模型）。"""
    conflicts = find_value_conflicts([item.bare for item in _draft_sentences(draft)])
    if conflicts:
        return CheckFailure(
            check=CHECK_CONSISTENCY,
            detail=f"{len(conflicts)} 处同一属性出现两个值",
            offenders=tuple(
                f"{attribute} 同时是 {left} 与 {right}（单位 {unit}）"
                for attribute, unit, left, right in conflicts
            )[:5],
        )
    return None


def verify_draft(
    draft: Draft,
    pack: EvidencePack,
    *,
    question: str,
    judge_client: LlmClient | None = None,
) -> VerificationReport:
    """跑五道校验。任何一道不过就停（§10.1 首句）。"""
    started = time.perf_counter()

    if not draft.answered:
        # §9.2 规则 3：模型自己说"证据不足"是按提示词正确行事，不是校验失败。
        return VerificationReport(
            passed=False,
            checks_run=(),
            failure=CheckFailure(
                check=CHECK_DRAFT_STATUS,
                detail="模型判定证据不足（status=unknown）",
                offenders=(_clip(draft.answer),) if draft.answer else (),
            ),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    ran: list[str] = []
    judge_result: JudgeResult | None = None
    for check in CHECKS:
        ran.append(check)
        if check == CHECK_CITATION_RANGE:
            failure = check_citation_range(draft, pack)
        elif check == CHECK_EVERY_CLAIM:
            failure = check_every_claim_cited(draft, pack)
        elif check == CHECK_GROUNDING:
            failure = check_grounding(draft, pack)
        elif check == CHECK_NLI:
            failure, judge_result = check_nli(judge_client, question, draft, pack)
        else:
            failure = check_consistency(draft, pack)
        if failure is not None:
            return _failed(failure, tuple(ran), started, judge_result, pack, draft)

    # 五道全过。第四道的结论必须带出去（`gate2_verdict` 与拒绝理由都靠它）。
    # 这里不能靠"某一道 return"收尾：第四道通过之后还有第五道要跑，提前 return
    # 会让第五道永远不执行——而那正是"内部一致"这道唯一能抓住自相矛盾的答案的地方。
    if judge_result is None:  # pragma: no cover - CHECKS 里一定有 NLI
        raise AssertionError("五道校验没有覆盖到 NLI 一道")
    return VerificationReport(
        passed=True,
        checks_run=tuple(ran),
        judge=judge_result,
        answer=draft.answer,
        citations=draft.citations,
        cited_evidence=referable_evidence(pack, draft.citations),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


def _failed(
    failure: CheckFailure,
    ran: tuple[str, ...],
    started: float,
    judge_result: JudgeResult | None,
    pack: EvidencePack,
    draft: Draft,
) -> VerificationReport:
    log_event(
        logger,
        "verification_failed",
        level=30,
        check=failure.check,
        tier=failure.tier,
        code=failure.code.value,
        offenders=len(failure.offenders),
    )
    return VerificationReport(
        passed=False,
        checks_run=ran,
        failure=failure,
        judge=judge_result,
        # 降级时给出的原文来自**草稿引用的证据**：那是系统认为与问题相关的部分，
        # 不是整包（整包会把不相关的内容一起推给用户，等于用噪声掩盖"没结论"）。
        cited_evidence=referable_evidence(pack, draft.citations or pack.numbers()),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )


__all__ = [
    "CHECKS",
    "CHECK_CITATION_RANGE",
    "CHECK_CONSISTENCY",
    "CHECK_DRAFT_STATUS",
    "CHECK_EVERY_CLAIM",
    "CHECK_GROUNDING",
    "CHECK_NLI",
    "CODE_BY_CHECK",
    "CheckFailure",
    "DegradeTier",
    "FALLBACK_ORIGINAL_ONLY",
    "FALLBACK_PARTIAL",
    "FALLBACK_SYSTEM",
    "TIER_BY_CHECK",
    "VerificationReport",
    "check_citation_range",
    "check_consistency",
    "check_every_claim_cited",
    "check_grounding",
    "check_nli",
    "referable_evidence",
    "verify_draft",
]
