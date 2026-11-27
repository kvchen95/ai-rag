"""时间语义归一（详细设计.md §5.3）。

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
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any, Literal

Granularity = Literal["day", "month", "quarter", "half_year", "year"]
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
# 绝对年月：2025年3月 / 2025-03。
# 第二分支的月份必须带边界：否则 "2025-2027" 会被截成 "2025-20"（m2=20 非法），
# 在归一入口就误报"无法换算的时间"。月份写成 0?[1-9]|1[0-2] 且后一位不是数字/连字符。
_ABS_MONTH = re.compile(
    r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月|(?P<y2>\d{4})-(?P<m2>0?[1-9]|1[0-2])(?![\d-])"
)
# 年份区间：2025-2027年 / 2025—2027年 / 2025年至2027年。
# 必须排在 _ABS_MONTH 与 _ABS_YEAR 之前：否则前者把区间截成非法年月并短路，
# 后者只取到其中某一个单年，两种结果都不是用户的口径。
_ABS_YEAR_RANGE = re.compile(
    r"(?P<y1>(?:19|20)\d{2})\s*年?\s*(?:[-—–~～]|至|到)\s*(?P<y2>(?:19|20)\d{2})\s*年"
)
# 绝对季度：2025年第三季度 / 2025 Q3 / 2025年第3季度。
_ABS_QUARTER = re.compile(
    r"(?P<y>\d{4})\s*年\s*(?:第\s*)?(?P<q>[一二三四1-4])\s*季度|"
    r"(?P<y2>\d{4})\s*[Qq](?P<q2>[1-4])|"
    r"(?P<q3>[Qq][1-4])\s*(?P<y3>\d{4})"
)
# 绝对半年：2025年上半年 / 2025年下半年 / 2026 H1 / H1 2026。
_ABS_HALF_YEAR = re.compile(
    r"(?P<y>\d{4})\s*年\s*(?P<half>上半年|下半年)|"
    r"(?P<y2>\d{4})\s*[Hh](?P<h2>[12])|"
    r"(?P<h3>[Hh][12])\s*(?P<y3>\d{4})"
)
# 绝对年：2025年 / FY2025
_ABS_YEAR = re.compile(r"(?P<y>\d{4})\s*年|FY\s*(?P<fy>\d{4})", re.IGNORECASE)

# 只做显式时间解析：相对时间、模糊时间、最新版不在这里猜。
# 解析不到就是没有时间窗；调用方必须显式说明“未解析”，不能静默放宽。
_UNPARSED_MARKERS = (
    "去年",
    "今年",
    "前年",
    "上一年",
    "本年度",
    "上个月",
    "本月",
    "这个月",
    "最近",
    "近期",
    "近来",
    "前段时间",
    "目前",
    "近日",
    "最新",
    "last ",
    "this ",
    "next ",
    "recent",
    "latest",
)

# 【已删除】裸月份/相对时间规则：显式解析不到就返回未解析，不再补年份或猜相对时间。



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
    # 报告期粒度优先按 report_period_start/end 精确过滤；旧手工夹具默认 False。
    use_report_period: bool = False

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
    target_time: str | None = None

    @property
    def has_window(self) -> bool:
        return self.range is not None

    def as_dict(self) -> dict[str, object]:
        return {
            "range": None if self.range is None else self.range.as_dict(),
            "note": self.note,
            "target_time": self.target_time,
        }


def _month_span(year: int, month: int) -> tuple[int, int]:
    last = calendar.monthrange(year, month)[1]
    return year * 10000 + month * 100 + 1, year * 10000 + month * 100 + last


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


_FORECAST_MARKERS = ("预测", "预计", "展望", "规划", "未来", "市场规模", "到", "至")
_DOC_TIME_MARKERS = ("报告", "年报", "季报", "半年报", "月报", "制度", "政策", "办法", "年度", "季度")


def _forecast_years(text: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"(?P<y>\d{4})\s*年", text or ""):
        year = m.group("y")
        after = (text or "")[m.end(): m.end() + 8]
        window = (text or "")[max(0, m.start() - 12): m.end() + 25]
        if any(marker in after for marker in _DOC_TIME_MARKERS):
            continue
        if any(marker in window for marker in _FORECAST_MARKERS):
            out.append(year)
    seen = set()
    result = []
    for y in out:
        if y not in seen:
            seen.add(y)
            result.append(y)
    return result


def _strip_forecast_years(text: str, years: list[str]) -> str:
    if not years:
        return text or ""
    year_set = set(years)
    def _repl(m: re.Match) -> str:
        if m.group("y") in year_set:
            return ""
        return m.group(0)
    return re.sub(r"(?P<y>\d{4})\s*年", _repl, text or "")



def normalise_time(
    question: str,
    *,
    client: Any = None,
    now: date | None = None,
    fiscal_start_month: int = 1,
) -> TimeNormalisation:
    """只做显式时间解析；不调用 LLM，也不猜相对/模糊时间。

    `client` 保留仅为兼容旧调用签名，传入也不会被使用。相对时间与模糊表达
    返回 `range=None` + note，由调用方显式处理“未解析”。
    """
    import re as _re
    _year_range = _re.search(r"(?P<y1>(?:19|20)\d{2})\s*年?\s*(?:[-—–~～]|至|到)\s*(?P<y2>(?:19|20)\d{2})\s*年", question or "")
    if _year_range:
        target_years = []
    else:
        target_years = _forecast_years(question or "")
    target_time = target_years[0] if target_years else None
    cleaned = _strip_forecast_years(question or "", target_years)
    result = _normalise_time_rules(cleaned, now=now, fiscal_start_month=fiscal_start_month)
    document_range = result.range
    if document_range is not None and document_range.granularity in ("year", "half_year", "quarter"):
        document_range = replace(document_range, use_report_period=True)
    return TimeNormalisation(
        question=question or "",
        range=document_range,
        note=result.note,
        target_time=target_time,
    )


def _normalise_time_rules(
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
    text = question or ""
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

    # 1b. 年份区间：2025-2027年 —— 明确记为"未限定时间"，不硬套单年闭区间。
    # 这类券商研报切片的 effective_date 常为 NULL，而 filters 只要拿到 time_range
    # 就会追加 effective_date BETWEEN 且 date_confidence='explicit'；闭区间会把
    # 正确证据全筛掉（B08 实测会变回零候选）。与 vague 词同口径：只多召回、不漏召回。
    hit = _ABS_YEAR_RANGE.search(text)
    if hit:
        return TimeNormalisation(
            text,
            note=f"「{hit.group(0)}」是年份区间，当前不做区间换算，未限定时间",
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

    # 2b. 绝对季度：2025年第三季度 / 2025 Q3 / Q3 2025。
    hit = _ABS_QUARTER.search(text)
    if hit:
        y = int(hit["y"] or hit["y2"] or hit["y3"])
        q_raw = hit["q"] or hit["q2"] or hit["q3"]
        q = int(str(q_raw).lstrip("Qq")) if str(q_raw).lstrip("Qq").isdigit() else _CN_DIGITS.get(str(q_raw), 0)
        if not (1 <= q <= 4):
            return TimeNormalisation(text, note=f"无法换算的时间：{hit.group(0)}")
        starts = {1: 101, 2: 401, 3: 701, 4: 1001}
        ends = {1: 331, 2: 630, 3: 930, 4: 1231}
        return TimeNormalisation(
            text,
            TimeRange(y * 10000 + starts[q], y * 10000 + ends[q], "quarter", hit.group(0), "explicit"),
        )

    # 2c. 绝对半年：2025年上半年 / 2026 H1 / H1 2026。
    hit = _ABS_HALF_YEAR.search(text)
    if hit:
        y = int(hit["y"] or hit["y2"] or hit["y3"])
        half = hit["half"] or (
            "上半年" if str(hit["h2"] or hit["h3"]).lstrip("Hh") == "1" else "下半年"
        )
        if half == "上半年":
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
            TimeRange(start, end, "year", hit.group(0), "explicit", note=fy_note),
        )

    # 相对时间/模糊时间/最新版不在这一层解析：显式返回未解析，不猜。
    if any(marker in text for marker in _UNPARSED_MARKERS):
        return TimeNormalisation(
            text,
            note="未识别到可解析的显式时间；未限定时间",
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
