"""Module."""

from __future__ import annotations

from dataclasses import dataclass

from api.generate.evidence import EvidencePack
from api.generate.llm import Draft, DraftFormatError
from api.resources import messages as msg


@dataclass(frozen=True, slots=True)
class RenderedDraft:
    """facts 渲染后的最终回答。.

    answer 由系统生成，citations 为 EvidencePack 局部编号。
    """

    answer: str
    citations: tuple[int, ...]
    draft: Draft


def render_draft(draft: Draft, pack: EvidencePack) -> RenderedDraft:
    """确定性地把 Draft.facts 渲染成最终 answer 与 [n]。.

    - 不改写 fact 文本；只在每个 fact 末追加 ` [n]...`。
    - evidence_ids 必须全部能在 pack 中映射到编号；未知 chunk_id 直接失败。
    - 引用编号按 fact 顺序、fact 内 evidence_ids 顺序稳定去重。
    """
    pack_by_chunk = {
        chunk_id: item.n for item in pack.items for chunk_id in item.chunk_ids
    }
    citations: list[int] = []
    seen: set[int] = set()
    rendered_facts: list[str] = []
    for fact in draft.facts:
        if not fact.evidence_ids:
            raise DraftFormatError(
                msg.RENDER_FACT_NO_EVIDENCE, raw=draft.raw
            )
        numbers: list[int] = []
        for evidence_id in fact.evidence_ids:
            if evidence_id not in pack_by_chunk:
                raise DraftFormatError(
                    msg.RENDER_UNKNOWN_EVIDENCE.format(
                        evidence_id=evidence_id
                    ),
                    raw=draft.raw,
                )
            number = pack_by_chunk[evidence_id]
            if number not in seen:
                seen.add(number)
                citations.append(number)
            if number not in numbers:
                numbers.append(number)
        suffix = "".join(f"[{number}]" for number in numbers)
        rendered_facts.append(_with_citations(fact.text, suffix))
    answer = "\n".join(rendered_facts)
    return RenderedDraft(answer=answer, citations=tuple(citations), draft=draft)


_TRAILING_PUNCT = ("。", "！", "？", "!", "?")


def _with_citations(text: str, suffix: str) -> str:
    """把角标放在句末标点之前，保证“一句话 + 它的角标”不被切句器拆开。."""
    if text and text[-1] in _TRAILING_PUNCT:
        return f"{text[:-1]}{suffix}{text[-1]}"
    return f"{text}{suffix}"
