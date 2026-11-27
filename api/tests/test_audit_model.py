"""QaLogEntry 的成对口径（不需要数据库）。

DB 层的 qa_log 只有单列 CHECK，所以"拒答必须带原因码""原因码与判定必须自洽"
这三条只能在这里拦。每条都验正反两个方向：
只验正向（坏值被拒）会漏掉"改成另一个合法值"的变异。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.audit import QaLogEntry
from api.errors import ErrorCode


def _entry(**overrides: object) -> QaLogEntry:
    payload: dict[str, object] = {
        "user_id": "u_dev",
        "user_depts": ["fin"],
        "user_roles": ["staff"],
        "question": "2025年差旅住宿标准是多少",
        "gate1_verdict": "pass",
        "latency_ms": 1840,
    }
    payload.update(overrides)
    return QaLogEntry(**payload)


def test_minimal_entry_is_valid() -> None:
    entry = _entry()
    assert entry.hop_count == 0
    assert entry.is_admin is False
    assert entry.retrieved_ids == []


def test_identity_is_required() -> None:
    with pytest.raises(ValidationError):
        _entry(user_id="")
    with pytest.raises(ValidationError):
        QaLogEntry(question="q", gate1_verdict="pass", latency_ms=1)


@pytest.mark.parametrize("verdict", ["reject_none", "reject_low"])
def test_reject_requires_reason(verdict: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _entry(gate1_verdict=verdict)
    assert "reject_reason" in str(excinfo.value)
    # 反方向：同一判定带上原因码必须通过——证明拒绝的是"缺原因"，不是"这个判定"。
    assert _entry(gate1_verdict=verdict, reject_reason=ErrorCode.LOW_CONFIDENCE)


@pytest.mark.parametrize(
    "reason", [ErrorCode.NO_EVIDENCE, ErrorCode.LOW_CONFIDENCE]
)
def test_reason_requires_reject_verdict(reason: ErrorCode) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _entry(gate1_verdict="pass", reject_reason=reason)
    assert "只能配拒答判定" in str(excinfo.value)
    assert _entry(gate1_verdict="reject_none", reject_reason=reason)


def test_unverifiable_can_coexist_with_supported() -> None:
    # 第五道失败时判别器仍可能判定 supported，两者不是自相矛盾。
    assert _entry(
        gate2_verdict="supported", reject_reason=ErrorCode.UNVERIFIABLE
    )
    # unverifiable 配 not_supported / skipped / None 也都成立
    for gate2 in ("not_supported", "skipped", None):
        assert _entry(gate2_verdict=gate2, reject_reason=ErrorCode.UNVERIFIABLE)


def test_hop_and_latency_are_non_negative() -> None:
    with pytest.raises(ValidationError):
        _entry(latency_ms=-1)
    with pytest.raises(ValidationError):
        _entry(hop_count=-1)


def test_unknown_field_is_rejected() -> None:
    """字段名写错时不能静默丢：审计少一列，事后才发现。"""
    with pytest.raises(ValidationError):
        _entry(user_departments=["fin"])
