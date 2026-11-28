"""Chinese message catalog for scripts/f2_demo_seed.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # f2_demo_seed.py:50 (HELP)
    "HELP_01": "不造图表切片夹具",
    # f2_demo_seed.py:121 (LOG)
    "LOG_01": "FAIL 样本不存在：",
    # f2_demo_seed.py:131 (LOG)
    "LOG_02": "FAIL 有切片缺六项随行信息：",
    # f2_demo_seed.py:175 (LOG)
    "LOG_03": "[promote] 版本已 active（上一次跑过），改为重新索引",
    # f2_demo_seed.py:198 (LOG)
    "LOG_04": "FAIL promote 失败：",
    # f2_demo_seed.py:262 (LOG)
    "LOG_05": "FAIL 找不到可裁的正文切片",
    # f2_demo_seed.py:338 (EXECUTE)
    "EXECUTE_01": (
        "【F2 演示夹具】住宿标准段落的版面裁图（非 VLM "
        "转译，仅用于验证切片图展示）。"
    ),
    # f2_demo_seed.py:356 (LOG)
    "LOG_06": "FAIL 图表切片索引失败",
}
