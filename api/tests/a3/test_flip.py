"""翻牌事务（详细设计.md §4.1、§4.4）。

这张卡最难"看起来对"的地方就是翻牌：三处改动分开写，每一步都不报错，但中间态
让线上一条也召不回。所以这里逐条断言**库的最终形状**，而不是"函数没抛异常"：

1. 三处一起变（旧版 deprecated + 新版 active + 新版切片 active）；
2. 旧版切片降级（否则旧条款还能被召回）；
3. 同一文档永远只有一个 active（`ux_active_version` 是兜底，这里要证明翻牌本身
   就不会产生两个）；
4. 事务性：翻牌中途失败 → 整体回滚，新版本退回 pending、旧版继续服务；
5. 并发：另一事务持锁时拿不到文档锁；
6. 前置条件：空版本翻不过去（翻它等于把服务翻空）、非 pending 版本翻不过去。
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from api.clients.pg import normalize_dsn
from api.ingest.doclock import DocumentLocked, lock_document
from api.ingest.flip import (
    EmptyVersion,
    NotPending,
    VersionNotFound,
    active_version,
    flip,
    rollback_to,
)
from api.ingest.sync import task_for
from api.tests.a3.conftest import (  # 只 import 辅助函数；fixtures 自动发现
    doc_key,
    seed_document,
)

pytestmark = pytest.mark.integration


def _version_row(conn, version_id: str) -> dict:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT status, activated_at, deprecated_at, superseded_by "
            "FROM doc_versions WHERE version_id = %s",
            (version_id,),
        )
        return cur.fetchone()


def _chunk_statuses(conn, version_id: str) -> dict[str, str]:
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT chunk_id, status FROM chunks WHERE version_id = %s",
            (version_id,),
        )
        return {r["chunk_id"]: r["status"] for r in cur.fetchall()}


def _active_versions(conn, key: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_id FROM doc_versions "
            "WHERE doc_key = %s AND status = 'active'",
            (doc_key(key),),
        )
        return [str(r[0]) for r in cur.fetchall()]


# --- 1. 三处一起变 ------------------------------------------------------------


def test_flip_moves_all_three_places(conn) -> None:
    """§4.1 的三处：旧版下线、新版生效、切片状态同步。"""
    old = seed_document(
        conn, key="flip", version_tag="old", version_status="active"
    )
    conn.execute(
        "UPDATE chunks SET status = 'active' WHERE version_id = %s", (old,)
    )
    new = seed_document(conn, key="flip", version_tag="new")
    conn.commit()

    result = flip(conn, new)
    conn.commit()

    assert result.previous_active == old
    assert result.activated_chunks == 2  # 新版本的 2 条切片

    old_row = _version_row(conn, old)
    assert old_row["status"] == "deprecated"
    assert (
        old_row["deprecated_at"] is not None
    ), "下线时间必须写入（回收窗口从这里起算）"
    assert old_row["superseded_by"] == new

    new_row = _version_row(conn, new)
    assert new_row["status"] == "active"
    assert (
        new_row["activated_at"] is not None
    ), "ck_doc_versions_active_has_activated_at"

    assert set(_chunk_statuses(conn, new).values()) == {"active"}
    # 旧版切片必须降级，否则已废止的条款还能被召回（§2.2）
    assert set(_chunk_statuses(conn, old).values()) == {"deprecated"}

    assert _active_versions(conn, "flip") == [new]
    doc = conn.execute(
        "SELECT status FROM documents WHERE doc_key = %s", (doc_key("flip"),)
    ).fetchone()
    assert doc[0] == "active"


def test_flip_is_the_only_way_to_get_exactly_one_active(conn) -> None:
    """连翻三次（三个版本）后仍然只有一个 active。"""
    v1 = seed_document(
        conn, key="chain", version_tag="v1", version_status="active"
    )
    conn.execute(
        "UPDATE chunks SET status = 'active' WHERE version_id = %s", (v1,)
    )
    v2 = seed_document(conn, key="chain", version_tag="v2")
    v3 = seed_document(conn, key="chain", version_tag="v3")
    conn.commit()

    flip(conn, v2)
    conn.commit()
    flip(conn, v3)
    conn.commit()

    assert _active_versions(conn, "chain") == [v3]
    statuses = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT version_id, status FROM doc_versions WHERE doc_key = %s",
            (doc_key("chain"),),
        ).fetchall()
    }
    assert statuses == {v1: "deprecated", v2: "deprecated", v3: "active"}


def test_flip_writes_desired_state_for_both_versions(conn) -> None:
    """期望状态必须与翻牌同事务写入。

    这是“Qdrant 一定知道发生过翻牌”的唯一保证。
    """
    old = seed_document(
        conn, key="desired", version_tag="old", version_status="active"
    )
    conn.execute(
        "UPDATE chunks SET status = 'active' WHERE version_id = %s", (old,)
    )
    new = seed_document(conn, key="desired", version_tag="new")
    conn.commit()

    flip(conn, new)
    conn.commit()

    new_task = task_for(conn, new)
    old_task = task_for(conn, old)
    assert new_task is not None and new_task["desired_status"] == "active"
    assert new_task["status"] == "pending"  # 还没推给 Qdrant
    assert old_task is not None and old_task["desired_status"] == "deprecated"
    assert old_task["doc_key"] == doc_key("desired")


def test_reflipping_an_active_version_is_refused_not_duplicated(conn) -> None:
    """重复翻同一版本：报 NotPending（幂等的正常结果）。

    不会造出第二个 active。
    """
    version = seed_document(conn, key="reflip", version_tag="v1")
    conn.commit()
    flip(conn, version)
    conn.commit()

    with pytest.raises(NotPending):
        flip(conn, version)
    conn.rollback()

    assert _active_versions(conn, "reflip") == [version]


def test_flip_unknown_version_is_refused(conn) -> None:
    with pytest.raises(VersionNotFound):
        flip(conn, "a3test/nope#v1")
    conn.rollback()


def test_empty_version_cannot_be_flipped(conn) -> None:
    """空版本翻不过去。

    放过去的话：旧版立刻下线、新版一条切片也没有——线上"什么都问不出来"，
    而且没有任何报错。这是最短的一条"翻牌把服务翻空"的路径。
    """
    old = seed_document(
        conn, key="empty", version_tag="old", version_status="active"
    )
    conn.execute(
        "UPDATE chunks SET status = 'active' WHERE version_id = %s", (old,)
    )
    empty = seed_document(conn, key="empty", version_tag="empty", chunks=[])
    conn.commit()

    with pytest.raises(EmptyVersion):
        flip(conn, empty)
    conn.rollback()

    assert _active_versions(conn, "empty") == [old], "旧版必须继续服务"


# --- 2. 事务性 ----------------------------------------------------------------


def test_flip_rolls_back_completely_on_failure(
    conn, pool: ConnectionPool, a3_dsn: str
) -> None:
    """翻牌中途失败 → 三处全部回滚，新版仍是 pending，旧版仍在服务。

    做法：先让翻牌跑起来，然后在同一个事务里制造一次失败（用数据库约束拒一次写）。
    这里选"给 documents 写一个 CHECK 不允许的状态"来制造失败——它发生在翻牌
    最后一步，前面的三处改动都已经执行过了。
    """
    old = seed_document(
        conn, key="atomic", version_tag="old", version_status="active"
    )
    conn.execute(
        "UPDATE chunks SET status = 'active' WHERE version_id = %s", (old,)
    )
    new = seed_document(conn, key="atomic", version_tag="new")
    conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation):
        flip(conn, new)
        # 翻牌已经改了三处；这里追加一条会被库拒掉的写，模拟"翻牌后半段失败"
        conn.execute(
            "UPDATE documents SET status = 'not-a-valid-status' "
            "WHERE doc_key = %s",
            (doc_key("atomic"),),
        )
        conn.commit()
    conn.rollback()

    assert _version_row(conn, new)["status"] == "pending"
    assert _version_row(conn, old)["status"] == "active"
    assert set(_chunk_statuses(conn, new).values()) == {"pending"}
    assert set(_chunk_statuses(conn, old).values()) == {"active"}
    assert _active_versions(conn, "atomic") == [old]
    # 期望状态也必须一起回滚：否则同步器会把一个"其实没翻成"的版本推成 active
    assert task_for(conn, new) is None


# --- 3. 并发 ------------------------------------------------------------------


def test_flip_waits_for_the_document_lock(conn, a3_dsn: str) -> None:
    """另一事务持文档锁时，`wait=False` 直接报 DocumentLocked（不等）。"""
    version = seed_document(conn, key="lock", version_tag="v1")
    conn.commit()

    blocker = psycopg.connect(normalize_dsn(a3_dsn))
    try:
        with blocker.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))", (doc_key("lock"),)
            )
        with pytest.raises(DocumentLocked):
            flip(conn, version, wait_for_lock=False)
        conn.rollback()
    finally:
        blocker.rollback()
        blocker.close()

    # 锁释放后能翻
    flip(conn, version)
    conn.commit()
    assert _active_versions(conn, "lock") == [version]


def test_concurrent_flips_never_produce_two_actives(
    pool: ConnectionPool, a3_dsn: str
) -> None:
    """两个线程同时翻同一文档的两个不同版本：最终只有一个 active。

    这是 §4.1「一份文档不能同时翻两次」的实际后果——两份同时生效会让同一句话
    两个版本都命中，引用都不知道该指哪一页。
    """
    with pool.connection() as setup:
        v1 = seed_document(setup, key="race", version_tag="v1")
        v2 = seed_document(setup, key="race", version_tag="v2")
        setup.commit()

    errors: list[BaseException] = []
    done = threading.Barrier(2)

    def attempt(version_id: str) -> None:
        try:
            with pool.connection() as c:
                done.wait(timeout=5)
                flip(c, version_id)
                c.commit()
        except BaseException as exc:  # noqa: BLE001 - 收集起来在主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=attempt, args=(v,)) for v in (v1, v2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    with pool.connection() as check:
        actives = _active_versions(check, "race")
    assert (
        len(actives) == 1
    ), f"expected exactly one active version, got {actives}"
    # 两个翻牌都可以成功（第二个把第一个刚翻上去的那个顶下来），或有一个被状态检
    # 查
    # 拒掉；但**不能**两个都报错，也不能产生两个 active。
    assert all(isinstance(e, NotPending | EmptyVersion) for e in errors), errors


# --- 4. 回滚 ------------------------------------------------------------------


def test_rollback_restores_the_previous_version(conn) -> None:
    """24h 内把旧版翻回来（§4.4）：新版下线、旧版生效、切片状态对称变化。"""
    v1 = seed_document(conn, key="rollback", version_tag="v1")
    conn.commit()
    flip(conn, v1)
    conn.commit()
    v2 = seed_document(conn, key="rollback", version_tag="v2")
    conn.commit()
    flip(conn, v2)
    conn.commit()

    result = rollback_to(conn, v1)
    conn.commit()

    assert result.restored == v1
    assert result.demoted == [v2]
    assert _active_versions(conn, "rollback") == [v1]
    assert (
        _version_row(conn, v1)["deprecated_at"] is None
    ), "翻回来的版本不该再有下线时间"
    assert _version_row(conn, v2)["status"] == "deprecated"
    assert set(_chunk_statuses(conn, v1).values()) == {"active"}
    assert set(_chunk_statuses(conn, v2).values()) == {"deprecated"}
    # 期望状态也要跟着反回来
    assert task_for(conn, v1)["desired_status"] == "active"
    assert task_for(conn, v2)["desired_status"] == "deprecated"


def test_rollback_only_works_inside_the_retention_window_by_state(conn) -> None:
    """回滚只接受 deprecated 的版本；已 purged 的翻不回来（切片都删了）。"""
    v1 = seed_document(conn, key="purged", version_tag="v1")
    conn.commit()
    flip(conn, v1)
    conn.commit()
    conn.execute(
        "UPDATE doc_versions SET status = 'purged' WHERE version_id = %s", (v1,)
    )
    conn.commit()

    with pytest.raises(NotPending):
        rollback_to(conn, v1)
    conn.rollback()


# --- 5. 锁与扫描互斥 ----------------------------------------------------------


def test_scan_and_flip_share_the_same_document_lock(conn, a3_dsn: str) -> None:
    """扫描与翻牌用同一个表达式（`pg_advisory_xact_lock(hashtext(doc_key))`）。

    实测方法：一个事务按扫描的写法持锁，另一个按翻牌的写法拿锁 → 拿不到。
    这条守的是"两处表达式漂移"：写法一旦不同，两把锁就互不相干了。
    """
    version = seed_document(conn, key="shared", version_tag="v1")
    conn.commit()

    holder = psycopg.connect(normalize_dsn(a3_dsn))
    try:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (doc_key("shared"),),
            )
        # 翻牌路径拿不到
        with pytest.raises(DocumentLocked):
            flip(conn, version, wait_for_lock=False)
        conn.rollback()
        # 直接调 lock_document 也拿不到（同一个表达式）
        with pytest.raises(DocumentLocked):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                    (doc_key("shared"),),
                )
                assert not cur.fetchone()[0]
            lock_document(conn, doc_key("shared"), wait=False)
        conn.rollback()
    finally:
        holder.rollback()
        holder.close()
    time.sleep(
        0.05
    )  # 让 holder 的事务真的结束（rollback 是同步的，这里只是稳妥）


def test_flip_updates_documents_status_to_active(conn) -> None:
    version = seed_document(
        conn, key="docstatus", version_tag="v1", doc_status="queued"
    )
    conn.commit()
    flip(conn, version)
    conn.commit()
    status = conn.execute(
        "SELECT status FROM documents WHERE doc_key = %s",
        (doc_key("docstatus"),),
    ).fetchone()[0]
    assert status == "active"


def test_active_version_helper(conn) -> None:
    assert active_version(conn, doc_key("nothing")) is None
    v = seed_document(conn, key="helper", version_tag="v1")
    conn.commit()
    flip(conn, v)
    conn.commit()
    assert active_version(conn, doc_key("helper")) == v
