"""翻牌：一个事务里改三处（详细设计.md §4.1）。.

    BEGIN
      pg_advisory_xact_lock(hashtext(doc_key))      -- 同一份文档不能同时翻两次
      旧 active  -> deprecated   (+ deprecated_at, superseded_by)
      新 pending -> active       (+ activated_at)   -- CHECK 要求同写
      UPDATE chunks SET status='active' WHERE version_id = 新版
      写 vector_sync_tasks（期望 active；旧版期望 deprecated）
    COMMIT

为什么三处必须同一个事务
------------------------

只改版本不改切片，会出现"版本已生效、切片还是 pending"的鬼状态：检索按
`v_active_chunks`（双 active）取数，那时一条都取不到——线上表现为"新版本明明上
了，问什么都答不出来"。反过来改切片不改版本，则是"旧版还在服务，但旧版的切片
已经被标成 deprecated"，同样一条都取不到。两种半成品都比"翻牌失败"更糟，因为
它们看起来是成功的。

为什么翻牌不做 Qdrant 调用
--------------------------

Qdrant 没有事务，把它塞进 PG 事务里只有两种结局：网络一抖就把一次正确的翻牌
回滚掉（可用性），或者 PG 提交成功而 Qdrant 请求丢失（一致性）。正确的是把
"向量库该是什么样"写进同一个事务（`vector_sync_tasks`），由同步器在事务外消费。
这样两件事同生共死，而 Qdrant 的延迟不再影响翻牌的成败。

新版本的前置条件
----------------

翻牌只接受 `pending` 版本，且**必须已经有切片**：没有切片的版本置 active 会让
旧版立刻下线、而新版一条也召不回——这是"翻牌把服务翻没了"的最短路径。空版本的
正确处理是把它判 failed（解析没产出东西），不是生效。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from api.ingest.doclock import lock_document
from api.ingest.sync import upsert_desired
from api.observability import get_logger, log_event

logger = get_logger("api.ingest.flip")


class FlipError(RuntimeError):
    """翻牌前置条件不满足。消息面向运维，包含 doc_key 与 version_id。."""


class VersionNotFoundError(FlipError):
    """VersionNotFoundError."""

    def __init__(self, version_id: str) -> None:
        """Initialize the instance."""
        super().__init__(f"version not found: {version_id}")
        self.version_id = version_id


class NotPendingError(FlipError):
    """要翻的版本不是 pending。.

    重复翻牌会走到这里——那是幂等的正常结果，不是 bug。
    """

    def __init__(self, version_id: str, status: str) -> None:
        """Initialize the instance."""
        super().__init__(f"version {version_id} is {status}, not pending")
        self.version_id = version_id
        self.status = status


class EmptyVersionError(FlipError):
    """版本下没有可检索切片。翻它等于把线上服务翻空。."""

    def __init__(self, version_id: str) -> None:
        """Initialize the instance."""
        super().__init__(
            f"version {version_id} has no chunks; refusing to activate"
        )
        self.version_id = version_id


@dataclass(slots=True)
class FlipResult:
    """FlipResult."""

    doc_key: str
    version_id: str
    previous_active: str | None = None
    activated_chunks: int = 0
    deprecated_versions: list[str] = field(default_factory=list)
    # 本次翻牌写进 vector_sync_tasks 的期望状态（version_id -> desired）
    desired: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "doc_key": self.doc_key,
            "version_id": self.version_id,
            "previous_active": self.previous_active,
            "activated_chunks": self.activated_chunks,
            "deprecated_versions": list(self.deprecated_versions),
            "vector_sync": dict(self.desired),
        }


# --- SQL ---------------------------------------------------------------------

SELECT_VERSION_SQL = """
SELECT version_id, doc_key, status FROM doc_versions WHERE version_id = %s
"""

# 旧 active 一次性拿下：同一事务里 `ux_active_version` 还会再拦一次，但那时错误
# 信息只剩"唯一约束冲突"，说不清是哪一份文档、哪个版本在抢。
DEPRECATE_ACTIVE_SQL = """
UPDATE doc_versions
SET status = 'deprecated',
    deprecated_at = now(),
    superseded_by = %s
WHERE doc_key = %s AND status = 'active' AND version_id <> %s
RETURNING version_id
"""

ACTIVATE_SQL = """
UPDATE doc_versions
SET status = 'active', activated_at = now(), deprecated_at = NULL
WHERE version_id = %s AND status = 'pending'
RETURNING version_id
"""

COUNT_CHUNKS_SQL = "SELECT count(*) FROM chunks WHERE version_id = %s"

ACTIVATE_CHUNKS_SQL = """
UPDATE chunks SET status = 'active'
WHERE version_id = %s AND status <> 'active'
"""

# 旧版切片：只有原本 active 的才降级。pending 切片保持 pending——它是"没上过线"的
# 中间产物，标成 deprecated 会让人误以为它曾经生效过。
DEPRECATE_CHUNKS_SQL = """
UPDATE chunks SET status = 'deprecated'
WHERE version_id = %s AND status = 'active'
"""

DOCUMENT_ACTIVE_SQL = (
    "UPDATE documents SET status = 'active' WHERE doc_key = %s"
)


def flip(
    conn: Connection[Any],
    version_id: str,
    *,
    wait_for_lock: bool = True,
) -> FlipResult:
    """把 `version_id` 翻成该文档的唯一生效版本。.

    **调用方负责 commit/rollback。**

    幂等性：
      - 重复翻同一个版本 → `NotPendingError`（已 active）。调用方可以把它当
        "已完成"。
      - 已被别的版本取代（deprecated）→ 也走 `NotPendingError`，走回滚请用
        `rollback_to`。
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SELECT_VERSION_SQL, (version_id,))
        row = cur.fetchone()
    if row is None:
        raise VersionNotFoundError(version_id)

    doc_key = str(row["doc_key"])
    status = str(row["status"])

    # 锁必须在**读状态之后、写之前**拿不到才安全——反过来（先读后锁）会读到
    # 别人事务中间的状态。所以顺序是：先锁文档，再重读状态。这里为了给出准确
    # 的 VersionNotFoundError 先读了一次，锁住后还会再读一次（用锁内的状态做
    # 判断）。
    lock_document(conn, doc_key, wait=wait_for_lock)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SELECT_VERSION_SQL, (version_id,))
        locked = cur.fetchone()
    if locked is None:
        raise VersionNotFoundError(version_id)
    status = str(locked["status"])
    if status != "pending":
        raise NotPendingError(version_id, status)

    with conn.cursor() as cur:
        cur.execute(COUNT_CHUNKS_SQL, (version_id,))
        chunk_count = int(cur.fetchone()[0])
    if chunk_count == 0:
        raise EmptyVersionError(version_id)

    result = FlipResult(doc_key=doc_key, version_id=version_id)

    with conn.cursor() as cur:
        cur.execute(DEPRECATE_ACTIVE_SQL, (version_id, doc_key, version_id))
        result.deprecated_versions = [str(r[0]) for r in cur.fetchall()]
    result.previous_active = (
        result.deprecated_versions[0] if result.deprecated_versions else None
    )

    with conn.cursor() as cur:
        cur.execute(ACTIVATE_SQL, (version_id,))
        if cur.fetchone() is None:  # pragma: no cover - 上面的状态检查已保证
            raise NotPendingError(version_id, status)

    with conn.cursor() as cur:
        cur.execute(ACTIVATE_CHUNKS_SQL, (version_id,))
        result.activated_chunks = cur.rowcount

    # 旧版切片同步降级（PG 侧）。向量库侧由 vector_sync_tasks 驱动。
    for old_version in result.deprecated_versions:
        with conn.cursor() as cur:
            cur.execute(DEPRECATE_CHUNKS_SQL, (old_version,))

    with conn.cursor() as cur:
        cur.execute(DOCUMENT_ACTIVE_SQL, (doc_key,))

    # 期望状态与翻牌同事务写入：这是"Qdrant 一定知道发生了翻牌"的唯一保证。
    result.desired[version_id] = "active"
    for old_version in result.deprecated_versions:
        result.desired[old_version] = "deprecated"
    for target, desired in result.desired.items():
        upsert_desired(
            conn, doc_key=doc_key, version_id=target, desired_status=desired
        )

    log_event(
        logger,
        "version_flipped",
        doc_key=doc_key,
        version_id=version_id,
        previous_active=result.previous_active,
        activated_chunks=result.activated_chunks,
    )
    return result


@dataclass(slots=True)
class RollbackResult:
    """RollbackResult."""

    doc_key: str
    restored: str
    demoted: list[str] = field(default_factory=list)
    activated_chunks: int = 0
    desired: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "doc_key": self.doc_key,
            "restored": self.restored,
            "demoted": list(self.demoted),
            "activated_chunks": self.activated_chunks,
            "vector_sync": dict(self.desired),
        }


# 回滚：24h 内把旧版翻回生效（§4.4「翻牌后发现问题要回滚」）。
# 与 flip 的区别只在"源状态"：flip 要求 pending，这里要求 deprecated。
RESTORE_SQL = """
UPDATE doc_versions
SET status = 'active', activated_at = now(), deprecated_at = NULL
WHERE version_id = %s AND status = 'deprecated'
RETURNING version_id
"""

DEMOTE_ACTIVE_SQL = """
UPDATE doc_versions
SET status = 'deprecated', deprecated_at = now(), superseded_by = %s
WHERE doc_key = %s AND status = 'active' AND version_id <> %s
RETURNING version_id
"""


def rollback_to(
    conn: Connection[Any], version_id: str, *, wait_for_lock: bool = True
) -> RollbackResult:
    """把一个已下线的版本翻回生效（回滚窗口内）。切道路径与 flip 完全对称。."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SELECT_VERSION_SQL, (version_id,))
        row = cur.fetchone()
    if row is None:
        raise VersionNotFoundError(version_id)
    doc_key = str(row["doc_key"])
    lock_document(conn, doc_key, wait=wait_for_lock)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SELECT_VERSION_SQL, (version_id,))
        locked = cur.fetchone()
    if locked is None:
        raise VersionNotFoundError(version_id)
    if str(locked["status"]) != "deprecated":
        raise NotPendingError(version_id, str(locked["status"]))

    result = RollbackResult(doc_key=doc_key, restored=version_id)

    with conn.cursor() as cur:
        cur.execute(DEMOTE_ACTIVE_SQL, (version_id, doc_key, version_id))
        result.demoted = [str(r[0]) for r in cur.fetchall()]

    # 顺序：先把当前 active 降级，再把目标版翻上来。反过来会撞
    # `ux_active_version`——两个 active 在库里同时存在的瞬间就被拒
    # （实测 UniqueViolation）。
    # flip() 里的顺序是"降级 -> 激活"，这里必须一致。
    with conn.cursor() as cur:
        cur.execute(RESTORE_SQL, (version_id,))
        if cur.fetchone() is None:  # pragma: no cover
            raise FlipError(f"version {version_id} could not be restored")

    with conn.cursor() as cur:
        cur.execute(ACTIVATE_CHUNKS_SQL, (version_id,))
        result.activated_chunks = cur.rowcount
    for demoted in result.demoted:
        with conn.cursor() as cur:
            cur.execute(DEPRECATE_CHUNKS_SQL, (demoted,))

    with conn.cursor() as cur:
        cur.execute(DOCUMENT_ACTIVE_SQL, (doc_key,))

    result.desired[version_id] = "active"
    for demoted in result.demoted:
        result.desired[demoted] = "deprecated"
    for target, desired in result.desired.items():
        upsert_desired(
            conn, doc_key=doc_key, version_id=target, desired_status=desired
        )

    log_event(
        logger,
        "version_rolled_back",
        doc_key=doc_key,
        version_id=version_id,
        demoted=result.demoted,
    )
    return result


def active_version(conn: Connection[Any], doc_key: str) -> str | None:
    """Run active_version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_id FROM doc_versions "
            "WHERE doc_key = %s AND status = 'active'",
            (doc_key,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


__all__ = [
    "EmptyVersionError",
    "FlipError",
    "FlipResult",
    "NotPendingError",
    "RollbackResult",
    "VersionNotFoundError",
    "active_version",
    "flip",
    "rollback_to",
]

# Backwards-compatible alias (N818: exception names use Error suffix).
VersionNotFound = VersionNotFoundError

# Backwards-compatible alias (N818: exception names use Error suffix).
NotPending = NotPendingError

# Backwards-compatible alias (N818: exception names use Error suffix).
EmptyVersion = EmptyVersionError
