"""复合问拆分：只走确定性规则，拆不出就保留原问。

拆分的单位是**可独立回答的问题**，不是短语。没有主体/指标/时间 scope 的
候选一律退回原问；尤其「同比增长多少」依赖基期与指标，绝不能单独检索。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Sequence

from api.temporal.types import TemporalScope
from api.time_range import normalise_time

# 指标词只用于判断「这不是一个碎片」，不参与事实生成。
_METRIC = re.compile(
    r"营业收入|营收|净利润|归母净利润|扣非净利润|毛利率|净利率|市场份额|"
    r"市场地位|市场规模|排名|占有率|销量|产量|收入|利润|资产|负债|现金流|"
    r"估值|价格|成本|费用|研发投入|同比|环比|增速|增长|预测|预计|展望|地位|"
    r"是多少|多少"
)
_TIME = re.compile(
    r"\d{4}|本年度|本季度|本年|去年|今年|明年|上年|同期|上半年|下半年|"
    r"前三季度|一季度|二季度|三季度|四季度|季度|年度|月份|月初|月底|上周|下周|"
    r"上个月|下个月|本月|最近|目前|当前|未来|届时|当时"
)
# 这些片段需要基期/上下文，拆出来没有独立语义。
_DERIVED_ONLY = re.compile(
    r"^(?:同比|环比|增速|增长|下降|占比|毛利率|净利率)(?:增长|下降|是多少|多少)?[？?。！!]*$"
)
_SPLIT = re.compile(r"\s*(?:\+|＋|；|;|。|！|!|\n)\s*")
_AND = re.compile(r"\s*(?:以及|并且|同时|和|与|及)\s*")
_STRIP = " \t\r\n，,、+＋；;。！!？?：:"


@dataclass(frozen=True, slots=True)
class SubQuestion:
    """独立检索单元。空 scope 是「未限定时间」，不是缺失时间。"""

    id: str
    question: str
    subject: str
    metric: str
    temporal: TemporalScope
    source: str = "rule"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "subject": self.subject,
            "metric": self.metric,
            "time_scope": self.temporal.as_dict(),
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class Decomposition:
    original: str
    subquestions: tuple[SubQuestion, ...]
    decomposed: bool
    notes: tuple[str, ...] = ()

    @property
    def is_compound(self) -> bool:
        return self.decomposed and len(self.subquestions) > 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "decomposed": self.decomposed,
            "notes": list(self.notes),
            "subquestions": [item.as_dict() for item in self.subquestions],
        }


def _scope(question: str, *, now: date | None) -> TemporalScope:
    from api.temporal.normalize import build_temporal_scope

    return build_temporal_scope(normalise_time(question, now=now))


def _subject_metric(question: str) -> tuple[str, str] | None:
    text = question.strip(_STRIP)
    if not _METRIC.search(text):
        return None
    # 主体边界取第一个「的/在/于」；后面的内容整体作为指标，避免把公司名截碎。
    boundary = re.search(r"的|在|于", text)
    subject = text[: boundary.start()].strip() if boundary else text
    subject = re.sub(r"^(?:请问|请|帮我|查一下|查询|了解)", "", subject).strip()
    metric = text[boundary.end() :].strip() if boundary else text
    if len(subject) < 2 or not metric or not _METRIC.search(metric):
        return None
    return subject, metric


def _make_subquestion(question: str, index: int, *, now: date | None, source: str) -> SubQuestion | None:
    text = question.strip(_STRIP)
    if not text or _DERIVED_ONLY.fullmatch(text):
        return None
    parts = _subject_metric(text)
    if parts is None:
        return None
    subject, metric = parts
    return SubQuestion(
        id=f"sq{index}",
        question=text + "？",
        subject=subject,
        metric=metric,
        temporal=_scope(text, now=now),
        source=source,
    )


def _one(question: str, *, now: date | None, notes: Iterable[str] = ()) -> Decomposition:
    sub = _make_subquestion(question, 1, now=now, source="original")
    # 原问不要求指标词（可能是指代或超范围问题）；QueryPlan 会处理后续澄清/拒答。
    if sub is None:
        sub = SubQuestion(
            id="sq1",
            question=question.strip(),
            subject="",
            metric="",
            temporal=_scope(question, now=now),
            source="original",
        )
    return Decomposition(question.strip(), (sub,), False, tuple(notes))


def _with_subject(fragment: str, subject: str) -> str:
    """给省略主体的分句补主体；已有主体则不覆盖。"""
    if not subject or _subject_metric(fragment) is not None:
        return fragment
    if re.match(r"^(?:同比|环比|以及|并且|同时|和|与|及)", fragment):
        return fragment
    return f"{subject}的{fragment}"


def _split_and(question: str, *, now: date | None) -> tuple[SubQuestion, ...] | None:
    parts = [part.strip(_STRIP) for part in _AND.split(question) if part.strip(_STRIP)]
    if len(parts) < 2:
        return None
    first_parts = _subject_metric(parts[0])
    if first_parts is None:
        return None
    subject = first_parts[0]
    filled = [_with_subject(part, subject) for part in parts]
    subs = tuple(
        sub
        for index, part in enumerate(filled, 1)
        if (sub := _make_subquestion(part, index, now=now, source="rule")) is not None
    )
    return subs if len(subs) == len(parts) and len(subs) > 1 else None


def _rule_split(question: str, *, now: date | None) -> tuple[SubQuestion, ...] | None:
    pieces = [part.strip(_STRIP) for part in _SPLIT.split(question) if part.strip(_STRIP)]
    if len(pieces) < 2:
        return None
    subs = tuple(
        sub
        for index, part in enumerate(pieces, 1)
        if (sub := _make_subquestion(part, index, now=now, source="rule")) is not None
    )
    if len(subs) == len(pieces) and len(subs) > 1:
        return subs
    # 「A的市场地位和 2030 市场规模预测」：左句提供主体，右侧缺主体时只补主体。
    if len(pieces) == 2 and _subject_metric(pieces[0]) is not None:
        filled = _with_subject(pieces[1], _subject_metric(pieces[0])[0])  # type: ignore[index]
        sub = _make_subquestion(filled, 2, now=now, source="rule")
        first = _make_subquestion(pieces[0], 1, now=now, source="rule")
        if first is not None and sub is not None:
            return (first, sub)
    return None


def decompose_question(
    question: str,
    *,
    now: date | None = None,
) -> Decomposition:
    """返回通过完整性校验的子问题；不合格则保留原问，绝不返回碎片。"""
    original = (question or "").strip()
    if not original:
        return Decomposition("", (), False, ("空问题",))
    # 原因/为什么是同一事实的追问，拆开会让 gate 把“原因”当成独立检索单元。
    if any(marker in original for marker in ("原因", "为什么", "为何")):
        return _one(original, now=now, notes=("原因追问随主问题整体检索",))
    subs = _rule_split(original, now=now)
    if subs is None:
        subs = _split_and(original, now=now)
    if subs is not None:
        return Decomposition(original, subs, True)

    return _one(original, now=now, notes=("无确定性拆分候选，保留原问",))


decompose = decompose_question
split_compound = decompose_question

__all__ = ["Decomposition", "SubQuestion", "decompose", "decompose_question", "split_compound"]
