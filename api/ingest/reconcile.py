"""对账：以 PG 为准覆盖向量库（详细设计.md §4.2 / §4.3）。

前一环 `sync.drain()` 负责"让 Qdrant 知道发生了什么"，它可能失败、也可能被跳过
（进程崩在中途）。对账是第二道：**不看历史，只看现在两边长什么样**，把差异逐处
修掉，并把每一处记进 `vector_drift_log`。

三类差异，修法各不同
--------------------

| 差异 | 判据 | 修法 |
|---|---|---|
| `missing_point`  | PG 有该切片、Qdrant 没有它的点 | 用 PG 里已存的 `embedding_dense` 补写（**不调编码服务**） |
| `status_mismatch`| 同一切片两边 status 不同 | 按 PG 改 payload.status（按目标状态分组批量改） |
| `orphan_point`   | Qdrant 有点、PG 里没有该切片（或版本已 purged） | 按 point id 删点 |
| `version_mismatch` | payload.version_id 与 PG 不一致 | **不自动修**：这是绕过约束写库的数据事故，只记一条 open 漂移并告警 |

为什么按 chunk_id 而不是按版本整批修
------------------------------------

`drain` 的整批改是个优化（一个版本的 payload 状态总是一起变），而对账的前提恰恰是
"这个假设已经不成立"——所以对账必须逐点比对。差异通常只有几条，逐点改的代价可以
接受；整批改会把"只有一条错"修成"整批都对"的假象，掩盖真正的起因。

`version_mismatch` 为什么不修
-----------------------------

切片挂错版本在 PG 里被复合外键 `fk_chunks_version` 挡着，正常不可能发生；真发生了
说明有人绕过约束写过库。对账顺手"修好"会抹掉事故现场，正确动作是留痕 + 让人来看。

只读模式
--------

`dry_run=True` / `--dry-run` 只比对不写，返回同一份差异清单。给"先看看有多少漂移
再决定动不动手"用；也是验收手段——破一次同步之后，对账 dry-run 必须能**看见**它。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row

from api.ingest.sync import mark_drift_fix_failed, mark_drift_fixed, record_drift
from api.ingest.vectors import QdrantVectorStore, points_from_rows
from api.observability import get_logger, log_event, log_exception

logger = get_logger("api.ingest.reconcile")

# PG 侧"向量库里该有副本"的切片：版本与切片都不是 purged。
# 与 v_active_chunks 的区别：那个视图只取双 active（可检索集），对账要的是全集
# ——pending / deprecated 的切片也必须有副本（§4.3 说旧版点要留到保留窗口结束）。
PG_CHUNKS_SQL = """
SELECT c.chunk_id, c.doc_key, c.version_id, c.status, c.chunk_type, c.parent_id,
       c.page_num, c.content_text, c.effective_date, c.effective_year,
       c.date_confidence, c.is_public, c.allowed_depts, c.allowed_roles,
       c.embedding_dense
FROM chunks c
JOIN doc_versions v ON v.version_id = c.version_id
WHERE v.status <> 'purged' AND c.status <> 'purged'
  AND (%(doc_key)s::text IS NULL OR c.doc_key = %(doc_key)s)
ORDER BY c.chunk_id
"""


@dataclass(slots=True)
class Drift:
    kind: str
    doc_key: str
    chunk_id: str | None = None
    version_id: str | None = None
    pg_value: str | None = None
    qdrant_value: str | None = None
    point_id: str | None = None
    detail: str = ""
    fixed: bool = False
    fix_error: str | None = None

    @property
    def dedupe_key(self) -> str:
        """同一处未修复的漂移只留一行（迁移 003 的 `uq_vector_drift_open_dedupe`）。

        键里刻意**不含**两边取值：status 从 active 漂到 deprecated 再漂到 purged 是
        "同一处还在漂"，不是三条不同的漂移，否则一次坏同步能在账上刷出好几行。
        """
        return f"{self.kind}:{self.chunk_id or self.point_id or '-'}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "doc_key": self.doc_key,
            "chunk_id": self.chunk_id,
            "version_id": self.version_id,
            "pg_value": self.pg_value,
            "qdrant_value": self.qdrant_value,
            "detail": self.detail,
            "fixed": self.fixed,
            "fix_error": self.fix_error,
        }


@dataclass(slots=True)
class ReconcileReport:
    doc_key: str | None
    dry_run: bool
    pg_chunks: int = 0
    qdrant_points: int = 0
    drifts: list[Drift] = field(default_factory=list)

    @property
    def healed(self) -> list[Drift]:
        return [d for d in self.drifts if d.fixed]

    @property
    def remaining(self) -> list[Drift]:
        return [d for d in self.drifts if not d.fixed]

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for drift in self.drifts:
            counts[drift.kind] = counts.get(drift.kind, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_key": self.doc_key,
            "dry_run": self.dry_run,
            "pg_chunks": self.pg_chunks,
            "qdrant_points": self.qdrant_points,
            "drift_counts": self.by_kind(),
            "drifts": [d.as_dict() for d in self.drifts],
            "healed": len(self.healed),
            "remaining": len(self.remaining),
        }


def _load_pg_chunks(conn: Connection[Any], doc_key: str | None) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(PG_CHUNKS_SQL, {"doc_key": doc_key})
        return list(cur.fetchall())


def _scroll_all(store: QdrantVectorStore) -> list[dict[str, Any]]:
    """全集合滚动（不带 doc_key 时）。"""
    points: list[dict[str, Any]] = []
    offset: Any = None
    while True:
        body: dict[str, Any] = {"limit": 512, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        resp = store._call("POST", f"/collections/{store.collection}/points/scroll", body)
        result = ((resp or {}).get("result") or {}) if isinstance(resp, dict) else {}
        page = result.get("points") or []
        points.extend(page)
        offset = result.get("next_page_offset")
        if not offset or not page:
            return points


def _load_qdrant_points(
    store: QdrantVectorStore, doc_key: str | None
) -> list[dict[str, Any]]:
    return store.scroll_doc(doc_key) if doc_key else _scroll_all(store)


def diff(
    conn: Connection[Any], store: QdrantVectorStore, *, doc_key: str | None = None
) -> ReconcileReport:
    """只比对，不写库、不改 Qdrant。"""
    report = ReconcileReport(doc_key=doc_key, dry_run=True)
    pg_rows = _load_pg_chunks(conn, doc_key)
    points = _load_qdrant_points(store, doc_key)
    report.pg_chunks = len(pg_rows)
    report.qdrant_points = len(points)

    pg_by_chunk = {str(row["chunk_id"]): row for row in pg_rows}
    seen_chunks: set[str] = set()

    for point in points:
        payload = point.get("payload") or {}
        chunk_id = payload.get("chunk_id")
        if not chunk_id:
            report.drifts.append(
                Drift(
                    kind="orphan_point",
                    doc_key=str(payload.get("doc_key") or "?"),
                    version_id=payload.get("version_id"),
                    qdrant_value=str(payload.get("status")),
                    point_id=str(point["id"]),
                    detail="point has no payload.chunk_id; cannot be mapped back to PG",
                )
            )
            continue
        chunk_id = str(chunk_id)
        seen_chunks.add(chunk_id)
        row = pg_by_chunk.get(chunk_id)
        if row is None:
            report.drifts.append(
                Drift(
                    kind="orphan_point",
                    doc_key=str(payload.get("doc_key") or "?"),
                    chunk_id=chunk_id,
                    version_id=payload.get("version_id"),
                    qdrant_value=str(payload.get("status")),
                    point_id=str(point["id"]),
                    detail="Qdrant has a point PG no longer has (chunk deleted or version purged)",
                )
            )
            continue

        pg_status = str(row["status"])
        qdrant_status = payload.get("status")
        if qdrant_status != pg_status:
            report.drifts.append(
                Drift(
                    kind="status_mismatch",
                    doc_key=str(row["doc_key"]),
                    chunk_id=chunk_id,
                    version_id=str(row["version_id"]),
                    pg_value=pg_status,
                    qdrant_value=str(qdrant_status) if qdrant_status is not None else None,
                    point_id=str(point["id"]),
                    detail=f"status pg={pg_status} qdrant={qdrant_status}",
                )
            )
        if payload.get("version_id") != str(row["version_id"]):
            report.drifts.append(
                Drift(
                    kind="version_mismatch",
                    doc_key=str(row["doc_key"]),
                    chunk_id=chunk_id,
                    version_id=str(row["version_id"]),
                    pg_value=str(row["version_id"]),
                    qdrant_value=payload.get("version_id"),
                    point_id=str(point["id"]),
                    detail="payload.version_id disagrees with PG (data incident; not auto-fixed)",
                )
            )

    for chunk_id, row in pg_by_chunk.items():
        if chunk_id in seen_chunks:
            continue
        report.drifts.append(
            Drift(
                kind="missing_point",
                doc_key=str(row["doc_key"]),
                chunk_id=chunk_id,
                version_id=str(row["version_id"]),
                pg_value=str(row["status"]),
                qdrant_value=None,
                detail="PG has the chunk but Qdrant has no point for it",
            )
        )
    return report


def reconcile(
    conn: Connection[Any],
    store: QdrantVectorStore,
    *,
    doc_key: str | None = None,
    dry_run: bool = False,
    log_drift: bool = True,
) -> ReconcileReport:
    """比对并（默认）按 PG 修好。`dry_run=True` 等价于 `diff()`。

    修复顺序：先补点、再改状态、最后清孤儿。补点必须在改状态之前——缺的点补回来时
    payload 已经带着 PG 的 status，再改一遍是多余动作；反过来先改状态会把"点根本不在"
    误判成"状态已经对了"。
    """
    report = diff(conn, store, doc_key=doc_key)
    report.dry_run = dry_run
    if dry_run:
        return report

    pg_rows = {str(row["chunk_id"]): row for row in _load_pg_chunks(conn, doc_key)}

    # 1) 补点（用 PG 已存的 embedding_dense，不调编码服务）
    ready = [
        pg_rows[d.chunk_id]
        for d in report.drifts
        if d.kind == "missing_point"
        and d.chunk_id in pg_rows
        and pg_rows[d.chunk_id].get("embedding_dense") is not None
    ]
    if ready:
        try:
            store.upsert(points_from_rows(ready))
            for row in ready:
                _mark(report, kind="missing_point", chunk_id=str(row["chunk_id"]), fixed=True)
        except Exception as exc:  # noqa: BLE001 - 修不了就留痕，不让对账整体失败
            log_exception(logger, "reconcile_backfill_failed")
            for row in ready:
                _mark(
                    report,
                    kind="missing_point",
                    chunk_id=str(row["chunk_id"]),
                    fixed=False,
                    error=str(exc),
                )
    for drift in report.drifts:
        if drift.kind == "missing_point" and not drift.fixed and drift.fix_error is None:
            drift.fix_error = "PG has no embedding_dense for this chunk; re-index its version"

    # 2) 改状态（按目标状态分组，一批一次往返）
    by_target: dict[str, list[str]] = {}
    for drift in report.drifts:
        if drift.kind == "status_mismatch" and drift.chunk_id and drift.pg_value:
            by_target.setdefault(drift.pg_value, []).append(drift.chunk_id)
    for status, chunk_ids in by_target.items():
        try:
            store.set_status_by_chunk_ids(chunk_ids, status)
            for chunk_id in chunk_ids:
                _mark(report, kind="status_mismatch", chunk_id=chunk_id, fixed=True)
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "reconcile_status_failed", status=status)
            for chunk_id in chunk_ids:
                _mark(
                    report, kind="status_mismatch", chunk_id=chunk_id, fixed=False, error=str(exc)
                )

    # 3) 清孤儿
    orphans = [d for d in report.drifts if d.kind == "orphan_point"]
    if orphans:
        try:
            store.delete_points([d.point_id for d in orphans if d.point_id])
            for drift in orphans:
                drift.fixed = True
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "reconcile_orphan_delete_failed")
            for drift in orphans:
                drift.fix_error = str(exc)

    # 4) 留痕：修好的销账、修不了的记一条 open
    if log_drift:
        for drift in report.drifts:
            try:
                if drift.fixed:
                    mark_drift_fixed(conn, drift.dedupe_key)
                    continue
                record_drift(
                    conn,
                    doc_key=drift.doc_key,
                    chunk_id=drift.chunk_id,
                    version_id=drift.version_id,
                    kind=drift.kind,
                    pg_value=drift.pg_value,
                    qdrant_value=drift.qdrant_value,
                    dedupe_key=drift.dedupe_key,
                )
                if drift.fix_error:
                    mark_drift_fix_failed(conn, drift.dedupe_key, drift.fix_error)
            except Exception:  # noqa: BLE001 - 留痕失败不该让对账整体失败
                log_exception(logger, "drift_log_write_failed", kind=drift.kind)

    log_event(
        logger,
        "reconcile_done",
        doc_key=doc_key,
        pg_chunks=report.pg_chunks,
        qdrant_points=report.qdrant_points,
        drift_counts=report.by_kind(),
        healed=len(report.healed),
        remaining=len(report.remaining),
    )
    return report


def _mark(
    report: ReconcileReport,
    *,
    kind: str,
    chunk_id: str,
    fixed: bool,
    error: str | None = None,
) -> None:
    """按 (kind, chunk_id) 标记修复结果。同一 chunk 可能同时有 status/version 两类漂移，
    所以不能只按 chunk_id 匹配。"""
    for drift in report.drifts:
        if drift.kind == kind and drift.chunk_id == chunk_id:
            drift.fixed = fixed
            if error:
                drift.fix_error = error


__all__ = ["Drift", "ReconcileReport", "diff", "reconcile"]
