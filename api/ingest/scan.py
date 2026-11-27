"""扫 `samples/` 并写库。.

口径见详细设计.md §3.1、附录 C.1 的 `POST /api/v1/ingest/scan`。

一次扫描做四件事，每件都只做一次：

1. **拿单任务锁**（`pg_try_advisory_lock`）。拿不到就是"正在跑"，抛
   `ScanInProgressError`，接口层转 409。不排队：扫描是人工/定时触发的
   运维动作，排队只会在几秒后跑一轮已经过时的扫描。
2. **列样本清单**（`fingerprint.list_samples`），并先做一次
   **doc_key 撞车检查**：两个文件归一化到同一个 key 时，两份都记失败
   （§4.4「撞车即拒绝入库」），其余文件照常——不能因为一个命名冲突把
   整轮扫描全废掉。
3. **逐文件判定**（`decide.classify`）并按结论写库。**一个文件一个事务**：
   某个文件写失败只回滚它自己，其它文件的结果保留。这也让"文件读不到 → 记失败、
   不动已有版本"能落地：失败任务在独立事务里写完就走。
4. **报计数**：`added / changed / skipped / failed` + 每条明细。

### 锁的实现细节（踩过的坑）

- 用**会话级** `pg_try_advisory_lock` 而不是事务级 `pg_advisory_xact_lock`：
  整轮扫描跨多个事务（一个文件一个），事务级锁在第一个 commit 就释放了，第二个
  扫描立刻能进来。
- 锁与所有写**共用同一条连接**。若锁挂在连接 A、写走连接 B，B 的连接池可能排在
  持有锁的 A 后面等，形成自己等自己的死锁。
- `finally` 里显式 unlock；unlock 失败就关掉连接——连接断了锁自动释放，总比让一个
  僵尸锁把之后的每次扫描都判成 409 强。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from api.clients.pg import PgDatabase
from api.ingest import jobs as job_store
from api.ingest.decide import Outcome, SkipReason, classify
from api.ingest.fingerprint import (
    SampleFile,
    file_hash,
    list_samples,
    version_id_for,
)
from api.ingest.lockkeys import SCAN_LOCK_KEY
from api.observability import get_logger, log_event, log_exception

logger = get_logger("api.ingest.scan")

# 单个错误消息落库前的截断长度：`ingest_jobs.error` 存"异常首行"，完整栈进日志。
ERROR_MAX_CHARS = 500

# 文档状态：`documents.status` 的口径（附录 A.1 的四个值）
DOC_STATUS_QUEUED = "queued"
DOC_STATUS_ACTIVE = "active"


class ScanInProgressError(RuntimeError):
    """已有扫描在跑。接口层据此返回 409（附录 C.1「409 任务进行中」）。."""

    def __init__(self) -> None:
        """Initialize the instance."""
        super().__init__("another sample scan is already running")


class SamplesRootMissingError(RuntimeError):
    """样本目录不存在或不是目录。.

    和"目录是空的"必须分开：空目录是正常状态（还没放样本），扫出 0 条；
    目录不存在则说明挂载没挂上或路径配错了，那时返回"扫到 0 个文件"会让运维
    以为一切正常，实际上一个文件都没看到过。
    """

    def __init__(self, root: Path) -> None:
        """Initialize the instance."""
        super().__init__(
            f"samples directory is missing or not a directory: {root}"
        )
        self.root = root


@dataclass(slots=True)
class ScanItem:
    """一个文件的扫描结论（明细，给日志与接口的 `items` 用）。."""

    doc_key: str
    filename: str
    outcome: Outcome
    reason: SkipReason | None = None
    version_id: str | None = None
    job_id: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        payload: dict[str, Any] = {
            "doc_key": self.doc_key,
            "filename": self.filename,
            "outcome": self.outcome.value,
        }
        for name in ("reason", "version_id", "job_id", "error"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = (
                    value.value if isinstance(value, SkipReason) else value
                )
        return payload


@dataclass(slots=True)
class ScanReport:
    """一轮扫描的结果。`counts` 的四个键就是附录 C.1 约定的返回口径。."""

    items: list[ScanItem] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Run counts."""
        tally = Counter(item.outcome.value for item in self.items)
        # 四个键恒定出现：接口的消费者不该因为"这轮没有新文件"就少一个字段。
        return {
            key: tally.get(key, 0) for key in ("add", "change", "skip", "fail")
        }

    @property
    def added(self) -> list[ScanItem]:
        """Run added."""
        return [item for item in self.items if item.outcome is Outcome.ADD]

    @property
    def changed(self) -> list[ScanItem]:
        """Run changed."""
        return [item for item in self.items if item.outcome is Outcome.CHANGE]

    @property
    def skipped(self) -> list[ScanItem]:
        """Run skipped."""
        return [item for item in self.items if item.outcome is Outcome.SKIP]

    @property
    def failed(self) -> list[ScanItem]:
        """Run failed."""
        return [item for item in self.items if item.outcome is Outcome.FAIL]

    def as_dict(self) -> dict[str, Any]:
        """接口响应体：`added/changed/skipped/failed` 是**计数**（附录 C.1）.

        明细放 `items`，另有按原因码的分布便于一眼看出跳过是正常还是异常。.
        """
        counts = self.counts
        return {
            "added": counts["add"],
            "changed": counts["change"],
            "skipped": counts["skip"],
            "failed": counts["fail"],
            "reasons": dict(
                sorted(
                    Counter(
                        item.reason.value
                        for item in self.items
                        if item.reason is not None
                    ).items()
                )
            ),
            "items": [item.as_dict() for item in self.items],
        }


def scan_samples(
    pg: PgDatabase,
    samples_root: Path,
    *,
    max_retries: int = 3,
) -> ScanReport:
    """扫一轮目录。拿不到锁抛 `ScanInProgressError`；其余异常不会中断整轮。.

    目录不存在时**先抛 `SamplesRootMissingError`，不拿锁**：拿锁再抛会让一次
    配置错误在日志里留下一条"扫描开始过"的痕迹，而它其实什么都没扫。
    """
    if not samples_root.is_dir():
        raise SamplesRootMissingError(samples_root)

    samples = list_samples(samples_root)
    report = ScanReport()

    with pg.session() as conn:
        _acquire_scan_lock(conn)
        try:
            for sample, collision in _collision_free(samples):
                if collision is not None:
                    report.items.append(collision)
                    continue
                report.items.append(
                    _scan_one(conn, sample, max_retries=max_retries)
                )
        finally:
            _release_scan_lock(conn)

    counts = report.counts
    log_event(
        logger,
        "samples_scanned",
        root=str(samples_root),
        files=len(samples),
        added=counts["add"],
        changed=counts["change"],
        skipped=counts["skip"],
        failed=counts["fail"],
    )
    return report


# --- 锁 ----------------------------------------------------------------------
#
# 这里有一处**实测过的顺序陷阱**（B2 在 `test_ingest_scan_integration` 上撞到
# 5 个用例连环报 409）：
#
#   `pg_try_advisory_lock` 是**会话级**锁，而 `conn.commit()` /
#   `conn.rollback()`
#   会把连接**还给池**；还池时 psycopg_pool 会 reset 连接，但 reset **不释放**
#   advisory lock（实测：reset 之后 `pg_locks` 里那条 advisory 记录还在）。
#
#   于是"先 commit 再把连接还池、等会儿拿另一条连接去 unlock"这件事不成立：
#   unlock 发给了另一个会话，原来的锁留在原后端上直到那个连接被回收。表现是
#   "第一次扫描之后，后面每次扫描都 409"——而且时好时坏（取决于池把哪条
#   连接给你）。
#
# 正确做法有两条，这里都做：
#
#   1. 拿锁与放锁**在同一个 `with pg.session()` 块里**（同一后端），
#      中间不 commit 拿到的锁（commit 会还连接，但我们在同一个块里所以拿
#      回来的是同一条）；
#   2. unlock 之后再 commit —— 先解锁，再让连接回到池里。
#
# 契约：调用方必须在一个 `pool.connection()` / `session()` 块内成对调用，
# 且不要在两者之间 commit。`scan_samples()` 就是这么用的。


def _acquire_scan_lock(conn: Connection[Any]) -> None:
    """会话级尝试拿锁。拿到返回 None，拿不到抛 `ScanInProgressError`。.

    **不 commit**：commit 会让连接还池，锁可能落在另一条前端连接上（见上文）。
    锁本身不依赖事务，所以不 commit 也能生效。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SCAN_LOCK_KEY,))
        row = cur.fetchone()
    if not row or not row[0]:
        raise ScanInProgressError()


def _release_scan_lock(conn: Connection[Any]) -> None:
    """解锁后提交。解锁失败就关连接——连接断了锁自动释放，总比留僵尸锁强。."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SCAN_LOCK_KEY,))
            row = cur.fetchone()
        if not row or not row[0]:
            # 解锁返回 false = 这把锁本来不在当前会话上（说明拿锁与放锁不是同一
            # 后端）。这时锁还挂在别的后端上，只能靠关连接强制释放——但我们的
            # 连接上并没有它，所以只能记一条日志，让运维知道发生了"错位"。
            log_event(logger, "scan_lock_release_missed", level=40)
        conn.commit()
    except Exception:  # noqa: BLE001 - 释放失败必须让连接死掉，否则锁会变僵尸
        log_exception(logger, "scan_lock_release_failed")
        try:
            conn.rollback()
            conn.close()
        except Exception:  # noqa: BLE001
            log_exception(logger, "scan_lock_connection_close_failed")


# --- 撞车检查 ----------------------------------------------------------------


def _collision_free(
    samples: Sequence[SampleFile],
) -> list[tuple[SampleFile, ScanItem | None]]:
    """把 doc_key 撞车的样本挑出来，剩下的原样返回。.

    返回 `(sample, collision_item_or_None)`；`collision` 非空表示这条已判定
    为失败、不需要再碰数据库。同一 key 下的**所有**文件都判失败（不只后来
    的那个）：
    谁先谁后取决于文件名排序，按顺序取一个会让结果随改名而变。
    """
    by_key: dict[str, list[SampleFile]] = {}
    for sample in samples:
        by_key.setdefault(sample.doc_key, []).append(sample)

    out: list[tuple[SampleFile, ScanItem | None]] = []
    for sample in samples:
        group = by_key[sample.doc_key]
        if len(group) == 1:
            out.append((sample, None))
            continue
        others = ", ".join(
            item.rel_path for item in group if item is not sample
        )
        out.append(
            (
                sample,
                ScanItem(
                    doc_key=sample.doc_key,
                    filename=sample.filename,
                    outcome=Outcome.FAIL,
                    reason=SkipReason.DOC_KEY_COLLISION,
                    error=(
                        f"doc_key_collision: {sample.rel_path} 与 {others} "
                        f"归一化到同一个 doc_key，拒绝入库（§4.4）"
                    ),
                ),
            )
        )
    return out


# --- 单文件 ------------------------------------------------------------------


def _scan_one(
    conn: Connection[Any], sample: SampleFile, *, max_retries: int
) -> ScanItem:
    """判定并写库。整个过程在**一个事务**里，失败只回滚这一个文件。."""
    try:
        content_hash = file_hash(sample.path)
    except OSError as exc:
        return _record_unreadable(conn, sample, exc)

    version_id = version_id_for(sample.doc_key, content_hash)
    try:
        item = _apply(
            conn, sample, content_hash=content_hash, version_id=version_id
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - 单文件失败不该中断整轮
        conn.rollback()
        log_exception(logger, "scan_file_failed", doc_key=sample.doc_key)
        return _record_failure(conn, sample, exc)
    return item


def _apply(
    conn: Connection[Any],
    sample: SampleFile,
    *,
    content_hash: str,
    version_id: str,
) -> ScanItem:
    # 与 A3 的翻牌事务抢同一把文档级锁：扫描正在给这份文档建新版本时，
    # 翻牌不该同时把它置 active（否则"新版本待转正"会瞬间变成 active）。
    """Run _apply."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (sample.doc_key,)
        )

    doc_status = _lock_document(conn, sample.doc_key)
    existing = _version_status(conn, sample.doc_key, content_hash)
    outcome, reason = classify(
        doc_exists=doc_status is not None, existing_status=existing
    )

    if outcome is Outcome.SKIP:
        existing_version = _existing_version_id(
            conn, sample.doc_key, content_hash
        )
        log_event(
            logger,
            "scan_skipped",
            doc_key=sample.doc_key,
            reason=reason.value if reason else None,
            version_id=existing_version,
        )
        return ScanItem(
            doc_key=sample.doc_key,
            filename=sample.filename,
            outcome=outcome,
            reason=reason,
            version_id=existing_version,
        )

    active = _active_version_id(conn, sample.doc_key)
    if doc_status is None:
        _insert_document(conn, sample, status=DOC_STATUS_QUEUED)
    else:
        # `filename` 是展示名（附录 A.1），改名不该产生新版本，只更新展示。
        # `status`：有 active 版本就保持 active——新版本待转正不影响旧版本继续
        # 服务（验收口径"指纹变化创建新版本（pending），旧 active 不受影响"）。
        # 没有 active 版本时置 queued，表示"还没有可服务版本"。
        _update_document(
            conn,
            sample,
            status=DOC_STATUS_ACTIVE if active else DOC_STATUS_QUEUED,
        )

    _insert_version(
        conn,
        version_id=version_id,
        doc_key=sample.doc_key,
        content_hash=content_hash,
    )
    job_id = job_store.enqueue_job(
        conn, doc_key=sample.doc_key, version_id=version_id
    )

    log_event(
        logger,
        "scan_enqueued",
        doc_key=sample.doc_key,
        version_id=version_id,
        job_id=job_id,
        outcome=outcome.value,
        supersedes_active=active,
    )
    return ScanItem(
        doc_key=sample.doc_key,
        filename=sample.filename,
        outcome=outcome,
        version_id=version_id,
        job_id=job_id,
    )


def _record_unreadable(
    conn: Connection[Any], sample: SampleFile, exc: OSError
) -> ScanItem:
    """文件读不到：记一条失败任务，不建版本、不动已有版本（§3.1 判定表）。.

    这里没有 version_id 可挂——正是 001 给 `ingest_jobs.version_id` 留空、
    且不挂 documents 外键的原因。
    """
    conn.rollback()
    error = _short_error(SkipReason.UNREADABLE.value, exc)
    try:
        job_id = job_store.enqueue_job(
            conn, doc_key=sample.doc_key, version_id=None
        )
        job_store.terminal_fail_job(conn, job_id, error)
        conn.commit()
    except Exception:  # noqa: BLE001 - 连失败任务都写不进去，只能报在响应里
        conn.rollback()
        log_exception(
            logger, "scan_unreadable_job_failed", doc_key=sample.doc_key
        )
        job_id = None
    log_event(
        logger,
        "scan_unreadable",
        doc_key=sample.doc_key,
        path=sample.rel_path,
        error=error,
    )
    return ScanItem(
        doc_key=sample.doc_key,
        filename=sample.filename,
        outcome=Outcome.FAIL,
        reason=SkipReason.UNREADABLE,
        job_id=job_id,
        error=error,
    )


def _record_failure(
    conn: Connection[Any], sample: SampleFile, exc: BaseException
) -> ScanItem:
    """写库本身失败（约束冲突、连接抖动……）。回滚后单独记一条失败任务。.

    `version_id` 也置空：那个版本行已经随回滚消失了，挂一个不存在的版本号会让
    `ingest_jobs` 指向幽灵。
    """
    error = _short_error("scan_write_failed", exc)
    job_id: int | None = None
    try:
        job_id = job_store.enqueue_job(
            conn, doc_key=sample.doc_key, version_id=None
        )
        job_store.terminal_fail_job(conn, job_id, error)
        conn.commit()
    except Exception:  # noqa: BLE001
        conn.rollback()
        log_exception(logger, "scan_failure_job_failed", doc_key=sample.doc_key)
    return ScanItem(
        doc_key=sample.doc_key,
        filename=sample.filename,
        outcome=Outcome.FAIL,
        error=error,
        job_id=job_id,
    )


def _short_error(prefix: str, exc: BaseException) -> str:
    """失败原因：`前缀: 异常类型: 首行`，限长。.

    完整栈进日志，库里只留能 grep 的一行。
    """
    detail = str(exc).strip().replace("\n", " ")
    text = (
        f"{prefix}: {type(exc).__name__}: {detail}"
        if detail
        else f"{prefix}: {type(exc).__name__}"
    )
    return text[:ERROR_MAX_CHARS]


# --- SQL ---------------------------------------------------------------------


def _lock_document(conn: Connection[Any], doc_key: str) -> str | None:
    """取 documents.status 并锁行。.

    `FOR UPDATE` 让同一文档的并发写入串行化——目前只有 A3 的翻牌会碰这些行，
    但"扫描写 documents"和"翻牌写 doc_versions"落在同一份文档上时，行锁能把
    "读到状态 → 据此决定写什么"这个窗口关掉一半（另一半由上面的咨询锁负责）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM documents WHERE doc_key = %s FOR UPDATE",
            (doc_key,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _version_status(
    conn: Connection[Any], doc_key: str, content_hash: str
) -> str | None:
    """同 (doc_key, file_hash) 的版本状态。.

    没有就是 None（这套内容还没进来过）。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM doc_versions "
            "WHERE doc_key = %s AND file_hash = %s",
            (doc_key, content_hash),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _existing_version_id(
    conn: Connection[Any], doc_key: str, content_hash: str
) -> str | None:
    """Run _existing_version_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_id FROM doc_versions "
            "WHERE doc_key = %s AND file_hash = %s",
            (doc_key, content_hash),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


def _active_version_id(conn: Connection[Any], doc_key: str) -> str | None:
    """Run _active_version_id."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_id FROM doc_versions "
            "WHERE doc_key = %s AND status = 'active'",
            (doc_key,),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


INSERT_DOCUMENT_SQL = """
INSERT INTO documents (doc_key, filename, status)
VALUES (%s, %s, %s)
"""


def _insert_document(
    conn: Connection[Any], sample: SampleFile, *, status: str
) -> None:
    """Run _insert_document."""
    with conn.cursor() as cur:
        cur.execute(
            INSERT_DOCUMENT_SQL, (sample.doc_key, sample.filename, status)
        )


UPDATE_DOCUMENT_SQL = (
    "UPDATE documents SET filename = %s, status = %s WHERE doc_key = %s"
)


def _update_document(
    conn: Connection[Any], sample: SampleFile, *, status: str
) -> None:
    """Run _update_document."""
    with conn.cursor() as cur:
        cur.execute(
            UPDATE_DOCUMENT_SQL, (sample.filename, status, sample.doc_key)
        )


INSERT_VERSION_SQL = """
INSERT INTO doc_versions (version_id, doc_key, file_hash, status)
VALUES (%s, %s, %s, 'pending')
"""


def _insert_version(
    conn: Connection[Any], *, version_id: str, doc_key: str, content_hash: str
) -> None:
    """建 pending 版本.

    **不置 active**：翻牌是 A3 的翻牌事务独占的动作
    （§4.1「状态只能由摄取任务和翻牌事务改」），扫描只负责把材料摆到待转正。.
    """
    with conn.cursor() as cur:
        cur.execute(INSERT_VERSION_SQL, (version_id, doc_key, content_hash))


# --- 文档查询（GET /api/v1/documents/{doc_key} 用） ---------------------------


DOCUMENT_SQL = """
SELECT d.doc_key, d.filename, d.page_count, d.status, d.created_at,
       v.version_id   AS active_version_id,
       v.file_hash    AS active_file_hash,
       v.activated_at AS active_activated_at,
       (SELECT count(*) FROM doc_versions x
         WHERE x.doc_key = d.doc_key) AS version_count,
       (SELECT count(*) FROM doc_versions x
         WHERE x.doc_key = d.doc_key AND x.status = 'pending') AS pending_count,
       (SELECT count(*) FROM chunks c
         WHERE c.doc_key = d.doc_key)  AS chunk_count
FROM documents d
LEFT JOIN doc_versions v ON v.doc_key = d.doc_key AND v.status = 'active'
WHERE d.doc_key = %s
"""


def get_document(conn: Connection[Any], doc_key: str) -> dict[str, Any] | None:
    """Run get_document."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(DOCUMENT_SQL, (doc_key,))
        return cur.fetchone()


__all__ = [
    "DOCUMENT_SQL",
    "SCAN_LOCK_KEY",
    "SamplesRootMissingError",
    "ScanInProgressError",
    "ScanItem",
    "ScanReport",
    "get_document",
    "scan_samples",
]

# Backwards-compatible alias (N818: exception names use Error suffix).
ScanInProgress = ScanInProgressError

# Backwards-compatible alias (N818: exception names use Error suffix).
SamplesRootMissing = SamplesRootMissingError
