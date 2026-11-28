"""核对 R2b 修复后 7 道应答题的检索候选数（只读报告 JSON）。."""

import json
import sys
from pathlib import Path

if __package__:
    from .resources.check_zero_candidates_msgs import MESSAGES as M
else:  # run as a script: python3 eval/check_zero_candidates.py
    from resources.check_zero_candidates_msgs import MESSAGES as M

report = Path(sys.argv[1])
data = json.loads(report.read_text(encoding="utf-8"))

targets = {"B07", "B08", "C01", "C02", "C06", "C07", "M03"}


# 找逐题记录：报告 JSON 结构里常见 records/results/questions
def find_records(obj):
    """在报告 JSON 里找到逐题记录所在的列表。."""
    for key in ("records", "results", "questions", "per_question", "items"):
        if isinstance(obj, dict) and isinstance(obj.get(key), list):
            return obj[key]
    return None


records = find_records(data)
if records is None:
    print("keys:", list(data.keys()))
    sys.exit(2)


def get(r, *keys):
    """按候选键名取第一个存在的字段。."""
    for k in keys:
        if k in r:
            return r[k]
    return None


print(f"report={report.name}  records={len(records)}")
hdr = (
    f"{'qid':<5} {'after_dedup':>11} {'dense_top1':>10} "
    f"{'detail':<26} {'top1':>8}"
)
print(hdr)
zero = 0
for r in records:
    qid = get(r, "qid", "id", "question_id")
    if qid not in targets:
        continue
    ad = get(r, "candidates_after_dedup", "after_dedup")
    dt = get(r, "dense_top1")
    detail = str(get(r, "detail", "verdict_reason"))[:26]
    top1 = get(r, "top1_report", "top1_snapshot")
    print(f"{qid:<5} {str(ad):>11} {str(dt):>10} {detail:<26} {str(top1):>8}")
    if ad == 0:
        zero += 1
print(f"{M['MODULE_01']}{zero} / 7")
