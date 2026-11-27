"""时间归一：把 `api.time_range.TimeNormalisation` 转成结构化 `TemporalScope`。

底层换算规则仍在 `api.time_range`（规则 + 受约束 LLM）；本模块负责把
文档有效时间 / 答案目标时间 / 置信度 / 依据拆成结构化字段，确保
`target_time` 不进入 `document_ranges`。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from api.temporal.types import TemporalScope
from api.time_range import TimeNormalisation, normalise_time


def build_temporal_scope(result: TimeNormalisation) -> TemporalScope:
    """把现有 TimeNormalisation 转成结构化 TemporalScope，保持兼容。"""
    ranges = (result.range,) if result.range is not None else ()
    targets = (result.target_time,) if result.target_time is not None else ()
    notes = (result.note,) if result.note else ()
    if result.range is not None:
        basis = result.range.basis
        confidence = "medium" if result.range.inferred else "high"
    else:
        basis = "explicit"
        confidence = "low"
    return TemporalScope(
        question=result.question,
        document_ranges=ranges,
        target_times=targets,
        basis=basis,
        confidence=confidence,  # type: ignore[arg-type]
        notes=notes,
    )


def normalize_temporal(
    question: str,
    *,
    client: Any = None,
    now: date | None = None,
    fiscal_start_month: int = 1,
) -> TemporalScope:
    return build_temporal_scope(
        normalise_time(
            question,
            client=client,
            now=now,
            fiscal_start_month=fiscal_start_month,
        )
    )


# 兼容别名：不同调用方/测试可能使用不同命名。
normalize_time = normalize_temporal
normalise_temporal = normalize_temporal
normalize = normalize_temporal