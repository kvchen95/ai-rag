"""往 PG 灌一批切片，验证过滤条件的执行计划（跑完清理）。.

存在的目的：功能测试无法区分 `&&` 与 `<@`、`is_public` 与 `is_public = true`
在执行计划上的差别。要证明"确权条件能走索引"，必须有足够的行数让计划器
选择索引，否则小表一律 Seq Scan，结论没有意义。

用法：python scripts/b2_explain_seed.py --dsn ... [--rows 20000] [--keep]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

if __package__:
    from .resources.b2_explain_seed_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/b2_explain_seed.py
    from resources.b2_explain_seed_msgs import MESSAGES as M

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

from api.clients.pg import normalize_dsn  # noqa: E402

DOC_KEY = "policy/b2-explain"
VERSION = f"{DOC_KEY}#seed"
HASH = "c" * 64
DEPTS = ["fin", "hr", "corp", "it", "ops", "legal"]
ROLES = ["staff", "approver", "auditor"]


def build_rows(n: int) -> list[tuple]:
    """Run build_rows."""
    rows = []
    for i in range(n):
        is_public = i % 10 == 0
        depts = [] if is_public else [DEPTS[i % len(DEPTS)]]
        roles = [] if is_public else [ROLES[i % len(ROLES)]]
        # 日期覆盖 2024-2026 三年，每月一行区间
        year = 2024 + (i % 3)
        month = 1 + (i % 12)
        day = 1 + (i % 28)
        eff_date = year * 10000 + month * 100 + day
        confidence = "explicit" if i % 4 else "inferred"
        rows.append(
            (
                f"ch_seed_{i:06d}",
                DOC_KEY,
                VERSION,
                "text",
                1 + (i % 50),
                Jsonb([]),
                f"{M['APPEND_01']}{i}",
                eff_date,
                year,
                confidence,
                is_public,
                depts,
                roles,
                "active",
            )
        )
    return rows


def main() -> int:
    """Run main."""
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL")
        or os.getenv("AI_RAG_TEST_DATABASE_URL"),
    )
    ap.add_argument("--rows", type=int, default=20000)
    ap.add_argument("--keep", action="store_true", help=M["HELP_01"])
    args = ap.parse_args()
    if not args.dsn:
        print(M["PRINT_01"], file=sys.stderr)
        return 2

    dsn = normalize_dsn(args.dsn)
    started = time.monotonic()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO documents (doc_key, filename, page_count, status) "
            "VALUES (%s, %s, 1, 'active') ON CONFLICT (doc_key) DO NOTHING",
            (DOC_KEY, f"{DOC_KEY}.pdf"),
        )
        conn.execute(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status,"
            " activated_at) "
            "VALUES (%s, %s, %s, 'active', now()) ON CONFLICT (version_id) DO"
            " NOTHING",
            (VERSION, DOC_KEY, HASH),
        )
        rows = build_rows(args.rows)
        with conn.cursor() as cur:
            cur.executemany(
                (
                    "\n"
                    "                INSERT INTO chunks (chunk_id, doc_key, "
                    "version_id, chunk_type, page_num, bbox,\n"
                    "                                    content_text, "
                    "effective_date, effective_year,\n"
                    "                                    date_confidence, "
                    "is_public, allowed_depts, allowed_roles, status)\n"
                    "                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s, %s, %s, %s, %s, %s)\n"
                    "                ON CONFLICT (chunk_id) DO NOTHING\n"
                    "                "
                ),
                rows,
            )
        conn.execute("ANALYZE chunks")
        total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        print(
            f"seeded rows={len(rows)}{M['PRINT_02']}{total}"
            f"{M['PRINT_03']}{time.monotonic() - started:.1f}s"
        )
        if not args.keep:
            conn.execute("DELETE FROM chunks WHERE doc_key = %s", (DOC_KEY,))
            conn.execute(
                "DELETE FROM doc_versions WHERE doc_key = %s", (DOC_KEY,)
            )
            conn.execute("DELETE FROM documents WHERE doc_key = %s", (DOC_KEY,))
            left = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            print(f"{M['PRINT_04']}{left}{M['PRINT_05']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
