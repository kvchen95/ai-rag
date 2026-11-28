"""Chinese message catalog for scripts/f2_stub_llm.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # f2_stub_llm.py:29 (COMPILE)
    "COMPILE_01": "^\\[(\\d+)\\] 来源：(.+)$",
    # f2_stub_llm.py:69 (COMPILE)
    "COMPILE_02": "【问题】\\s*(.+)$",
    # f2_stub_llm.py:89 (ANSWER)
    "ANSWER_01": "证据包为空，未找到可验证依据。",
    # f2_stub_llm.py:97 (BUILD_ANSWER)
    "BUILD_ANSWER_01": "图",
    # f2_stub_llm.py:98 (FIGURE_LIKE)
    "FIGURE_LIKE_01": "裁图",
    # f2_stub_llm.py:98 (FIGURE_LIKE)
    "FIGURE_LIKE_02": "图表",
    # f2_stub_llm.py:189 (HELP)
    "HELP_01": "打印提示词与生成的答案",
}
