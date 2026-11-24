"""R2b 正式评测：按 expected_verdict 分组统计 + 失败归因（只读，不改数据）。

输入：
  eval/reports/redline_20261124_220427.json  (make eval 产物)
  eval/golden_50.jsonl                        (黄金集)
  eval/r2b_qalog.csv                          (评测窗口 qa_log 原始导出)
输出：
  eval/r2b_analysis.json
"""
import csv
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "reports" / "redline_20261124_220427.json"
GOLDEN = ROOT / "golden_50.jsonl"
QALOG = ROOT / "r2b_qalog.csv"
OUT = ROOT / "r2b_analysis.json"


def pct(values, p):
    """线性插值分位数，等价于 numpy.percentile(method='linear')。"""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def desc(values):
    vals = [v for v in values if v is not None]
    return {
        "n_valid": len(vals),
        "min": min(vals) if vals else None,
        "p10": pct(vals, 10),
        "median": statistics.median(vals) if vals else None,
        "p90": pct(vals, 90),
        "max": max(vals) if vals else None,
        "mean": statistics.fmean(vals) if vals else None,
        "gt_0.20": sum(1 for v in vals if v > 0.20),
        "ge_0.20": sum(1 for v in vals if v >= 0.20),
        "gt_0.40": sum(1 for v in vals if v > 0.40),
        "ge_0.40": sum(1 for v in vals if v >= 0.40),
    }


report = json.loads(REPORT.read_text(encoding="utf-8"))
golden = {}
for line in GOLDEN.read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    row = json.loads(line)
    golden[row["question"]] = row

qa = {}
with QALOG.open(encoding="utf-8", newline="") as fh:
    for row in csv.DictReader(fh):
        snap = json.loads(row["score_snapshot"])
        qa[row["question"]] = {
            "qa_id": int(row["id"]),
            "gate1_verdict": row["gate1_verdict"],
            "gate2_verdict": row["gate2_verdict"],
            "reject_reason": row["reject_reason"],
            "latency_ms": int(row["latency_ms"]),
            "model": row["model"],
            "created_at": row["created_at"],
            "snap": snap,
        }

records = []
unmatched = []
for q in report["questions"]:
    gid = q["qid"]
    g = golden.get(q.get("question", ""))
    # run_eval 的 questions 不带 question 文本；用 qid 对应的 golden 行重取
    if g is None:
        for line in GOLDEN.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["id"] == gid:
                g = r
                break
    if g is None:
        unmatched.append(gid)
        continue
    row = qa.get(g["question"])
    snap = row["snap"] if row else {}
    rerank = snap.get("rerank") or {}
    fused = snap.get("fused") or []
    records.append({
        "qid": gid,
        "category": q["category"],
        "expected": q["expected"],
        "actual": q["actual"],
        "score": q["score"],
        "top1_report": q["top1"],
        "top1_snapshot": snap.get("top1"),
        "detail": snap.get("detail"),
        "dense_top1": snap.get("dense_top1"),
        "sparse_top1": snap.get("sparse_top1"),
        "fused_top1": fused[0] if fused else None,
        "fused_n": len(fused),
        "candidates_after_dedup": snap.get("after_dedup"),
        "rerank_top1": rerank.get("top1"),
        "rerank_scored": rerank.get("scored"),
        "rerank_degrade": rerank.get("degrade"),
        "rerank_degraded": rerank.get("degraded"),
        "rerank_elapsed_ms": rerank.get("elapsed_ms"),
        "rerank_fallback": rerank.get("fallback"),
        "qa_id": row["qa_id"] if row else None,
        "qa_model": row["model"] if row else None,
        "answer_len": len(q.get("answer") or ""),
        "citations_total": q["citations_total"],
        "reject_reason": q["reject_reason"],
        "expected_doc_keys": g.get("expected_doc_keys") or [],
        "doc_keys_tbd": not (g.get("expected_doc_keys") or [])
                        or all(str(k).startswith("TBD") for k in g.get("expected_doc_keys") or []),
    })

groups = {
    "answerable_pass_partial": [r for r in records if r["expected"] in ("pass", "partial")],
    "reject_reject_none_low": [r for r in records if r["expected"] in ("reject_none", "reject_low")],
    "clarify": [r for r in records if r["expected"] == "clarify"],
}
stats = {}
for name, rows in groups.items():
    stats[name] = {
        "n": len(rows),
        "top1": desc([r["top1_report"] for r in rows]),
        "top1_with_none_as_zero": desc([0.0 if r["top1_report"] is None else r["top1_report"] for r in rows]),
        "dense_top1_diagnostic": desc([r["dense_top1"] for r in rows]),
        "sparse_top1_diagnostic": desc([r["sparse_top1"] for r in rows]),
        "fused_top1_diagnostic": desc([r["fused_top1"] for r in rows]),
        "rerank_elapsed_ms": desc([r["rerank_elapsed_ms"] for r in rows]),
        "degrade_reasons": dict(Counter(r["rerank_degrade"] for r in rows)),
        "scored_counts": dict(Counter(r["rerank_scored"] for r in rows)),
        "actual_verdicts": dict(Counter(r["actual"] for r in rows)),
        "answer_len_gt0": sum(1 for r in rows if r["answer_len"] > 0),
    }

out = {
    "source": {
        "report": str(REPORT),
        "golden": str(GOLDEN),
        "qa_log": str(QALOG),
        "report_generated_at": report["generated_at"],
        "report_started_at": report["started_at"],
        "qa_rows": len(qa),
        "matched": len(records),
        "unmatched": unmatched,
    },
    "snapshot_from_report": report["counts"],
    "precondition_from_report": report["precondition"],
    "red_lines_from_report": report["red_lines"],
    "groups": stats,
    "global": {
        "degrade_reasons": dict(Counter(r["rerank_degrade"] for r in records)),
        "actual_verdicts": dict(Counter(r["actual"] for r in records)),
        "top1_valid": sum(1 for r in records if r["top1_report"] is not None),
        "top1_none": sum(1 for r in records if r["top1_report"] is None),
        "candidate_zero": sum(1 for r in records if r["candidates_after_dedup"] == 0),
        "answer_len_gt0": sum(1 for r in records if r["answer_len"] > 0),
        "qa_models": dict(Counter(str(r["qa_model"]) for r in records)),
        "tbd_questions": [r["qid"] for r in records if r["doc_keys_tbd"]],
        "tbd_by_group": {g: sum(1 for r in rows if r["doc_keys_tbd"]) for g, rows in groups.items()},
    },
    "records": records,
}
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({
    "matched": out["source"]["matched"],
    "qa_rows": out["source"]["qa_rows"],
    "top1_valid": out["global"]["top1_valid"],
    "degrade_reasons": out["global"]["degrade_reasons"],
    "actual_verdicts": out["global"]["actual_verdicts"],
    "tbd": out["global"]["tbd_questions"],
    "candidate_zero": out["global"]["candidate_zero"],
    "answer_len_gt0": out["global"]["answer_len_gt0"],
    "groups": {k: {"n": v["n"], "top1_valid": v["top1"]["n_valid"],
                   "dense_top1_median": v["dense_top1_diagnostic"]["median"],
                   "fused_top1_median": v["fused_top1_diagnostic"]["median"],
                   "degrade": v["degrade_reasons"]} for k, v in stats.items()},
}, ensure_ascii=False, indent=2))
