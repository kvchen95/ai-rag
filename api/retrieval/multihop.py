"""多跳编排（详细设计.md §8.3）。

多跳不是"多查几次"，是"带着已查实的事实去查下一件事"。所以这个模块的核心不是
循环，而是**黑板**：一张只放确定事实的表。

黑板的三条纪律（§8.3 原话"不放候选、不放分数、不放模型推测"）：

1. 每条事实必须带切片编号、页码、跳次、原文摘录 —— 少了任何一项，后面就无法
   "回查这条结论从哪来"，而那正是断链时要说清的东西；
2. 分数不进口袋。分数是**这一跳**的判定依据，进黑板就会被当成事实的一部分；
3. 推测不进口袋。任何"可能是""一般"的句子都不该由这里产生——这个模块根本
   不生成文本，它只搬运已定分的事实条目。所以"推测"在结构上就进不来。

三个停止条件（§8.3："谁先满足谁生效"），顺序也就是代价顺序：

    a. 本跳不过阈      → 断链（`partial` 或 `reject_none`）
    b. 证据齐了        → `pass`
    c. 跳数到 Max      → `partial`

为什么"第一跳就断链"是拒答而不是 partial（§8.3 边界表）：一跳都没查实，
"已查实的部分"是空集，给空集 + 断链说明与一句"查不到"没有区别，反而让用户
以为系统查了很多地方。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Sequence

from api.observability import get_logger, log_event

logger = get_logger("api.retrieval.multihop")

StopReason = Literal["fulfilled", "max_hops", "broken", "empty", "conflict"]


@dataclass(frozen=True, slots=True)
class Fact:
    """黑板条目：一条确定事实。字段与 §8.3 一一对应，不多不少。"""

    claim: str          # 一句原文摘录（不是模型转述）
    chunk_id: str
    page_num: int
    hop: int            # 第几跳查到的（1-based）
    doc_key: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "chunk_id": self.chunk_id,
            "page_num": self.page_num,
            "hop": self.hop,
            "doc_key": self.doc_key,
        }


@dataclass(frozen=True, slots=True)
class HopOutcome:
    """一跳的结果。`gap` 是本跳开始时缺什么（也是拆子问题的依据）。"""

    hop: int
    sub_question: str
    gap: str
    passed: bool
    facts: tuple[Fact, ...] = ()
    top1: float | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "hop": self.hop,
            "sub_question": self.sub_question,
            "gap": self.gap,
            "passed": self.passed,
            "top1": None if self.top1 is None else round(self.top1, 6),
            "facts": [fact.as_dict() for fact in self.facts],
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class MultiHopResult:
    """多跳的最终产物。`facts` 就是交给生成层的"已查实部分"。"""

    facts: tuple[Fact, ...]
    hops: tuple[HopOutcome, ...]
    stop: StopReason
    uncovered: tuple[str, ...] = ()
    # 断链点：断在第几跳、缺什么。§8.3 要求"断在哪一步"必须能交出去。
    broken_at: int | None = None

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    @property
    def fulfilled(self) -> bool:
        return self.stop == "fulfilled" and not self.uncovered

    def as_audit(self) -> dict[str, Any]:
        return {
            "stop": self.stop,
            "hops": self.hop_count,
            "facts": len(self.facts),
            "uncovered": list(self.uncovered),
            "broken_at": self.broken_at,
            "detail": [hop.as_dict() for hop in self.hops],
        }


class Blackboard:
    """只放确定事实的黑板（§8.3）。

    `add` 的唯一入口要求整条事实齐全，而不是先建一条后补字段：分数先落地再被
    "顺手"塞进来，是这类黑板最常见的腐化方式。
    """

    def __init__(self) -> None:
        self._facts: list[Fact] = []
        self._chunk_ids: set[str] = set()

    def add(self, fact: Fact) -> bool:
        """加一条事实。同一 chunk 只说一次（返回 False 表示已在黑板上）。

        为什么按 chunk_id 去重而不是按 claim 文本：同一条原文换个引用方式还是
        同一条事实，按文本去重会让"同一句被引两次"变成两条，覆盖检查于是以为
        缺口被填上了。
        """
        if fact.chunk_id in self._chunk_ids:
            return False
        self._chunk_ids.add(fact.chunk_id)
        self._facts.append(fact)
        return True

    def extend(self, facts: Sequence[Fact]) -> int:
        return sum(1 for fact in facts if self.add(fact))

    @property
    def facts(self) -> tuple[Fact, ...]:
        return tuple(self._facts)

    def chunk_ids(self) -> tuple[str, ...]:
        return tuple(fact.chunk_id for fact in self._facts)

    def __len__(self) -> int:
        return len(self._facts)


# 一跳的动作签名：给它"这一跳要查什么"，返回 HopOutcome。
# 做成回调而不是内联检索：多跳的**编排**（停止条件、断链、黑板）与**每一跳怎么查**
# 是两件事，前者要能在没有 Qdrant/PG 的情况下被穷举测试，后者需要真服务。
HopRunner = Callable[[int, str, str], HopOutcome]
# 缺口检测：给黑板当前状态，返回还缺什么（空 tuple = 齐了）。
GapProbe = Callable[[Blackboard], Sequence[str]]
# 子问题拆分：给缺口，返回这一跳要问什么。
SubQuestioner = Callable[[str], str]


def run_multihop(
    *,
    max_hops: int,
    runner: HopRunner,
    gap_probe: GapProbe,
    sub_questioner: SubQuestioner,
    initial_gap: Sequence[str] = (),
) -> MultiHopResult:
    """跑多跳，返回事实与停止原因（§8.3）。

    每跳都是"先判缺口 → 拆子问题 → 查 → 过阈判定 → 写黑板"这五步，顺序不能变：
    先查再判缺口会让这一跳查到的东西与缺口无关（那正是"多查几次"而不是多跳）。

    断链的两种落点（§8.3）：
    - 第一跳就断 → `broken` + `facts` 为空（调用方据此 `reject_none`）；
    - 中途断     → `broken` + 已有事实（调用方据此 `partial`）。
    """
    if max_hops < 0:
        raise ValueError(f"max_hops 必须 >= 0，实际 {max_hops}")
    board = Blackboard()
    hops: list[HopOutcome] = []
    gap = tuple(initial_gap)
    stop: StopReason = "max_hops"
    broken_at: int | None = None

    for hop in range(1, max_hops + 1):
        gap = tuple(gap_probe(board))
        if not gap:
            stop = "fulfilled"
            break
        target = gap[0]
        sub_question = sub_questioner(target)
        outcome = runner(hop, sub_question, target)
        hops.append(outcome)

        if not outcome.passed:
            stop = "broken"
            broken_at = hop
            if not board.facts:
                # 第一跳就断链 → §8.3 边界表：直接拒答，不进多跳。
                stop = "empty"
            break

        # 子问题与已查实事实冲突 → 停，输出冲突说明，不强行统一（§8.3 边界表）。
        conflict = _conflict(outcome, board)
        if conflict is not None:
            stop = "conflict"
            broken_at = hop
            log_event(
                logger,
                "multihop_conflict",
                level=30,
                hop=hop,
                chunk_id=conflict,
            )
            break

        board.extend(outcome.facts)
    else:
        # for 正常跑完 = 跳数用尽。再看一眼缺口，可能最后一跳刚好填满。
        gap = tuple(gap_probe(board))
        stop = "fulfilled" if not gap else "max_hops"

    uncovered = tuple(gap_probe(board))
    result = MultiHopResult(
        facts=board.facts,
        hops=tuple(hops),
        stop=stop,
        uncovered=uncovered,
        broken_at=broken_at,
    )
    log_event(
        logger,
        "multihop_finished",
        stop=result.stop,
        hops=result.hop_count,
        facts=len(result.facts),
        uncovered=list(uncovered),
        broken_at=broken_at,
    )
    return result


def _conflict(outcome: HopOutcome, board: Blackboard) -> str | None:
    """本跳查到的切片是否与黑板上已有事实"冲突"。

    可机械判定的冲突只有一种：**本跳的切片与黑板里某条事实是同一个 chunk 但摘录
    不同**——同一段原文被读成两句不一样的话，说明有一侧是转述而非摘录。
    语义级矛盾（"500 元" vs "800 元"）属于生成后的 §10 第五道校验，不在这一层猜；
    在这里做文本相似度判定只会引入一个必须调参的阈值。

    返回冲突的 chunk_id，无冲突返回 None。
    """
    known = {fact.chunk_id: fact.claim for fact in board.facts}
    for fact in outcome.facts:
        previous = known.get(fact.chunk_id)
        if previous is not None and previous != fact.claim:
            return fact.chunk_id
    return None


__all__ = [
    "Blackboard",
    "Fact",
    "GapProbe",
    "HopOutcome",
    "HopRunner",
    "MultiHopResult",
    "StopReason",
    "SubQuestioner",
    "run_multihop",
]
