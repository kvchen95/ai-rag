"""Query 理解（详细设计.md §6）。

这一章把"用户随口说的话"变成"一次能执行的检索"。做三件事，顺序不能换：

    normalise_time  →  指代消解  →  同义/缩写/口语扩展  →  单/多跳预判

四条铁律（每条都在代码里有一处对应的检查，不靠注释提醒）：

1. **改写产物不参与事实认定，只用于检索**。§6.2 原话："改写只做同义词扩展、缩写
   展开、口语转书面"。所以 `QueryPlan.retrieval_text` 只交给检索层，
   `QueryPlan.original` 原样带进审计与最终答案——同一句话在两处各有一份，
   谁想拿改写后的句子去生成，会先看到 `original` 就在旁边。
2. **不改权限**。改写只看问句文本，不吃身份。任何"按用户所属部门补全问句"的写法
   都会把权限判断从 `filters.py` 泄漏到这里，两处迟早不一致。
3. **不改数字**。词表自检（`lexicon.validate_tables`）在构造期扫一遍，表里出现
   数字就直接抛——改一个数字会让 §10.2 的数字对齐拿改写后的数字去核原文，核不上
   整条拒答。
4. **否定不反转**（§6.1 边界表）。"不含住宿的报销"里的"住宿"不参与同义扩展：
   `NEGATION_MARKERS` 命中时跳过该位点。

指代消解（§6.1）刻意保守：只有"上一轮恰好命中一个实体"时才替换。多个候选
→ 触发澄清（`NeedsClarification`）；上一轮是拒答 → 不继承任何实体。宁可查不到
也不猜，因为猜错的代价是把答案指向另一份文档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Sequence

from api.observability import get_logger, log_event
from api.query import lexicon
from api.time_range import TimeNormalisation, normalise_time

logger = get_logger("api.query.understand")

Mode = Literal["single", "multi"]

# 指代词（§6.1 表格第一列的"那它呢""这个流程"）。
# 只收短指代：长句里的"这个"多半是限定词（"这个标准是多少"已经有实体了），
# 当成指代去替换反而会把问句改坏。
_PRONOUNS = re.compile(
    r"那它呢|那这个呢|这个流程|那个流程|这份|那份|它呢|这个|那个|这些|那些|前者|后者"
)
# 触发多跳的说法（§6.3 表格）。
_MULTI_MARKERS = tuple(lexicon.MULTI_HOP_MARKERS)
# 明显超范围（§6.3 边界表最后一行）。
_OUT_OF_SCOPE = tuple(lexicon.OUT_OF_SCOPE_MARKERS)


@dataclass(frozen=True, slots=True)
class RewriteStep:
    """一次替换的留痕。审计要能回答"这句话为什么被改成这样"。"""

    kind: Literal["acronym", "synonym", "colloquial", "pronoun"]
    source: str
    target: str
    position: int

    def as_text(self) -> str:
        return f"{self.source}→{self.target}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "from": self.source,
            "to": self.target,
            "at": self.position,
        }


class NeedsClarification(Exception):
    """指代有多个候选（§6.1"指代不清（多个候选）→ 不替换，触发澄清"）。

    抛异常而不是返回一个 `clarify=True` 字段：调用方必须显式处理它，
    否则"忘了判"会表现为"按原句检索"，而原句里带着一个指代词——检索必然跑偏，
    却不会报错。
    """

    def __init__(self, pronoun: str, candidates: Sequence[str]) -> None:
        self.pronoun = pronoun
        self.candidates = list(candidates)
        super().__init__(f"指代 {pronoun!r} 有多个候选: {self.candidates}")


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """§6.4「这一章交给下一步什么」的字段，一个不多一个不少。"""

    # §6.4 的 `query`：用于检索，不是给用户看的。
    # 名字取 `retrieval_text` 而不是 `query`：字段名要说清它只能用于检索，
    # 否则总有人把它塞进提示词。
    retrieval_text: str
    # §6.4 的 `entities`：供澄清与覆盖检查。
    entities: tuple[str, ...]
    # §6.4 的 `time_range`：已归一。
    time: TimeNormalisation
    # §6.4 的 `mode`：预判，可被检索结果推翻（§8.3 第一跳就找齐时说单跳）。
    mode: Mode
    # §6.4 的 `rewrite_trace`：审计用，**不进提示词**。
    trace: tuple[RewriteStep, ...]
    # 原文：改写不参与事实认定，所以原文必须一直在手边。
    original: str
    # 覆盖检查用的**全部表面形式**（归一后的写法 + 原文里的写法）。
    #
    # 实测撞出来的坑：问句"住宿费和交通费分别…"归一后实体是「住宿费限额」，而库里的
    # 正文写的是"住宿费标准"——拿归一后的词去正文里找，永远找不到，覆盖检查于是
    # 判定"这一跳没填上缺口"，多跳空转到 Max Hops。所以覆盖检查必须两种写法都试：
    # 归一后的（命中新语料）+ 原文的（命中旧语料）。
    coverage_terms: tuple[str, ...] = ()
    # 超范围的问句不检索（§6.3 边界表）：门控直接用 reject_none 收口。
    out_of_scope: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def entities_or_text(self) -> tuple[str, ...]:
        """实体为空时退回检索文本里的领域词，供覆盖检查用。

        覆盖检查（"关键实体是否在证据里出现"）拿不到实体就等于跳过检查，
        那正好是"答非所问"漏过去的那条路。
        """
        return self.entities or (self.retrieval_text,)

    def as_audit(self) -> dict[str, Any]:
        """审计快照。**不含原文**——原文在 `QaLogEntry.question` 里另存一份。"""
        return {
            "retrieval_text": self.retrieval_text,
            "entities": list(self.entities),
            "mode": self.mode,
            "rewrite_trace": [step.as_text() for step in self.trace],
            "time": self.time.as_dict(),
            "out_of_scope": self.out_of_scope,
            "notes": list(self.notes),
        }


# --- 改写 ---------------------------------------------------------------------


def _is_negated(text: str, start: int) -> bool:
    """替换位点前 4 个字符内出现否定标记就是否定句（§6.1）。

    4 个字符是因为中文否定短语最长四个字（"不包括"）。取整句前文会因为
    "不含住宿，但交通费能报多少"把一个正常的同义替换也杀掉。
    """
    window = text[max(0, start - 4) : start]
    return any(marker in window for marker in lexicon.NEGATION_MARKERS)


def rewrite(text: str) -> tuple[str, tuple[RewriteStep, ...]]:
    """同义 / 缩写 / 口语三类替换。**单趟左到右、最长匹配、幂等**。

    为什么必须是单趟而不是"每张表各扫一遍"（实测撞出来的）：逐表替换会级联。
    `住宿标 → 住宿费限额` 之后，同义表里的 `住宿费 → 住宿费限额` 会命中刚替换进去的
    结果，产出 `住宿费限额限额`；再跑一遍还会更长。这类错误不会报错，只会让
    检索串里堆满重复片段，BM25 的 bigram 频率被自己的替换结果污染。

    最长匹配同理：`住宿费标准` 与 `住宿费` 都能命中同一处，先匹配短的会把长词拆成
    两半（`住宿费限额标准`），而正确的替换只有一个（`住宿费限额`）。

    **幂等守卫**（实测撞出来的第二个缺陷）：输入本来就是正式说法时，长的左值
    （`住宿费限额`）先被守住，但它内部的短左值（`住宿费`）仍会被匹配到——
    `住宿费限额是多少` 变成 `住宿费限额限额是多少`。所以匹配前要看一眼
    "替换后会不会产生 `目标 + 别的字` 这种重复"：如果命中的原文已经是某条
    规则的目标词的前缀延伸，就当作已经是正式说法，跳过。

    否定（§6.1）：命中位点前 4 个字里有否定标记就**整段跳过**——跳过的是这一处，
    不是整句，否则一个"不含"会让后面所有同义扩展全部失效。
    """
    # 所有目标词，按长度降序：用来判断"这个位置已经处在正式说法内部"。
    targets = sorted(
        set(lexicon.SYNONYMS.values())
        | set(lexicon.ACRONYMS.values())
        | set(lexicon.COLLOQUIAL.values()),
        key=len,
        reverse=True,
    )
    # 三类表合成一张，按左值长度降序：同一位置总是先试最长的。
    candidates: list[tuple[str, str, str]] = []
    for kind, table in (
        ("colloquial", lexicon.COLLOQUIAL),
        ("acronym", lexicon.ACRONYMS),
        ("synonym", lexicon.SYNONYMS),
    ):
        candidates.extend((key, value, kind) for key, value in table.items())
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    steps: list[RewriteStep] = []
    out: list[str] = []
    pos = 0
    while pos < len(text):
        if _is_negated(text, pos):
            # 否定位点：整段跳过匹配，只把当前字符原样搬过去。
            out.append(text[pos])
            pos += 1
            continue
        matched = False
        for key, target, kind in candidates:
            if not text.startswith(key, pos):
                continue
            if _inside_target(text, pos, key, targets):
                # 命中的原文已经处在某个正式说法内部，替换只会制造重复片段。
                break
            out.append(_join_without_overlap(out, target))
            steps.append(
                RewriteStep(kind=kind, source=key, target=target, position=pos)  # type: ignore[arg-type]
            )
            # 跳过整段被替换的原文：绝不重新扫描替换产物（那就是级联的来源）。
            pos += len(key)
            matched = True
            break
        if not matched:
            out.append(text[pos])
            pos += 1
    return "".join(out), tuple(steps)


def _join_without_overlap(out: list[str], target: str) -> str:
    """把 `target` 接到已输出文本后面，但去掉与末尾重复的部分。

    实测撞出来的第三个缺陷：`报销能报多少` 里 `能报多少 → 报销额度`，直接接上去
    得到 `报销报销额度`。这不报错，但检索串里"报销"的 bigram 频率被自己翻倍，
    BM25 会因此把这条查询的权重压到"报销"这一个词上。

    只在**重叠长度 >= 2** 时合并：单字重叠（"和"、"的"）多半是巧合，
    合并会把正常句子改坏（"报告的额度" + "额度" → 不该变成"报告的额度"）。
    """
    prefix = "".join(out)
    for size in range(min(len(prefix), len(target)), 1, -1):
        if prefix.endswith(target[:size]):
            return target[size:]
    return target


def _inside_target(text: str, pos: int, key: str, targets: Sequence[str]) -> bool:
    """`text[pos:]` 是否以"某个正式说法"开头、且 `key` 只是它的前缀？

    `住宿费限额` 里的 `住宿费`：命中，且从 pos 起是 `住宿费限额` 的开头，
    而 `住宿费限额` 本身就是某条规则的目标词 → 跳过替换。反过来
    `住宿费的报销` 里的 `住宿费`：从 pos 起不是任何目标词的前缀 → 正常替换。

    只对"短的左值"生效：左值等于目标词时（`差标 → 差旅标准`），
    `差标` 不是任何目标的开头，本来就不会误判，加判断只会增加一次无用扫描。
    """
    for target in targets:
        if len(target) <= len(key):
            continue
        if text.startswith(target, pos) and key in target:
            return True
    return False


def extract_entities(text: str) -> tuple[str, ...]:
    """领域词抽取。按出现位置排序、去重，供澄清与覆盖检查。

    为什么不用分词器：分词结果带词典版本，换一版就是另一组实体，覆盖检查会跟着
    漂移。这里的实体集合只来自固定词表，同一段问句永远抽出同一组实体。
    """
    hits: list[tuple[int, str]] = []
    for term in lexicon.DOMAIN_TERMS:
        pos = text.find(term)
        if pos >= 0:
            hits.append((pos, term))
    ordered = [term for _, term in sorted(hits)]
    # 一个词是另一个词的子串时只留长的（"出差"⊂"出差补助"）：短的会同时命中，
    # 让覆盖检查以为有两个实体，其中一个必然在证据里找不到。
    kept: list[str] = []
    for term in ordered:
        if any(term != other and term in other for other in ordered):
            continue
        kept.append(term)
    return tuple(dict.fromkeys(kept))


def detect_mode(text: str) -> tuple[Mode, tuple[str, ...]]:
    """单跳/多跳预判（§6.3）。返回 (模式, 命中的标记)。

    判定不看问句长短，看"答案能不能在一份文档的一段话里找齐"——这句话在代码里
    的落地只能是词表标记，因为"要不要跨文档"在检索前不可能知道。所以这里的结论
    必须可被推翻：§8.3 明确说第一跳找齐了就改回单跳。
    """
    hits = tuple(marker for marker in _MULTI_MARKERS if marker in text)
    return ("multi" if hits else "single"), hits


def is_out_of_scope(text: str) -> str | None:
    """超范围的问句直接拒答、不检索（§6.3 边界表）。返回命中的标记或 None。"""
    for marker in _OUT_OF_SCOPE:
        if marker in text:
            return marker
    return None


def resolve_pronoun(
    text: str, previous_entities: Sequence[str] | None
) -> tuple[str, RewriteStep | None]:
    """指代消解（§6.1）。保守到几乎不动手。

    三种情形：
    - 没有指代词 → 原样返回；
    - 有指代词、上一轮恰好一个实体 → 替换（唯一的机会）；
    - 有指代词、上一轮零个或多个候选 → 抛 `NeedsClarification`，不猜。

    "上一轮是拒答"由调用方表达成 `previous_entities=None`：拒答后不该继承任何实体
    （§6.1 边界表），而"没传"和"传了空"在语义上是同一件事，所以两者都走"零候选"。
    """
    match = _PRONOUNS.search(text)
    if match is None:
        return text, None
    candidates = [item for item in (previous_entities or ()) if item]
    if len(candidates) != 1:
        raise NeedsClarification(match.group(0), candidates)
    target = candidates[0]
    replaced = text[: match.start()] + target + text[match.end() :]
    return replaced, RewriteStep(
        kind="pronoun", source=match.group(0), target=target, position=match.start()
    )


def understand(
    question: str,
    *,
    previous_entities: Sequence[str] | None = None,
    now: date | None = None,
    fiscal_start_month: int = 1,
) -> QueryPlan:
    """一句问话 → 一次可执行的检索（§6 的出口）。

    `previous_entities` 是多轮的唯一入口：传上一轮 `QueryPlan.entities`。
    不传就是单轮，指代词会触发澄清而不是被猜。
    """
    original = (question or "").strip()
    if not original:
        raise ValueError("question 不能为空")

    trace: list[RewriteStep] = []
    notes: list[str] = []

    # 1. 指代消解。放在最先：它可能抛出澄清，后面几步的产物就都不该存在。
    operated, pronoun_step = resolve_pronoun(original, previous_entities)
    if pronoun_step is not None:
        trace.append(pronoun_step)

    # 2. 时间归一。在改写之前跑：词表里若混进"上个月"这类词，改写会把它换掉，
    #    时间归一就再也认不出来了。用 `operated` 而不是 `original`，这样
    #    「那它呢」被换成「差旅管理办法」之后，时间词仍能被看到。
    time_result = normalise_time(operated, now=now, fiscal_start_month=fiscal_start_month)
    if time_result.note:
        notes.append(time_result.note)
    # 补了年份的标注挂在 `range.note` 上（TimeRange 自己的字段），不在
    # `TimeNormalisation.note` 里。§5.3 要求它"一路带到答案"，所以这里必须
    # 把它提上来——只挂在 range 上的话，下游要取两处才拿得到，迟早漏一处。
    if time_result.range is not None and time_result.range.note:
        notes.append(time_result.range.note)

    # 3. 同义 / 缩写 / 口语。
    rewritten, steps = rewrite(operated)
    trace.extend(steps)

    # 4. 实体与跳数。
    entities = extract_entities(rewritten)
    # 原文里抽到的实体也要留在覆盖检查里：归一后的写法未必出现在库里（见
    # `QueryPlan.coverage_terms` 的说明）。顺序是"归一后的在前"——它能命中新语料，
    # 而原文写法是兜底。
    original_entities = extract_entities(operated)
    coverage_terms = tuple(
        dict.fromkeys([*entities, *original_entities, *(s.source for s in steps)])
    )
    if not entities:
        entities = extract_entities(operated)
    mode, multi_hits = detect_mode(rewritten)
    if multi_hits:
        notes.append(f"多跳预判命中: {', '.join(multi_hits)}")

    scope_hit = is_out_of_scope(rewritten)
    if scope_hit:
        notes.append(f"超出库范围: {scope_hit}")

    plan = QueryPlan(
        retrieval_text=rewritten,
        entities=entities,
        coverage_terms=coverage_terms,
        time=time_result,
        mode=mode,
        trace=tuple(trace),
        original=original,
        out_of_scope=scope_hit is not None,
        notes=tuple(notes),
    )
    log_event(
        logger,
        "query_understood",
        # 日志里不打原文（可能带业务信息），只打口径；原文进审计表。
        mode=plan.mode,
        entities=list(plan.entities),
        rewrites=[step.as_text() for step in plan.trace],
        has_time_window=plan.time.has_window,
        out_of_scope=plan.out_of_scope,
    )
    return plan


__all__ = [
    "Mode",
    "NeedsClarification",
    "QueryPlan",
    "RewriteStep",
    "detect_mode",
    "extract_entities",
    "is_out_of_scope",
    "resolve_pronoun",
    "rewrite",
    "understand",
]
