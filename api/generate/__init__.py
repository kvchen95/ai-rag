"""受限生成层（详细设计.md §9 / 附录 D）。."""

from __future__ import annotations

from api.generate.evidence import Evidence, EvidencePack, build_evidence_pack
from api.generate.llm import (
    Draft,
    DraftFormatError,
    Fact,
    GenerationRejected,
    GenerationUnavailable,
    LlmClient,
    generate_draft,
    parse_draft,
)

__all__ = [
    "Fact",
    "Draft",
    "DraftFormatError",
    "Evidence",
    "EvidencePack",
    "GenerationRejected",
    "GenerationUnavailable",
    "LlmClient",
    "build_evidence_pack",
    "generate_draft",
    "parse_draft",
]
