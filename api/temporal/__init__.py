from __future__ import annotations

from api.temporal.types import TemporalScope
from api.temporal.extract import (
    build_temporal_scope,
    extract,
    extract_temporal,
    normalize,
    normalize_temporal,
    normalize_time,
    normalise_temporal,
)
from api.temporal.normalize import (
    build_temporal_scope as _build_temporal_scope,
    normalize_temporal as _normalize_temporal,
)
from api.temporal.documents import (
    fetch_documents,
    select_doc_keys,
    select_documents,
    select_temporal_docs,
)
from api.temporal.decompose import decompose, decompose_question, split_compound
from api.temporal.compound import aggregate, aggregate_compound, aggregate_gates
from api.temporal.facts import TemporalFact, extract_table_facts
from api.temporal.graph import TemporalComparison, TemporalKnowledgeGraph
from api.temporal.query import FactQuery, resolve_fact_query
from api.temporal.repository import TemporalFactRepository

__all__ = [
    "TemporalScope",
    "build_temporal_scope",
    "normalize_temporal",
    "normalize_time",
    "normalise_temporal",
    "normalize",
    "extract_temporal",
    "extract",
    "select_documents",
    "select_doc_keys",
    "select_temporal_docs",
    "fetch_documents",
    "TemporalFact",
    "extract_table_facts",
    "TemporalComparison",
    "TemporalKnowledgeGraph",
    "FactQuery",
    "resolve_fact_query",
    "TemporalFactRepository",
]
