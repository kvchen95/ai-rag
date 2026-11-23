#!/usr/bin/env python3
"""Rebuild the Qdrant collection from PostgreSQL content_text (full or scoped).

PostgreSQL is the source of truth: chunk rows carry the text, the permissions and
the effective date. This script re-derives, for each chunk:

  * dense  : bge-small-zh-v1.5 (512d, TEI)          -> vector["dense"]
  * sparse : Qdrant server-side BM25, modifier=idf   -> vector["bm25"] = {"text": ...}
             （正文先过 `qdrant_common.bm25_text()` 做中文 bigram 切分；不切的话
             整句中文只产出 1 个 token，关键词路对中文等于废掉）
  * payload: doc_key / version_id / status / is_public / allowed_depts /
             allowed_roles / effective_year / chunk_type (+ chunk_id, parent_id, page_num)

So the whole index can be thrown away and rebuilt without touching PG (方案B 的
"稀疏向量可随时从 content_text 全量重建"). Point ids are uuid5(chunk_id), so a
re-run overwrites instead of duplicating.

Examples (run inside the compose network):

    # what would happen — no writes, no embedding of the corpus
    docker compose exec -T api python scripts/qdrant_rebuild.py --dry-run

    # full rebuild of everything searchable (version active + chunk active)
    docker compose exec -T api python scripts/qdrant_rebuild.py

    # one document only, and remove stale points for it
    docker compose exec -T api python scripts/qdrant_rebuild.py --doc-key policy/travel-cn --prune

    # first 200 chunks, for a quick sanity check
    docker compose exec -T api python scripts/qdrant_rebuild.py --limit 200

Exit codes: 0 ok, 2 collection incompatible/missing, 3 cannot connect, 4 partial errors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Iterator

import qdrant_common as qc

DEFAULT_SQL_ACTIVE = """
SELECT chunk_id, doc_key, version_id, chunk_type, parent_id, page_num,
       effective_year, effective_date, date_confidence,
       is_public, allowed_depts, allowed_roles, content_text,
       'active'::text AS status   -- view is active-only by definition
FROM v_active_chunks
{where}
ORDER BY doc_key, version_id, page_num, chunk_id
"""

# --all-status: everything that is not purged (e.g. a pending version being staged)
DEFAULT_SQL_ALL = """
SELECT c.chunk_id, c.doc_key, c.version_id, c.chunk_type, c.parent_id, c.page_num,
       c.effective_year, c.effective_date, c.date_confidence,
       c.is_public, c.allowed_depts, c.allowed_roles, c.content_text, c.status
FROM chunks c
JOIN doc_versions v ON v.version_id = c.version_id
WHERE v.status <> 'purged' AND c.status <> 'purged'
{where}
ORDER BY c.doc_key, c.version_id, c.page_num, c.chunk_id
"""


def normalise_dsn(dsn: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix):]
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
    return f"postgresql://{auth}{os.getenv('PGHOST', 'postgres')}:{os.getenv('POSTGRES_PORT', '5432')}/{os.getenv('POSTGRES_DB', 'ai_rag')}"


def build_where(doc_keys: list[str] | None, version_ids: list[str] | None, table_alias: str) -> tuple[str, dict[str, Any]]:
    clauses, params = [], {}
    if doc_keys:
        clauses.append(f"{table_alias}doc_key = ANY(%(doc_keys)s)")
        params["doc_keys"] = doc_keys
    if version_ids:
        clauses.append(f"{table_alias}version_id = ANY(%(version_ids)s)")
        params["version_ids"] = version_ids
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def connect(dsn: str):
    try:
        import psycopg  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError:
        print("ERROR: psycopg is not installed in this interpreter.\n"
              "       Run inside the app container:\n"
              "         docker compose exec -T api python scripts/qdrant_rebuild.py ...",
              file=sys.stderr)
        sys.exit(3)
    try:
        return psycopg.connect(dsn, row_factory=dict_row)
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cannot connect to PostgreSQL: {exc}", file=sys.stderr)
        sys.exit(3)


def stream_rows(conn, sql: str, params: dict[str, Any], itersize: int) -> Iterator[dict[str, Any]]:
    with conn.cursor(name="chunk_stream") as cur:  # server-side cursor: no full load in RAM
        cur.itersize = itersize
        cur.execute(sql, params)
        for row in cur:
            yield row


def probe_embedding(url: str, model: str, dim: int) -> tuple[bool, str]:
    try:
        vec = qc.embed_texts(url, ["差旅住宿标准"], model=model)[0]
    except Exception as exc:  # noqa: BLE001
        return False, f"embedding service unreachable: {exc}"
    norm = sum(v * v for v in vec) ** 0.5
    if len(vec) != dim:
        return False, f"embedding dim={len(vec)} != {dim} (model/collection mismatch)"
    if abs(norm - 1.0) > 0.02:
        return False, f"embedding not L2-normalised (norm={norm:.4f}); Cosine would be distorted"
    return True, f"dim={len(vec)} l2norm={norm:.4f}"


def summarise_rows(conn, sql_count: str, params: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cur:  # conn uses dict_row: rows are dicts
        cur.execute(sql_count, params)
        for row in cur.fetchall():
            out[row["chunk_type"] or "unknown"] = int(row["count"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="rebuild Qdrant dense+bm25 from PG content_text")
    ap.add_argument("--dsn", help="postgresql:// DSN (default: $DATABASE_URL)")
    ap.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"))
    ap.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", qc.DEFAULT_COLLECTION))
    ap.add_argument("--embedding-url", default=os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:7997/v1"))
    ap.add_argument("--embedding-model", default=os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
    ap.add_argument("--dim", type=int, default=int(os.getenv("EMBEDDING_DIM", str(qc.DEFAULT_DIM))))
    ap.add_argument("--batch-size", type=int, default=int(os.getenv("REBUILD_BATCH_SIZE", "32")),
                    help="chunks per embedding call / upsert (TEI max client batch is 32)")
    ap.add_argument("--limit", type=int, help="stop after N chunks (sanity check)")
    ap.add_argument("--doc-key", action="append", help="only this doc_key (repeatable)")
    ap.add_argument("--version-id", action="append", help="only this version_id (repeatable)")
    ap.add_argument("--all-status", action="store_true",
                    help="include non-active (but not purged) chunks, e.g. staging a pending version")
    ap.add_argument("--prune", action="store_true",
                    help="delete points in scope whose chunk_id no longer exists in PG")
    ap.add_argument("--ensure-collection", action="store_true",
                    help="create the collection if missing and add missing payload indexes")
    ap.add_argument("--dry-run", action="store_true", help="report the plan; no embedding of the corpus, no writes")
    ap.add_argument("--json", action="store_true", help="print one JSON summary line at the end")
    args = ap.parse_args()

    t0 = time.monotonic()
    base = args.qdrant_url.rstrip("/")
    dsn = resolve_dsn(args.dsn)
    template = DEFAULT_SQL_ALL if args.all_status else DEFAULT_SQL_ACTIVE
    alias = "c." if args.all_status else ""
    where, params = build_where(args.doc_key, args.version_id, alias)
    sql = template.format(where=where)
    count_sql = f"SELECT chunk_type, count(*) FROM ({sql}) s GROUP BY 1 ORDER BY 1"

    print(f"target : {dsn.split('@')[-1]} -> qdrant {base} / {args.collection} (dim={args.dim})")
    print(f"scope  : status={'all-but-purged' if args.all_status else 'active only'}"
          f"{' doc_key=' + ','.join(args.doc_key) if args.doc_key else ''}"
          f"{' version_id=' + ','.join(args.version_id) if args.version_id else ''}"
          f"{' limit=' + str(args.limit) if args.limit else ''}")

    conn = connect(dsn)
    try:
        counts = summarise_rows(conn, count_sql, params)
        total = sum(counts.values())
        if not counts:
            print("rows   : 0 (nothing to rebuild; PG has no matching chunks)")
        else:
            print(f"rows   : {total} chunks {counts}")

        ok, msg = probe_embedding(args.embedding_url, args.embedding_model, args.dim)
        print(f"embed  : {args.embedding_model} @ {args.embedding_url} -> {msg}")
        if not ok:
            print("ERROR: embedding service is not usable", file=sys.stderr)
            return 3

        info = qc.collection_info(base, args.collection)
        if info is None:
            print("qdrant : collection missing")
            if not (args.ensure_collection or args.dry_run):
                print("ERROR: run scripts/qdrant_init.py first (or pass --ensure-collection)", file=sys.stderr)
                return 2
            if args.ensure_collection and not args.dry_run:
                qc.create_collection(base, args.collection, args.dim)
                created = qc.ensure_payload_indexes(base, args.collection)
                print(f"qdrant : created collection + {len(created)} payload index(es)")
        else:
            problems = qc.spec_mismatches(info, args.dim)
            print(f"qdrant : exists (points={info.get('points_count')})")
            if problems:
                for p in problems:
                    print(f"  MISMATCH: {p}", file=sys.stderr)
                print("ERROR: collection incompatible with this embedding config; "
                      "run scripts/qdrant_init.py --recreate first", file=sys.stderr)
                return 2
            if args.ensure_collection and not args.dry_run:
                created = qc.ensure_payload_indexes(base, args.collection)
                if created:
                    print(f"qdrant : added payload index(es) {created}")

        plan_total = min(total, args.limit) if args.limit else total
        batches = (plan_total + args.batch_size - 1) // args.batch_size if plan_total else 0
        print(f"plan   : {plan_total} point(s) in {batches} batch(es) of {args.batch_size}"
              f"{', prune stale points in scope' if args.prune else ''}")

        if args.dry_run:
            print("dry-run: nothing written (chunks were not embedded)")
            if args.json:
                print(json.dumps({"dry_run": True, "rows": counts, "total": total,
                                  "batches": batches, "dim": args.dim}, ensure_ascii=False))
            return 0

        if plan_total == 0 and not args.prune:
            print("done   : nothing to do")
            return 0

        # ---- write path ----------------------------------------------------
        processed = 0
        skipped: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        started = time.monotonic()

        def flush(points: list[dict[str, Any]]) -> None:
            if not points:
                return
            qc.request("PUT", f"{base}/collections/{args.collection}/points?wait=true",
                       {"points": points}, timeout=180.0)

        buffer_texts: list[str] = []
        buffer_rows: list[dict[str, Any]] = []

        for row in stream_rows(conn, sql, params, itersize=max(args.batch_size, 32)):
            if args.limit and processed >= args.limit:
                break
            text = (row.get("content_text") or "")
            if not text.strip():
                skipped.append((row["chunk_id"], "empty content_text"))
                continue
            buffer_texts.append(text)
            buffer_rows.append(row)
            if len(buffer_rows) < args.batch_size:
                continue

            vectors = qc.embed_texts(args.embedding_url, buffer_texts, model=args.embedding_model)
            if len(vectors) != len(buffer_rows):
                print(f"ERROR: embedding returned {len(vectors)} vectors for {len(buffer_rows)} texts",
                      file=sys.stderr)
                return 4
            points = []
            for r, vec in zip(buffer_rows, vectors):
                pid = qc.point_id_for(r["chunk_id"])
                seen_ids.add(pid)
                points.append({
                    "id": pid,
                    "vector": {
                        qc.DENSE_VECTOR: vec,
                        qc.SPARSE_VECTOR: qc.bm25_vector(r["content_text"]),
                    },
                    "payload": qc.build_payload(r),
                })
            flush(points)
            processed += len(points)
            buffer_texts, buffer_rows = [], []
            rate = processed / max(time.monotonic() - started, 1e-6)
            print(f"  upserted {processed}/{plan_total} ({rate:.1f} chunks/s)", flush=True)

        # last partial batch
        if buffer_rows:
            vectors = qc.embed_texts(args.embedding_url, buffer_texts, model=args.embedding_model)
            points = []
            for r, vec in zip(buffer_rows, vectors):
                pid = qc.point_id_for(r["chunk_id"])
                seen_ids.add(pid)
                points.append({
                    "id": pid,
                    "vector": {
                        qc.DENSE_VECTOR: vec,
                        qc.SPARSE_VECTOR: qc.bm25_vector(r["content_text"]),
                    },
                    "payload": qc.build_payload(r),
                })
            flush(points)
            processed += len(points)
            print(f"  upserted {processed}/{plan_total} (final batch)", flush=True)

        # ---- verification --------------------------------------------------
        in_scope = qc.count_points(base, args.collection)
        print(f"verify : upserted={processed} skipped={len(skipped)} collection_points={in_scope}")

        pruned = 0
        if args.prune:
            keep = seen_ids
            scope_docs = set(args.doc_key or [])
            scope_versions = set(args.version_id or [])
            stale: list[str] = []
            for pt in qc.scroll_points(base, args.collection):
                pl = pt.get("payload") or {}
                if scope_docs and pl.get("doc_key") not in scope_docs:
                    continue
                if scope_versions and pl.get("version_id") not in scope_versions:
                    continue
                if pt["id"] not in keep:
                    stale.append(pt["id"])
            for i in range(0, len(stale), 256):
                qc.request("POST", f"{base}/collections/{args.collection}/points/delete?wait=true",
                           {"points": stale[i:i + 256]}, timeout=120.0)
            pruned = len(stale)
            print(f"prune  : deleted {pruned} stale point(s) in scope")

        elapsed = time.monotonic() - t0
        print(f"done   : {processed} chunk(s) in {elapsed:.1f}s"
              f"{f', {len(skipped)} skipped' if skipped else ''}")
        for cid, why in skipped[:10]:
            print(f"  skip {cid}: {why}")
        if args.json:
            print(json.dumps({"dry_run": False, "upserted": processed, "skipped": len(skipped),
                              "pruned": pruned, "collection_points": in_scope,
                              "rows": counts, "seconds": round(elapsed, 1)}, ensure_ascii=False))
        return 4 if skipped else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
