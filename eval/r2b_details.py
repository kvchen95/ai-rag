"""打印 R2b 分析的关键明细：零候选问题、分组 top1 描述、degrade 分布。."""

import json
from pathlib import Path

if __package__:
    from .resources.r2b_details_msgs import MESSAGES as M
else:  # run as a script: python3 eval/r2b_details.py
    from resources.r2b_details_msgs import MESSAGES as M

d = json.loads(
    (Path(__file__).resolve().parent / "r2b_analysis.json").read_text(
        encoding="utf-8"
    )
)
print(M['MODULE_01'])
for r in d["records"]:
    if r["candidates_after_dedup"] == 0:
        print(
            r["qid"],
            r["category"],
            "expected=",
            r["expected"],
            "dense_top1=",
            r["dense_top1"],
            "sparse_top1=",
            r["sparse_top1"],
            "detail=",
            r["detail"],
            "lat=",
            r["rerank_elapsed_ms"],
        )
print(M['MODULE_02'])
for name, g in d["groups"].items():
    print(name, "n=", g["n"])
    for metric in (
        "top1",
        "dense_top1_diagnostic",
        "sparse_top1_diagnostic",
        "fused_top1_diagnostic",
        "rerank_elapsed_ms",
    ):
        print("  ", metric, json.dumps(g[metric], ensure_ascii=False))
    print("   degrade", g["degrade_reasons"], "scored", g["scored_counts"])
print(M['MODULE_03'])
for r in d["records"]:
    if (
        r["expected"] in ("pass", "partial")
        and r["candidates_after_dedup"]
        and r["rerank_degrade"] == "rerank_timeout"
    ):
        print(
            r["qid"],
            "dense=",
            r["dense_top1"],
            "sparse=",
            r["sparse_top1"],
            "fused1=",
            r["fused_top1"],
            "rr_ms=",
            r["rerank_elapsed_ms"],
        )
