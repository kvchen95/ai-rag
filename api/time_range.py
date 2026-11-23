"""时间语义归一（详细设计.md §5.3）。

用户说的时间大多不是绝对日期，得先换算。规则固定成表，不做语义猜测——
猜测的代价是把"三月"理解成 2025-03 还是 2026-03，而这个差别会直接进过滤条件，
让本该召回的证据消失，或者让过期条款混进来。

四条口径（对应 §5.3 的边界三条 + 交付物）：

1. **相对年以系统时钟为准**：去年/前年/今年。结果是一个确定的闭区间，但它是
   算出来的，不是用户说的——所以 `basis="relative"`，与"用户写了 2025 年"区分开。
2. **上个月按日历算月末**：31 天的月份不会被算成 30 天。跨年（1 月的"上个月"）
   落到上一年 12 月。
3. **缺年份补当前年并标注**：三月的标准 → 2026-03，`inferred=True`，
   `note` 里写清补了哪一年。这个标注要一路带到答案里（§5.3 "并在回答里注明假设"）。
4. **换算不了的不加条件，但要说出来**：最近/前段时间 → 不加时间条件，
   `note="未限定时间"`。"最新版"是同一种处理：它与时间过滤互斥，只取当前生效版本。

刻意不做的事：不做"大概""左右""上半年"这类猜测式换算。规则表里没有的说法
一律落到"未限定时间"——少一个时间条件只会多召回，错了也还能被门控拦住；
猜错一个时间窗却会让答案指向错误的条款。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Granularity = Literal["day", "month", "year"]
Basis = Literal["explicit", "relative", "assumed_year"]

_CN_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

# 明确的绝对日期：2025年3月15日 / 2025-03-15 / 2025/3/1
_ABS_DATE = re.compile(
    r"(?P<y>\d{4})\s*[-/年]\s*(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*[日号]?"
)
# 绝对年月：2025年3月 / 2025-03
_ABS_MONTH = re.compile(
    r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月|(?P<y2>\d{4})-(?P<m2>\d{1,2})(?!-)"
)
# 绝对年：2025年
_ABS_YEAR = re.compile(r"(?P<y>\d{4})\s*年")

# 相对年 + 月：去年十二月 / 前年3月
_REL_YEAR_MONTH = re.compile(
    r"(?P<rel>去年|前年|今年|上一年|本年度)\s*(?P<mon>[一二三四五六七八九十]{1,2}|\d{1,2})\s*月"
)
_REL_YEAR = re.compile(r"去年|前年|今年|上一年|本年度")
_PREV_MONTH = re.compile(r"上个?月")
_THIS_MONTH = re.compile(r"本月|这个月|当月")
_BARE_MONTH = re.compile(r"(?P<mon>[一二三四五六七八九十]{1,2}|\d{1,2})\s*月份?")
_ACTIVE_ONLY = re.compile(r"最新版|最新一版|现行版|最新")
_VAGUE = re.compile(r"最近|近期|近来|前段?时间|目前|近日")

# 「三月」里的「三」不能是「前三年」「十二个月」的一部分，也不能吃日期里的月。
# 句子的前一位若是数字或「个/年/第/几/多」，就不是月份表达。
_MONTH_NOT_A_MONTH_AFTER = set("个第几年数多0123456789")


@dataclass(frozen=True, slots=True)
class TimeRange:
    """一个已归一的绝对时间窗，闭区间，YYYYMMDD。

    与 `chunks.effective_date` 同一口径（int），两边（PG / Qdrant）直接可比。
    """

    date_from: int
    date_to: int
    granularity: Granularity
    matched: str
    basis: Basis
    inferred: bool = False
    note: str | None = None

    @property
    def years(self) -> tuple[int, ...]:
        """覆盖到的自然年。用于排障与日志，不参与过滤（过滤按 effective_date 精确）。"""
        return tuple(range(self.date_from // 10000, self.date_to // 10000 + 1))

    def as_dict(self) -> dict[str, object]:
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
    """归一结果：有窗口就带上，没有窗口也要说明为什么没有。

    `range=None` 不是"忘了处理"，而是三种明确情形之一：问句里没有时间词、
    时间词换算不了（vague）、或者明确要最新版（与时间过滤互斥）。
    `note` 必须能直接放进答案与审计，所以写成给人看的一句话。
    """

    question: str
    range: TimeRange | None = None
    note: str | None = None

    @property
    def has_window(self) -> bool:
        return self.range is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "range": None if self.range is None else self.range.as_dict(),
            "note": self.note,
        }


def _month_span(year: int, month: int) -> tuple[int, int]:
    last = calendar.monthrange(year, month)[1]
    return year * 10000 + month * 100 + 1, year * 10000 + month * 100 + last


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _year_span(year: int, fiscal_start_month: int) -> tuple[int, int]:
    """某个自然年 / 财年的闭区间。fiscal_start_month=1 就是自然年。"""
    if fiscal_start_month == 1:
        return year * 10000 + 101, year * 10000 + 1231
    start = date(year, fiscal_start_month, 1)
    # 财年结束日是下一财年起始日的前一天：用日期算术，不手写"3 月 31 日"。
    end = date(year + 1, fiscal_start_month, 1) - timedelta(days=1)
    return start.year * 10000 + start.month * 100 + start.day, (
        end.year * 10000 + end.month * 100 + end.day
    )


def _fiscal_label(today: date, fiscal_start_month: int) -> int:
    """今天落在哪个（财）年的标签上。1 月起始时就是自然年。"""
    if fiscal_start_month == 1:
        return today.year
    return today.year if today.month >= fiscal_start_month else today.year - 1


def _parse_month(token: str) -> int | None:
    token = token.strip()
    if not token:
        return None
    if token.isdigit():
        month = int(token)
        return month if 1 <= month <= 12 else None
    if len(token) == 1:
        return _CN_DIGITS.get(token)
    # 十一 / 十二
    if token[0] == "十":
        tail = _CN_DIGITS.get(token[1]) if len(token) > 1 else None
        return 10 + tail if tail else None
    if token[-1] == "十" and token[0] in _CN_DIGITS:
        return _CN_DIGITS[token[0]] * 10
    if len(token) == 2 and token[0] in _CN_DIGITS and token[1] == "十":
        return _CN_DIGITS[token[0]] * 10
    if (
        len(token) == 3
        and token[1] == "十"
        and token[0] in _CN_DIGITS
        and token[2] in _CN_DIGITS
    ):
        return _CN_DIGITS[token[0]] * 10 + _CN_DIGITS[token[2]]
    return None


def _bare_month_is_real(question: str, start: int) -> bool:
    """排除「前三年」「十二个月」「第 3 月」里的"月/年"被当成月份。"""
    if start == 0:
        return True
    prev = question[start - 1]
    if prev in _MONTH_NOT_A_MONTH_AFTER:
        return False
    # 「三个月」「6 个月」这种量词：前一位是「个」
    return True


def normalise_time(
    question: str,
    *,
    now: date | None = None,
    fiscal_start_month: int = 1,
) -> TimeNormalisation:
    """把问句里的时间说法换成一个绝对闭区间。

    `now` 与 `fiscal_start_month` 都显式传入（默认取系统时钟与自然年），
    这样测试不必依赖"跑测试那天是几号"——一个只在某些日期通过的用例等于没有用例。
    """
    if not (1 <= fiscal_start_month <= 12):
        raise ValueError(f"fiscal_start_month 必须是 1..12，实际 {fiscal_start_month}")
    today = now or date.today()
    text = question or ""
    fiscal_label = _fiscal_label(today, fiscal_start_month)
    fy_note = (
        None
        if fiscal_start_month == 1
        else f"财年以 {fiscal_start_month} 月起算（不是自然年）"
    )

    # 1. 绝对日期：2025年3月15日
    hit = _ABS_DATE.search(text)
    if hit:
        y, m, d = int(hit["y"]), int(hit["m"]), int(hit["d"])
        if not _valid_ymd(y, m, d):
            return TimeNormalisation(
                text, note=f"无法换算的时间：{hit.group(0)} 不是合法日期"
            )
        stamp = y * 10000 + m * 100 + d
        return TimeNormalisation(
            text,
            TimeRange(stamp, stamp, "day", hit.group(0), "explicit"),
        )

    # 2. 绝对年月：2025年3月 / 2025-03
    hit = _ABS_MONTH.search(text)
    if hit:
        y = int(hit["y"] or hit["y2"])
        m = int(hit["m"] or hit["m2"])
        if not _valid_ymd(y, m, 1):
            return TimeNormalisation(
                text, note=f"无法换算的时间：{hit.group(0)} 不是合法年月"
            )
        start, end = _month_span(y, m)
        return TimeNormalisation(
            text, TimeRange(start, end, "month", hit.group(0), "explicit")
        )

    # 3. 相对年 + 月：去年十二月 / 前年3月（先匹配，否则「去年」会把整年吃掉）
    hit = _REL_YEAR_MONTH.search(text)
    if hit:
        month = _parse_month(hit["mon"])
        rel = hit["rel"]
        years_back = {"去年": 1, "上一年": 1, "前年": 2, "今年": 0, "本年度": 0}[rel]
        year = fiscal_label - years_back
        if month is None:
            return TimeNormalisation(text, note=f"无法换算的时间：{hit.group(0)}")
        start, end = _month_span(year, month)
        note = fy_note if fiscal_start_month != 1 else None
        return TimeNormalisation(
            text,
            TimeRange(
                start,
                end,
                "month",
                hit.group(0),
                "relative",
                note=(
                    "月份表述按自然年解释（基础版不做财年内月份消歧）" if note else None
                ),
            ),
        )

    # 4. 绝对年：2025年
    hit = _ABS_YEAR.search(text)
    if hit:
        y = int(hit["y"])
        start, end = _year_span(y, fiscal_start_month)
        return TimeNormalisation(
            text,
            TimeRange(start, end, "year", hit.group(0), "explicit", note=fy_note),
        )

    # 5. 相对年：去年 / 前年 / 今年
    hit = _REL_YEAR.search(text)
    if hit:
        rel = hit.group(0)
        years_back = {"去年": 1, "上一年": 1, "前年": 2, "今年": 0, "本年度": 0}[rel]
        start, end = _year_span(fiscal_label - years_back, fiscal_start_month)
        return TimeNormalisation(
            text, TimeRange(start, end, "year", rel, "relative", note=fy_note)
        )

    # 6. 上个月（跨年：1 月的上个月落在上一年 12 月）
    hit = _PREV_MONTH.search(text)
    if hit:
        y, m = _shift_month(today.year, today.month, -1)
        start, end = _month_span(y, m)
        return TimeNormalisation(
            text, TimeRange(start, end, "month", hit.group(0), "relative")
        )

    # 7. 本月
    hit = _THIS_MONTH.search(text)
    if hit:
        start, end = _month_span(today.year, today.month)
        return TimeNormalisation(
            text, TimeRange(start, end, "month", hit.group(0), "relative")
        )

    # 8. 裸月份：三月的标准 → 补当前年，并标注（§5.3 交付物里点名的一条）
    for hit in _BARE_MONTH.finditer(text):
        if not _bare_month_is_real(text, hit.start()):
            continue
        month = _parse_month(hit["mon"])
        if month is None:
            continue
        start, end = _month_span(today.year, month)
        return TimeNormalisation(
            text,
            TimeRange(
                start,
                end,
                "month",
                hit.group(0),
                "assumed_year",
                inferred=True,
                note=f"问句未写年份，按当前年份 {today.year} 解释为 {today.year}-{month:02d}",
            ),
        )

    # 9. 最新版：与时间过滤互斥，只取当前生效版本
    hit = _ACTIVE_ONLY.search(text)
    if hit:
        return TimeNormalisation(
            text, note="最新版：不加时间条件，只取当前生效版本（§5.3）"
        )

    # 10. 换算不了的：不加条件，但在答案里说明
    hit = _VAGUE.search(text)
    if hit:
        return TimeNormalisation(
            text, note=f"「{hit.group(0)}」无法换算成区间，未限定时间"
        )

    return TimeNormalisation(text, note=None)


def _valid_ymd(year: int, month: int, day: int) -> bool:
    if not (1900 <= year <= 2999) or not (1 <= month <= 12) or not (1 <= day <= 31):
        return False
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True
