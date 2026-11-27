"""ingest_jobs 状态机与重试计数（纯逻辑，不需要数据库）。

状态机是这张卡唯一"有副作用且不可逆"的东西：一条任务被误判成 succeeded 之后，
没有任何东西会去重跑它，切片就永远缺一份。所以这里做三件事：

1. 把**所有**状态对都过一遍，逐个断言能否迁移——包括反方向（failed -> queued
   必须被拒），只测允许的那几个会让"多加一条允许边"悄悄通过。
2. 重试计数的边界：额度用完那一次必须直接 failed，而不是"再回 queued 一次然后
   永远卡在 queued"。
3. `failed` 必须带 error：空串也算没带（DB 的 CHECK 只拦 NULL）。
"""

from __future__ import annotations

import pytest

from api.ingest.jobs import (
    ALLOWED,
    TERMINAL,
    IllegalTransition,
    JobStatus,
    _assert_error,
    can_transition,
    retry_budget_left,
)

ALL_STATUSES = list(JobStatus)

# 允许的迁移，逐条写出（不引用 ALLOWED：测试若从被测代码取期望值，等于没断言）。
EXPECTED_EDGES = {
    ("queued", "running"),
    ("queued", "canceled"),
    ("running", "queued"),
    ("running", "succeeded"),
    ("running", "failed"),
}


def test_allowed_edges_are_exactly_these() -> None:
    actual = {
        (src.value, dst.value)
        for src, targets in ALLOWED.items()
        for dst in targets
    }
    assert actual == EXPECTED_EDGES


@pytest.mark.parametrize(("src", "dst"), sorted(EXPECTED_EDGES))
def test_allowed_edges_pass(src: str, dst: str) -> None:
    assert can_transition(src, dst)


def test_every_other_edge_is_rejected() -> None:
    """(5x5=25) - 5 条允许 = 20 条必须被拒。

    逐对断言而不是抽查：这个循环红了就说明有人加了一条没想清楚的边
    （最典型的是 failed -> queued，它会让"重试次数"变成一个可以无限重来的数）。
    """
    for src in ALL_STATUSES:
        for dst in ALL_STATUSES:
            if (src.value, dst.value) in EXPECTED_EDGES:
                continue
            assert not can_transition(
                src.value, dst.value
            ), f"{src} -> {dst} should be rejected"


@pytest.mark.parametrize("status", sorted(s.value for s in TERMINAL))
def test_terminal_states_are_dead_ends(status: str) -> None:
    for dst in ALL_STATUSES:
        assert not can_transition(status, dst.value), f"{status} -> {dst}"


def test_failed_is_terminal_not_retryable() -> None:
    """重试是 running -> queued，不是 failed -> queued。

    若允许 failed -> queued，一条一直失败的任务会在每轮扫描时重新入队，
    日志被同一个问题刷满，而"重试了几次"这个数永远不涨。
    """
    assert not can_transition("failed", "queued")
    assert not can_transition("failed", "running")


def test_retry_budget_counts_retries_not_attempts() -> None:
    """`max_retries=3` = 1 次尝试 + 3 次重试 = 最多 4 次执行。"""
    assert retry_budget_left("queued", 0, max_retries=3) == 3
    assert retry_budget_left("running", 2, max_retries=3) == 1
    assert retry_budget_left("queued", 3, max_retries=3) == 0
    # 额度用完还没成功 -> 下一次失败直接 failed，报告里不该显示"还有机会"。
    assert retry_budget_left("failed", 3, max_retries=3) == 0
    assert retry_budget_left("running", 9, max_retries=3) == 0  # 不返回负数


@pytest.mark.parametrize("status", sorted(s.value for s in TERMINAL))
def test_terminal_states_never_report_retry_budget(status: str) -> None:
    assert retry_budget_left(status, 0, max_retries=3) == 0


def test_failed_must_carry_a_real_error_message() -> None:
    for bad in ("", "   ", "\n"):
        with pytest.raises(ValueError):
            _assert_error(bad)
    _assert_error("parse_failed: page 3 has no text layer")


def test_illegal_transition_message_is_actionable() -> None:
    """日志里要能直接看出"哪条任务、从哪到哪"——否则并发问题无从追。"""
    exc = IllegalTransition(42, "failed", "queued")
    assert "42" in str(exc) and "failed" in str(exc) and "queued" in str(exc)
    assert (exc.job_id, exc.src, exc.dst) == (42, "failed", "queued")
