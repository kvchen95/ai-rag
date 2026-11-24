"""Summarize a redline eval report: model_timeout count + red lines."""
import json
import sys

for path in sys.argv[1:]:
    d = json.load(open(path))
    qs = d["questions"]
    mt = [q["qid"] for q in qs if q.get("reject_reason") == "model_timeout" or q.get("actual") == "model_timeout"]
    traps = {q["qid"]: q.get("trap_safe") for q in qs if q.get("trap_safe") is not None}
    rl1 = next((r for r in d["red_lines"] if r["key"] == "RL1"), {})
    print(f"{path}:")
    print(f"  model_timeout = {len(mt)}  ids={mt}")
    print(f"  RL1 = {rl1.get('status')} {rl1.get('value')}")
    print(f"  trap_safe values = {sorted(set(traps.values()), key=str)}  n={len(traps)}")
    print(f"  verdict = {d['verdict']}")
