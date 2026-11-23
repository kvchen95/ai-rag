"""`ingest_jobs` 状态机与重试计数（详细设计.md §3.1 / §4.4，附录 B）。

状态机定稿（`db/migrations/002_ingest_job_status.sql` 把它落成了 CHECK）：

```
            enqueue
              |
              v
           +------+   claim    +---------+  succeed  +-----------+
           |queued|----------->| running |---------->| succeeded |
           +------+            +---------+           +-----------+
              ^                  |     |
     requeue  |                  |     | terminal_fail / retry exhausted
   (retry+1)  +------------------+     v
                                    +--------+
                                    | failed |   （终态：不再回头）
                                    +--------+
                                     ^
           cancel                    |
        queued --------------------->+--> canceled  （终态）
```

三条规矩，写在这里就不用在 worker/接口里各记一遍：

1. **落库前置条件比文档更严**：`failed` 必须带 error、终态必须带 finished_at、
   `running` 必须带 started_at。002 里的 CHECK 与这里的 `_assert_*` 是同一套口径的
   两层——数据库拦得住，但错误信息只有断言能说清"缺的是哪一项"。
2. **重试不是重新入队新行**：`running -> queued` 原地把 `retry_count+1`。这样一条
   任务的"失败过几次"一直是同一行上的一个数，统计与审计对得上。
3. **failed 是终态**：重试计数耗尽后置 failed，不再自动回到 queued。否则"一直失败
   的文件"会每轮扫描都重跑一次，把日志刷满而看不出是同一个问题（§4.4 的"失败重试
   三次转人工清单"就是这个意思）。

所有函数都吃一个**已开启事务的连接**，不自己开事务：扫描要把 documents /
doc_versions / ingest_jobs 写在同一个事务里，失败整体回滚（§4.1 的"三处一起成功
或一起失败"精神）。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row


class JobStatus(StrEnum):
    """与 `ck_ingest_jobs_status` 的五个值逐字对应。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED})

# 允许的迁移。表里没有的一律 `IllegalTransition`——不做"自动修一下"，
# 因为这条状态机的错都是并发的错，猜出来的修法只会把问题推到更难查的地方。
ALLOWED: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.QUEUED, JobStatus.SUCCEEDED, JobStatus.FAILED}
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """状态机不允许的迁移。消息里带 job_id 与前后状态，日志里能直接定位。"""

    def __init__(self, job_id: int, src: str, dst: str) -> None:
        super().__init__(f"ingest_jobs#{job_id}: {src} -> {dst} is not allowed")
        self.job_id = job_id
        self.src = src
        self.dst = dst


class UnknownJob(LookupError):
    def __init__(self, job_id: int) -> None:
        super().__init__(f"ingest_jobs#{job_id} not found")
        self.job_id = job_id


def can_transition(src: str, dst: str) -> bool:
    return JobStatus(dst) in ALLOWED[JobStatus(src)]


def _transition(job_id: int, src: str, dst: str) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(job_id, src, dst)


# --- 写操作 ------------------------------------------------------------------

ENQUEUE_SQL = """
INSERT INTO ingest_jobs (doc_key, version_id, status)
VALUES (%s, %s, 'queued')
RETURNING job_id
"""


def enqueue_job(conn: Connection[Any], *, doc_key: str, version_id: str | None) -> int:
    """入队。同一版本已有未结束任务时由 `uq_ingest_jobs_open_version` 拦住（唯一冲突）。

    `version_id` 可空是 001 特意留的：文件读不到时只记一条失败任务，那时还没有版本行。
    """
    with conn.cursor() as cur:
        cur.execute(ENQUEUE_SQL, (doc_key, version_id))
        row = cur.fetchone()
    if row is None:  # pragma: no cover - RETURNING 一定有一行
        raise RuntimeError("enqueue_job returned no job_id")
    return int(row[0])


CLAIM_SQL = """
WITH next AS (
    SELECT job_id
    FROM ingest_jobs
    WHERE status = 'queued'
      AND (%(doc_key)s::text IS NULL OR doc_key = %(doc_key)s)
    ORDER BY created_at, job_id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE ingest_jobs j
SET status = 'running', started_at = now()
FROM next
WHERE j.job_id = next.job_id
RETURNING j.job_id, j.doc_key, j.version_id, j.retry_count, j.created_at
"""


def claim_next_job(
    conn: Connection[Any], *, doc_key: str | None = None
) -> dict[str, Any] | None:
    """取一条 queued 任务并置 running（先进先出）。

    `FOR UPDATE SKIP LOCKED`：多个 worker 同时取任务时互相跳过对方已锁的行，
    不用应用层再排一次队。没有可跑的任务返回 `None`，不是异常。

    `doc_key` 可选：worker 想按文档串行（同一文档的两个版本不该并行解析）或
    定向调试某份文档时用。不传就是全局先进先出。
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(CLAIM_SQL, {"doc_key": doc_key})
        return cur.fetchone()


SUCCEED_SQL = """
UPDATE ingest_jobs
SET status = 'succeeded', finished_at = now(), error = NULL
WHERE job_id = %s AND status = 'running'
RETURNING job_id
"""


def succeed_job(conn: Connection[Any], job_id: int) -> None:
    """running -> succeeded。清掉 error：成功行留着上次失败原因会误导排障。"""
    with conn.cursor() as cur:
        cur.execute(SUCCEED_SQL, (job_id,))
        if cur.fetchone() is None:
            raise IllegalTransition(job_id, _current_status(conn, job_id), "succeeded")


FAIL_SQL = """
UPDATE ingest_jobs
SET status = CASE WHEN retry_count < %s THEN 'queued' ELSE 'failed' END,
    retry_count = CASE WHEN retry_count < %s THEN retry_count + 1 ELSE retry_count END,
    started_at  = CASE WHEN retry_count < %s THEN NULL ELSE started_at END,
    finished_at = CASE WHEN retry_count < %s THEN NULL ELSE now() END,
    error = %s
WHERE job_id = %s AND status = 'running'
RETURNING job_id, status, retry_count
"""


def fail_job(
    conn: Connection[Any], job_id: int, error: str, *, max_retries: int = 3
) -> tuple[JobStatus, int]:
    """running 失败：还能重试就回 queued（retry_count+1），用尽就 failed（终态）。

    返回 (新状态, 当前 retry_count)，调用方据此决定要不要告警。

    `max_retries=3` 的口径是"1 次尝试 + 3 次重试 = 最多 4 次执行机会"，与 §4.4
    "同步失败重试三次"一致。`retry_count` 记的是**已经重试过的次数**，所以
    `retry_count < max_retries` 就是"还有额度"。

    幂等性：只更新 `status='running'` 的行。worker 崩了以后被重投的同一 job 再来
    一次 fail，会因为已经是 queued/failed 而报 IllegalTransition，而不是把计数
    再加一次——重复计数会让"重试几次后转人工"的判断失真。
    """
    _assert_error(error)
    with conn.cursor() as cur:
        cur.execute(
            FAIL_SQL,
            (max_retries, max_retries, max_retries, max_retries, error, job_id),
        )
        row = cur.fetchone()
    if row is None:
        raise IllegalTransition(job_id, _current_status(conn, job_id), "failed|queued")
    return JobStatus(row[1]), int(row[2])


TERMINAL_FAIL_SQL = """
UPDATE ingest_jobs
SET status = 'failed', finished_at = now(), error = %s
WHERE job_id = %s AND status IN ('queued', 'running')
RETURNING job_id
"""


def terminal_fail_job(conn: Connection[Any], job_id: int, error: str) -> None:
    """直接判 failed（不计重试）。用于"重试也没意义"的失败：文件消失、样本格式不支持。

    queued 也能直接判失败：扫描期就发现文件读不到时，任务还没有被任何 worker
    claim 过，让它排队等一个必然失败的解析没有意义。
    """
    _assert_error(error)
    with conn.cursor() as cur:
        cur.execute(TERMINAL_FAIL_SQL, (error, job_id))
        if cur.fetchone() is None:
            raise IllegalTransition(job_id, _current_status(conn, job_id), "failed")


CANCEL_SQL = """
UPDATE ingest_jobs
SET status = 'canceled', finished_at = now()
WHERE job_id = %s AND status IN ('queued', 'running')
RETURNING job_id
"""


def cancel_job(conn: Connection[Any], job_id: int) -> None:
    """人工取消。running 也能取消：worker 下一轮心跳发现状态变了就退出（由 A2/A3 实现）。"""
    with conn.cursor() as cur:
        cur.execute(CANCEL_SQL, (job_id,))
        if cur.fetchone() is None:
            raise IllegalTransition(job_id, _current_status(conn, job_id), "canceled")


# --- 读操作 ------------------------------------------------------------------


def _current_status(conn: Connection[Any], job_id: int) -> str:
    """给 IllegalTransition 用的当前状态；行不存在时抛 UnknownJob。"""
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM ingest_jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    if row is None:
        raise UnknownJob(job_id)
    return str(row[0])


def get_job(conn: Connection[Any], job_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM ingest_jobs WHERE job_id = %s", (job_id,))
        return cur.fetchone()


def latest_job_for_doc(conn: Connection[Any], doc_key: str) -> dict[str, Any] | None:
    """某文档最近一条任务。`GET /api/v1/documents/{doc_key}` 的"最近错误"取它。"""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM ingest_jobs WHERE doc_key = %s "
            "ORDER BY created_at DESC, job_id DESC LIMIT 1",
            (doc_key,),
        )
        return cur.fetchone()


def open_job_count(conn: Connection[Any], *, doc_key: str | None = None) -> int:
    """未结束任务数（queued + running）。给"队列积压"告警与验收用。

    `doc_key` 可选：按文档看"还在跑吗"。全局计数在有真实文档在跑的库上
    永远不为 0，用它做单文档断言只会得到噪声。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM ingest_jobs "
            "WHERE status IN ('queued', 'running') "
            "  AND (%(doc_key)s::text IS NULL OR doc_key = %(doc_key)s)",
            {"doc_key": doc_key},
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def retry_budget_left(status: str, retry_count: int, *, max_retries: int = 3) -> int:
    """还剩几次重试机会。终态一律 0——不给人"再等等它自己会重试"的错觉。"""
    if JobStatus(status) in TERMINAL:
        return 0
    return max(0, max_retries - retry_count)


def _assert_error(error: str) -> None:
    """failed 行的 error 不能是空串：`ck_ingest_jobs_failed_has_error` 只拦 NULL，
    空白串能过 CHECK 但在排障时等于没记，所以在应用层也挡一道。"""
    if not error or not error.strip():
        raise ValueError("failed ingest job must carry a non-empty error message")
