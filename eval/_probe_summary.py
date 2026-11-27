"""汇总 draft_status_evidence_probe 的 JSON：每题证据命中与 top1。."""
import json
from pathlib import Path

d = json.loads(
    Path("/workspace/ai-rag/eval/draft_status_evidence_probe.json").read_text(
        encoding="utf-8"
    )
)
for x in d["detail"]:
    print(
        f"qn{x['qn']:>2} {x['golden_id']:>3} top1={x['top1']:.3f} "
        f"facts={','.join(x['golden_facts'])} "
        f"hit={','.join(x['facts_in_pack'])} "
        f"docs={len(x['golden_docs_in_pack'])}/{len(x['golden_docs'])}  "
        f"{x['question'][:24]}"
    )
