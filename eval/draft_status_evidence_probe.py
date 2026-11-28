#!/usr/bin/env python3
"""R2c draft_status over-refusal probe (read-only).

Answers one question with evidence: for the 20 `draft_status=unknown` answerable
questions, does the evidence pack actually contain the golden fact?

Inputs (read-only):
  * eval/golden_50.jsonl
  * qa_log ids 1260..1294 (redline_20261124_233521 window)
  * v_active_chunks (content_text / table_md, exactly what fetch_texts reads)

No DB writes, no golden edits, no threshold changes.
Run: python eval/draft_status_evidence_probe.py
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

if __package__:
    from .resources.draft_status_evidence_probe_msgs import MESSAGES as M
else:  # run as a script: python3 eval/draft_status_evidence_probe.py
    from resources.draft_status_evidence_probe_msgs import MESSAGES as M

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "eval" / "golden_50.jsonl"
DOCKER = "/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
COMPOSE = [
    "compose",
    "-f",
    "infra/docker-compose.yml",
    "exec",
    "-T",
    "postgres",
    "psql",
    "-U",
    "ai_rag",
    "-d",
    "ai_rag",
    "-t",
    "-A",
    "-v",
    "ON_ERROR_STOP=1",
    "-c",
]


def psql_json(sql: str):
    """在 api 容器里跑一条 psql 查询，返回 JSON 结果。."""
    out = subprocess.run(
        [DOCKER, *COMPOSE, sql], cwd=ROOT, capture_output=True, text=True
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {out.stderr[:500]}")
    blob = out.stdout.strip()
    return json.loads(blob) if blob else None


def numbers(text: str) -> set[str]:
    """Pull *answer* numbers out of golden key_evidence (not page/year labels).

    '东方证券 p1：目标价格 129.48 元' -> {'129.48'}; '25Q1' / 'p4' / '2025年'
    are query context and must not count as evidence hits.
    """
    text = re.sub(r"[pP]\s*\d+", " ", text)  # page labels
    text = re.sub(
        M['TEXT_01'], " ", text
    )  # 2025 / 2025年 / 2025E
    text = re.sub(
        r"\b\d{1,2}\s*[Qq]\s*\d?\b|\b[Qq]\d\b", " ", text
    )  # 25Q1 / 1Q25 / Q1
    text = re.sub(M['TEXT_02'], " ", text)  # 25-27年
    found = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    return {n for n in found if float(n.replace(",", "")) >= 10}


def distinctive(values: set[str]) -> set[str]:
    """Answer values only: decimals, thousands, or integers >= 100.

    Small integers (percent points, quarter labels) appear everywhere by
    coincidence and would fake an 'evidence hit'.
    """
    return {n for n in values if "." in n or "," in n or float(n) >= 100}


def main() -> None:
    """入口：逐题重放检索，记录证据包与草稿状态。."""
    goldens = [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    rows = psql_json("""
      SELECT jsonb_agg(jsonb_build_object(
        'id', id, 'qn', id-1259, 'question', question,
        'top1', score_snapshot->>'top1',
        'reject_reason', reject_reason,
        'check', score_snapshot->'verify'->'failure'->>'check',
        'offender', score_snapshot->'verify'->'failure'->'offenders'->0,
        'used_chars', score_snapshot->'evidence_pack'->>'used_chars',
        'truncated', score_snapshot->'evidence_pack'->>'truncated',
        'pack', score_snapshot->'evidence_pack'->'items'
      ) ORDER BY id) FROM qa_log
      WHERE id BETWEEN 1260 AND 1294
        AND score_snapshot->'verify'->'failure'->>'check'='draft_status'
    """)

    assert rows is not None, "qa_log query returned no rows"
    rows = [r for r in rows if r is not None]
    chunk_ids = sorted({c["chunk_id"] for r in rows for c in r["pack"]})
    texts = psql_json(
        """
      SELECT coalesce(jsonb_object_agg(chunk_id, jsonb_build_object(
               'doc_key', doc_key, 'page_num', page_num,
               'chunk_type', chunk_type,
               'content_text', content_text, 'table_md', table_md
             )), '{}'::jsonb)
      FROM v_active_chunks WHERE chunk_id = ANY(%s::text[])
    """.replace(
            "%s::text[]",
            "ARRAY[" + ",".join("'" + c + "'" for c in chunk_ids) + "]::text[]",
        )
    )
    assert texts is not None, "chunk text query returned no rows"

    detail = []
    for r in rows:
        g = goldens[r["qn"] - 1]
        # fetch_texts._text_for: table -> table_md(else content_text); else
        # content_text
        blocks = []
        pack_docs: set[str] = set()
        for c in r["pack"]:
            t = texts.get(c["chunk_id"], {})
            pack_docs.add(c["doc_key"])
            body = (
                (t.get("table_md") or t.get("content_text") or "")
                if t.get("chunk_type") == "table"
                else (t.get("content_text") or "")
            )
            blocks.append(f"[{c['n']}] {body}")
        pack_text = "\n".join(blocks)
        facts = numbers(" ".join(g["key_evidence"]))
        key_facts = distinctive(facts)
        hit = sorted(n for n in key_facts if n in pack_text)
        expect_docs = set(g["expected_doc_keys"])
        detail.append(
            {
                "qn": r["qn"],
                "golden_id": g["id"],
                "question": r["question"],
                "top1": float(r["top1"]),
                "used_chars": int(r["used_chars"]),
                "truncated": r["truncated"] is True or r["truncated"] == "true",
                "n_items": len(r["pack"]),
                "golden_facts": sorted(facts),
                "distinctive_facts": sorted(key_facts),
                "distinctive_facts_in_pack": hit,
                "golden_docs": sorted(expect_docs),
                "golden_docs_in_pack": sorted(expect_docs & pack_docs),
                "pack_docs": sorted(pack_docs),
                "model_answer": r["offender"],
            }
        )

    n = len(detail)
    summary = {
        "n_questions": n,
        "questions_with_distinctive_facts_defined": sum(
            bool(d["distinctive_facts"]) for d in detail
        ),
        "questions_with_any_distinctive_fact_in_pack": sum(
            bool(d["distinctive_facts_in_pack"]) for d in detail
        ),
        "questions_with_no_distinctive_fact_in_pack": sum(
            not d["distinctive_facts_in_pack"] for d in detail
        ),
        "golden_doc_present": sum(
            bool(d["golden_docs_in_pack"]) for d in detail
        ),
        "golden_doc_absent": sum(not d["golden_docs_in_pack"] for d in detail),
        "truncated_true": sum(bool(d["truncated"]) for d in detail),
        "top1_min": min(d["top1"] for d in detail),
        "top1_max": max(d["top1"] for d in detail),
    }
    print(
        json.dumps(
            {"summary": summary, "detail": detail}, ensure_ascii=False, indent=2
        )
    )

    out = ROOT / "eval" / "draft_status_evidence_probe.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"summary": summary, "detail": detail}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(f"\n[written] {out}", flush=True)


if __name__ == "__main__":
    main()
