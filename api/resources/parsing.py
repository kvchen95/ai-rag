"""解析阶段的排版与文件名正则表（详细设计.md §3.2 / §4.4）。.

版面启发式依赖大量中文字面量，集中在这里便于逐条对照样本调试。
"""

from __future__ import annotations

import re

# 数字+单位：金额、比例、期限、日期。带这些的行即便重复也不删——
# 它多半是每页都印的"标准值"提示，删掉就是丢正文（§3.2 的原话）。
NUMERIC_UNIT = re.compile(
    r"\d[\d,.]*\s*"
    r"(?:元|块|万|亿|%|％|天|日|个工作日|工作日|个月|月|年|"
    r"小时|分钟|次|人|页|pt|px|CNY|RMB|USD|¥|\$)"
    r"|(?:CNY|RMB|USD|¥|\$)\s*\d",
    re.IGNORECASE,
)
DIGITS = re.compile(r"\d+")
CJK_RUN = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)

# 金额/单位后缀：`1,000 元` 与 `1000` 应归一到同一个数。
MONEY_SUFFIX = re.compile(
    r"(元|块|万|亿|天|日|个月|年|小时|分钟|次|人|页|CNY|RMB|USD)$",
    re.IGNORECASE,
)

# 列表项前缀：● / 1. / （一）、 等。切段时先剥掉它再判断句末标点。
LIST_ITEM_PREFIX = re.compile(
    r"^(?:[●⚫•\-–\*]\s+|\d+[.)]\s+|[（(]?[一二三四五六七八九十]+[、.)]\s*)"
)

# 图注/表注的引导词。只在同页、区域上方找，避免把正文第一行当图注。
CAPTION_KEYWORDS = ("table ", "figure ", "表 ", "图 ", "表：", "图：", "表1", "图1")
UNIT_HEADER_MARKER = "单位"

# 文件名尾部的版本/副本修饰词（§4.4）：同一个 doc_key 只能有一个版本。
FILE_TAIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # -v2 / _v2.1 / v3
    re.compile(r"[-_\s]*v\d+(?:[.\-]\d+)*$", re.IGNORECASE),
    # 20250301 / 2025-03-01 / 2025_03 / 2025年3月 / 2025年3月1日 / 2025
    re.compile(
        r"[-_\s]*\d{4}"
        r"(?:[-_.年]\d{1,2}(?:[-_.月]\d{1,2}日?|月)?)?"
        r"(?:版|年度?)?$"
    ),
    # 第2版 / 第2稿 / 2版 / 二稿
    re.compile(r"[-_\s]*(?:第\s*\d+\s*[版稿次]|[二三四五六七八九十]+稿)$"),
    # 修订 / 修订版 / 终版 / 定稿 / final / draft
    re.compile(
        r"[-_\s]*(?:修订版?|终版?|正式版?|定稿版?|最新版?|final|draft)$",
        re.IGNORECASE,
    ),
    # 副本 / 拷贝 / copy / 备份，裸写或被括号包住（括号整组匹配，不留半截）
    re.compile(
        r"[-_\s]*(?:副本|拷贝|备份|copy|副本拷贝)$"
        r"|[-_\s]*[\(（\[【](?:副本|拷贝|备份|copy)[\)）\]】]$",
        re.IGNORECASE,
    ),
    # (2) / （2） / [2]
    re.compile(r"[-_\s]*[\(（\[【]\s*\d+\s*[\)）\]】]$"),
    # 纯序号段尾：-2 / _2
    re.compile(r"[-_\s]+\d{1,2}$"),
)
