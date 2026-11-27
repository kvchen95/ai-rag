"""确定性计算工具。

只接受结构化四则表达式，不从自然语言里猜公式。百分比符号仅表示输入/结果的
量纲，不参与数值换算；例如 ``45.46% - 37.90%`` 按 45.46 - 37.90 执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from api.verify.numbers import Quantity, extract_quantities


class CalculationError(ValueError):
    """表达式不安全、无法解析或无法计算。"""


@dataclass(frozen=True, slots=True)
class CalculationResult:
    value: Decimal
    inputs: tuple[Decimal, ...]


_TOKEN = re.compile(r"\s*(?:(?P<number>\d+(?:\.\d+)?)|(?P<op>[+\-*/()]))")
_ALLOWED = re.compile(r"^[\d\s.+\-*/()]+$")


def _tokens(expression: str) -> list[str]:
    text = (expression or "").replace("，", "").replace(",", "")
    text = text.replace("×", "*").replace("÷", "/").replace("−", "-").replace("％", "%")
    if "%" in text:
        text = text.replace("%", "")
    if not text.strip() or not _ALLOWED.fullmatch(text):
        raise CalculationError("表达式只能包含数字、括号和四则运算符")
    tokens: list[str] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise CalculationError(f"无法解析表达式: {expression}")
        tokens.append(match.group("number") or match.group("op"))
        pos = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.pos = 0
        self.inputs: list[Decimal] = []

    def parse(self) -> Decimal:
        value = self._expr()
        if self.pos != len(self.tokens):
            raise CalculationError("表达式存在多余内容")
        return value

    def _peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self._peek()
        if token is None or (expected is not None and token != expected):
            raise CalculationError(f"表达式缺少 {expected or '操作数'}")
        self.pos += 1
        return token

    def _expr(self) -> Decimal:
        value = self._term()
        while self._peek() in ("+", "-"):
            op = self._take()
            right = self._term()
            value = value + right if op == "+" else value - right
        return value

    def _term(self) -> Decimal:
        value = self._factor()
        while self._peek() in ("*", "/"):
            op = self._take()
            right = self._factor()
            if op == "*":
                value *= right
            else:
                if right == 0:
                    raise CalculationError("除数不能为 0")
                value /= right
        return value

    def _factor(self) -> Decimal:
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
            raise CalculationError(f"无效数字: {token}") from exc
        self.inputs.append(value)
        return value


def execute_expression(expression: str) -> CalculationResult:
    parser = _Parser(_tokens(expression))
    value = parser.parse()
    return CalculationResult(value=value, inputs=tuple(parser.inputs))


def _result_quantity(result: str) -> Quantity:
    items = extract_quantities(result)
    if len(items) != 1:
        raise CalculationError("result 必须且只能包含一个数值")
    return items[0]


def validate_calculation(expression: str, result: str, evidence_text: str) -> tuple[Decimal, str] | None:
    """输入来自被引证据且重算一致时返回 (结果值, 规范单位)，否则返回 None。"""
    try:
        calculated = execute_expression(expression)
        target = _result_quantity(result)
    except CalculationError:
        return None

    evidence_values = {
        item.value
        for item in extract_quantities(evidence_text)
        if item.unit != "date" and not (item.unit == "年" and 1900 <= int(item.value) <= 2999)
    }
    percent_scale = "%" in (expression or "") or "％" in (expression or "")
    for value in calculated.inputs:
        if value in evidence_values:
            continue
        if percent_scale and value == Decimal(100):
            continue
        return None

    number = re.search(r"\d+(?:\.\d+)?", target.raw)
    decimals = len(number.group(0).split(".", 1)[1]) if number and "." in number.group(0) else 0
    # 结果通常保留两位小数；按展示精度给半位容差。
    tolerance = Decimal(1).scaleb(-decimals) / Decimal(2)
    if abs(calculated.value - target.value) > tolerance:
        # 模型常把同比写成 ratio（0.6652），结果却写成百分数（66.52%）。
        # 只在 result 单位是 % 且计算值落在 0..1 时接受这种等价表达。
        if target.unit == "%" and calculated.value >= 0 and abs(
            calculated.value * Decimal(100) - target.value
        ) <= tolerance:
            return target.value, target.unit
        return None
    return target.value, target.unit


__all__ = ["CalculationError", "CalculationResult", "execute_expression", "validate_calculation"]
