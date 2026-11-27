"""把时序事实转成生成层可引用的证据块。

图里的值是结构化事实，但仍必须落到原文片段：同一切片的多期事实合并成一条证据，
避免同一张表出现多个角标。取不到原文的图事实不进入证据包。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from api.generate.evidence import Evidence, EvidencePack
from api.retrieval.texts import fetch_texts
from api.temporal.graph import TemporalKnowledgeGraph
from api.temporal.query import FactQuery


def temporal_evidence(
    graph: TemporalKnowledgeGraph,
    query: FactQuery,
    pg: Any,
    *,
    start_n: int,
) -> tuple[Evidence, ...]:
    facts = [
        fact
        for metric in query.all_metrics
        for fact in (
            graph.at(query.subject, metric, query.period_start, query.period_end),
            graph.at(query.subject, metric, query.prior_start, query.prior_end),
        )
        if fact is not None
    ]
    facts = list({fact.fact_id: fact for fact in facts}.values())
    if not facts:
        return ()

    by_chunk: dict[str, list[Any]] = {}
    for fact in facts:
        by_chunk.setdefault(fact.source_chunk_id, []).append(fact)

    texts = fetch_texts(pg, by_chunk)
    out: list[Evidence] = []
    for index, (chunk_id, items) in enumerate(by_chunk.items()):
        chunk = texts.get(chunk_id)
        if chunk is None:
            continue
        lines = [
            f"{item.metric}（{item.period_start} 至 {item.period_end}）：{item.value} {item.unit}"
            for item in items
        ]
        out.append(
            Evidence(
                n=start_n + index,
                chunk_id=chunk_id,
                chunk_ids=(chunk_id,),
                doc_key=chunk.doc_key,
                doc_name=chunk.doc_name,
                page_num=chunk.page_num,
                bbox=list(chunk.bbox or []),
                text="\n".join(lines),
                chunk_type=chunk.chunk_type,
                rerank_score=None,
                fused_rank=0,
            )
        )
    # 重新按实际生成的条数编号，跳过取不到原文的 chunk。
    return tuple(replace(item, n=start_n + i) for i, item in enumerate(out))


def merge_evidence(pack: EvidencePack, extra: tuple[Evidence, ...]) -> EvidencePack:
    """把图证据追加到证据包，按已有编号续号；同 chunk 不重复。"""
    if not extra:
        return pack
    existing = {item.chunk_id for item in pack.items}
    fresh = [item for item in extra if item.chunk_id not in existing]
    if not fresh:
        return pack
    start = len(pack.items) + 1
    items = pack.items + tuple(replace(item, n=start + i) for i, item in enumerate(fresh))
    used = pack.used_chars + sum(len(item.text) + 80 + 8 for item in fresh)
    return replace(pack, items=items, used_chars=used, truncated=pack.truncated)


__all__ = ["merge_evidence", "temporal_evidence"]
