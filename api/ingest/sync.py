"""向量库副本的状态同步、重试与漂移账（详细设计.md §4.2）。.

三件事，各自只有一个入口：

    upsert_desired()   翻牌/回滚/回收事务里写"向量库该是什么样"（同事务，不丢）
    drain()            消费就绪的期望状态：改 payload.status / 删点，
                       失败退避重试
    record_drift()     重试耗尽或对账发现不一致时留痕，让漂移被数出来

为什么是"期望状态"而不是"任务流水"
----------------------------------

翻牌是幂等的（同一版本短时间内可能被翻来翻去），任务流水会引入"两条相反指令
谁先谁后"的问题；期望状态永远只有一句"它现在应该是 X"，重放天然幂等。
`vector_sync_tasks` 一个版本一行（迁移 003 里有完整说明）。

为什么用租约（lease）而不是把行锁住做完 HTTP
--------------------------------------------

`SELECT ... FOR UPDATE` 后用同一条连接发网络请求，会让"一次 Qdrant 抖动"变成一个
持锁几十秒的长事务，把同一份文档的下一次翻牌全挡住。租约的做法是：抢到的行把
`next_attempt_at` 推到未来（= 租出去），**先提交**，再做 HTTP，最后回写结果。
进程中途崩掉时租约到期，任务自动重新可领——不需要"清理僵尸任务"的额外流程。

重试的口径
----------

`vector_sync_max_attempts=4` 是**总尝试次数**（1 次尝试 + 3 次重试），与 §4.2
"同步失败重试三次"一致。退避 `base * 2^(attempt-1)`，默认 1/2/4 秒：翻牌后必须
一分钟内同步完，退避不能变成"指数退避到十分钟"。

校验不是"请求没报错"
--------------------

Qdrant 的 200 只说明它收下了请求，不说明状态对了（filter 写错时它照样返回
completed，一条都不匹配）。所以每次同步后按 version_id 回读计数，与 PG 里该版本
的切片状态分布对账：desired=active 时"该版本 active 点数"必须等于 PG 的
active 切片数；desired=deprecated 时 active 点数必须为 0；desired=purged 时
该版本点数必须为 0。对不上就判失败并进重试——这才是"同步成功"的定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api.ingest.vectors import QdrantVectorStore, VectorStoreError
from api.observability import get_logger, log_event, log_exception

logger = get_logger("api.ingest.sync")

# 租约时长：抢到任务后这段时间内别的 worker 不会重领。
# 一次按版本改 payload 是毫秒级，60s 足够覆盖一次 Qdrant 抖动；崩了也只是
# 等 60s。
LEASE_SECONDS = 60


# --------------------------------------------------------------- desired state

UPSERT_DESIRED_SQL = """
INSERT INTO vector_sync_tasks (version_id, doc_key, desired_status, status)
VALUES (%(version_id)s, %(doc_key)s, %(desired)s, 'pending')
ON CONFLICT (version_id) DO UPDATE
SET doc_key      = EXCLUDED.doc_key,
    desired_status = EXCLUDED.desired_status,
    -- 期望变了就重置重试计数与失败态：上一轮的失败是"上一件事"的失败，
    -- 不该让一次新的翻牌继承它、一上来就没额度。
    status       = CASE
                     WHEN vector_sync_tasks.desired_status
                          IS DISTINCT FROM EXCLUDED.desired_status
                       THEN 'pending'
                     ELSE vector_sync_tasks.status
                   END,
    attempts     = CASE
                     WHEN vector_sync_tasks.desired_status
                          IS DISTINCT FROM EXCLUDED.desired_status
                       THEN 0
                     ELSE vector_sync_tasks.attempts
                   END,
    next_attempt_at = CASE
                     WHEN vector_sync_tasks.desired_status
                          IS DISTINCT FROM EXCLUDED.desired_status
                       THEN now()
                     ELSE vector_sync_tasks.next_attempt_at
                   END,
    last_error   = CASE
                     WHEN vector_sync_tasks.desired_status
                          IS DISTINCT FROM EXCLUDED.desired_status
                       THEN NULL
                     ELSE vector_sync_tasks.last_error
                   END,
    updated_at   = now()
RETURNING version_id, desired_status, status
"""


def upsert_desired(
    conn: Connection[Any], *, doc_key: str, version_id: str, desired_status: str
) -> dict[str, Any]:
    """写一条期望状态。**必须与造成它的状态变更在同一个事务里**（见模块说明）。.

    幂等：同一版本重复写同一个 desired 不会重置重试计数，也不会把已达成的 done
    打回 pending——否则每小时的对账会把已经同步好的版本反复重推。
    """
    if desired_status not in ("active", "deprecated", "purged"):
        raise ValueError(f"unsupported desired status: {desired_status}")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            UPSERT_DESIRED_SQL,
            {
                "version_id": version_id,
                "doc_key": doc_key,
                "desired": desired_status,
            },
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover - RETURNING 一定有一行
        raise RuntimeError("upsert_desired returned no row")
    return row


# ------------------------------------------------------------------- drift log

INSERT_DRIFT_SQL = """
INSERT INTO vector_drift_log
    (doc_key, chunk_id, version_id, kind, pg_value, qdrant_value, dedupe_key)
VALUES (%(doc_key)s, %(chunk_id)s, %(version_id)s, %(kind)s,
        %(pg_value)s, %(qdrant_value)s, %(dedupe_key)s)
ON CONFLICT (dedupe_key) WHERE NOT fixed DO NOTHING
RETURNING id
"""


def record_drift(
    conn: Connection[Any],
    *,
    doc_key: str,
    kind: str,
    chunk_id: str | None = None,
    version_id: str | None = None,
    pg_value: str | None = None,
    qdrant_value: str | None = None,
    dedupe_key: str | None = None,
) -> int | None:
    """记一条漂移。同一处未修复的漂移只留一行（`uq_vector_drift_open_dedupe`）。.

    返回新插入的行 id；`None` 表示这条已经记过且尚未修复（去重命中）。
    """
    key = (
        dedupe_key or f"{kind}:{doc_key}:{chunk_id or '-'}:{version_id or '-'}"
    )
    with conn.cursor() as cur:
        cur.execute(
            INSERT_DRIFT_SQL,
            {
                "doc_key": doc_key,
                "chunk_id": chunk_id,
                "version_id": version_id,
                "kind": kind,
                "pg_value": pg_value,
                "qdrant_value": qdrant_value,
                "dedupe_key": key,
            },
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


MARK_DRIFT_FIXED_SQL = """
UPDATE vector_drift_log SET fixed = true, fixed_at = now(), fix_error = NULL
WHERE dedupe_key = %s AND NOT fixed
"""

MARK_DRIFT_FAILED_SQL = """
UPDATE vector_drift_log SET fix_error = %s
WHERE dedupe_key = %s AND NOT fixed
"""


def mark_drift_fixed(conn: Connection[Any], dedupe_key: str) -> int:
    """Run mark_drift_fixed."""
    with conn.cursor() as cur:
        cur.execute(MARK_DRIFT_FIXED_SQL, (dedupe_key,))
        return cur.rowcount


def mark_drift_fix_failed(
    conn: Connection[Any], dedupe_key: str, error: str
) -> None:
    """Run mark_drift_fix_failed."""
    with conn.cursor() as cur:
        cur.execute(MARK_DRIFT_FAILED_SQL, (error[:500], dedupe_key))


# ------------------------------------------------------------------- claiming

CLAIM_SQL = """
WITH next AS (
    SELECT version_id
    FROM vector_sync_tasks
    WHERE status = 'pending' AND next_attempt_at <= now()
    ORDER BY next_attempt_at, updated_at, version_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE vector_sync_tasks t
SET attempts = t.attempts + 1,
    next_attempt_at = now() + make_interval(secs => %(lease)s),
    updated_at = now()
FROM next
WHERE t.version_id = next.version_id
RETURNING t.version_id, t.doc_key, t.desired_status, t.attempts
"""


def claim_due(
    conn: Connection[Any], *, lease_s: int = LEASE_SECONDS
) -> dict[str, Any] | None:
    """领一条到期的期望状态（租约式，见模块说明）。没有则返回 None。."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(CLAIM_SQL, {"lease": lease_s})
        return cur.fetchone()


MARK_DONE_SQL = """
UPDATE vector_sync_tasks
SET status = 'done', synced_status = desired_status, synced_at = now(),
    last_error = NULL, updated_at = now()
WHERE version_id = %s
"""

MARK_FAILED_SQL = """
UPDATE vector_sync_tasks
SET status = CASE WHEN attempts >= %(max_attempts)s THEN 'failed'
                  ELSE 'pending' END,
    last_error = %(error)s,
    next_attempt_at = now() + make_interval(secs => %(backoff)s),
    updated_at = now()
WHERE version_id = %(version_id)s
RETURNING status, attempts
"""


def mark_done(conn: Connection[Any], version_id: str) -> None:
    """Run mark_done."""
    with conn.cursor() as cur:
        cur.execute(MARK_DONE_SQL, (version_id,))


def mark_failed(
    conn: Connection[Any],
    version_id: str,
    error: str,
    *,
    max_attempts: int,
    backoff_s: float,
) -> tuple[str, int]:
    """记一次失败：还有额度就退回 pending 并退避，用完就 failed。.

    `backoff_s` 是**这一次**要等多久（调用方按 attempts 算指数退避），不是基数——
    把公式留在调用方，这里只负责落库。
    """
    if not error or not error.strip():
        raise ValueError(
            "a failed vector sync must carry a non-empty error message"
        )
    with conn.cursor() as cur:
        cur.execute(
            MARK_FAILED_SQL,
            {
                "version_id": version_id,
                "error": error[:500],
                "max_attempts": max_attempts,
                "backoff": backoff_s,
            },
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError(
            f"vector_sync_tasks {version_id} disappeared while marking failure"
        )
    return str(row[0]), int(row[1])


def backoff_for(attempts: int, base_s: float) -> float:
    """第 `attempts` 次失败后的等待：base * 2^(attempts-1)，封顶 8×base。."""
    exponent = max(0, attempts - 1)
    return base_s * (2 ** min(exponent, 3))


# ------------------------------------------------------------------ applying

PG_CHUNK_STATUS_COUNTS_SQL = """
SELECT status, count(*) AS n FROM chunks WHERE version_id = %s GROUP BY status
"""


@dataclass(slots=True)
class SyncOutcome:
    """SyncOutcome."""

    version_id: str
    doc_key: str
    desired: str
    ok: bool
    detail: str = ""
    attempts: int = 1
    status: str = "pending"


def apply_desired(
    conn: Connection[Any],
    store: QdrantVectorStore,
    *,
    version_id: str,
    desired: str,
) -> None:
    """把一条期望状态推到 Qdrant。失败抛异常（由 drain 记进重试）。."""
    if desired == "purged":
        store.delete_version_points(version_id)
        return
    store.set_status_by_version(version_id, desired)


def verify_desired(
    conn: Connection[Any],
    store: QdrantVectorStore,
    *,
    version_id: str,
    desired: str,
) -> tuple[bool, str]:
    """回读校验：Qdrant 的实际分布必须与 PG 里该版本切片的分布一致。.

    这是"同步成功"的定义——只看 HTTP 状态码会把"filter 写错、一条都没匹配"
    当成成功（实测：filter 不匹配任何点时 Qdrant 照样返回 completed）。

    比较的是**两边的状态分布**，不是"有没有点"：
      - 该版本的点总数必须等于 PG 切片总数（除 purged）：少了就是丢了点，
        多了就是脏点。
      - `status=desired` 的点数必须等于 PG 里对应状态的切片数：这才证明
        payload 真的改对了。
    """
    with conn.cursor() as cur:
        cur.execute(PG_CHUNK_STATUS_COUNTS_SQL, (version_id,))
        pg_counts = {str(r[0]): int(r[1]) for r in cur.fetchall()}
    pg_total = sum(pg_counts.values())

    if desired == "purged":
        actual = store.count_version(version_id)
        return (
            actual == 0,
            f"pg_chunks={pg_total} qdrant_points={actual} (expect 0)",
        )

    actual_total = store.count_version(version_id)
    if actual_total != pg_total:
        return False, f"point count: pg={pg_total} qdrant={actual_total}"

    expected_on_desired = pg_counts.get(desired, 0)
    actual_on_desired = store.count_version(version_id, status=desired)
    if actual_on_desired != expected_on_desired:
        return False, (
            f"status={desired}: pg={expected_on_desired} "
            f"qdrant={actual_on_desired} (pg_dist={pg_counts})"
        )
    return True, f"points={actual_total} status={desired}:{actual_on_desired}"


# ---------------------------------------------------------------------- drain


@dataclass(slots=True)
class DrainReport:
    """DrainReport."""

    processed: int = 0
    done: int = 0
    retried: int = 0
    failed: int = 0
    outcomes: list[SyncOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Run ok."""
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "processed": self.processed,
            "done": self.done,
            "retried": self.retried,
            "failed": self.failed,
            "outcomes": [
                {
                    "version_id": o.version_id,
                    "doc_key": o.doc_key,
                    "desired": o.desired,
                    "ok": o.ok,
                    "status": o.status,
                    "attempts": o.attempts,
                    "detail": o.detail,
                }
                for o in self.outcomes
            ],
        }


def drain(
    pool: ConnectionPool[Any],
    store: QdrantVectorStore,
    *,
    max_attempts: int = 4,
    backoff_s: float = 1.0,
    limit: int = 200,
) -> DrainReport:
    """把到期的期望状态全部推给 Qdrant（直到没有到期任务或达到 limit）。.

    每一条任务三个短事务：**领（提交）→ 应用 + 校验 → 回写**。
    见模块说明里的租约。
    `apply_desired` / `verify_desired` 只要一条连接（PG 读 + Qdrant 调用），
    所以它们各自开一个短事务，绝不把 HTTP 请求包在持锁事务里。
    """
    report = DrainReport()
    for _ in range(limit):
        with pool.connection() as conn:
            task = claim_due(conn)
            conn.commit()
        if task is None:
            break

        report.processed += 1
        version_id = str(task["version_id"])
        desired = str(task["desired_status"])
        attempts = int(task["attempts"])
        outcome = SyncOutcome(
            version_id=version_id,
            doc_key=str(task["doc_key"]),
            desired=desired,
            ok=False,
            attempts=attempts,
        )

        try:
            with pool.connection() as conn:
                apply_desired(
                    conn, store, version_id=version_id, desired=desired
                )
            with pool.connection() as conn:
                ok, detail = verify_desired(
                    conn, store, version_id=version_id, desired=desired
                )
            outcome.detail = detail
            outcome.ok = ok
            if not ok:
                raise VectorStoreError(f"verification failed: {detail}")
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落进重试或漂移账
            log_exception(
                logger,
                "vector_sync_failed",
                version_id=version_id,
                desired=desired,
            )
            with pool.connection() as conn:
                status, attempts_after = mark_failed(
                    conn,
                    version_id,
                    _short_error("vector_sync_failed", exc),
                    max_attempts=max_attempts,
                    backoff_s=backoff_for(attempts, backoff_s),
                )
                if status == "failed":
                    record_drift(
                        conn,
                        doc_key=str(task["doc_key"]),
                        version_id=version_id,
                        kind="sync_failed",
                        pg_value=desired,
                        qdrant_value=None,
                        dedupe_key=f"sync_failed:{version_id}:{desired}",
                    )
                conn.commit()
            outcome.status = status
            outcome.attempts = attempts_after
            if status == "failed":
                report.failed += 1
            else:
                report.retried += 1
            report.outcomes.append(outcome)
            continue

        with pool.connection() as conn:
            mark_done(conn, version_id)
            # 之前若留下"同一处的漂移未修"记录，这次同步成功后一并销账。
            mark_drift_fixed(conn, f"sync_failed:{version_id}:{desired}")
            mark_drift_fixed(conn, f"status_mismatch:{version_id}:{desired}")
            conn.commit()
        outcome.status = "done"
        report.done += 1
        report.outcomes.append(outcome)

    if report.processed:
        log_event(
            logger,
            "vector_sync_drained",
            processed=report.processed,
            done=report.done,
            retried=report.retried,
            failed=report.failed,
        )
    return report


def pending_count(conn: Connection[Any], *, doc_key: str | None = None) -> int:
    """还有多少条没同步完（验收"没有沉默漂移"的直接指标）。."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM vector_sync_tasks WHERE status <> 'done' "
            "  AND (%(doc_key)s::text IS NULL OR doc_key = %(doc_key)s)",
            {"doc_key": doc_key},
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def task_for(conn: Connection[Any], version_id: str) -> dict[str, Any] | None:
    """Run task_for."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM vector_sync_tasks WHERE version_id = %s",
            (version_id,),
        )
        return cur.fetchone()


def _short_error(prefix: str, exc: BaseException) -> str:
    """Run _short_error."""
    detail = str(exc).strip().replace("\n", " ")
    text = (
        f"{prefix}: {type(exc).__name__}: {detail}"
        if detail
        else f"{prefix}: {type(exc).__name__}"
    )
    return text[:500]


__all__ = [
    "DrainReport",
    "SyncOutcome",
    "apply_desired",
    "backoff_for",
    "claim_due",
    "drain",
    "mark_done",
    "mark_drift_fixed",
    "mark_failed",
    "pending_count",
    "record_drift",
    "task_for",
    "upsert_desired",
    "verify_desired",
]
