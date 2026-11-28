"""生成一个仅用于诊断的 rerank 请求体：8 条 ~600 字真实长度文本。.

不写入服务配置，不改动评测数据。输出 eval/rerank_probe.json。
"""

import json
from pathlib import Path

if __package__:
    from .resources.make_rerank_probe_msgs import MESSAGES as M
else:  # run as a script: python3 eval/make_rerank_probe.py
    from resources.make_rerank_probe_msgs import MESSAGES as M

para = M['PARA_01']
texts = [f"{M['TEXTS_01']}{i+1}{M['TEXTS_02']}" + para * 2 for i in range(8)]
body = {
    "query": M['QUERY_01'],
    "texts": texts,
    "raw_scores": False,
}
out = Path(__file__).resolve().parent / "rerank_probe.json"
out.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
print(out, "text lengths:", [len(t) for t in texts])
