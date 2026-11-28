"""Chinese message catalog for scripts/b2_explain_filters.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # b2_explain_filters.py:49 (TIME_RANGE)
    "TIME_RANGE_01": "三月",
    # b2_explain_filters.py:93 (PRINT)
    "PRINT_01": "ERROR: 需要 --dsn 或 DATABASE_URL",
    # b2_explain_filters.py:102 (PRINT)
    "PRINT_02": "chunks 总行数: ",
    # b2_explain_filters.py:103 (PRINT)
    "PRINT_03": "（过滤预算 ",
    # b2_explain_filters.py:103 (PRINT)
    "PRINT_04": "ms，§7.5 召回 30ms 的一部分）\n",
    # b2_explain_filters.py:106 (PRINT)
    "PRINT_05": "警告：行数太少，计划器一律 Seq Scan，结论仅供参考\n",
    # b2_explain_filters.py:112 (LABEL)
    "LABEL_01": " (带 ORDER BY)",
    # b2_explain_filters.py:112 (LABEL)
    "LABEL_02": " (纯过滤)",
    # b2_explain_filters.py:115 (PRINT)
    "PRINT_06": "  执行时间: ",
    # b2_explain_filters.py:124 (PRINT)
    "PRINT_07": "超出预算:\n  ",
}
