"""Chinese message catalog for scripts/b2_explain_cleanup.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # b2_explain_cleanup.py:23 (PRINT)
    "PRINT_01": "ERROR: DATABASE_URL 未设置",
    # b2_explain_cleanup.py:32 (PRINT)
    "PRINT_02": "cleaned: 剩余 chunks ",
    # b2_explain_cleanup.py:32 (PRINT)
    "PRINT_03": " 行，临时索引已删",
}
