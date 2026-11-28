"""Chinese message catalog for scripts/verify_parse_pipeline.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # verify_parse_pipeline.py:149 (PRINT)
    "PRINT_01": "\n[1] 样本与版本号",
    # verify_parse_pipeline.py:162 (PRINT)
    "PRINT_02": "\n[2] 解析与切片（每片必须带齐六项随行信息）",
    # verify_parse_pipeline.py:216 (PRINT)
    "PRINT_03": "\n[3] 读回校验",
    # verify_parse_pipeline.py:255 (CHECK)
    "CHECK_01": "all rows are status=pending (翻牌是 A3 的事)",
    # verify_parse_pipeline.py:283 (PRINT)
    "PRINT_04": "\n[4] 版面能力（合并单元格、OCR 兜底、页眉页脚）",
    # verify_parse_pipeline.py:288 (PRINT)
    "PRINT_05": "\n[5] --keep: 保留 ",
    # verify_parse_pipeline.py:288 (PRINT)
    "PRINT_06": " 行（记得手动清理）",
    # verify_parse_pipeline.py:290 (PRINT)
    "PRINT_07": "\n[5] 清理",
    # verify_parse_pipeline.py:531 (CHECK)
    "CHECK_02": ": table_cells is NULL (004 迁移的列没写进去)",
}
