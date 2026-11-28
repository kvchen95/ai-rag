"""Chinese message catalog for scripts/smoke.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # smoke.py:109 (POST)
    "POST_01": "2025年差旅住宿标准是多少",
    # smoke.py:109 (POST)
    "POST_02": "差旅报销规定",
    # smoke.py:128 (QUERY)
    "QUERY_01": "2025年差旅住宿标准是多少",
    # smoke.py:130 (POST)
    "POST_03": "住宿费每晚上限500元，超标自理。",
    # smoke.py:131 (POST)
    "POST_04": "公司年度营收同比增长12%。",
    # smoke.py:151 (POST)
    "POST_05": "住宿费每晚上限500元，超标自理。",
    # smoke.py:163 (TEXT)
    "TEXT_01": "住宿费每晚上限500元，超标自理。",
    # smoke.py:181 (POST)
    "POST_06": "差旅住宿标准",
    # smoke.py:190 (TEXT)
    "TEXT_02": "差旅住宿标准",
}
