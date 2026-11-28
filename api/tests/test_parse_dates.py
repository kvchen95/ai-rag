"""业务时间与可信度（§3.5 第四项随行信息 / §5.2 时间过滤）。.

这一项最容易出错的不是"认不认得出日期"，而是**把推断当显式**：

* 时间过滤只认 `date_confidence='explicit'`（§5.2）。如果"2025 revision"这种标题
  年份被写成 explicit，用户问"2024 年的标准"时，一条只是"正文提到 2025"的切片
  会被当成"这条在 2024 年生效"——而它可能 2023 年就生效了。
* 反过来，如果正文真的写了"生效日期：2025-03-01"却记成 inferred，时间过滤永远
  不会认它，检索就会漏掉最关键的那条。

所以三类情形的判据分开测：完整日期 → explicit；只有年份/年月 → inferred；
什么都没有 → 空。另外单独钉住"引导词"这条：一段正文里出现的任何日期都不该
变成切片生效日期（目录里的"发布日期"、发票里的"开票日期"都会干扰）。
"""

from __future__ import annotations

import pytest

from api.parse.dates import (
    EMPTY,
    BusinessDate,
    document_date,
    find_date,
    merge_dates,
)

# (文本, 期望日期, 期望年份, 期望可信度, 理由)
CASES = [
    # --- 完整日期 + 引导词 → explicit ---
    ("生效日期：2025-03-01。", 20250301, 2025, "explicit", "显式日期 + 引导词"),
    ("本制度自 2025/3/1 起执行。", 20250301, 2025, "explicit", "斜杠分隔"),
    ("实施日期 2025.03.01", 20250301, 2025, "explicit", "点分隔"),
    ("2025年3月1日生效", 20250301, 2025, "explicit", "中文日期"),
    (
        "Effective date: 20250301",
        20250301,
        2025,
        "explicit",
        "紧凑格式 + 英文引导词",
    ),
    ("Issued 2025-03-01", 20250301, 2025, "explicit", "英文引导词"),
    # --- 只有年份 → inferred ---
    (
        "Travel Expense Policy (CN) - 2025 revision",
        None,
        2025,
        "inferred",
        "标题年份",
    ),
    ("Leave Policy - 2025", None, 2025, "inferred", "标题年份（无 revision）"),
    ("2025年度差旅办法", None, 2025, "inferred", "年度"),
    # --- 只有年月 → inferred（故意不当 explicit）---
    ("2025年3月 修订", None, 2025, "inferred", "只有年月"),
    # --- 什么都没有 ---
    ("这一段没有任何时间信息。", None, None, "inferred", "无日期"),
    ("", None, None, "inferred", "空文本"),
]


@pytest.mark.parametrize(("text", "date", "year", "confidence", "why"), CASES)
def test_find_date_classification(
    text: str, date: int | None, year: int | None, confidence: str, why: str
) -> None:
    """Run test_find_date_classification."""
    found = find_date(text)
    assert found.effective_date == date, why
    assert found.effective_year == year, why
    assert found.confidence == confidence, why


def test_explicit_requires_a_lead_word() -> None:
    """`require_lead=True` 时，没有引导词的日期不算生效日期。.

    用途：切片级判定。一段正文里出现的任何日期都不该变成"这批切片的生效时间"，
    只有明说"生效日期：…"的才算（目录里的"发布日期"、注释里的"修订于"都要排除）。
    """
    assert (
        find_date("2025-03-01 我们开了个会", require_lead=True).effective_date
        is None
    )
    assert (
        find_date("生效日期 2025-03-01", require_lead=True).effective_date
        == 20250301
    )


def test_document_level_date_does_not_require_a_lead_word() -> None:
    """文档级日期不需要引导词：`- 2025 revision` 就是材料年份。."""
    assert (
        document_date(
            "Travel Expense Policy (CN) - 2025 revision"
        ).effective_year
        == 2025
    )


def test_business_date_derives_the_year_from_the_date() -> None:
    """`effective_year` 由 `effective_date` 派生（附录 A.3）。.

    两者不一致的行会让时间过滤按错误的年去筛，而那种错在 SQL 里完全看不出来。
    """
    value = BusinessDate(20250301, 2025, "explicit")
    assert value.effective_year == value.effective_date // 10000


def test_business_date_rejects_a_mismatched_year() -> None:
    """Run test_business_date_rejects_a_mismatched_year."""
    with pytest.raises(AssertionError):
        BusinessDate(20250301, 2024, "explicit")


def test_business_date_rejects_an_out_of_range_date() -> None:
    """Run test_business_date_rejects_an_out_of_range_date."""
    with pytest.raises(ValueError):
        BusinessDate(18991231, 1899, "explicit")


def test_business_date_rejects_an_unknown_confidence() -> None:
    """Run test_business_date_rejects_an_unknown_confidence."""
    with pytest.raises(ValueError):
        BusinessDate(None, None, "guessed")


def test_month_only_dates_are_not_explicit() -> None:
    """`2025年3月` 不等于 `2025-03-01 生效`。.

    当 explicit 会让"3 月 15 日"的提问命中一条 3 月 1 日生效的推断——
    而它可能根本还没生效。
    """
    found = find_date("2025年3月 修订")
    assert not found.is_explicit
    assert found.confidence == "inferred"


def test_invalid_calendar_dates_are_skipped() -> None:
    """`2025-13-45` 不是日期，别把它当 2026-01-14 之类的东西。."""
    assert find_date("生效日期 2025-13-45").effective_date is None


def test_leap_day_is_accepted() -> None:
    """Run test_leap_day_is_accepted."""
    assert find_date("生效日期 2024-02-29").effective_date == 20240229


def test_merge_prefers_the_slice_level_date() -> None:
    """切片级结果优先，缺什么用文档级补；`confidence` 取严格的那一个。."""
    slice_level = BusinessDate(20250301, 2025, "explicit")
    doc_level = BusinessDate(None, 2024, "inferred")
    assert merge_dates(slice_level, doc_level) == slice_level


def test_merge_falls_back_to_the_document_year() -> None:
    """Run test_merge_falls_back_to_the_document_year."""
    assert (
        merge_dates(EMPTY, BusinessDate(None, 2024, "inferred")).effective_year
        == 2024
    )


def test_merge_does_not_fabricate_a_full_date() -> None:
    """“切片有年、文档有日”不能拼成完整日期——那是在造事实。."""
    slice_level = BusinessDate(None, 2025, "inferred")
    doc_level = BusinessDate(20240301, 2024, "explicit")
    merged = merge_dates(slice_level, doc_level)
    assert merged.effective_date is None
    assert merged.effective_year == 2025
    assert merged.confidence == "inferred"


def test_empty_is_the_neutral_element() -> None:
    """Run test_empty_is_the_neutral_element."""
    assert EMPTY.effective_date is None
    assert EMPTY.effective_year is None
    assert EMPTY.confidence == "inferred"
    assert not EMPTY.is_explicit


def test_document_date_uses_the_first_page_when_the_title_has_none() -> None:
    """Run test_document_date_uses_the_first_page_when_the_title_has_none."""
    found = document_date(None, ["2025 版制度汇编", "其他内容"])
    assert found.effective_year == 2025


def test_document_date_returns_empty_without_any_hint() -> None:
    """Run test_document_date_returns_empty_without_any_hint."""
    assert document_date(None, ["没有年份的一段话"]) == EMPTY


def test_document_date_never_produces_an_explicit_date() -> None:
    """文档级日期只用来兜底，绝不进 explicit——否则时间过滤会把它当真。."""
    found = document_date("2025-03-01 生效的制度")
    assert not found.is_explicit
