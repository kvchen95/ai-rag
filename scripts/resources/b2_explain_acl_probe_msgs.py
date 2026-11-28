"""Chinese message catalog for scripts/b2_explain_acl_probe.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # b2_explain_acl_probe.py:25 (PRINT)
    "PRINT_01": "ERROR: 需要 psycopg（在 api 容器里跑）",
    # b2_explain_acl_probe.py:42 (TRIALS)
    "TRIALS_01": "A 现状",
    # b2_explain_acl_probe.py:44 (TRIALS)
    "TRIALS_02": "B +is_public 部分索引",
    # b2_explain_acl_probe.py:51 (TRIALS)
    "TRIALS_03": "C +depts/roles 部分索引（已有，重建为部分索引）",
    # b2_explain_acl_probe.py:60 (TRIALS)
    "TRIALS_04": "D B+C 全套",
    # b2_explain_acl_probe.py:98 (PRINT)
    "PRINT_02": "ERROR: DATABASE_URL 未设置",
    # b2_explain_acl_probe.py:104 (PRINT)
    "PRINT_03": "行数太少，计划器一律 Seq Scan，结论不可信。先灌种子数据。",
    # b2_explain_acl_probe.py:113 (PRINT)
    "PRINT_04": "\n  节点: ",
    # b2_explain_acl_probe.py:113 (PRINT)
    "PRINT_05": "\n  时间: ",
    # b2_explain_acl_probe.py:118 (PRINT)
    "PRINT_06": "\n临时索引已全部删除",
}
