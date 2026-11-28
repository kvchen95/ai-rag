"""Chinese message catalog for eval/make_rerank_probe.py.

Keys are generated; each entry notes its source line in the script.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # make_rerank_probe.py:10 (PARA)
    "PARA_01": (
        "中芯国际2025年第一季度营业收入为163.01亿元，同比增长29.44%，"
        "毛利率按国际财务报告准则为22.5%，产能利用率上升至89.6%，"
        "环比增长4.1个百分点，晶圆出货量229万片折合8英寸标准逻辑晶圆，"
        "环比增长15%。东方证券给予目标价129.48元，"
        "华泰证券上调港股目标价至63港元。公司持续推进先进制程研发，"
        "资本开支维持高位，折旧压力对毛利率形成一定影响，"
        "但产能利用率改善带动单位成本下降，管理层对全年需求保持谨慎乐观，"
        "预计下半年订单可见度将逐步提升，海外客户库存周期接近尾声。"
    ),
    # make_rerank_probe.py:18 (TEXTS)
    "TEXTS_01": "第",
    # make_rerank_probe.py:18 (TEXTS)
    "TEXTS_02": "节 财务与经营摘要。",
    # make_rerank_probe.py:20 (QUERY)
    "QUERY_01": "中芯国际2025年第一季度营业收入是多少？同比增速多少？",
}
