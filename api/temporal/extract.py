from __future__ import annotations

from datetime import date
from typing import Any

from api.temporal.normalize import build_temporal_scope, normalize_temporal
from api.temporal.types import TemporalScope
from api.time_range import TimeNormalisation, normalise_time


# 兼容别名：extract 也承担“抽取”入口。
extract_temporal = normalize_temporal
extract = normalize_temporal
normalize = normalize_temporal
normalize_temporal = normalize_temporal
normalize_time = normalize_temporal
normalise_temporal = normalize_temporal


def build_temporal_scope(result: TimeNormalisation) -> TemporalScope:
    from api.temporal.normalize import build_temporal_scope as _build

    return _build(result)