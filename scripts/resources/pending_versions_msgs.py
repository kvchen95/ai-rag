"""Chinese message catalog for scripts/pending_versions.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # pending_versions.py:36 (PRINT)
    "PRINT_01": "没有待转正版本",
    # pending_versions.py:38 (PRINT)
    "PRINT_02": "待转正 ",
    # pending_versions.py:38 (PRINT)
    "PRINT_03": " 个：",
    # pending_versions.py:41 (PRINT)
    "PRINT_04": "   切片 ",
    # pending_versions.py:41 (PRINT)
    "PRINT_05": " 条",
}
