"""Chinese message catalog for scripts/promote_all.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # promote_all.py:38 (PRINT)
    "PRINT_01": "没有待转正的版本",
    # promote_all.py:41 (PRINT)
    "PRINT_02": "待转正 ",
    # promote_all.py:41 (PRINT)
    "PRINT_03": " 个版本",
    # promote_all.py:55 (PRINT)
    "PRINT_04": "\n失败的版本（其余已成功转正）：",
    # promote_all.py:60 (PRINT)
    "PRINT_05": "全部转正完成",
}
