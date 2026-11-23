"""业务时间与可信度（详细设计.md §3.5 / §5.2 / 附录 A.3）。

两条规矩，写在这里是为了别处不再各自解释：

1. **只有显式日期才写 `effective_date`**（`date_confidence='explicit'`）。
   时间过滤只认 `explicit`（§5.2），所以"推断出来的年份"必须写成 `inferred`——
   否则用户问"2024 年的标准"，系统会拿一条只是"正文提到 2024"的切片当答案。
2. **推断不是猜**：能推断的只有"整篇文档的生效年份"这一种情形（版本头部的
   `2025 revision`、文件名里的年份）。段落里出现"去年""最近"一律不算。

三种结果：

| 情形 | effective_date | effective_year | confidence |
|---|---|---|---|
| 正文有完整日期（2025-03-01 / 20250301 / 2025年3月1日） | 20250301 | 2025 | explicit |
| 正文只有年份（2025 revision / 2025年度） | NULL | 2025 | inferred |
| 什么都没有 | NULL | NULL | inferred |

`effective_year` 由 `effective_date` 派生（附录 A.3：`由日期派生`）——派生列必须
在同一个函数里算，分成两处就会出现"日期是 2026、年份是 2025"的行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# 完整日期：2025-03-01 / 2025/3/1 / 2025.03.01 / 2025年3月1日 / 20250301
#
# `日?` 那个可选后缀是**必须加负向断言**的：不加的话 `2025年3月`（只有年月）
# 会被匹配成"2025年3月" + 空日后缀 → 被当成 2025-03-01 的显式日期。
# 实测就是这么错的（"2025年3月 修订"被记成 explicit 的 3 月 1 日）。
# 负向断言要求：如果后面紧跟日号，它必须是真日期；否则必须不是数字。
_FULL_DATE = re.compile(
    r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?(?!\d)"
    r"|(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"
)
# 年月（无日）：2025年3月 / 2025-03 / 2025.3
_YEAR_MONTH = re.compile(r"(?<!\d)(\d{4})\s*[-/.年]\s*(\d{1,2})(?![\d日])\s*月?(?![\d日])")
# 只有年份：2025 / 2025年 / 2025 年度 / 2025 revision
_YEAR = re.compile(r"(?<!\d)(20\d{2}|19\d{2})(?!\d)\s*(?:年|年度|版|revision|edition)?", re.IGNORECASE)

# 日期前的引导词：有它才敢把"2025-03-01"当生效日期，而不是"文件里随便一个日期"。
# 目录里的"发布日期"、发票里的"开票日期"都不该变成切片生效日期。
_DATE_LEADS = (
    "生效",
    "实施",
    "执行",
    "发布",
    "修订",
    "effective",
    "effective date",
    "issued",
    "revised",
    "as of",
)


@dataclass(frozen=True, slots=True)
class BusinessDate:
    """切片的业务时间。三个字段与 `chunks` 的三列逐字对应。"""

    effective_date: int | None = None
    effective_year: int | None = None
    confidence: str = "inferred"

    def __post_init__(self) -> None:
        if self.confidence not in {"explicit", "inferred"}:
            raise ValueError(f"bad date confidence: {self.confidence}")
        if self.effective_date is not None:
            if not (19000101 <= self.effective_date <= 29991231):
                raise ValueError(f"effective_date out of range: {self.effective_date}")
            # 派生列必须与日期自洽：这两行若不一致，时间过滤会按错误的年去筛。
            assert self.effective_year == self.effective_date // 10000, (
                f"effective_year {self.effective_year} does not match "
                f"effective_date {self.effective_date}"
            )

    @property
    def is_explicit(self) -> bool:
        return self.confidence == "explicit"


EMPTY = BusinessDate()


def _valid_month_day(month: int, day: int) -> bool:
    if not (1 <= month <= 12):
        return False
    days = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return 1 <= day <= days[month - 1]


def _mk(year: int, month: int | None, day: int | None) -> int | None:
    if year < 1900 or year > 2999:
        return None
    month = month or 1
    day = day or 1
    if not _valid_month_day(month, day):
        return None
    return year * 10000 + month * 100 + day


def find_date(text: str, *, require_lead: bool = False) -> BusinessDate:
    """从一段文本里找业务日期。

    `require_lead=True` 时要求日期前面 20 个字符内出现引导词（生效/实施/effective…）。
    切片级用 `require_lead=True`：一段正文里出现的任何日期都不该变成"这批切片的
    生效时间"，只有明说"生效日期：…"的才算。文档级（`document_date`）用 False——
    标题里的 `2025 revision` 就是这份材料的年份。
    """
    if not text:
        return EMPTY
    for match in _FULL_DATE.finditer(text):
        groups = match.groups()
        if groups[0] is not None:
            year, month, day = int(groups[0]), int(groups[1]), int(groups[2])
        else:
            year, month, day = int(groups[3]), int(groups[4]), int(groups[5])
        if require_lead and not _has_lead(text, match.start()):
            continue
        value = _mk(year, month, day)
        if value is None:
            continue
        return BusinessDate(value, year, "explicit")

    for match in _YEAR_MONTH.finditer(text):
        year, month = int(match.group(1)), int(match.group(2))
        if require_lead and not _has_lead(text, match.start()):
            continue
        if not (1900 <= year <= 2999) or not _valid_month_day(month, 1):
            continue
        # **只有年月就没有 `effective_date`**：把"2025年3月"写成 20250301 是在
        # 说"3 月 1 日生效"，而原文只说"3 月"。这个谎会让"3 月 15 日的标准"
        # 的提问命中一条可能还没生效的条款（§5.2 的时间过滤只认 explicit，
        # 但显式的错日期比 inferred 更危险——它会被时间过滤当成真的）。
        return BusinessDate(None, year, "inferred")

    for match in _YEAR.finditer(text):
        year = int(match.group(1))
        if require_lead and not _has_lead(text, match.start()):
            continue
        value = _mk(year, None, None)
        if value is None:
            continue
        return BusinessDate(None, year, "inferred")

    return EMPTY


def _has_lead(text: str, index: int, *, window: int = 20) -> bool:
    prefix = text[max(0, index - window) : index].lower()
    return any(lead in prefix for lead in _DATE_LEADS)


def document_date(title: str | None, first_pages_text: Sequence[str] | Iterable[str] = ()) -> BusinessDate:
    """文档级日期：标题优先，其次前几页正文。

    只用于给"正文里完全没写日期"的切片兜底（写 `inferred`）。它不进 `explicit`,
    所以不会被时间过滤当成"这条切片在 2025 年生效"。
    """
    if title:
        found = find_date(title)
        if found.effective_year is not None:
            return BusinessDate(None, found.effective_year, "inferred")
    for text in first_pages_text:
        found = find_date(text)
        if found.effective_year is not None:
            return BusinessDate(None, found.effective_year, "inferred")
    return EMPTY


def merge_dates(primary: BusinessDate, fallback: BusinessDate) -> BusinessDate:
    """切片级结果优先，缺什么用文档级补。

    `confidence` 取**严格的那一个**：切片写了显式日期就用 explicit；只有补出来的
    年份就用 inferred。不做"切片有年、文档有日就拼成完整日期"——那是在造事实。
    """
    if primary.effective_date is not None:
        return primary
    if primary.effective_year is not None:
        return primary
    return fallback


__all__ = [
    "BusinessDate",
    "EMPTY",
    "document_date",
    "find_date",
    "merge_dates",
]
