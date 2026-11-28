"""Chinese message catalog for scripts/b2_explain_seed.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # b2_explain_seed.py:53 (APPEND)
    "APPEND_01": "种子正文 ",
    # b2_explain_seed.py:74 (HELP)
    "HELP_01": "跑完不清理",
    # b2_explain_seed.py:77 (PRINT)
    "PRINT_01": "ERROR: 需要 --dsn 或 DATABASE_URL",
    # b2_explain_seed.py:116 (PRINT)
    "PRINT_02": " (总行数 ",
    # b2_explain_seed.py:116 (PRINT)
    "PRINT_03": ") 用时 ",
    # b2_explain_seed.py:126 (PRINT)
    "PRINT_04": "cleaned: 剩余 ",
    # b2_explain_seed.py:126 (PRINT)
    "PRINT_05": " 行",
}
