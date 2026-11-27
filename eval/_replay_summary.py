"""汇总 retrieval_replay_probe 的输出：候选、rerank 与去重丢弃明细。."""
import json
from collections import Counter
from pathlib import Path

for tag in ("B03", "B18"):
    d = json.loads(
        Path(f"/workspace/ai-rag/eval/retrieval_replay_{tag}.json").read_text(
            encoding="utf-8"
        )
    )
    cands = d["hybrid_candidates"]
    print(f"=== {tag}: {d['question']} ===")
    print(
        "hybrid candidate n:", len(cands), "rerank_scored:", d["rerank_scored"]
    )
    print(
        "doc counts:",
        dict(Counter(c["doc_key"].split("/")[-1][:22] for c in cands)),
    )
    print(
        "dong-fang candidates:",
        [c["chunk_id"] for c in cands if "dong-fang" in c["doc_key"]],
    )
    print(
        "table chunk candidates:",
        [c["chunk_id"] for c in cands if c["chunk_type"] == "table"],
    )
    print(
        "golden chunk in hybrid:",
        d["golden_chunk_in_hybrid"],
        d["golden_chunk_detail"],
    )
    print("final top8 first3:", d["final_top8"][:3], "top1:", d["top1"])
    print()
