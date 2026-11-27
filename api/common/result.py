"""Explicit success, failure, and degradation result types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E")


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    """Successful result."""

    value: T

    @property
    def ok(self) -> bool:
        """Return True for success."""
        return True


@dataclass(frozen=True, slots=True)
class Fail(Generic[E]):
    """Explicit failure with a stable code and reason."""

    code: str
    reason: str
    error: E | None = None

    @property
    def ok(self) -> bool:
        """Return False for failure."""
        return False


@dataclass(frozen=True, slots=True)
class Degraded(Generic[T]):
    """Successful result produced through an audited degradation."""

    value: T
    code: str
    reason: str
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return True because a degraded value is usable."""
        return True

    @property
    def degraded(self) -> bool:
        """Return True to make degradation explicit."""
        return True


Result = Ok[T] | Fail[E]


def degrade(
    value: T,
    *,
    code: str,
    reason: str,
    audit: dict[str, Any] | None = None,
) -> Degraded[T]:
    """Build the only allowed audited degradation result."""
    if not code.strip():
        raise ValueError("degrade code must not be empty")
    if not reason.strip():
        raise ValueError("degrade reason must not be empty")
    return Degraded(
        value=value,
        code=code,
        reason=reason,
        audit={} if audit is None else dict(audit),
    )


__all__ = ["Degraded", "Fail", "Ok", "Result", "degrade"]
