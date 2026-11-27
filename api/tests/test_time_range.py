"""时间解析：只接受显式区间；相对/模糊时间必须是 unparsed，不猜、不兜底。"""

from __future__ import annotations

from datetime import date

import pytest

from api.time_range import normalise_time

NOW = date(2026, 3, 15)


def _range(question: str, **kwargs: object):
    result = normalise_time(question, now=NOW, **kwargs)  # type: ignore[arg-type]
    assert (
        result.range is not None
    ), f"{question!r} 应解析出时间窗，实际 note={result.note}"
    return result.range


def test_absolute_year() -> None:
    tr = _range("2025年的差旅标准")
    assert (tr.date_from, tr.date_to) == (20250101, 20251231)
    assert tr.basis == "explicit"


def test_fy_prefix_is_supported() -> None:
    tr = _range("FY2025 revenue")
    assert (tr.date_from, tr.date_to) == (20250101, 20251231)


def test_absolute_month_and_day() -> None:
    assert _range("2025年3月的差旅标准").date_from == 20250301
    tr = _range("2025年3月15日的通知")
    assert tr.date_from == tr.date_to == 20250315


def test_iso_style_dates() -> None:
    assert _range("2025-03 的标准").date_from == 20250301
    assert _range("2025-03-15 起执行").date_from == 20250315


def test_chinese_and_english_half_year() -> None:
    assert _range("海光信息 2026 年上半年营业收入").date_from == 20260101
    assert _range("海光信息 2026 年下半年营业收入").date_from == 20260701
    assert _range("ACME 2026 H1 revenue").date_from == 20260101
    assert _range("ACME H1 2026 revenue").date_from == 20260101


def test_chinese_and_english_quarter() -> None:
    assert _range("长鑫科技 2026 年第三季度毛利率").date_from == 20260701
    assert _range("ACME 2026 Q3 revenue").date_from == 20260701
    assert _range("ACME Q3 2026 revenue").date_from == 20260701


def test_year_range_is_unparsed_not_single_year() -> None:
    result = normalise_time(
        "东方证券对中芯国际2025-2027年归母净利润的预测", now=NOW
    )
    assert result.range is None
    assert "未限定时间" in (result.note or "")
    assert "2025-2027年" in (result.note or "")


@pytest.mark.parametrize(
    "question", ["去年", "今年", "上个月", "最近", "最新版本"]
)
def test_relative_and_vague_time_is_unparsed(question: str) -> None:
    result = normalise_time(question, now=NOW)
    assert result.range is None
    assert "未限定时间" in (result.note or "")


def test_relative_time_never_calls_llm() -> None:
    class _Boom:
        def complete(self, *args, **kwargs):
            raise AssertionError("time parser must not call LLM")

    assert normalise_time("去年", client=_Boom(), now=NOW).range is None


def test_no_time_word_means_no_condition() -> None:
    result = normalise_time("差旅住宿标准是多少", now=NOW)
    assert result.range is None
    assert result.note is None


def test_invalid_calendar_date_is_unparsed() -> None:
    result = normalise_time("2025年2月30日的文件", now=NOW)
    assert result.range is None
    assert "无法换算" in (result.note or "")


@pytest.mark.parametrize(
    "question", ["前三年内的规定", "十二个月以内", "三个月的试用期是多久"]
)
def test_non_month_expressions_do_not_produce_a_window(question: str) -> None:
    assert normalise_time(question, now=NOW).range is None


def test_explicit_year_with_fiscal_start_month() -> None:
    tr = _range("2026年", fiscal_start_month=4)
    assert (tr.date_from, tr.date_to) == (20260401, 20270331)
    assert "财年" in (tr.note or "")


def test_invalid_fiscal_month_is_rejected() -> None:
    with pytest.raises(ValueError, match="1..12"):
        normalise_time("2026年", now=NOW, fiscal_start_month=13)


def test_as_dict_is_json_ready() -> None:
    import json

    result = normalise_time("2025年", now=NOW)
    payload = json.loads(json.dumps(result.as_dict(), ensure_ascii=False))
    assert payload["range"]["from"] == 20250101
    assert payload["range"]["granularity"] == "year"


def test_years_property_covers_the_window() -> None:
    assert _range("2024年").years == (2024,)
