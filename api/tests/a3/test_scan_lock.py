"""扫描锁的会话归属（详细设计.md §3.1；B2 报的“僵尸锁”回归）。.

`pg_try_advisory_lock` 是**会话级**锁，而 psycopg_pool 在连接还池时会
reset —— reset **不释放** advisory lock（实测）。所以“拿锁后 commit →
还池 → 稍后拿另一条连接 unlock”会让锁留在原后端上，表现是“第一次扫描之后
每次扫描都 409”，而且时好时坏（看池给了哪条连接）。

这组用例锁住三件事：

1. 连续多次扫描全部成功（不是靠运气拿到同一条连接）；
2. 扫描结束后池里没有残留的 advisory lock；
3. 另一条连接上真持锁时，扫描确实报 ScanInProgress（证明锁真的跨会话有效，
   而不是"锁了个寂寞"）。
"""

from __future__ import annotations

import psycopg
import pytest

from api.clients.pg import PgDatabase, normalize_dsn
from api.ingest.lockkeys import SCAN_LOCK_KEY
from api.ingest.scan import ScanInProgress, _acquire_scan_lock, scan_samples

pytestmark = pytest.mark.integration

NS = "a3scanlock"


@pytest.fixture
def db(migrated: str) -> PgDatabase:
    """**两条**连接：僵尸锁依赖"有多条连接可还"，只开一条测不出来。."""
    database = PgDatabase(migrated, min_size=1, max_size=4, connect_timeout_s=3)
    database.open()
    try:
        yield database
    finally:
        with database.session() as conn:
            conn.execute("SELECT pg_advisory_unlock_all()")
            conn.commit()
        database.close()


@pytest.fixture
def samples(tmp_path):
    """Run samples."""
    root = tmp_path / "samples"
    (root / NS).mkdir(parents=True)
    (root / NS / "lock-probe.pdf").write_bytes(b"%PDF-1.4\n% lock probe\n")
    return root


@pytest.fixture(autouse=True)
def _cleanup(migrated: str):
    """Run _cleanup."""
    def purge() -> None:
        """Run purge."""
        with psycopg.connect(normalize_dsn(migrated), autocommit=True) as conn:
            conn.execute(
                "DELETE FROM chunks WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
            conn.execute(
                "DELETE FROM doc_versions WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
            conn.execute(
                "DELETE FROM ingest_jobs WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
            conn.execute(
                "DELETE FROM documents WHERE doc_key LIKE %s", (f"{NS}/%",)
            )

    purge()
    try:
        yield
    finally:
        purge()


def _advisory_locks(db: PgDatabase) -> int:
    """Run _advisory_locks."""
    rows = db.fetch_all(
        "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory'"
    )
    return int(rows[0]["n"]) if rows else 0


def test_three_consecutive_scans_all_succeed(db: PgDatabase, samples) -> None:
    """连续三次扫描都必须能拿锁。.

    这条用例的形态是关键：如果锁泄漏，第一次成功、第二次 409（锁在原后端上），
    第三次可能又成功（池把带锁的那条连接给了它）。所以"跑三次全过"才说明锁真的
    成对释放了，只跑一次永远看不出来。
    """
    first = scan_samples(db, samples)
    assert first.counts["add"] == 1

    for attempt in (2, 3):
        report = scan_samples(db, samples)
        assert (
            report.counts["skip"] == 1
        ), f"scan #{attempt} failed: {report.counts}"


def test_no_advisory_lock_left_after_scan(db: PgDatabase, samples) -> None:
    """Run test_no_advisory_lock_left_after_scan."""
    scan_samples(db, samples)
    # 把连接全还给池之后再查：此时若还有锁，就说明它挂在某条"空闲"连接上
    assert _advisory_locks(db) == 0


def test_lock_on_another_session_blocks_the_scan(
    db: PgDatabase, samples, migrated: str
) -> None:
    """另一会话真持锁 → ScanInProgress（证明这把锁跨会话有效）。."""
    blocker = psycopg.connect(normalize_dsn(migrated), autocommit=True)
    try:
        blocker.execute("SELECT pg_advisory_lock(%s)", (SCAN_LOCK_KEY,))
        with pytest.raises(ScanInProgress):
            scan_samples(db, samples)
    finally:
        blocker.execute("SELECT pg_advisory_unlock_all()")
        blocker.close()

    # 释放后立刻能扫（不留后遗症）
    assert scan_samples(db, samples).counts["add"] == 1


def test_acquire_without_commit_still_takes_the_lock(
    db: PgDatabase, samples, migrated: str
) -> None:
    """`_acquire_scan_lock` 不 commit 也要真的拿到锁。.

    这正是修复点：先 commit 会把连接还池，锁可能落在别的后端上。用另一条**独立**
    连接验证"锁确实被这台机器持有"。
    """
    with db.session() as conn:
        _acquire_scan_lock(conn)
        probe = psycopg.connect(normalize_dsn(migrated), autocommit=True)
        try:
            got = probe.execute(
                "SELECT pg_try_advisory_lock(%s)", (SCAN_LOCK_KEY,)
            ).fetchone()[0]
        finally:
            probe.execute("SELECT pg_advisory_unlock_all()")
            probe.close()
        assert got is False, "the lock must be visible to other sessions"
    # 退出 session 块后锁该没了（scan_samples 的 finally 会解锁；这里手动清）
    with db.session() as conn:
        conn.execute("SELECT pg_advisory_unlock_all()")
        conn.commit()
