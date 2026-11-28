"""确定性计算工具。.

只接受结构化四则表达式，不从自然语言里猜公式。百分比符号仅表示输入/结果的
量纲，不参与数值换算；例如 ``45.46% - 37.90%`` 按 45.46 - 37.90 执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from api.resources import messages as msg
from api.resources.units import YEAR_UNIT
from api.verify.numbers import Quantity, extract_quantities


class CalculationError(ValueError):
    """表达式不安全、无法解析或无法计算。."""


@dataclass(frozen=True, slots=True)
class CalculationResult:
    """CalculationResult."""

    value: Decimal
    inputs: tuple[Decimal, ...]


_TOKEN = re.compile(r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<op>[+\-*/()]))")
_ALLOWED = re.compile(r"^[\d\s.+\-*/()]+$")


def _tokens(expression: str) -> list[str]:
    """Run _tokens."""
    text = (expression or "").replace("，", "").replace(",", "")
    text = (
        text.replace("×", "*")
        .replace("÷", "/")
        .replace("−", "-")
        .replace("％", "%")
    )
    if "%" in text:
        text = text.replace("%", "")
    if not text.strip() or not _ALLOWED.fullmatch(text):
        raise CalculationError(msg.CALC_BAD_CHARS)
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise CalculationError(
                msg.CALC_UNPARSABLE.format(expression=expression)
            )
        tokens.append(match.group("number") or match.group("op"))
        pos = match.end()
    return tokens


class _Parser:
    """_Parser."""

    def __init__(self, tokens: list[str]) -> None:
        """Initialize the instance."""
        self.tokens = tokens
        self.pos = 0
        self.inputs: list[Decimal] = []

    def parse(self) -> Decimal:
        """Run parse."""
        value = self._expr()
        if self.pos != len(self.tokens):
            raise CalculationError(msg.CALC_TRAILING)
        return value

    def _peek(self) -> str | None:
        """Run _peek."""
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        """Run _take."""
        token = self._peek()
        if token is None or (expected is not None and token != expected):
            raise CalculationError(
                msg.CALC_MISSING_TOKEN.format(expected=expected or msg.OPERAND)
            )
        self.pos += 1
        return token

    def _expr(self) -> Decimal:
        """Run _expr."""
        value = self._term()
        while self._peek() in ("+", "-"):
            op = self._take()
            right = self._term()
            value = value + right if op == "+" else value - right
        return value

    def _term(self) -> Decimal:
        """Run _term."""
        value = self._factor()
        while self._peek() in ("*", "/"):
            op = self._take()
            right = self._factor()
            if op == "*":
                value *= right
            else:
                if right == 0:
                    raise CalculationError(msg.CALC_DIV_ZERO)
                value /= right
        return value

    def _factor(self) -> Decimal:
        """Run _factor."""
        if self._peek() in ("+", "-"):
            op = self._take()
            value = self._factor()
            return value if op == "+" else -value
        if self._peek() == "(":
            self._take("(")
            value = self._expr()
            self._take(")")
            return value
        token = self._take()
        try:
            value = Decimal(token)
        except Exception as exc:  # noqa: BLE001 - token 已受限，仍把转换失败显式化
            raise CalculationError(
                msg.CALC_BAD_NUMBER.format(token=token)
            ) from exc
        self.inputs.append(value)
        return value


def execute_expression(expression: str) -> CalculationResult:
    """Run execute_expression."""
    parser = _Parser(_tokens(expression))
    value = parser.parse()
    return CalculationResult(value=value, inputs=tuple(parser.inputs))


def _result_quantity(result: str) -> Quantity:
    """Run _result_quantity."""
    items = extract_quantities(result)
    if len(items) != 1:
        raise CalculationError(msg.CALC_RESULT_SINGLE)
    return items[0]


def validate_calculation(
    expression: str, result: str, evidence_text: str
) -> tuple[Decimal, str] | None:
    """输入来自被引证据且重算一致时返回 (结果值, 规范单位)，否则返回 None。."""
    try:
        calculated = execute_expression(expression)
        target = _result_quantity(result)
    except CalculationError:
        return None

    evidence_values = {
        item.value
        for item in extract_quantities(evidence_text)
        if item.unit != "date"
        and not (item.unit == YEAR_UNIT and 1900 <= int(item.value) <= 2999)
    }
    percent_scale = "%" in (expression or "") or "％" in (expression or "")
    for value in calculated.inputs:
        if value in evidence_values:
            continue
        if percent_scale and value == Decimal(100):
            continue
        return None

    number = re.search(r"\d+(?:\.\d+)?", target.raw)
    decimals = (
        len(number.group(0).split(".", 1)[1])
        if number and "." in number.group(0)
        else 0
    )
    # 结果通常保留两位小数；按展示精度给半位容差。
    tolerance = Decimal(1).scaleb(-decimals) / Decimal(2)
    if abs(calculated.value - target.value) > tolerance:
        # 模型常把同比写成 ratio（0.6652），结果却写成百分数（66.52%）。
        # 只在 result 单位是 % 且计算值落在 0..1 时接受这种等价表达。
        if (
            target.unit == "%"
            and calculated.value >= 0
            and abs(calculated.value * Decimal(100) - target.value) <= tolerance
        ):
            return target.value, target.unit
        return None
    return target.value, target.unit


__all__ = [
    "CalculationError",
    "CalculationResult",
    "execute_expression",
    "validate_calculation",
]
