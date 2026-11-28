"""数字与专名的抽取 / 归一 / 定位（§10.1 第三道、§10.2）。.

这个文件钉的是 §10.2 那张表本身：四个"对上 / 不对上"的例子必须原样成立。
它的价值在于**归一规则一旦被改坏，带数字的答案会全部被拒**（或全部放过）——
两种都不会报错，只会让 96% 数值准确率那条红线悄悄失效。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.verify.numbers import (
    extract_in_context,
    extract_proper_nouns,
    extract_quantities,
    find_value_conflicts,
    locate_proper_nouns,
    locate_quantities,
    parse_chinese_number,
)


def keys(text: str) -> set[tuple[Decimal, str]]:
    """Run keys."""
    return {item.as_key() for item in extract_quantities(text)}


# --- §10.2 的四个例子 ---------------------------------------------------------


def test_five_million_matches_its_arabic_form() -> None:
    """“5,000,000 元”与“五百万元”对上（归一后相等）。."""
    missing, _ = locate_quantities(
        "住宿费限额为五百万元。", "住宿费标准：5,000,000 元/人"
    )
    assert missing == ()


def test_table_header_unit_applies_to_numbers_without_inline_unit() -> None:
    """Run test_table_header_unit_applies_to_numbers_without_inline_unit."""
    evidence = "单位：元币种：人民币\n| 经营活动现金流量净额 | 311,314,642.46 |"
    missing, _ = locate_quantities(
        "经营活动现金流量净额为311,314,642.46元。", evidence
    )
    assert missing == ()


def test_workdays_do_not_match_days() -> None:
    """“30 个工作日”与“30 天”**不对上**：单位不同就是不同的数。."""
    missing, _ = locate_quantities(
        "审批时限为 30 天。", "审批时限：30 个工作日"
    )
    assert len(missing) == 1
    assert missing[0].unit == "天"


def test_eighty_percent_matches_eight_tenths() -> None:
    """“80%”与“八成”对上（成 ×10 折成百分比）。."""
    missing, _ = locate_quantities("报销比例为 80%。", "报销比例：八成")
    assert missing == ()


def test_two_date_notations_match() -> None:
    """“2025-03-01”与“2025 年 3 月 1 日”对上（都归一成 YYYYMMDD）。."""
    missing, _ = locate_quantities(
        "自 2025-03-01 起执行。", "本办法自 2025 年 3 月 1 日起执行。"
    )
    assert missing == ()


def test_date_is_not_split_into_three_bare_numbers() -> None:
    """日期必须先整体摘掉。.

    拆成 2025/03/01 三个裸数字会让每条带日期的答案被拒。
    """
    items = extract_quantities("2025 年 3 月 1 日")
    assert len(items) == 1
    assert items[0].unit == "date"
    assert items[0].value == Decimal(20250301)


# --- 归一细节 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("三", 3),
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("五百", 500),
        ("五百万", 5_000_000),
        ("一千零二十", 1020),
        ("三亿", 300_000_000),
    ],
)
def test_chinese_number_parsing(text: str, expected: int) -> None:
    """Run test_chinese_number_parsing."""
    assert parse_chinese_number(text) == Decimal(expected)


def test_chinese_number_without_unit_is_not_extracted() -> None:
    """“第一”“这一”里的字不是数字。.

    不带单位的纯中文数字一律不认。

    不认的代价是"三倍"这类无单位说法要靠阿拉伯数字表达；认错的代价是每条答案
    都因为一个虚词被判"数字定位不到"。
    """
    assert extract_quantities("第一步是这一条") == ()


def test_range_shares_the_unit_with_both_bounds() -> None:
    """“500-600 元”里两个数都带“元”。.

    否则 500 会变成无单位数字、与证据对不上。
    """
    assert keys("500-600 元") == {(Decimal(500), "元"), (Decimal(600), "元")}


def test_thousands_separator_and_fullwidth_are_normalised() -> None:
    """Run test_thousands_separator_and_fullwidth_are_normalised."""
    assert keys("5,000 元") == {(Decimal(5000), "元")}
    assert keys("5，000 元") == {(Decimal(5000), "元")}


def test_percent_written_in_chinese_words() -> None:
    """Run test_percent_written_in_chinese_words."""
    assert keys("百分之八十") == {(Decimal(80), "%")}
    assert keys("千分之五") == {(Decimal(0.5), "%")}


def test_duplicate_values_are_deduplicated() -> None:
    """同一句里重复出现的同一个数只算一次（子片重叠会重复内容）。."""
    items = extract_quantities("每人每天 500 元，500 元含早餐")
    assert (
        len([item for item in items if item.as_key() == (Decimal(500), "元")])
        == 1
    )


def test_bare_number_without_unit_is_kept_but_never_matches_a_united_one() -> (
    None
):
    """无单位数字与带单位数字不相等——这是"宁可判不过"的方向。."""
    missing, items = locate_quantities("共 500 条", "每人每天 500 元")
    assert items[0].unit == "条"
    assert missing


def test_table_cell_bare_number_can_use_claim_unit() -> None:
    """表格 content_text 丢表头单位时，数值完全一致仍应通过。."""
    evidence = (
        "主要会计数据: 营业收入 | 本报告期（1－6月）: 9,099,020,774.81 | "
        "上年同期: 5,464,235,057.15 | 本报告期比上年同期增减(%): 66.52"
    )
    missing, _ = locate_quantities(
        "海光信息2026年上半年营业收入为9,099,020,774.81元，同比增长66.52%。",
        evidence,
    )
    assert missing == ()


# --- 专名 ---------------------------------------------------------------------


def test_clause_numbers_and_codes_are_extracted_verbatim() -> None:
    """Run test_clause_numbers_and_codes_are_extracted_verbatim."""
    found = extract_proper_nouns("依据第十二条与 GB/T 19001-2016 执行")
    assert "第十二条" in found
    assert "GB/T 19001-2016" in found


def test_proper_noun_must_appear_verbatim() -> None:
    """Run test_proper_noun_must_appear_verbatim."""
    missing, items = locate_proper_nouns("依据第十九条执行", "依据第九条执行")
    assert items == ("第十九条",)
    assert missing == ("第十九条",)


# --- 第五道：同属性同单位两个值 -----------------------------------------------


def test_same_attribute_two_values_is_a_conflict() -> None:
    """Run test_same_attribute_two_values_is_a_conflict."""
    conflicts = find_value_conflicts(
        ["住宿费限额为 500 元", "住宿费限额为 800 元"]
    )
    assert len(conflicts) == 1
    attribute, unit, left, right = conflicts[0]
    assert attribute == "住宿费限额"
    assert unit == "元"
    assert {left, right} == {"500", "800"}


def test_different_attributes_are_not_a_conflict() -> None:
    """Run test_different_attributes_are_not_a_conflict."""
    assert (
        find_value_conflicts(["住宿费限额为 500 元", "交通费标准为 800 元"])
        == ()
    )


def test_same_value_twice_is_not_a_conflict() -> None:
    """Run test_same_value_twice_is_not_a_conflict."""
    assert (
        find_value_conflicts(["住宿费限额为 500 元", "住宿费限额为 500 元"])
        == ()
    )


def test_unit_difference_is_not_a_conflict() -> None:
    """同属性不同单位是两个不同的量（"30 天"与"30 个工作日"），不判冲突。."""
    assert (
        find_value_conflicts(["审批时限为 30 天", "审批时限为 30 个工作日"])
        == ()
    )


def test_chinese_and_arabic_values_are_compared_after_normalisation() -> None:
    """同一属性的中文数字与阿拉伯数字必须被判成冲突（归一后才可比）。."""
    conflicts = find_value_conflicts(
        ["住宿费限额为五百元", "住宿费限额为 800 元"]
    )
    assert conflicts and {conflicts[0][2], conflicts[0][3]} == {"500", "800"}


def test_different_years_are_not_a_value_conflict() -> None:
    """Run test_different_years_are_not_a_value_conflict."""
    claims = [
        "2026年上半年营业收入为9,099,020,774.81元。",
        "2025年上半年营业收入为5,464,235,057.15元。",
    ]
    assert find_value_conflicts(claims) == ()


def test_attributes_are_stripped_of_quantity_prefixes() -> None:
    """`每人每天500元` 与 `500元` 的属性名该归一成同一个。.

    规则是剥掉量词前缀。
    """
    items = extract_in_context("住宿费限额为每人每天 500 元")
    assert items and items[0].attribute == "住宿费限额"
