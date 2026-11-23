#!/usr/bin/env python3
"""A3 运维入口：写向量、翻牌、同步、对账、回收（详细设计.md §4）。

子命令：

    index     <version_id>            写 Qdrant 点（dense + bm25），回填 embedding_dense
    promote   <version_id>            索引 -> 翻牌 -> 同步 -> 对账（一条命令把版本转正）
    flip      <version_id>            只翻牌（旧版下线、新版生效、切片状态同步）
    rollback  <version_id>            24h 内把旧版翻回生效
    sync                              消费期望状态（改 payload.status / 删点）
    reconcile [--doc-key K] [--dry-run] 两边对账；默认按 PG 修好
    reclaim   [--doc-key K] [--dry-run] 删过窗口的旧版本（切片+点+图）
    status                            一眼看现状：版本状态分布、待同步、未修漂移

设计取向：每个子命令都**可重复执行**，失败都有非零退出码。运维脚本最怕"跑完了
不知道算不算成功"，所以每条路径都打印一行机器可读 JSON（`--json`）加一行人类摘要。

用法（容器内，源码挂载所以改完即生效）：

    docker compose exec -T api python scripts/a3_ingest.py status
    docker compose exec -T api python scripts/a3_ingest.py promote policy/travel-cn#7b0bcd16bc6b
    docker compose exec -T api python scripts/a3_ingest.py reconcile --dry-run
    docker compose exec -T api python scripts/a3_ingest.py reclaim --retention-hours 0

宿主也能跑（需要 psycopg / httpx 与可达的 PG/Qdrant）：
    DATABASE_URL=postgresql://... QDRANT_URL=http://127.0.0.1:6333 \
    EMBEDDING_BASE_URL=http://127.0.0.1:7997/v1 ... python scripts/a3_ingest.py status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import ConnectionPool  # noqa: E402

from api.ingest.flip import FlipError, flip, rollback_to  # noqa: E402
from api.ingest.pipeline import promote  # noqa: E402
from api.ingest.reclaim import reclaim  # noqa: E402
from api.ingest.reconcile import reconcile  # noqa: E402
from api.ingest.sync import drain, pending_count  # noqa: E402
from api.ingest.vectors import Embedder, QdrantVectorStore, index_version  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


# --- 环境 ---------------------------------------------------------------------


def normalise_dsn(dsn: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres+psycopg://"):
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix) :]
    return dsn


def resolve_dsn(arg: str | None) -> str:
    if arg:
        return normalise_dsn(arg)
    env = os.getenv("DATABASE_URL") or os.getenv("PG_DSN")
    if env:
        return normalise_dsn(env)
    user = os.getenv("POSTGRES_USER", "ai_rag")
    pwd = os.getenv("POSTGRES_PASSWORD", "")
    auth = f"{user}:{pwd}@" if pwd else f"{user}@"
    host = os.getenv("PGHOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "ai_rag")
    return f"postgresql://{auth}{host}:{port}/{db}"


def build_embedder(args: argparse.Namespace) -> Embedder:
    return Embedder(
        base_url=args.embedding_url or os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:7997/v1"),
        model=args.embedding_model or os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
        dim=args.dim or int(os.getenv("EMBEDDING_DIM", "512")),
        api_key=os.getenv("EMBEDDING_API_KEY", "local"),
        batch_size=args.batch_size,
    )


def build_store(args: argparse.Namespace) -> QdrantVectorStore:
    return QdrantVectorStore(
        base_url=args.qdrant_url or os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
        collection=args.collection or os.getenv("QDRANT_COLLECTION", "kb_chunks"),
        api_key=os.getenv("QDRANT_API_KEY", ""),
    )


def build_pool(dsn: str, *, size: int = 4) -> ConnectionPool:
    return ConnectionPool(conninfo=dsn, min_size=1, max_size=size, open=True)


# --- 输出 ---------------------------------------------------------------------


def emit(payload: dict, *, args: argparse.Namespace, human: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(human)
        if args.verbose:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# --- 子命令 -------------------------------------------------------------------


def cmd_index(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    embedder = build_embedder(args)
    store = build_store(args)
    try:
        with pool.connection() as conn:
            report = index_version(
                conn,
                store,
                embedder,
                args.version_id,
                batch_size=args.batch_size,
                write_dense_back=not args.no_dense_backfill,
            )
            conn.commit()
        emit(
            {"index": report.as_dict(), "ok": report.ok},
            args=args,
            human=(
                f"indexed {report.points}/{report.rows} chunk(s) of {report.version_id} "
                f"(skipped={len(report.skipped)}, dense_back={report.dense_written})"
            ),
        )
        return EXIT_OK if report.ok else EXIT_FAILED
    finally:
        embedder.close()
        store.close()
        pool.close()


def cmd_promote(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    embedder = build_embedder(args)
    store = build_store(args)
    try:
        result = promote(
            pool,
            store,
            embedder,
            args.version_id,
            sync_max_attempts=args.sync_max_attempts,
            sync_backoff_s=args.sync_backoff,
            batch_size=args.batch_size,
            reconcile_after=not args.no_reconcile,
        )
        human = _promote_summary(result.as_dict())
        emit(result.as_dict(), args=args, human=human)
        return EXIT_OK if result.ok else EXIT_FAILED
    except FlipError as exc:
        # 版本不存在 / 不是 pending / 空版本：前置条件问题，不是脚本故障。
        # 命令行要给能 grep 的一行，不要一整段栈——运维看到栈只会以为代码坏了。
        emit(
            {"version_id": args.version_id, "ok": False, "error": str(exc)},
            args=args,
            human=f"promote refused: {type(exc).__name__}: {exc}",
        )
        return EXIT_FAILED
    finally:
        embedder.close()
        store.close()
        pool.close()


def _promote_summary(payload: dict) -> str:
    index = payload.get("index") or {}
    flip_payload = payload.get("flip") or {}
    drain_payload = payload.get("drain") or {}
    rec = payload.get("reconcile") or {}
    lines = [
        f"promote {payload['version_id']}: {'OK' if payload['ok'] else 'FAILED'}",
        f"  index    : points={index.get('points')}/{index.get('rows')} skipped={index.get('skipped')}",
        f"  flip     : previous={flip_payload.get('previous_active')} "
        f"activated_chunks={flip_payload.get('activated_chunks')}",
        f"  sync     : done={drain_payload.get('done')} retried={drain_payload.get('retried')} "
        f"failed={drain_payload.get('failed')}",
        f"  reconcile: healed={rec.get('healed')} remaining={rec.get('remaining')} "
        f"drift={rec.get('drift_counts')}",
    ]
    if payload.get("error"):
        lines.append(f"  error    : {payload['error']}")
    return "\n".join(lines)


def cmd_flip(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    store = build_store(args)
    embedder = build_embedder(args)
    try:
        with pool.connection() as conn:
            result = flip(conn, args.version_id)
            conn.commit()
        # 翻牌后立刻推一次同步：翻牌本身不做 Qdrant 调用（见 flip.py 说明），
        # 但运维命令跑完就该是"线上已经对了"的状态，否则要等 worker 的下一轮。
        sync = drain(
            pool,
            store,
            max_attempts=args.sync_max_attempts,
            backoff_s=args.sync_backoff,
        ) if not args.no_sync else None
        payload = {"flip": result.as_dict(), "drain": sync.as_dict() if sync else None}
        emit(
            payload,
            args=args,
            human=(
                f"flipped {result.version_id} (previous={result.previous_active}, "
                f"chunks={result.activated_chunks}, "
                f"sync_done={sync.done if sync else 'skipped'})"
            ),
        )
        return EXIT_OK if (sync is None or sync.ok) else EXIT_FAILED
    except FlipError as exc:
        emit({"ok": False, "error": str(exc)}, args=args, human=f"flip refused: {exc}")
        return EXIT_FAILED
    finally:
        embedder.close()
        store.close()
        pool.close()


def cmd_rollback(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    store = build_store(args)
    embedder = build_embedder(args)
    try:
        with pool.connection() as conn:
            result = rollback_to(conn, args.version_id)
            conn.commit()
        sync = drain(
            pool, store, max_attempts=args.sync_max_attempts, backoff_s=args.sync_backoff
        ) if not args.no_sync else None
        emit(
            {"rollback": result.as_dict(), "drain": sync.as_dict() if sync else None},
            args=args,
            human=(
                f"rolled back to {result.restored} (demoted={result.demoted}, "
                f"chunks={result.activated_chunks})"
            ),
        )
        return EXIT_OK if (sync is None or sync.ok) else EXIT_FAILED
    except FlipError as exc:
        emit({"ok": False, "error": str(exc)}, args=args, human=f"rollback refused: {exc}")
        return EXIT_FAILED
    finally:
        embedder.close()
        store.close()
        pool.close()


def cmd_sync(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    store = build_store(args)
    try:
        report = drain(
            pool,
            store,
            max_attempts=args.sync_max_attempts,
            backoff_s=args.sync_backoff,
            limit=args.limit,
        )
        emit(
            report.as_dict(),
            args=args,
            human=(
                f"sync: processed={report.processed} done={report.done} "
                f"retried={report.retried} failed={report.failed}"
            ),
        )
        return EXIT_OK if report.ok else EXIT_FAILED
    finally:
        store.close()
        pool.close()


def cmd_reconcile(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    store = build_store(args)
    try:
        with pool.connection() as conn:
            report = reconcile(
                conn, store, doc_key=args.doc_key, dry_run=args.dry_run, log_drift=not args.no_log
            )
            conn.commit()
        payload = report.as_dict()
        head = "reconcile (dry-run)" if args.dry_run else "reconcile"
        human = (
            f"{head}: pg={report.pg_chunks} qdrant={report.qdrant_points} "
            f"drift={report.by_kind()} healed={len(report.healed)} "
            f"remaining={len(report.remaining)}"
        )
        if report.remaining and args.verbose:
            human += "\n" + "\n".join(
                f"  {d.kind} {d.chunk_id or d.point_id}: "
                f"pg={d.pg_value} qdrant={d.qdrant_value} {d.fix_error or ''}"
                for d in report.remaining[:20]
            )
        emit(payload, args=args, human=human)
        # dry-run 发现漂移也算失败（给 CI/验收用：有漂移就不是 0）
        return EXIT_OK if not report.remaining else EXIT_FAILED
    finally:
        store.close()
        pool.close()


def cmd_reclaim(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    store = build_store(args)
    figures = Path(args.figures_dir or os.getenv("FIGURES_DIR", "figures"))
    try:
        with pool.connection() as conn:
            report = reclaim(
                conn,
                retention_hours=args.retention_hours,
                figures_dir=None if args.no_figures else figures,
                doc_key=args.doc_key,
                limit=args.limit,
                dry_run=args.dry_run,
                only_version=args.version_id,
            )
            conn.commit()
        # 点删除走同步器（库行已经删了，`purged` 期望状态会驱动它）
        sync = None
        if not args.dry_run and report.purged:
            sync = drain(
                pool, store, max_attempts=args.sync_max_attempts, backoff_s=args.sync_backoff
            )
        payload = report.as_dict()
        if sync is not None:
            payload["drain"] = sync.as_dict()
        emit(
            payload,
            args=args,
            human=(
                f"reclaim{'(dry-run)' if args.dry_run else ''}: "
                f"purged={report.purged} chunks={payload['chunks']} figures={payload['figures']} "
                f"backfilled={report.skipped_unknown_time}"
                + (f" sync_failed={sync.failed}" if sync else "")
            ),
        )
        return EXIT_OK if (sync is None or sync.ok) else EXIT_FAILED
    finally:
        store.close()
        pool.close()


STATUS_SQL = """
SELECT v.status AS version_status, count(*) AS n
FROM doc_versions v
WHERE (%(doc_key)s::text IS NULL OR v.doc_key = %(doc_key)s)
GROUP BY 1 ORDER BY 1
"""

CHUNK_STATUS_SQL = """
SELECT c.status AS chunk_status, count(*) AS n
FROM chunks c
WHERE (%(doc_key)s::text IS NULL OR c.doc_key = %(doc_key)s)
GROUP BY 1 ORDER BY 1
"""

ACTIVE_SQL = """
SELECT doc_key, count(*) AS n FROM doc_versions
WHERE status = 'active' AND (%(doc_key)s::text IS NULL OR doc_key = %(doc_key)s)
GROUP BY 1 HAVING count(*) > 1
"""

DRIFT_SQL = """
SELECT kind, count(*) AS n FROM vector_drift_log WHERE NOT fixed GROUP BY 1 ORDER BY 1
"""


def cmd_status(args: argparse.Namespace) -> int:
    dsn = resolve_dsn(args.dsn)
    pool = build_pool(dsn)
    store = build_store(args)
    try:
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(STATUS_SQL, {"doc_key": args.doc_key})
            versions = {r["version_status"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute(CHUNK_STATUS_SQL, {"doc_key": args.doc_key})
            chunks = {r["chunk_status"]: int(r["n"]) for r in cur.fetchall()}
            cur.execute(ACTIVE_SQL, {"doc_key": args.doc_key})
            double_active = [dict(r) for r in cur.fetchall()]
            cur.execute(DRIFT_SQL)
            open_drift = {r["kind"]: int(r["n"]) for r in cur.fetchall()}
            pending = pending_count(conn, doc_key=args.doc_key)
        points = store.collection_points()
        payload = {
            "doc_key": args.doc_key,
            "versions": versions,
            "chunks": chunks,
            "double_active": double_active,
            "vector_sync_pending": pending,
            "open_drift": open_drift,
            "qdrant_points": points,
        }
        human = (
            f"versions={versions} chunks={chunks} qdrant_points={points} "
            f"sync_pending={pending} open_drift={open_drift}"
        )
        if double_active:
            human += f"\n  !! DOUBLE ACTIVE: {double_active}"
        emit(payload, args=args, human=human)
        # 双 active 是硬红线（验收第一条），出现就非零退出
        return EXIT_OK if not double_active else EXIT_FAILED
    finally:
        store.close()
        pool.close()


# --- argparse -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dsn", help="postgresql:// DSN (default: $DATABASE_URL)")
    common.add_argument("--qdrant-url", help="default: $QDRANT_URL or http://127.0.0.1:6333")
    common.add_argument("--collection", help="default: $QDRANT_COLLECTION or kb_chunks")
    common.add_argument("--embedding-url", help="default: $EMBEDDING_BASE_URL")
    common.add_argument("--embedding-model", help="default: $EMBEDDING_MODEL")
    common.add_argument("--dim", type=int, help="default: $EMBEDDING_DIM or 512")
    common.add_argument("--batch-size", type=int, default=32)
    common.add_argument("--sync-max-attempts", type=int, default=4)
    common.add_argument("--sync-backoff", type=float, default=1.0)
    common.add_argument("--json", action="store_true", help="print one JSON object")
    common.add_argument("--verbose", "-v", action="store_true")

    ap = argparse.ArgumentParser(description="A3: 双向量入库 / 翻牌 / 同步 / 对账 / 回收")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", parents=[common], help="write dense+bm25 points for a version")
    p.add_argument("version_id")
    p.add_argument("--no-dense-backfill", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("promote", parents=[common], help="index -> flip -> sync -> reconcile")
    p.add_argument("version_id")
    p.add_argument("--no-reconcile", action="store_true")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("flip", parents=[common], help="flip a pending version to active")
    p.add_argument("version_id")
    p.add_argument("--no-sync", action="store_true")
    p.set_defaults(func=cmd_flip)

    p = sub.add_parser("rollback", parents=[common], help="restore a deprecated version (24h window)")
    p.add_argument("version_id")
    p.add_argument("--no-sync", action="store_true")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("sync", parents=[common], help="push pending desired states to Qdrant")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("reconcile", parents=[common], help="diff PG vs Qdrant and heal")
    p.add_argument("--doc-key")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-log", action="store_true", help="do not write vector_drift_log")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("reclaim", parents=[common], help="purge versions past the retention window")
    p.add_argument("--doc-key")
    p.add_argument("--version-id", help="purge this version regardless of window (must be deprecated)")
    p.add_argument("--retention-hours", type=int, default=int(os.getenv("VECTOR_RETENTION_HOURS", "24")))
    p.add_argument("--figures-dir", help="default: $FIGURES_DIR or figures")
    p.add_argument("--no-figures", action="store_true", help="skip figure deletion")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_reclaim)

    p = sub.add_parser("status", parents=[common], help="version/chunk/sync/drift overview")
    p.add_argument("--doc-key")
    p.set_defaults(func=cmd_status)

    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":
    sys.exit(main())
