"""时间语义归一（详细设计.md §5.3）。.

用户说的时间大多不是绝对日期。这里的口径是**只解析显式时间**，不猜：猜错的代价是
"三月"被理解成 2025-03 还是 2026-03，而这个差别会直接进过滤条件，让本该召回的
证据消失，或者让过期条款混进来。

四条口径（对应 §5.3 的边界三条 + 交付物）：

1. **只认显式绝对时间**：`2026Q3`/`2026年第三季度`、`2026H1`/`2026年上半年`、
   `2025-03-15`、`2025年3月`、`2025年`/`FY2025`。换算结果是一个确定的闭区间，
   `basis="explicit"`。
2. **相对时间与模糊时间不换算**：去年/今年/上个月/最近/最新版等一律返回
   `range=None` + `note`，由调用方显式处理"未限定时间"，不按系统时钟替用户补。
3. **缺年份不补**：`三月的标准` 不推成 2026-03，同样落到"未限定时间"。
4. **年份区间不套单年闭区间**：`2025-2027年` 不是"猜不出"，而是不能当单年窗口——
   过滤器拿到 window 就会强制按报告期/effective_date 过滤，预测类研报的切片日期
   常为 NULL，套上去会把正确证据筛没（见 §1b 注释）。

报告期语义：`year/half_year/quarter` 粒度的区间带 `use_report_period=True`，
过滤器优先按 `report_period_start/end` 精确匹配，metadata 缺失时才退到
`effective_date`/`effective_year`（详见 `api/filters.py`）。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Literal

from api.resources import time_grammar

# 时间语法表只有一份（api/resources/time_grammar.py）；本地别名保持既有引用名。
_CN_DIGITS = time_grammar.CN_DIGITS
_ABS_DATE = time_grammar.ABS_DATE
_ABS_MONTH = time_grammar.ABS_MONTH
_ABS_YEAR_RANGE = time_grammar.ABS_YEAR_RANGE
_ABS_QUARTER = time_grammar.ABS_QUARTER
_ABS_HALF_YEAR = time_grammar.ABS_HALF_YEAR
_ABS_YEAR = time_grammar.ABS_YEAR
_UNPARSED_MARKERS = time_grammar.UNPARSED_MARKERS
HALF_YEAR_FIRST = time_grammar.HALF_YEAR_FIRST
HALF_YEAR_SECOND = time_grammar.HALF_YEAR_SECOND
NOTE_FISCAL_START = time_grammar.NOTE_FISCAL_START
NOTE_INVALID_DATE = time_grammar.NOTE_INVALID_DATE
NOTE_YEAR_RANGE = time_grammar.NOTE_YEAR_RANGE
NOTE_INVALID_MONTH = time_grammar.NOTE_INVALID_MONTH
NOTE_INVALID_QUARTER = time_grammar.NOTE_INVALID_QUARTER
NOTE_UNPARSED = time_grammar.NOTE_UNPARSED
FISCAL_START_INVALID = time_grammar.FISCAL_START_INVALID

Granularity = Literal["day", "month", "quarter", "half_year", "year"]
Basis = Literal["explicit", "relative", "assumed_year"]

# 【已删除】裸月份/相对时间规则：显式解析不到就返回未解析，
# 不再补年份或猜相对时间。


@dataclass(frozen=True, slots=True)
class TimeRange:
    """一个已归一的绝对时间窗，闭区间，YYYYMMDD。.

    与 `chunks.effective_date` 同一口径（int），两边（PG / Qdrant）直接可比。
    """

    date_from: int
    date_to: int
    granularity: Granularity
    matched: str
    basis: Basis
    inferred: bool = False
    note: str | None = None
    # 报告期粒度优先按 report_period_start/end 精确过滤；旧手工夹具默认 False。
    use_report_period: bool = False

    @property
    def years(self) -> tuple[int, ...]:
        """覆盖到的自然年。.

        用于排障与日志，不参与过滤（过滤按 effective_date 精确）。
        """
        return tuple(range(self.date_from // 10000, self.date_to // 10000 + 1))

    def as_dict(self) -> dict[str, object]:
        """Run as_dict."""
        return {
            "from": self.date_from,
            "to": self.date_to,
            "granularity": self.granularity,
            "matched": self.matched,
            "basis": self.basis,
            "inferred": self.inferred,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class TimeNormalisation:
    """归一结果：有窗口就带上，没有窗口也要说明为什么没有。.

    `range=None` 不是"忘了处理"，而是三种明确情形之一：问句里没有时间词、
    时间词换算不了（vague）、或者明确要最新版（与时间过滤互斥）。
    `note` 必须能直接放进答案与审计，所以写成给人看的一句话。
    """

    question: str
    range: TimeRange | None = None
    note: str | None = None
    target_time: str | None = None

    @property
    def has_window(self) -> bool:
        """Run has_window."""
        return self.range is not None

    def as_dict(self) -> dict[str, object]:
        """Run as_dict."""
        return {
            "range": None if self.range is None else self.range.as_dict(),
            "note": self.note,
            "target_time": self.target_time,
        }


def _month_span(year: int, month: int) -> tuple[int, int]:
    """Run _month_span."""
    last = calendar.monthrange(year, month)[1]
    return year * 10000 + month * 100 + 1, year * 10000 + month * 100 + last


def _year_span(year: int, fiscal_start_month: int) -> tuple[int, int]:
    """某个自然年 / 财年的闭区间。fiscal_start_month=1 就是自然年。."""
    if fiscal_start_month == 1:
        return year * 10000 + 101, year * 10000 + 1231
    start = date(year, fiscal_start_month, 1)
    # 财年结束日是下一财年起始日的前一天：用日期算术，不手写"3 月 31 日"。
    end = date(year + 1, fiscal_start_month, 1) - timedelta(days=1)
    return start.year * 10000 + start.month * 100 + start.day, (
        end.year * 10000 + end.month * 100 + end.day
    )


def normalise_time(
    question: str,
    *,
    client: Any = None,
    now: date | None = None,
    fiscal_start_month: int = 1,
) -> TimeNormalisation:
    """只做显式时间解析；不调用 LLM，也不猜相对/模糊时间。.

    `client` 保留仅为兼容旧调用签名，传入也不会被使用。相对时间与模糊表达
    返回 `range=None` + note，由调用方显式处理“未解析”。
    """
    result = _normalise_time_rules(
        question, now=now, fiscal_start_month=fiscal_start_month
    )
    document_range = result.range
    if document_range is not None and document_range.granularity in (
        "year",
        "half_year",
        "quarter",
    ):
        document_range = replace(document_range, use_report_period=True)
    return TimeNormalisation(
        question=question or "",
        range=document_range,
        note=result.note,
        target_time=None,
    )


def _normalise_time_rules(
    question: str,
    *,
    now: date | None = None,
    fiscal_start_month: int = 1,
) -> TimeNormalisation:
    """把问句里的时间说法换成一个绝对闭区间。.

    `now` 与 `fiscal_start_month` 都显式传入（默认取系统时钟与自然年），
    这样测试不必依赖"跑测试那天是几号"——一个只在某些日期通过的用例等于没有用例。
    """
    if not (1 <= fiscal_start_month <= 12):
        raise ValueError(
            FISCAL_START_INVALID.format(value=fiscal_start_month)
        )
    text = question or ""
    fy_note = (
        None
        if fiscal_start_month == 1
        else NOTE_FISCAL_START.format(start_month=fiscal_start_month)
    )

    # 1. 绝对日期：2025年3月15日
    hit = _ABS_DATE.search(text)
    if hit:
        y, m, d = int(hit["y"]), int(hit["m"]), int(hit["d"])
        if not _valid_ymd(y, m, d):
            return TimeNormalisation(
                text,
                note=NOTE_INVALID_DATE.format(matched=hit.group(0)),
            )
        stamp = y * 10000 + m * 100 + d
        return TimeNormalisation(
            text,
            TimeRange(stamp, stamp, "day", hit.group(0), "explicit"),
        )

    # 1b. 年份区间：2025-2027年 —— 明确记为"未限定时间"，不硬套单年闭区间。
    # 这类券商研报切片的 effective_date 常为 NULL，而 filters 只要拿到
    # time_range 就会追加 effective_date BETWEEN 且 date_confidence='explicit'；
    # 闭区间会把正确证据全筛掉（B08 实测会变回零候选）。与 vague 词同口径：
    # 只多召回、不漏召回。
    hit = _ABS_YEAR_RANGE.search(text)
    if hit:
        return TimeNormalisation(
            text,
            note=NOTE_YEAR_RANGE.format(matched=hit.group(0)),
        )

    # 2. 绝对年月：2025年3月 / 2025-03
    hit = _ABS_MONTH.search(text)
    if hit:
        y = int(hit["y"] or hit["y2"])
        m = int(hit["m"] or hit["m2"])
        if not _valid_ymd(y, m, 1):
            return TimeNormalisation(
                text,
                note=NOTE_INVALID_MONTH.format(matched=hit.group(0)),
            )
        start, end = _month_span(y, m)
        return TimeNormalisation(
            text, TimeRange(start, end, "month", hit.group(0), "explicit")
        )

    # 2b. 绝对季度：2025年第三季度 / 2025 Q3 / Q3 2025。
    hit = _ABS_QUARTER.search(text)
    if hit:
        y = int(hit["y"] or hit["y2"] or hit["y3"])
        q_raw = hit["q"] or hit["q2"] or hit["q3"]
        q = (
            int(str(q_raw).lstrip("Qq"))
            if str(q_raw).lstrip("Qq").isdigit()
            else _CN_DIGITS.get(str(q_raw), 0)
        )
        if not (1 <= q <= 4):
            return TimeNormalisation(
                text, note=NOTE_INVALID_QUARTER.format(matched=hit.group(0))
            )
        starts = {1: 101, 2: 401, 3: 701, 4: 1001}
        ends = {1: 331, 2: 630, 3: 930, 4: 1231}
        return TimeNormalisation(
            text,
            TimeRange(
                y * 10000 + starts[q],
                y * 10000 + ends[q],
                "quarter",
                hit.group(0),
                "explicit",
            ),
        )

    # 2c. 绝对半年：2025年上半年 / 2026 H1 / H1 2026。
    hit = _ABS_HALF_YEAR.search(text)
    if hit:
        y = int(hit["y"] or hit["y2"] or hit["y3"])
        half = hit["half"] or (
            HALF_YEAR_FIRST
            if str(hit["h2"] or hit["h3"]).lstrip("Hh") == "1"
            else HALF_YEAR_SECOND
        )
        if half == HALF_YEAR_FIRST:
            start, end = y * 10000 + 101, y * 10000 + 630
        else:
            start, end = y * 10000 + 701, y * 10000 + 1231
        return TimeNormalisation(
            text, TimeRange(start, end, "half_year", hit.group(0), "explicit")
        )

    # 4. 绝对年：2025年 / FY2026
    hit = _ABS_YEAR.search(text)
    if hit:
        y = int(hit["y"] or hit["fy"])
        start, end = _year_span(y, fiscal_start_month)
        return TimeNormalisation(
            text,
            TimeRange(
                start, end, "year", hit.group(0), "explicit", note=fy_note
            ),
        )

    # 相对时间/模糊时间/最新版不在这一层解析：显式返回未解析，不猜。
    if any(marker in text for marker in _UNPARSED_MARKERS):
        return TimeNormalisation(
            text,
            note=NOTE_UNPARSED,
        )

    return TimeNormalisation(text, note=None)


def _valid_ymd(year: int, month: int, day: int) -> bool:
    """Run _valid_ymd."""
    if (
        not (1900 <= year <= 2999)
        or not (1 <= month <= 12)
        or not (1 <= day <= 31)
    ):
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True
