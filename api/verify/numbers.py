"""数字与专名的抽取、归一与定位（详细设计.md §10.1 第三道、§10.2）。.

这一层是"数字对齐"的全部实现。它必须是**纯规则**，不能交给模型：交给模型的后果是
校验器与被校验者是同一个东西，它说"对上了"没有任何信息量。

§10.2 给了三步归一和四个反例，这里逐条落地（证据写法 / 草稿写法 → 判定）：

- `5,000,000 元` / `五百万元` → 对上。中文数字转阿拉伯 + 万元折算成元。
- `30 个工作日` / `30 天` → **不对**。单位不同就是不同的数，不做
  "天≈工作日"的善意换算。
- `80%` / `八成` → 对上。「成」按 ×10 折成百分比。
- `2025-03-01` / `2025 年 3 月 1 日` → 对上。两种日期写法归一到同一个
  YYYYMMDD。

三条刻意的取舍：

1. **不抽取"没有单位的阿拉伯数字"以外的任何自由文本**。中文数字只在后面紧跟
   一个已知单位时才认（"三月""十五五"这类不算数），否则 `这一` `第一` 里的字
   会被当成数字，校验会因为一个虚词全红。
2. **日期先摘掉再抽数字**。`2025-03-01` 若先按普通数字抽，会得到 2025 / 03 /
   01 三个裸数字，而证据里的写法是 `2025 年 3 月 1 日`——抽出来的是三个不同的
   数，比对必然失败，于是每条带日期的答案都被拒。摘掉整段日期后单独比
   YYYYMMDD 才是可比的。
3. **区间（`500-600 元`）里两个数都带单位**。只给最后一个数挂单位会让 `500` 变成
   无单位数字，与证据里的 `500 元` 对不上，而它们其实是同一个数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from api.resources.units import (
    ARABIC as _ARABIC,
    ATTR as _ATTR,
    ATTR_STRIP_PREFIX as _ATTR_STRIP_PREFIX,
    ATTR_TRAILING as _ATTR_TRAILING,
    CHINESE as _CHINESE,
    CHINESE_PERCENT as _CHINESE_PERCENT,
    CN_BIG as _CN_BIG,
    CN_DIGITS as _CN_DIGITS,
    CN_UNITS as _CN_UNITS,
    DATE_PATTERNS as _DATE_PATTERNS,
    NO_UNIT_LABEL,
    ORDINAL_PREFIX,
    PER_MILLE,
    PROPER_EXACT as _PROPER_EXACT,
    RANGE_TAIL as _RANGE_TAIL,
    UNIT_TABLE,
    YEAR_UNIT,
)


@dataclass(frozen=True, slots=True)
class Quantity:
    """一个可比的量：数值 + 规范单位。.

    单位是**规范后**的写法（元 / 工作日 / 天 / % / date / 空串），不是原文写法：
    `万元` 与 `元` 归到同一个单位后，"五百万"与"5,000,000 元"才可比。
    """

    value: Decimal
    unit: str
    raw: str

    def as_key(self) -> tuple[Decimal, str]:
        """Run as_key."""
        return (_normalise_decimal(self.value), self.unit)

    def __str__(self) -> str:  # pragma: no cover - 只用于报错信息
        """Return a human-readable representation."""
        return f"{self.value}{self.unit or ''}"


def _normalise_decimal(value: Decimal) -> Decimal:
    """去掉数值的尾随零，`500` 与 `500.00` 必须是同一个量。."""
    normalised = value.normalize()
    # Decimal('5E+2') 与 Decimal('500') 相等但 repr 不同，
    # 量化到 0 位消除科学计数法。
    return (
        normalised.quantize(Decimal(1))
        if normalised == normalised.to_integral_value()
        else normalised
    )


def parse_chinese_number(text: str) -> Decimal | None:
    """解析中文数字（五百万 / 八十 / 十二 / 三）。解析不了返回 None。.

    只覆盖到"亿"，够用于制度里的金额、期限、比例；再往上（万亿）在制度文本里不会出现，
    硬扩只会让解析器更难被证明是对的。
    """
    if not text:
        return None
    total = Decimal(0)
    section = Decimal(0)
    digit: Decimal | None = None
    for char in text:
        if char in _CN_DIGITS:
            digit = Decimal(_CN_DIGITS[char])
            continue
        if char in _CN_UNITS:
            if digit is None:
                digit = Decimal(1)  # 「十二」的「十」前面没有数字，按 1 处理
            section += digit * _CN_UNITS[char]
            digit = None
            continue
        if char in _CN_BIG:
            if digit is not None:
                section += digit
                digit = None
            if section == 0:
                section = Decimal(1)
            total += section * _CN_BIG[char]
            section = Decimal(0)
            continue
        return None
    if digit is not None:
        section += digit
    return total + section


def _date_quantities(text: str) -> tuple[list[Quantity], str]:
    """抽出日期，并把原文里这些位置抹成空格（避免被当裸数字二次抽取）。."""
    found: list[Quantity] = []
    masked = text
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(masked):
            year = int(match.group("y"))
            month = int(match.group("m"))
            day_group = match.groupdict().get("d")
            day = int(day_group) if day_group else 0
            if not (1 <= month <= 12 and 0 <= day <= 31):
                # 不是日期（`1900-13` 这类），留着按普通数字处理。
                continue
            value = Decimal(year * 10000 + month * 100 + day)
            found.append(Quantity(value=value, unit="date", raw=match.group(0)))
            masked = (
                masked[: match.start()]
                + " " * (match.end() - match.start())
                + masked[match.end() :]
            )
    return found, masked


def extract_quantities(text: str) -> tuple[Quantity, ...]:
    """抽取一段文本里的全部可比量（按出现顺序去重）。.

    去重是必要的：证据里一句话出现两次同一个数（子片重叠处）会让"集合比对"变成
    多重集比对，而 §10.2 要的是"能不能定位到这个值"，不是出现次数。
    """
    if not text:
        return ()
    dates, masked = _date_quantities(text)
    out: list[Quantity] = list(dates)

    # 百分之八十 / 千分之五
    for match in _CHINESE_PERCENT.finditer(masked):
        raw = match.group("cn")
        value = (
            parse_chinese_number(raw) if not raw[0].isdigit() else Decimal(raw)
        )
        if value is None:
            continue
        if match.group("kind") == PER_MILLE:
            value = value / Decimal(1000) * Decimal(100)
        unit_factor = UNIT_TABLE["%"]
        out.append(
            Quantity(
                value=value * unit_factor[1],
                unit=unit_factor[0],
                raw=match.group(0),
            )
        )
    masked = _CHINESE_PERCENT.sub(lambda m: " " * len(m.group(0)), masked)

    for match in _CHINESE.finditer(masked):
        value = parse_chinese_number(match.group("cn"))
        if value is None:
            continue
        spec = UNIT_TABLE.get(match.group("unit"))
        if spec is None:
            continue
        unit, factor = spec
        out.append(
            Quantity(value=value * factor, unit=unit, raw=match.group(0))
        )
    masked = _CHINESE.sub(lambda m: " " * len(m.group(0)), masked)

    for match in _ARABIC.finditer(masked):
        # “第 66 页”“第 8 页”是引用定位，不是需要核对的数值。
        if match.start() > 0 and masked[match.start() - 1] == ORDINAL_PREFIX:
            continue
        raw_num = match.group("num").replace(",", "").replace("，", "")
        try:
            value = Decimal(raw_num)
        except Exception:  # noqa: BLE001 - 形如 `1,2,3` 的脏串按解析失败跳过
            continue
        unit_text = match.group("unit")
        if unit_text is None:
            # 范围写法：`500-600 元` 里第一个数的单位在第二个数后面。
            tail = _RANGE_TAIL.match(masked, match.end())
            if tail:
                unit_text = tail.group("unit")
        if unit_text is None:
            out.append(Quantity(value=value, unit="", raw=match.group(0)))
            continue
        spec = UNIT_TABLE.get(unit_text)
        if spec is None:  # pragma: no cover - 正则与表同源，走不到
            out.append(Quantity(value=value, unit="", raw=match.group(0)))
            continue
        unit, factor = spec
        out.append(
            Quantity(value=value * factor, unit=unit, raw=match.group(0))
        )

    seen: set[tuple[Decimal, str]] = set()
    unique: list[Quantity] = []
    for item in out:
        key = item.as_key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return tuple(unique)


def locate_quantities(
    claim: str, evidence_text: str
) -> tuple[tuple[Quantity, ...], tuple[Quantity, ...]]:
    """返回 (claim 里定位不到的, claim 里的全部)。空的第一项 = 全部定位成功。.

    比对口径：**数值相等且规范单位一致**（§10.2 原话"要求数值相等且单位一致"）。
    表格切片的 `content_text` 常只保留单元格、不带“单位：元”表头；
    因此证据侧数值完全一致且无单位时，允许按 claim 的单位解释，
    避免把表头单位误判为无证据。

    年份属于查询/报告期范围，不由这道数值校验处理；它应由时间过滤与判别器校验。
    """
    claim_items = extract_quantities(claim)
    if not claim_items:
        return (), ()
    evidence_items = extract_quantities(evidence_text)
    known = {item.as_key() for item in evidence_items}
    missing = []
    for item in claim_items:
        if item.unit == YEAR_UNIT and 1900 <= int(item.value) <= 2999:
            continue
        if item.as_key() in known:
            continue
        if any(
            value.value == item.value and value.unit == ""
            for value in evidence_items
        ):
            # 表格 content_text 往往只保留单元格，不带“单位：元”表头；
            # 数值完全一致且证据侧无单位时，允许按 claim 的单位解释。
            continue
        missing.append(item)
    return tuple(missing), claim_items


def extract_proper_nouns(text: str) -> tuple[str, ...]:
    """抽取格式固定的专名：条款号与编码。.

    机构名/公司名/部门名交给 judge 校验，避免中文分词启发式误判。
    """
    if not text:
        return ()
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _PROPER_EXACT:
        for match in pattern.finditer(text):
            item = match.group(0).strip()
            if item and item not in seen:
                seen.add(item)
                found.append(item)
    return tuple(found)


def locate_proper_nouns(
    claim: str, evidence_text: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """固定格式专名按原文子串定位。."""
    items = extract_proper_nouns(claim)
    if not items:
        return (), ()
    missing = tuple(item for item in items if item not in evidence_text)
    return missing, items


# --- 第五道：同属性同单位不得出现两个值 ---------------------------------------
#
# "同一件事"的可操作口径：数字前面紧邻的那个中文短语（"住宿费限额为 **500**
# 元"里的「住宿费限额」）。取前缀短语而不是让模型给实体，是因为这一道必须能用规
# 则做（§10.1 第五道原话"不必再调一次模型"）。
#
# 抽取不到属性名（数字前面没有中文，如"共 500 元"）时不参与冲突判定：判它
# 和谁都冲突会让"总数 500 元 + 明细 500 元"这种正常表述被拒答。
# 正则与剥词表见 `api.resources.units`（`ATTR` / `ATTR_TRAILING` /
# `ATTR_STRIP_PREFIX`）。


@dataclass(frozen=True, slots=True)
class QuantityInContext:
    """带属性名的量。属性名是"这件事"的标识，单位是"同一量纲"的标识。."""

    attribute: str
    quantity: Quantity

    def as_key(self) -> tuple[str, str]:
        """Run as_key."""
        return (self.attribute, self.quantity.unit)


# 属性名前后的噪声。尾部要反复剥：`住宿费限额为每人每天` 里既有连接词又有量词前
# 缀，剥一次只解决一个。
def _clean_attribute(attribute: str) -> str:
    """Run _clean_attribute."""
    changed = True
    while changed and attribute:
        changed = False
        for suffix in _ATTR_STRIP_PREFIX:
            if attribute.endswith(suffix) and len(attribute) > len(suffix):
                attribute = attribute[: -len(suffix)]
                changed = True
        while attribute and attribute[-1] in _ATTR_TRAILING:
            attribute = attribute[:-1]
            changed = True
        for prefix in _ATTR_STRIP_PREFIX:
            if attribute.startswith(prefix) and len(attribute) > len(prefix):
                attribute = attribute[len(prefix) :]
                changed = True
    return attribute


def extract_in_context(text: str) -> tuple[QuantityInContext, ...]:
    """抽出带属性上下文的量（第五道用）。.

    量本身的识别**复用 `extract_quantities` 的正则**（`_ARABIC` / `_CHINESE` /
    `_CHINESE_PERCENT`）：第五道与第三道的口径必须一致，两套正则迟早对不上，
    而那种不一致的表现是"第三道说这个数有据、第五道说它和另一个冲突"。
    """
    if not text:
        return ()
    out: list[QuantityInContext] = []
    spans: list[tuple[int, int]] = []
    for pattern in (_CHINESE_PERCENT, _CHINESE, _ARABIC):
        for match in pattern.finditer(text):
            if any(start <= match.start() < end for start, end in spans):
                continue
            spans.append((match.start(), match.end()))
            # 从这一处量开始往后取一小段窗口做归一：单位紧跟在数后面，
            # 而 `extract_quantities` 要整段才能拿到单位与范围写法。
            window = text[match.start() : match.start() + 32]
            quantities = extract_quantities(window)
            if not quantities:
                continue
            head = text[max(0, match.start() - 16) : match.start()]
            attr_match = _ATTR.search(head)
            attribute = _clean_attribute(
                attr_match.group(1) if attr_match else ""
            )
            # 年份属于指标上下文的一部分：2026 年营业收入与 2025 年营业收入不是
            # “同一属性两个值”，而是两个不同期间的合法数值。_ATTR 只吃中文，
            # 会把年份吃掉，所以这里把属性名前的四位年份补回冲突键。
            if attr_match:
                year_match = re.search(
                    r"(?:19|20)\d{2}", head[: attr_match.start()]
                )
                if year_match:
                    attribute = f"{year_match.group(0)}{attribute}"
            out.append(
                QuantityInContext(attribute=attribute, quantity=quantities[0])
            )
    return tuple(out)


def find_value_conflicts(
    claims: list[str] | tuple[str, ...],
) -> tuple[tuple[str, str, str, str], ...]:
    """找出"同一属性同一单位出现两个不同值"的冲突。.

    返回 (属性, 单位, 值A, 值B) 四元组；空 tuple = 无冲突。
    """
    seen: dict[tuple[str, str], tuple[Decimal, str]] = {}
    conflicts: list[tuple[str, str, str, str]] = []
    for claim in claims:
        for item in extract_in_context(claim):
            if not item.attribute:
                continue
            key = item.as_key()
            value = _normalise_decimal(item.quantity.value)
            previous = seen.get(key)
            if previous is None:
                seen[key] = (value, claim)
                continue
            if previous[0] != value:
                conflicts.append(
                    (
                        item.attribute,
                        item.quantity.unit or NO_UNIT_LABEL,
                        str(previous[0]),
                        str(value),
                    )
                )
    return tuple(conflicts)


__all__ = [
    "UNIT_TABLE",
    "Quantity",
    "QuantityInContext",
    "extract_in_context",
    "extract_proper_nouns",
    "extract_quantities",
    "find_value_conflicts",
    "locate_proper_nouns",
    "locate_quantities",
    "parse_chinese_number",
]
