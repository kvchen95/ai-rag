"""扫描判定表（§3.1 五格）与跳过原因细分。.

判定函数的五格是全卡最容易改错的地方：混掉一格就悄悄多一个版本、或者把已生效
的版本顶掉。这里每一格都正反各测一遍——只测"坏值被拒"会漏掉"改成另一个合法值"
的变异（例如把 version_failed 改成 unchanged，行为从"跳过并提示"变成"跳过且
装作正常"，报告里再也看不出那个文件一直失败）。
"""

from __future__ import annotations

import pytest

from api.ingest.decide import (
    REVERT_STATUSES,
    SEEN_SKIP_STATUSES,
    Outcome,
    SkipReason,
    VersionStatus,
    classify,
)


def test_new_doc_key_is_add() -> None:
    """Run test_new_doc_key_is_add."""
    assert classify(doc_exists=False, existing_status=None) == (
        Outcome.ADD,
        None,
    )


def test_existing_doc_with_new_hash_is_change() -> None:
    """Run test_existing_doc_with_new_hash_is_change."""
    assert classify(doc_exists=True, existing_status=None) == (
        Outcome.CHANGE,
        None,
    )


@pytest.mark.parametrize("status", sorted(SEEN_SKIP_STATUSES))
def test_seen_content_is_skipped_as_unchanged(status: str) -> None:
    """Run test_seen_content_is_skipped_as_unchanged."""
    assert classify(doc_exists=True, existing_status=status) == (
        Outcome.SKIP,
        SkipReason.UNCHANGED,
    )


def test_failed_version_is_not_silently_retried() -> None:
    """同指纹的版本已 failed：§2.2 的 failed 是终态。.

    判成 unchanged 会让报告里看不出"这个文件一直解析失败"，运维只会看到
    "skipped 一直很大"却不知道为什么。
    """
    assert classify(doc_exists=True, existing_status="failed") == (
        Outcome.SKIP,
        SkipReason.VERSION_FAILED,
    )


@pytest.mark.parametrize("status", sorted(REVERT_STATUSES))
def test_reverted_content_is_reported_not_recreated(status: str) -> None:
    """内容回退到 deprecated/purged 版本时只能报出来。.

    唯一约束让它建不了新版本，静默当 unchanged 会让人以为回滚已经生效了。
    """
    assert classify(doc_exists=True, existing_status=status) == (
        Outcome.SKIP,
        SkipReason.CONTENT_REVERT,
    )


def test_every_version_status_has_a_defined_outcome() -> None:
    """枚举里每个值都必须有确定结论，不留"落到默认分支"的洞。.

    新增一个状态而没在 classify 里想清楚处置方式时，这条会红。
    """
    outcomes = {
        s.value: classify(doc_exists=True, existing_status=s.value)
        for s in VersionStatus
    }
    assert set(outcomes) == {s.value for s in VersionStatus}
    # 只有 pending/active/skipped 能落到 unchanged；failed 与回退态各有
    # 自己的码。
    unchanged = {
        k
        for k, v in outcomes.items()
        if v == (Outcome.SKIP, SkipReason.UNCHANGED)
    }
    assert unchanged == {"pending", "active", "skipped"}


def test_unknown_status_is_rejected_not_guessed() -> None:
    """非法状态不许当 unchanged 混过去：它会掩盖一次数据损坏。."""
    with pytest.raises(ValueError):
        classify(doc_exists=True, existing_status="done")


def test_skip_outcomes_never_create_versions() -> None:
    """跳过就是不写库。.

    任何 SKIP 结论都必须带原因码，否则日志里查不出为什么跳过。
    """
    for status in VersionStatus:
        outcome, reason = classify(
            doc_exists=True, existing_status=status.value
        )
        if outcome is Outcome.SKIP:
            assert reason is not None, status
        else:  # pragma: no cover - 当前没有非 SKIP 的既有状态分支
            assert reason is None
