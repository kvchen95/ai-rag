"""确定性计算工具：只执行结构化算式，不解析自然语言里的算术。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from api.generate.calculator import (
    CalculationError,
    execute_expression,
    validate_calculation,
)


def test_percentage_difference_is_executed_exactly() -> None:
    calculated = execute_expression("45.46% - 37.90%")
    assert calculated.value == Decimal("7.56")
    assert calculated.inputs == (Decimal("45.46"), Decimal("37.90"))


def test_parentheses_and_percentage_ratio_are_supported() -> None:
    calculated = execute_expression("(100 - 80) / 100 * 100%")
    assert calculated.value == Decimal("20")


def test_calculation_result_can_be_rounded() -> None:
    result = validate_calculation(
        "4,472,044,952.74 / 2,846,508,812.66",
        "约 1.57 倍",
        "研发投入 4,472,044,952.74 元；归母净利润 2,846,508,812.66 元。",
    )
    assert result is not None
    assert result == (Decimal("1.57"), "倍")


def test_ratio_expression_can_report_percentage() -> None:
    result = validate_calculation(
        "(9099020774.81 - 5464235057.15) / 5464235057.15",
        "≈66.52%",
        "2026年半年度: 9,099,020,774.81；2025年半年度: 5,464,235,057.15",
    )
    assert result == (Decimal("66.52"), "%")


def test_calculation_rejects_wrong_result() -> None:
    assert (
        validate_calculation(
            "45.46% - 37.90%", "7.65 个百分点", "45.46%；37.90%"
        )
        is None
    )


def test_calculation_rejects_input_missing_from_evidence() -> None:
    assert (
        validate_calculation("45.46% - 37.90%", "7.56 个百分点", "45.46%")
        is None
    )


@pytest.mark.parametrize(
    "expression", ["__import__('os')", "45 ** 2", "1 / 0", "abc + 1"]
)
def test_calculation_rejects_unsafe_or_invalid_expression(
    expression: str,
) -> None:
    with pytest.raises(CalculationError):
        execute_expression(expression)
