#!/usr/bin/env python3
"""Backfill temporal_facts from already-ingested table chunks.

Usage:
  python scripts/backfill_temporal_facts.py --dry-run
  python scripts/backfill_temporal_facts.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.clients.pg import PgDatabase  # noqa: E402
from api.temporal.facts import extract_table_facts  # noqa: E402
from api.temporal.repository import TemporalFactRepository  # noqa: E402

_UNIT = re.compile(r"单位\s*[:：]\s*(万元|亿元|元|%|％)")
_SQL = """
SELECT c.chunk_id, c.doc_key, c.table_md, c.content_text,
       c.report_period_start::text AS report_period_start,
       c.report_period_end::text AS report_period_end,
       c.period_granularity,
       d.company
FROM chunks c
JOIN documents d ON d.doc_key = c.doc_key
JOIN doc_versions v ON v.version_id = c.version_id
WHERE c.status = 'active'
  AND v.status = 'active'
  AND c.chunk_type = 'table'
  AND c.table_md IS NOT NULL
  AND c.report_period_start IS NOT NULL
  AND c.report_period_end IS NOT NULL
  AND d.company IS NOT NULL
ORDER BY c.doc_key, c.page_num, c.chunk_id
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("use --dry-run or --apply")

    pg = PgDatabase(
        os.getenv(
            "DATABASE_URL", "postgresql://ai_rag:ai_rag@postgres:5432/ai_rag"
        )
    )
    pg.open()
    try:
        rows = pg.fetch_all(_SQL)
        all_facts = []
        for row in rows:
            match = _UNIT.search(row.get("content_text") or "")
            unit = match.group(1).replace("％", "%") if match else ""
            facts = extract_table_facts(
                chunk_id=str(row["chunk_id"]),
                doc_key=str(row["doc_key"]),
                subject=str(row["company"]),
                report_period_start=str(row["report_period_start"]),
                report_period_end=str(row["report_period_end"]),
                table_md=str(row["table_md"]),
                unit=unit,
                period_granularity=str(
                    row.get("period_granularity") or "range"
                ),
            )
            all_facts.extend(facts)
            print(f"{row['chunk_id']}: {len(facts)} facts")
            if args.verbose:
                for fact in facts:
                    print(
                        f"  {fact.metric} | {fact.value} {fact.unit} | "
                        f"{fact.period_start}..{fact.period_end}"
                    )
        print(f"total: {len(rows)} table chunks, {len(all_facts)} facts")
        if args.dry_run:
            return 0
        repo = TemporalFactRepository(pg)
        cleared = repo.clear_all()
        inserted = repo.upsert(all_facts)
        print(
            f"applied: cleared {cleared} old facts, upserted {inserted} facts"
        )
        return 0
    finally:
        pg.close()


if __name__ == "__main__":
    raise SystemExit(main())
