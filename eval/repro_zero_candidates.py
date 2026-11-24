"""复现：R2b 7 道应答题零候选的真实根因探针（只读，不改库不改配置）。

跑法（容器内，源码已 bind-mount）：
    docker compose -f infra/docker-compose.yml exec -T api python eval/repro_zero_candidates.py

预期（修复前）：7 题 out_of_scope=True，scope_hit='预测' → pipeline 在 build_filter
之前就 _reject_without_retrieval，detail='超出库范围，未检索'、filter={}、routes 0/0。
预期（修复后）：7 题 out_of_scope=False，能走到 build_filter。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from api.query import is_out_of_scope, understand

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "eval" / "golden_50.jsonl"
TARGETS = ("B07", "B08", "C01", "C02", "C06", "C07", "M03")

rows = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
by_id = {row["id"]: row for row in rows}

now = date(2026, 11, 24)
print(f"{'qid':<5} {'scope_hit':<8} {'out_of_scope':<12} {'time_range':<28} question")
for qid in TARGETS:
    q = by_id[qid]["question"]
    hit = is_out_of_scope(q)
    plan = understand(q, now=now)
    tr = plan.time.range
    trs = (
        f"{tr.date_from}-{tr.date_to}/{tr.basis}"
        if tr is not None
        else "None"
    )
    print(f"{qid:<5} {str(hit):<8} {str(plan.out_of_scope):<12} {trs:<28} {q}")

print()
print("== 反向对照：其余应答题是否命中超范围 ==")
for row in rows:
    if row["expected_verdict"] not in ("pass", "partial"):
        continue
    if row["id"] in TARGETS:
        continue
    hit = is_out_of_scope(row["question"])
    if hit:
        print(f"  非目标应答题也命中: {row['id']} <- {hit}")

print()
print("== 陷阱题 T07（含股价语义）是否仍被超范围拦截 ==")
t07 = by_id["T07"]["question"]
print(f"  T07={t07!r}  scope_hit={is_out_of_scope(t07)!r}")
print(f"  '帮我预测一下明年的股价' scope_hit={is_out_of_scope('帮我预测一下明年的股价')!r}")
