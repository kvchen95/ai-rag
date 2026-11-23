"""外部依赖的探针类型与协议。

`Probe` 是所有健康检查的唯一返回形状：ok / 耗时 / 错误一句话 / 附加上下文。
统一形状的好处是 healthz 不用为每个依赖写一套分支，也不会出现
"PG 挂了报 503、Qdrant 挂了报 200" 这种不对称。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Probe:
    ok: bool
    latency_ms: float
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 2),
            "error": self.error,
        }
        payload.update(self.extra)
        return payload


def short_error(exc: BaseException) -> str:
    """探针/日志里的错误摘要：一行、有类型、限长。"""
    text = str(exc).strip().replace("\n", " ")
    head = type(exc).__name__
    return f"{head}: {text}"[:300] if text else head


class HealthProbe(Protocol):
    """能回答"你活着吗"的东西。PG/Qdrant 客户端、测试替身都实现它。"""

    def ping(self, *, timeout_s: float | None = None) -> Probe: ...


class AsyncHealthProbe(Protocol):
    async def ping(self, *, timeout_s: float | None = None) -> Probe: ...
