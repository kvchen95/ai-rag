"""同步与重试（详细设计.md §4.2）。

`drain()` 是"让 Qdrant 知道翻牌发生了"的那一步。它必须满足四件事，每件都有对应用例：

1. **成功路径**：翻牌写完期望状态 → drain 之后 Qdrant 的 status 与 PG 一致；
2. **校验不是看 HTTP 200**：filter 写错时 Qdrant 也返回 completed，所以 drain 会按
   version_id 回读计数并与 PG 的分布比对，对不上就判失败；
3. **重试与退避**：失败不丢任务，按 attempts 指数退避，额度耗尽转 failed 并记漂移；
4. **幂等**：重复写同一个期望状态不重置已达成的 done（否则每小时对账会反复重推）。
"""

from __future__ import annotations

import pytest

from api.ingest.flip import flip
from api.ingest.sync import (
    DrainReport,
    backoff_for,
    claim_due,
    drain,
    mark_failed,
    pending_count,
    record_drift,
    task_for,
    upsert_desired,
    verify_desired,
)
from api.ingest.vectors import QdrantVectorStore, index_version
from api.tests.a3.conftest import (  # 只有辅助函数需要 import；fixtures 由 conftest 自动发现
    doc_key,
    seed_document,
)

pytestmark = pytest.mark.integration


class BrokenStore(QdrantVectorStore):
    """模拟 Qdrant 挂掉：所有写操作抛错，读操作真实执行。"""

    fail_writes = True

    def set_status_by_version(self, version_id: str, status: str) -> None:
        if self.fail_writes:
            raise ConnectionError("qdrant is down (simulated)")
        super().set_status_by_version(version_id, status)

    def delete_version_points(self, version_id: str) -> None:
        if self.fail_writes:
            raise ConnectionError("qdrant is down (simulated)")
        super().delete_version_points(version_id)


@pytest.fixture
def broken_store(qdrant_reachable: str, collection: str) -> BrokenStore:
    s = BrokenStore(base_url=qdrant_reachable, collection=collection)
    try:
        yield s
    finally:
        s.close()


def _promote(conn, store, embedder, key: str, *, old_tag: str | None = None) -> tuple[str, str | None]:
    """建（可选）旧版 + 新版，索引并翻牌。

    旧版也必须**先真正索引过**（真实世界里它当初就是这样上线的）：否则同步器会
    发现"PG 有 2 条 active 切片、Qdrant 一个点都没有"，那是另一种（合法的）不一致，
    会把本用例想验的东西搅乱。
    """
    old = None
    if old_tag is not None:
        old = seed_document(conn, key=key, version_tag=old_tag, version_status="active")
        conn.execute("UPDATE chunks SET status = 'active' WHERE version_id = %s", (old,))
        conn.commit()
        index_version(conn, store, embedder, old)
        conn.commit()
    new = seed_document(conn, key=key, version_tag="cur")
    conn.commit()
    index_version(conn, store, embedder, new)
    flip(conn, new)
    conn.commit()
    return new, old


# --- 1. 成功路径 --------------------------------------------------------------


def test_drain_applies_status_to_qdrant(conn, pool, store, embedder) -> None:
    """翻牌后 drain：新版点变 active、旧版点变 deprecated，两边分布一致。"""
    new, old = _promote(conn, store, embedder, "syncok", old_tag="prev")
    assert old is not None

    # 翻牌前：新版的点必须是 pending（不可检索），否则那个窗口会让半成品上线
    assert store.count_version(new, status="pending") == 2
    assert store.count_version(new, status="active") == 0

    report = drain(pool, store, max_attempts=4, backoff_s=0.0)

    assert report.ok, report.as_dict()
    assert report.failed == 0
    assert store.count_version(new, status="active") == 2
    assert store.count_version(old, status="deprecated") == 2
    assert store.count_version(old, status="active") == 0
    assert task_for(conn, new)["status"] == "done"
    assert task_for(conn, old)["status"] == "done"
    assert pending_count(conn) == 0


def test_drain_is_idempotent(conn, pool, store, embedder) -> None:
    """第二次 drain 什么都不做（任务已 done，不再到期）。"""
    _promote(conn, store, embedder, "drainidem")
    first = drain(pool, store, max_attempts=4, backoff_s=0.0)
    second = drain(pool, store, max_attempts=4, backoff_s=0.0)

    assert first.done >= 1
    assert second.processed == 0, second.as_dict()
    assert second.done == 0


def test_upsert_desired_does_not_reset_a_done_task(conn, store, embedder, pool) -> None:
    """重复写同一个期望状态不该把 done 打回 pending。

    否则每小时的对账会把已经同步好的版本反复重推——每次都"成功"，但日志与
    Qdrant 的写入量会随时间线性增长。
    """
    new, _ = _promote(conn, store, embedder, "norepeat")
    drain(pool, store, max_attempts=4, backoff_s=0.0)
    before = task_for(conn, new)
    assert before["status"] == "done"

    upsert_desired(conn, doc_key=doc_key("norepeat"), version_id=new, desired_status="active")
    conn.commit()

    after = task_for(conn, new)
    assert after["status"] == "done"
    assert after["attempts"] == before["attempts"]
    assert after["synced_at"] == before["synced_at"]


def test_desired_change_resets_retry_budget(conn, store, embedder) -> None:
    """期望**变了**才重置：上一轮的失败不该让一次新的翻牌一上来就没额度。"""
    new, _ = _promote(conn, store, embedder, "reset")
    # 先人为把它弄成失败态
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE vector_sync_tasks SET status = 'failed', attempts = 4,"
            " last_error = 'boom' WHERE version_id = %s",
            (new,),
        )
    conn.commit()

    upsert_desired(conn, doc_key=doc_key("reset"), version_id=new, desired_status="active")
    conn.commit()

    row = task_for(conn, new)
    # 期望没变（还是 active）→ 保留失败态
    assert row["status"] == "failed"
    assert row["attempts"] == 4

    upsert_desired(conn, doc_key=doc_key("reset"), version_id=new, desired_status="deprecated")
    conn.commit()
    row = task_for(conn, new)
    # 期望变了 → 重置
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["last_error"] is None


# --- 2. 校验不是看 HTTP 200 ---------------------------------------------------


def test_verify_detects_wrong_status_even_when_qdrant_returns_200(conn, store, embedder) -> None:
    """把 PG 状态改掉（不回写 Qdrant）→ verify 必须判不一致。

    这条守的是"Qdrant 的 200 只说明它收下了请求"：filter 写错、改错版本、
    一条都没匹配上，它全都返回 completed。
    """
    new, _ = _promote(conn, store, embedder, "verify")

    # 让 Qdrant 侧先"正确"，再在 PG 里把一条切片改成 deprecated
    store.set_status_by_version(new, "active")
    ok, detail = verify_desired(conn, store, version_id=new, desired="active")
    assert ok, detail

    conn.execute(
        "UPDATE chunks SET status = 'deprecated' WHERE version_id = %s "
        "AND chunk_id = (SELECT min(chunk_id) FROM chunks WHERE version_id = %s)",
        (new, new),
    )
    conn.commit()

    ok, detail = verify_desired(conn, store, version_id=new, desired="active")
    assert not ok
    assert "status=active" in detail


def test_verify_detects_missing_points(conn, store, embedder) -> None:
    """点少了（被别的操作删掉）→ verify 判不一致。"""
    new, _ = _promote(conn, store, embedder, "verifymiss")
    store.set_status_by_version(new, "active")
    assert verify_desired(conn, store, version_id=new, desired="active")[0]

    store.delete_version_points(new)
    ok, detail = verify_desired(conn, store, version_id=new, desired="active")
    assert not ok
    assert "point count" in detail


# --- 3. 重试与退避 ------------------------------------------------------------


def test_drain_retries_then_marks_failed_and_records_drift(
    conn, pool, broken_store, embedder
) -> None:
    """同步失败：按额度重试，耗尽后 failed 并记一条 sync_failed 漂移。

    `max_attempts=1` 让第一次失败就用完额度（不必等退避），断言的是"额度用完之后
    发生了什么"：任务 failed、漂移账上多一条、drain 报 not ok。
    """
    new, _ = _promote(conn, broken_store, embedder, "syncfail")
    # 上面 index 用的是 broken_store（只坏写操作，upsert 正常），翻牌也正常
    before = _drift_count(conn)

    report = drain(pool, broken_store, max_attempts=1, backoff_s=0.0)

    assert not report.ok
    assert report.failed >= 1
    row = task_for(conn, new)
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert row["last_error"], "failed 必须带原因"

    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, version_id, fixed FROM vector_drift_log WHERE version_id = %s",
            (new,),
        )
        drifts = cur.fetchall()
    assert _drift_count(conn) > before
    assert any(d["kind"] == "sync_failed" and not d["fixed"] for d in drifts)


def test_retry_keeps_task_pending_until_budget_runs_out(conn, pool, broken_store, embedder) -> None:
    """还有额度时任务退回 pending（不丢），并推到未来（退避）。"""
    new, _ = _promote(conn, broken_store, embedder, "syncbackoff")

    report = drain(pool, broken_store, max_attempts=3, backoff_s=3600.0)

    assert report.retried >= 1
    assert report.failed == 0
    row = task_for(conn, new)
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    # 退避到一小时后：紧接着再 drain 一次不该重试它
    again = drain(pool, broken_store, max_attempts=3, backoff_s=3600.0)
    assert again.processed == 0


def test_backoff_grows_exponentially_and_is_capped() -> None:
    """退避公式：base * 2^(attempt-1)，封顶 8×base。

    §4.2 要求"翻牌后一分钟内必须同步完"，所以退避不能真的指数涨到十分钟。
    """
    assert backoff_for(1, 1.0) == 1.0
    assert backoff_for(2, 1.0) == 2.0
    assert backoff_for(3, 1.0) == 4.0
    assert backoff_for(4, 1.0) == 8.0
    assert backoff_for(9, 1.0) == 8.0  # 封顶
    assert backoff_for(1, 0.0) == 0.0


def test_failed_task_recovers_when_qdrant_comes_back(conn, pool, broken_store, embedder) -> None:
    """Qdrant 恢复后，failed 的任务能被**对账**修好（不是靠自动复活）。

    这是验收"同步失败可对账修复"的落点：failed 不会自己回来（否则一个持续失败的
    对象会无限重试），但 `upsert_desired` 或对账能看到差异并纠正。
    """
    from api.ingest.reconcile import reconcile

    new, _ = _promote(conn, broken_store, embedder, "recover")
    drain(pool, broken_store, max_attempts=1, backoff_s=0.0)
    assert task_for(conn, new)["status"] == "failed"
    assert store_active(conn, broken_store, new) == 0

    # Qdrant 回来了
    broken_store.fail_writes = False
    report = reconcile(conn, broken_store, doc_key=doc_key("recover"))
    conn.commit()

    assert report.drifts, "对账必须能看见这处不一致（否则它就是个静默漂移）"
    assert not report.remaining, report.as_dict()
    assert store_active(conn, broken_store, new) == 2
    # 对账把状态改对之后，那条 sync_failed 漂移该被销账
    assert task_for(conn, new)["status"] == "failed"  # 任务本身仍是 failed……
    # ……但状态已经对了，所以对账不再报漂移
    assert not reconcile(conn, broken_store, doc_key=doc_key("recover")).remaining
    conn.commit()


def store_active(conn, store: QdrantVectorStore, version_id: str) -> int:
    del conn
    return store.count_version(version_id, status="active")


def _drift_count(conn) -> int:
    return int(conn.execute("SELECT count(*) FROM vector_drift_log").fetchone()[0])


# --- 4. 抢占与记录 ------------------------------------------------------------


def test_claim_due_leases_the_task(conn, store, embedder) -> None:
    """领任务会把它推到未来（租约），同一时刻别的 worker 领不到。

    进程崩在中途时租约到期，任务自动重新可领——不需要"清理僵尸任务"的流程。
    """
    new, _ = _promote(conn, store, embedder, "lease")
    conn.commit()

    first = claim_due(conn)
    conn.commit()
    assert first is not None
    assert first["version_id"] == new
    assert first["attempts"] == 1

    # 刚被领走，还在租约里 → 领不到
    assert claim_due(conn) is None
    conn.rollback()


def test_mark_failed_requires_a_reason(conn, store, embedder) -> None:
    new, _ = _promote(conn, store, embedder, "reason")
    conn.commit()
    with pytest.raises(ValueError, match="non-empty error"):
        mark_failed(conn, new, "   ", max_attempts=4, backoff_s=0.0)
    conn.rollback()


def test_record_drift_dedupes_open_entries(conn) -> None:
    """同一处未修复的漂移只留一行（否则每小时对账会刷 24 条一样的记录）。"""
    first = record_drift(conn, doc_key=doc_key("drift"), kind="status_mismatch",
                         chunk_id="ch_x", pg_value="active", qdrant_value="pending")
    conn.commit()
    second = record_drift(conn, doc_key=doc_key("drift"), kind="status_mismatch",
                          chunk_id="ch_x", pg_value="active", qdrant_value="deprecated")
    conn.commit()
    assert first is not None
    assert second is None, "second open drift for the same place must be deduped"
    # 只数本用例命名空间的行。真库里存在 promote 留下的历史 drift 账（已 fixed），
    # 全库 count 会把它们一起算进来误红；这里的命名空间每个用例前后由 _cleanup_rows
    # 清空，所以基线恒为 0，重复插入一条 open drift 仍会让这个数变成 2 -> 红。
    total = conn.execute(
        "SELECT count(*) FROM vector_drift_log WHERE doc_key LIKE %s",
        (f"{doc_key('drift')}%",),
    ).fetchone()[0]
    assert total == 1


def test_drain_report_shape() -> None:
    report = DrainReport(processed=1, done=1)
    payload = report.as_dict()
    assert payload["processed"] == 1 and payload["done"] == 1
    assert payload["outcomes"] == []
