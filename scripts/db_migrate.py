#!/usr/bin/env python3
"""App-side PostgreSQL migration runner for ai-rag.

Why not initdb: ``infra/initdb`` only runs on a brand-new data volume, so a schema
change would never reach an existing dev database. Migrations live in
``db/migrations`` and are applied here, recorded in ``schema_migrations``.

Usage (host has no python deps — run inside the api container):

    docker compose exec -T api python scripts/db_migrate.py --status
    docker compose exec -T api python scripts/db_migrate.py --dry-run
    docker compose exec -T api python scripts/db_migrate.py

    # against another database (e.g. a scratch db for testing a fresh install)
    docker compose exec -T api python scripts/db_migrate.py \
        --dsn postgresql://ai_rag:***@postgres:5432/ai_rag_migtest

Behaviour:
  * files are applied in lexical order, one file = one transaction
  * a file already recorded is skipped; if its checksum changed, the run aborts
    (edit an applied migration = drift; add a new file instead)
  * a global advisory lock serialises concurrent runs
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from typing import NoReturn

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
ADVISORY_LOCK_KEY = 0x61695F726167  # "ai_rag" — stable across runs
LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    elapsed_ms int         NOT NULL DEFAULT 0
);
COMMENT ON TABLE schema_migrations IS '已应用的迁移文件（version=文件名，checksum=sha256 前 16 位）';
"""


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def normalise_dsn(dsn: str) -> str:
    """Accept SQLAlchemy-style DSNs used by api/worker ($DATABASE_URL)."""
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres+psycopg://"):
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
    host = os.getenv("PGHOST", "postgres")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "ai_rag")
    auth = f"{user}:{pwd}@" if pwd else f"{user}@"
    return f"postgresql://{auth}{host}:{port}/{db}"


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def discover() -> list[tuple[str, str]]:
    if not MIGRATIONS_DIR.is_dir():
        die(f"migrations dir not found: {MIGRATIONS_DIR}")
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        out.append((path.name, path.read_text(encoding="utf-8")))
    if not out:
        die(f"no *.sql migrations in {MIGRATIONS_DIR}")
    return out


def connect(dsn: str):
    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        die("psycopg is not installed in this interpreter.\n"
            "       Run inside the app container instead:\n"
            "         docker compose exec -T api python scripts/db_migrate.py", 3)
    try:
        return psycopg.connect(dsn, autocommit=False)
    except Exception as exc:  # noqa: BLE001
        die(f"cannot connect: {exc}", 3)


def applied_map(conn, create: bool = True) -> dict[str, str]:
    with conn.cursor() as cur:
        if create:
            cur.execute(LEDGER_SQL)
            conn.commit()
        else:
            cur.execute("SELECT to_regclass('public.schema_migrations') IS NOT NULL")
            if not cur.fetchone()[0]:
                conn.rollback()
                return {}
            conn.commit()
        cur.execute("SELECT version, checksum FROM schema_migrations ORDER BY version")
        return dict(cur.fetchall())


def show_status(conn) -> int:
    applied = applied_map(conn)
    files = discover()
    print(f"{'file':44} {'state':10} checksum")
    print("-" * 72)
    drifted = 0
    for name, text in files:
        cs = checksum(text)
        if name not in applied:
            state = "PENDING"
        elif applied[name] != cs:
            state = "DRIFT"
            drifted += 1
        else:
            state = "applied"
        print(f"{name:44} {state:10} {cs}")
    extra = sorted(set(applied) - {n for n, _ in files})
    for name in extra:
        print(f"{name:44} {'MISSING':10} {applied[name]}  (recorded but file is gone)")
    print(f"\napplied={len(set(applied) & {n for n, _ in files})} pending={sum(1 for n, _ in files if n not in applied)}")
    return 2 if drifted else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="apply db/migrations/*.sql in order")
    ap.add_argument("--dsn", help="postgresql:// DSN (default: $DATABASE_URL)")
    ap.add_argument("--status", action="store_true", help="show applied/pending, change nothing")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args()

    dsn = resolve_dsn(args.dsn)
    safe = dsn.split("@")[-1] if "@" in dsn else dsn
    print(f"target : {safe}")
    print(f"source : {MIGRATIONS_DIR}")

    files = discover()

    conn = connect(dsn)
    try:
        if args.status:
            return show_status(conn)

        if args.dry_run:
            applied = applied_map(conn, create=False)
            pending = [n for n, _ in files if n not in applied]
            drift = [n for n, t in files if n in applied and applied[n] != checksum(t)]
            print(f"applied: {len(set(applied) & {n for n, _ in files})} file(s) recorded")
            print(f"plan   : would apply {len(pending)} file(s) in order -> {', '.join(pending) or '(none)'}")
            if drift:
                print(f"DRIFT  : {', '.join(drift)} changed after being applied -> run would abort")
            print("dry-run: nothing executed (drop --dry-run to apply)")
            return 2 if drift else 0

        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
            conn.commit()

        applied = applied_map(conn)
        todo = [(n, t) for n, t in files if n not in applied]
        drift = [n for n, t in files if n in applied and applied[n] != checksum(t)]
        if drift:
            die("already-applied migrations changed on disk: " + ", ".join(drift)
                + "\n       add a new migration file instead of editing history", 2)
        if not todo:
            print("nothing to do: schema is up to date")
            return 0

        for name, text in todo:
            cs = checksum(text)
            started = time.monotonic()
            print(f"apply  : {name} ({len(text)} bytes, sha {cs}) ...", end="", flush=True)
            try:
                with conn.cursor() as cur:
                    cur.execute(text)  # no params -> simple protocol, multi-statement OK
                    elapsed = int((time.monotonic() - started) * 1000)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, checksum, elapsed_ms) VALUES (%s, %s, %s)",
                        (name, cs, elapsed),
                    )
                conn.commit()
                print(f" ok ({elapsed} ms)")
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                print(" FAILED")
                die(f"{name}: {exc}")
        print(f"done   : applied {len(todo)} migration(s)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
