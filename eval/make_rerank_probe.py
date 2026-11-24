"""生成一个仅用于诊断的 rerank 请求体：8 条 ~600 字的真实长度文本。
不写入服务配置，不改动评测数据。输出 eval/rerank_probe.json。
"""
import json
from pathlib import Path

para = ("中芯国际2025年第一季度营业收入为163.01亿元，同比增长29.44%，"
        "毛利率按国际财务报告准则为22.5%，产能利用率上升至89.6%，环比增长4.1个百分点，"
        "晶圆出货量229万片折合8英寸标准逻辑晶圆，环比增长15%。"
        "东方证券给予目标价129.48元，华泰证券上调港股目标价至63港元。"
        "公司持续推进先进制程研发，资本开支维持高位，折旧压力对毛利率形成一定影响，"
        "但产能利用率改善带动单位成本下降，管理层对全年需求保持谨慎乐观，"
        "预计下半年订单可见度将逐步提升，海外客户库存周期接近尾声。")
texts = [f"第{i+1}节 财务与经营摘要。" + para * 2 for i in range(8)]
body = {"query": "中芯国际2025年第一季度营业收入是多少？同比增速多少？",
        "texts": texts, "raw_scores": False}
out = Path(__file__).resolve().parent / "rerank_probe.json"
out.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
print(out, "text lengths:", [len(t) for t in texts])
