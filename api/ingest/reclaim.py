"""回收：旧版本先下线、留 24 小时、再删（详细设计.md §4.3）。.

删除是级联的：切片行、向量点、切片图一起清；**样本原件不删**（它是测试资产）。
删除失败不卡线上：先删库行（用户可感知的那一面已经安全），向量点进期望状态
`purged` 由同步器补删——§4.4「删除时向量库不可用时，先删库行，向量点进待删清单，
恢复后补删」正是这个形状。

为什么窗口是 24 小时而不是"立刻删"
----------------------------------

保留窗口是给回滚留的门：新版解析错了，窗口内还能把旧版翻回来（§4.4），超过窗口
就只能重跑解析。所以窗口的起点必须是**下线那一刻**，不是"生效那一刻"
（001 的索引建在 activated_at 上，是个过期口径，003 补了 `deprecated_at`）。

`deprecated_at IS NULL` 的处理
------------------------------

这类行是别的模块（或测试夹具）直接改状态留下的。**不能猜它刚下线**就删——那等于
把窗口清零；也**不能永远不删**——那会让回收任务每次都在同一批行上空转。正确做法是
先补上 `now()`（从此刻开始计时）并跳过本轮：未知下线时间按"刚下线"处理，下一轮才
可能删。代价是晚一轮（几分钟），收益是绝不提前删掉可回滚的版本。

切片图（figures/）
------------------

`figures/{chunk_id}.webp` 是 §3.4 的裁图。删图失败不影响库的一致性（重跑解析会
重新裁），所以单独计数、不当作回收失败——但要在报告里能看到，否则"图一直删不掉"
会悄悄把磁盘吃满。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from api.ingest.doclock import lock_document
from api.ingest.sync import upsert_desired
from api.observability import get_logger, log_event, log_exception

logger = get_logger("api.ingest.reclaim")


class RetentionNotReachedError(RuntimeError):
    """还没到回收时间。给"人工点一次回收"用：明确告诉你最早什么时候能删。."""

    def __init__(
        self, version_id: str, deprecated_at: Any, retention_hours: int
    ) -> None:
        """Initialize the instance."""
        super().__init__(
            f"version {version_id} deprecated at {deprecated_at}, "
            f"retention is {retention_hours}h"
        )
        self.version_id = version_id


@dataclass(slots=True)
class ReclaimItem:
    """ReclaimItem."""

    version_id: str
    doc_key: str
    chunks: int = 0
    figures: int = 0
    figure_errors: int = 0
    # 点删不掉时置 true：库行已删，点在 vector_sync_tasks 里等同步器补删
    deferred_points: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "version_id": self.version_id,
            "doc_key": self.doc_key,
            "chunks": self.chunks,
            "figures": self.figures,
            "figure_errors": self.figure_errors,
            "deferred_points": self.deferred_points,
            "error": self.error,
        }


@dataclass(slots=True)
class ReclaimReport:
    """ReclaimReport."""

    retention_hours: int
    dry_run: bool
    items: list[ReclaimItem] = field(default_factory=list)
    skipped_recent: int = 0
    skipped_unknown_time: int = 0

    @property
    def purged(self) -> int:
        """Run purged."""
        return len(self.items)

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "retention_hours": self.retention_hours,
            "dry_run": self.dry_run,
            "purged": self.purged,
            "skipped_recent": self.skipped_recent,
            "chunks": sum(i.chunks for i in self.items),
            "figures": sum(i.figures for i in self.items),
            "items": [i.as_dict() for i in self.items],
        }


# --- SQL ---------------------------------------------------------------------

# 候选：已下线且过了窗口。`deprecated_at IS NULL` 的行单独用另一条语句处理
# （补时间戳），不混在这一条里——混进来会让"哪些是真空过了窗口"变得不可读。
DUE_SQL = """
SELECT version_id, doc_key, deprecated_at
FROM doc_versions
WHERE status = 'deprecated'
  AND deprecated_at IS NOT NULL
  AND deprecated_at <= now() - make_interval(hours => %(hours)s)
  AND (%(doc_key)s::text IS NULL OR doc_key = %(doc_key)s)
ORDER BY deprecated_at
LIMIT %(limit)s
"""

UNKNOWN_TIME_SQL = """
SELECT version_id, doc_key FROM doc_versions
WHERE status = 'deprecated' AND deprecated_at IS NULL
LIMIT %(limit)s
"""

BACKFILL_TIME_SQL = """
UPDATE doc_versions SET deprecated_at = now()
WHERE version_id = %s AND deprecated_at IS NULL
"""

CHUNK_IDS_SQL = "SELECT chunk_id FROM chunks WHERE version_id = %s"

# 先删切片（chunks 有 FK ON DELETE CASCADE 到版本，这里显式删是为了拿到 chunk_id
# 列表用于删图和统计；直接删版本会让 CASCADE 静默带走，事后查不到删了多少）。
DELETE_CHUNKS_SQL = "DELETE FROM chunks WHERE version_id = %s"

MARK_PURGED_SQL = """
UPDATE doc_versions SET status = 'purged'
WHERE version_id = %s AND status = 'deprecated'
"""


def reclaim(
    conn: Connection[Any],
    *,
    retention_hours: int = 24,
    figures_dir: Path | None = None,
    doc_key: str | None = None,
    limit: int = 100,
    dry_run: bool = False,
    only_version: str | None = None,
) -> ReclaimReport:
    """回收过期版本。.

    **调用方负责 commit**（每个版本一个事务边界更清楚，见下方说明）。

    `only_version` 用于定向回收（脚本/测试），此时忽略窗口与 limit——但仍然
    要求该版本确实是 `deprecated`。
    """
    report = ReclaimReport(retention_hours=retention_hours, dry_run=dry_run)

    if only_version is not None:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT version_id, doc_key, status, deprecated_at "
                "FROM doc_versions "
                "WHERE version_id = %s",
                (only_version,),
            )
            row = cur.fetchone()
        if row is None:
            return report
        if str(row["status"]) != "deprecated":
            raise ReclaimError(
                f"version {only_version} is {row['status']}, not deprecated"
            )
        candidates = [row]
    else:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(UNKNOWN_TIME_SQL, {"limit": limit})
            unknown = list(cur.fetchall())
            cur.execute(
                DUE_SQL,
                {"hours": retention_hours, "doc_key": doc_key, "limit": limit},
            )
            candidates = list(cur.fetchall())

        # 未知下线时间：补时间戳、跳过本轮（见模块说明）
        for row in unknown:
            report.skipped_unknown_time += 1
            if dry_run:
                continue
            with conn.cursor() as cur:
                cur.execute(BACKFILL_TIME_SQL, (row["version_id"],))
            log_event(
                logger,
                "reclaim_backfilled_deprecated_at",
                version_id=row["version_id"],
                doc_key=row["doc_key"],
            )

    for row in candidates:
        item = _reclaim_one(
            conn,
            version_id=str(row["version_id"]),
            doc_key=str(row["doc_key"]),
            figures_dir=figures_dir,
            dry_run=dry_run,
        )
        report.items.append(item)

    log_event(
        logger,
        "reclaim_done",
        retention_hours=retention_hours,
        purged=report.purged,
        dry_run=dry_run,
        skipped_unknown_time=report.skipped_unknown_time,
    )
    return report


class ReclaimError(RuntimeError):
    """ReclaimError."""

    pass


def _reclaim_one(
    conn: Connection[Any],
    *,
    version_id: str,
    doc_key: str,
    figures_dir: Path | None,
    dry_run: bool,
) -> ReclaimItem:
    """Run _reclaim_one."""
    item = ReclaimItem(version_id=version_id, doc_key=doc_key)

    # 与扫描/翻牌抢同一把文档锁：回收正在删某版本的切片时，翻牌不该把它翻回
    # active（那会得到"版本 active 但切片已被删"的行）。dry_run 不拿锁——它
    # 什么都改不了，拿锁只会给线上的翻牌添堵。
    if not dry_run:
        lock_document(conn, doc_key)

    with conn.cursor() as cur:
        cur.execute(CHUNK_IDS_SQL, (version_id,))
        chunk_ids = [str(r[0]) for r in cur.fetchall()]
    item.chunks = len(chunk_ids)

    if dry_run:
        return item

    # 先写"点该删"的期望状态，再删库行：反过来如果进程崩在中间，库里已经没有
    # 版本行，就再也没有东西能告诉同步器"这些点该删"——向量库里会留下永久孤儿点。
    upsert_desired(
        conn, doc_key=doc_key, version_id=version_id, desired_status="purged"
    )

    with conn.cursor() as cur:
        cur.execute(DELETE_CHUNKS_SQL, (version_id,))
        cur.execute(MARK_PURGED_SQL, (version_id,))
        if cur.rowcount == 0:  # pragma: no cover - 候选查询已限定 deprecated
            raise ReclaimError(
                f"version {version_id} left deprecated state during reclaim"
            )

    if figures_dir is not None:
        item.figures, item.figure_errors = _delete_figures(
            figures_dir, chunk_ids
        )

    log_event(
        logger,
        "version_purged",
        doc_key=doc_key,
        version_id=version_id,
        chunks=item.chunks,
        figures=item.figures,
    )
    return item


def _delete_figures(figures_dir: Path, chunk_ids: list[str]) -> tuple[int, int]:
    """删 `figures/{chunk_id}.webp`。返回 (真删掉的, 失败数)。.

    只把**真的删掉了文件**计进返回值。`missing_ok=True` 让"文件本来就不存在"
    不报错（表格/正文切片没有图，这是常态），但它也不该被算成"删了一张
    图"——否则报告上的数字会随目录里的残留状态变化，运维没法用它判断磁盘
    是否真的清干净了。

    失败不抛：图丢了可以重裁，库一致性不受影响；但计数要报出来，否则磁盘会被悄悄
    吃满。
    """
    deleted = 0
    errors = 0
    for chunk_id in chunk_ids:
        path = figures_dir / f"{chunk_id}.webp"
        try:
            if path.exists():
                path.unlink()
                deleted += 1
        except OSError:
            errors += 1
            log_exception(
                logger,
                "figure_delete_failed",
                chunk_id=chunk_id,
                path=str(path),
            )
    return deleted, errors


def due_count(
    conn: Connection[Any],
    *,
    retention_hours: int = 24,
    doc_key: str | None = None,
) -> int:
    """还有多少个版本已过窗口（回收任务的"待办"指标）。."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM doc_versions WHERE status = 'deprecated' "
            "  AND deprecated_at IS NOT NULL "
            "  AND deprecated_at <= now() - make_interval(hours => %(hours)s) "
            "  AND (%(doc_key)s::text IS NULL OR doc_key = %(doc_key)s)",
            {"hours": retention_hours, "doc_key": doc_key},
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


__all__ = [
    "ReclaimError",
    "ReclaimItem",
    "ReclaimReport",
    "RetentionNotReachedError",
    "due_count",
    "reclaim",
]

# Backwards-compatible alias (N818: exception names use Error suffix).
RetentionNotReached = RetentionNotReachedError
