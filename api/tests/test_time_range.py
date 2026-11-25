"""时间归一（§5.3）：固定时钟，逐个说法验，含跨年与"缺年份补当前年"。

`now` 显式传入而不是依赖系统时钟——一个只在某些日期通过的用例等于没有用例，
而这张表里全是"月末""跨年"这类跟日期强相关的行为。
"""

from __future__ import annotations

from datetime import date

import pytest

from api.time_range import normalise_time

# 固定在 2026-03-15：既不是月初月末，也不在跨年边界上（跨年用例自己指定 now）。
NOW = date(2026, 3, 15)


def _range(question: str, **kwargs: object):
    result = normalise_time(question, now=NOW, **kwargs)  # type: ignore[arg-type]
    assert (
        result.range is not None
    ), f"{question!r} 应归一出时间窗，实际 note={result.note}"
    return result.range


# --- 卡片点名的四种说法 -----------------------------------------------------


def test_last_year() -> None:
    tr = _range("去年差旅住宿标准")
    assert (tr.date_from, tr.date_to) == (20250101, 20251231)
    assert tr.basis == "relative"
    assert tr.inferred is False


def test_last_month() -> None:
    tr = _range("上个月的费用标准")
    assert (tr.date_from, tr.date_to) == (20260201, 20260228)
    assert tr.granularity == "month"


def test_absolute_half_year_is_normalised_before_year() -> None:
    result = normalise_time("海光信息 2026 年上半年营业收入是多少？", now=NOW)
    assert result.range is not None
    assert (result.range.date_from, result.range.date_to) == (20260101, 20260630)
    assert result.range.granularity == "half_year"


def test_absolute_second_half_year_is_normalised() -> None:
    result = normalise_time("海光信息 2026 年下半年营业收入是多少？", now=NOW)
    assert result.range is not None
    assert (result.range.date_from, result.range.date_to) == (20260701, 20261231)
    assert result.range.granularity == "half_year"


def test_last_month_handles_31_day_month() -> None:
    """月底边界按日历算：2026-01 的上个月是 2025-12-31，不是 30 号。"""
    tr = normalise_time("上个月的差标", now=date(2026, 1, 10)).range
    assert tr is not None
    assert (tr.date_from, tr.date_to) == (20251201, 20251231)


def test_last_month_across_year_boundary() -> None:
    tr = normalise_time("上个月的差标", now=date(2026, 1, 1)).range
    assert tr is not None
    assert tr.date_from // 100 == 202512


def test_last_month_lands_on_leap_february() -> None:
    """2028 是闰年：二月 29 天。手写 28 会悄悄漏掉一天。"""
    tr = normalise_time("上个月", now=date(2028, 3, 1)).range
    assert tr is not None
    assert (tr.date_from, tr.date_to) == (20280201, 20280229)


def test_year_before_last() -> None:
    tr = _range("前年制度怎么规定的")
    assert (tr.date_from, tr.date_to) == (20240101, 20241231)


def test_month_without_year_is_assumed_and_flagged() -> None:
    """§5.3 交付物里点名的一条：缺年份补当前年，并标注。"""
    result = normalise_time("三月的标准", now=NOW)
    assert result.range is not None
    assert (result.range.date_from, result.range.date_to) == (20260301, 20260331)
    assert result.range.inferred is True
    assert result.range.basis == "assumed_year"
    # 标注要能直接进答案：必须点名补了哪一年。
    assert "2026" in (result.range.note or "")


# --- explicit / inferred 的分界 --------------------------------------------


def test_explicit_year_is_not_inferred() -> None:
    tr = _range("2025年的差旅标准")
    assert (tr.date_from, tr.date_to) == (20250101, 20251231)
    assert tr.basis == "explicit"
    assert tr.inferred is False


def test_explicit_month_is_not_inferred() -> None:
    tr = _range("2025年3月的差旅标准")
    assert (tr.date_from, tr.date_to) == (20250301, 20250331)
    assert tr.basis == "explicit"
    assert tr.inferred is False


def test_explicit_day_is_a_single_day_window() -> None:
    tr = _range("2025年3月15日的通知")
    assert tr.date_from == tr.date_to == 20250315
    assert tr.granularity == "day"


def test_iso_style_dates_are_recognised() -> None:
    assert _range("2025-03 的标准").date_from == 20250301
    assert _range("2025-03-15 起执行").date_from == 20250315


def test_relative_and_assumed_are_distinguishable() -> None:
    """两类"算出来的"时间不能混成一个：相对年有确定依据，补年份是假设。
    合并了就无法决定措辞该不该带"假设"。"""
    relative = _range("去年")
    assumed = _range("三月")
    assert relative.basis == "relative" and relative.inferred is False
    assert assumed.basis == "assumed_year" and assumed.inferred is True


# --- 相对年 + 月：跨年表述按自然年拆 ---------------------------------------


def test_last_year_december() -> None:
    tr = _range("去年十二月的差标")
    assert (tr.date_from, tr.date_to) == (20251201, 20251231)


def test_year_before_last_march() -> None:
    tr = _range("前年3月的记录")
    assert (tr.date_from, tr.date_to) == (20240301, 20240331)


# --- 不该加时间条件的情形 ---------------------------------------------------


def test_no_time_word_means_no_condition() -> None:
    result = normalise_time("差旅住宿标准是多少", now=NOW)
    assert result.has_window is False
    assert result.note is None


def test_latest_version_is_mutually_exclusive_with_time_filter() -> None:
    """§5.3：最新版 → 不加时间条件，只取当前生效版本。"""
    result = normalise_time("最新版的住宿标准", now=NOW)
    assert result.has_window is False
    assert "生效版本" in (result.note or "")


@pytest.mark.parametrize("question", ["最近的通知", "前段时间的制度", "近期的规定"])
def test_vague_words_add_no_condition_but_say_so(question: str) -> None:
    """换算不了就不加条件，但必须说出来——静默当成"没提时间"会让用户以为查了时间。"""
    result = normalise_time(question, now=NOW)
    assert result.has_window is False
    assert "未限定时间" in (result.note or "")


# --- 假阳性：不是时间的"月" -------------------------------------------------


@pytest.mark.parametrize(
    "question",
    ["前三年内的规定", "十二个月以内", "第3月的账", "三个月的试用期是多久"],
)
def test_non_month_expressions_do_not_produce_a_window(question: str) -> None:
    """「前三年」「十二个月」「三个月」里的"月/年"不是月份表达。
    误判的后果不是少召回，而是**多一个凭空造出来的时间窗**，把正确证据筛掉。"""
    result = normalise_time(question, now=NOW)
    # 「前三年」不含相对年词，不应产生窗口。
    assert result.range is None


def test_cn_numerals_up_to_twelve() -> None:
    assert _range("十一月").date_from == 20261101
    assert _range("十二月").date_from == 20261201


def test_invalid_calendar_date_yields_no_window() -> None:
    """2025年2月30日不是日期：不能当成 20250230 塞进过滤器。"""
    result = normalise_time("2025年2月30日的文件", now=NOW)
    assert result.range is None
    assert "无法换算" in (result.note or "")


# --- 财年（配置项，默认自然年）---------------------------------------------


def test_fiscal_year_shifts_the_window_when_configured() -> None:
    """财年 4 月起算：2026-03-15 仍属 2025 财年（2025-04-01 至 2026-03-31）。"""
    tr = _range("今年", fiscal_start_month=4)
    assert (tr.date_from, tr.date_to) == (20250401, 20260331)
    assert "财年" in (tr.note or "")


def test_fiscal_year_after_start_belongs_to_current_label() -> None:
    """5 月已进入 2026 财年。反方向：证明上一条不是"永远算成上一年"。"""
    tr = normalise_time("今年", now=date(2026, 5, 1), fiscal_start_month=4).range
    assert tr is not None
    assert (tr.date_from, tr.date_to) == (20260401, 20270331)


def test_natural_year_by_default_has_no_fiscal_note() -> None:
    tr = _range("今年")
    assert (tr.date_from, tr.date_to) == (20260101, 20261231)
    assert tr.note is None


def test_fiscal_label_before_fiscal_start_belongs_to_previous_year() -> None:
    """3 月在 4 月起算的财年里还属于上一财年（2025 财年）。"""
    tr = normalise_time("今年", now=date(2026, 3, 1), fiscal_start_month=4).range
    assert tr is not None
    assert tr.date_from == 20250401


def test_invalid_fiscal_month_is_rejected() -> None:
    with pytest.raises(ValueError, match="1..12"):
        normalise_time("今年", now=NOW, fiscal_start_month=13)


# --- 输出的可用性 -----------------------------------------------------------


def test_as_dict_is_json_ready() -> None:
    """归一结果要能直接进日志与审计，所以必须是纯 JSON 类型。"""
    import json

    result = normalise_time("去年", now=NOW)
    payload = json.loads(json.dumps(result.as_dict(), ensure_ascii=False))
    assert payload["range"]["from"] == 20250101
    assert payload["range"]["granularity"] == "year"


def test_years_property_covers_the_window() -> None:
    assert _range("2024年").years == (2024,)


# --- 年份区间：明确记为"未限定时间"（B08 年份区间归一）--------------------


@pytest.mark.parametrize(
    "question",
    [
        "东方证券对中芯国际2025-2027年归母净利润的预测分别是多少？",  # B08 原题
        "2025-2027年归母净利润的预测",
        "2025—2027年的营收预测",
        "2025～2027年的营收预测",
        "2025年至2027年的营收预测",
    ],
)
def test_year_range_is_recorded_as_unbounded_time(question: str) -> None:
    """年份区间不等于单年闭区间：研报类文档的 effective_date 常为 NULL，
    一旦归一出 window，过滤器就会追加 effective_date BETWEEN + explicit 条件，
    把正确证据全筛掉（B08 会变回零候选）。明确记为"未限定时间"才安全。"""
    result = normalise_time(question, now=NOW)
    assert result.range is None
    assert result.note is not None
    assert "未限定时间" in result.note
    assert "无法换算" not in result.note


def test_year_range_without_nian_is_not_mistaken_for_an_illegal_month() -> None:
    """变异守卫：'2025-2027' 曾被 _ABS_MONTH 截成 '2025-20'，谎报成
    "无法换算的时间：2025-20 不是合法年月"——正则中间态外泄且错判。"""
    result = normalise_time("2025-2027 归母净利润预测", now=NOW)
    assert result.range is None
    assert "无法换算" not in (result.note or "")
    assert "2025-20" not in (result.note or "")
