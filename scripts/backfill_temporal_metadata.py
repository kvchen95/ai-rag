#!/usr/bin/env python3
"""Backfill temporal metadata for already-ingested sample reports.

Usage:
  python scripts/backfill_temporal_metadata.py --dry-run
  python scripts/backfill_temporal_metadata.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

PERIODS = {
    "san-ji-bao": ("quarterly", "2026-07-01", "2026-09-30", "quarter"),
    "ban-nian-bao": ("interim", "2026-01-01", "2026-06-30", "half_year"),
}

COMPANIES = {
    "hai-guang-xin-xi": "海光信息",
    "han-wu-ji": "寒武纪",
    "yu-shu-ke-ji": "宇树科技",
    "zhang-xin-ke-ji": "长鑫科技",
}


def parse_doc_key(doc_key: str) -> tuple[str, str, str, str, str]:
    stem = doc_key.split("/")[-1]
    company_slug = None
    for slug in COMPANIES:
        if stem.startswith(slug):
            company_slug = slug
            break
    if company_slug is None:
        raise ValueError(f"cannot identify company for {doc_key}")
    report_type, start, end, granularity = PERIODS["san-ji-bao" if "san-ji-bao" in stem else "ban-nian-bao"]
    return COMPANIES[company_slug], report_type, start, end, granularity


def main() -> int:
    apply = "--apply" in sys.argv
    dry_run = "--dry-run" in sys.argv
    if not apply and not dry_run:
        print("usage: --dry-run or --apply", file=sys.stderr)
        return 2

    dsn = os.getenv("DATABASE_URL", "postgresql://ai_rag:ai_rag_dev_password@postgres:5432/ai_rag")
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://", "postgres+psycopg://"):
        if dsn.startswith(prefix):
            dsn = "postgresql://" + dsn[len(prefix):]

    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT doc_key, filename FROM documents ORDER BY doc_key")
            docs = cur.fetchall()
            plan = []
            for doc_key, filename in docs:
                company, report_type, start, end, granularity = parse_doc_key(doc_key)
                plan.append((doc_key, company, report_type, start, end, granularity))
                print(f"{doc_key}: company={company}, type={report_type}, period={start}..{end}, granularity={granularity}")

            if dry_run:
                print(f"dry-run: would update {len(plan)} documents and their active chunks")
                return 0

            for doc_key, company, report_type, start, end, granularity in plan:
                cur.execute(
                    """
                    UPDATE documents
                    SET company=%s, report_type=%s,
                        report_period_start=%s, report_period_end=%s,
                        publication_date=NULL
                    WHERE doc_key=%s
                    """,
                    (company, report_type, start, end, doc_key),
                )
                cur.execute(
                    """
                    UPDATE chunks
                    SET report_period_start=%s,
                        report_period_end=%s,
                        publication_date=NULL,
                        period_granularity=%s,
                        period_confidence='filename'
                    WHERE doc_key=%s
                    """,
                    (start, end, granularity, doc_key),
                )
            conn.commit()
            print(f"applied: updated {len(plan)} documents and their chunks")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
