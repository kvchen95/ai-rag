"""数字与专名的抽取、归一与定位（详细设计.md §10.1 第三道、§10.2）。

这一层是"数字对齐"的全部实现。它必须是**纯规则**，不能交给模型：交给模型的后果是
校验器与被校验者是同一个东西，它说"对上了"没有任何信息量。

§10.2 给了三步归一和四个反例，这里逐条落地：

| 证据写法 | 草稿写法 | 判定 | 本条为什么这么写 |
|---|---|---|---|
| 5,000,000 元 | 五百万元 | 对上 | 中文数字转阿拉伯 + 万元折算成元 |
| 30 个工作日 | 30 天 | **不对** | 单位不同就是不同的数，不做"天≈工作日"的善意换算 |
| 80% | 八成 | 对上 | 「成」按 ×10 折成百分比 |
| 2025-03-01 | 2025 年 3 月 1 日 | 对上 | 两种日期写法归一到同一个 YYYYMMDD |

三条刻意的取舍：

1. **不抽取"没有单位的阿拉伯数字"以外的任何自由文本**。中文数字只在后面紧跟一个已知
   单位时才认（"三月""十五五"这类不算数），否则 `这一` `第一` 里的字会被当成数字，
   校验会因为一个虚词全红。
2. **日期先摘掉再抽数字**。`2025-03-01` 若先按普通数字抽，会得到 2025 / 03 / 01 三个
   裸数字，而证据里的写法是 `2025 年 3 月 1 日`——抽出来的是三个不同的数，比对必然
   失败，于是每条带日期的答案都被拒。摘掉整段日期后单独比 YYYYMMDD 才是可比的。
3. **区间（`500-600 元`）里两个数都带单位**。只给最后一个数挂单位会让 `500` 变成
   无单位数字，与证据里的 `500 元` 对不上，而它们其实是同一个数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

# 单位归一表：写法 → (规范单位, 折算系数)。
#
# 折算只做"量纲内换算"（万元→元、亿→元、成→%），**不做跨量纲的等价**
# （工作日≠天、月≠30 天）。§10.2 的反例点名了这条。
UNIT_TABLE: dict[str, tuple[str, Decimal]] = {
    "万元": ("元", Decimal(10000)),
    "亿元": ("元", Decimal(100000000)),
    "千元": ("元", Decimal(1000)),
    "元": ("元", Decimal(1)),
    "块": ("元", Decimal(1)),
    "个工作日": ("工作日", Decimal(1)),
    "工作日": ("工作日", Decimal(1)),
    "天": ("天", Decimal(1)),
    "日": ("天", Decimal(1)),
    "个月": ("月", Decimal(1)),
    "月": ("月", Decimal(1)),
    "年": ("年", Decimal(1)),
    "小时": ("小时", Decimal(1)),
    "分钟": ("分钟", Decimal(1)),
    "星期": ("周", Decimal(1)),
    "周": ("周", Decimal(1)),
    "次": ("次", Decimal(1)),
    "人": ("人", Decimal(1)),
    "份": ("份", Decimal(1)),
    "条": ("条", Decimal(1)),
    "页": ("页", Decimal(1)),
    "项": ("项", Decimal(1)),
    "倍": ("倍", Decimal(1)),
    "%": ("%", Decimal(1)),
    "％": ("%", Decimal(1)),
    "成": ("%", Decimal(10)),
}

# 长的先匹配：「个工作日」必须在「日」之前，否则会切出一个「个」+「工作日」的怪组合。
_UNITS = "|".join(sorted((re.escape(u) for u in UNIT_TABLE), key=len, reverse=True))

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "两": 2,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_BIG = {"万": 10000, "亿": 100000000}

# 日期：先摘掉，避免拆成三个裸数字。
_DATE_PATTERNS = (
    re.compile(r"(?P<y>\d{4})\s*[-/年]\s*(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*[日号]?"),
    re.compile(r"(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月"),
    re.compile(r"(?P<y>\d{4})-(?P<m>\d{1,2})(?![\d-])"),
)

# 阿拉伯数字 + 可选单位。千分位逗号与全角数字都在这里归一。
_ARABIC = re.compile(
    r"(?P<num>\d[\d,，]*(?:\.\d+)?)\s*(?P<unit>" + _UNITS + r")?"
)
# 范围分隔符：`500-600 元` / `500 至 600 元`。第二个数的单位要回填给第一个。
_RANGE_TAIL = re.compile(r"\s*(?:-|~|～|—|–|至|到)\s*\d[\d,，]*(?:\.\d+)?\s*(?P<unit>" + _UNITS + r")")
# 中文数字 + 必带单位（不带单位的中文数字一律不认，见模块头第 1 条）。
#
# 前置守卫 `(?<![第这那每某几哪上前后])`：`第一步` `这一条` `那两天` 里的字是
# **序数/指代**，不是量。不挡它们，`这一条` 会被抽成 `1 条`，然后第三道校验拿着
# 一个从来不存在的"1 条"去证据里找，每条答案都判不过——而错的地方在这里。
_CHINESE = re.compile(
    r"(?<![第这那每某几哪上前后])(?P<cn>[零〇一两二三四五六七八九十百千万亿]+)\s*(?P<unit>"
    + _UNITS
    + r")"
)
# 百分之X / 千分之X
_CHINESE_PERCENT = re.compile(
    r"(?P<kind>百分之|千分之)\s*(?P<cn>[零〇一两二三四五六七八九十百千万亿]+|\d[\d.]*)"
)

# 专名：条款号 / 编号 / 机构与系统名。
#
# 条款号与编码是**机械可判**的（格式固定），所以整段照抄去比对。
# 机构名与系统名做不到——中文没有分词边界，`住宿费由财务部承担` 里"最左匹配"
# 会切出 `住宿费由财务部`，于是证据里写着"由公司财务部承担"时整条判失败。
# 所以机构名走"后缀锚定 + 向前回退到虚词为止"：先定位后缀（部/中心/系统…），
# 再往前吃中文字符，撞到虚词或满 4 字就停。`由财务部` → `财务部`。
_PROPER_EXACT = (
    re.compile(r"第[零〇一两二三四五六七八九十百\d]{1,4}[条章节款项]"),
    re.compile(r"[A-Z]{2,}[/-]?[A-Z]?\s?\d{2,}(?:-\d+)?"),
)
_ORG_SUFFIX = re.compile(
    r"(?:事业部|委员会|办公室|部门|中心|公司|集团|系统|平台|模块|部|处|科|组)"
)
# 虚词/功能字：机构名向前回退时遇到它就停（它不是名字的一部分）。
_FUNCTION_CHARS = set(
    "由和与及的在对按经交同或等为是不均应可须需上下内外后前也都还就要把被从向这那其该本各每"
)


@dataclass(frozen=True, slots=True)
class Quantity:
    """一个可比的量：数值 + 规范单位。

    单位是**规范后**的写法（元 / 工作日 / 天 / % / date / 空串），不是原文写法：
    `万元` 与 `元` 归到同一个单位后，"五百万"与"5,000,000 元"才可比。
    """

    value: Decimal
    unit: str
    raw: str

    def as_key(self) -> tuple[Decimal, str]:
        return (_normalise_decimal(self.value), self.unit)

    def __str__(self) -> str:  # pragma: no cover - 只用于报错信息
        return f"{self.value}{self.unit or ''}"


def _normalise_decimal(value: Decimal) -> Decimal:
    """去掉数值的尾随零，`500` 与 `500.00` 必须是同一个量。"""
    normalised = value.normalize()
    # Decimal('5E+2') 与 Decimal('500') 相等但 repr 不同，量化到 0 位消除科学计数法。
    return normalised.quantize(Decimal(1)) if normalised == normalised.to_integral_value() else normalised


def parse_chinese_number(text: str) -> Decimal | None:
    """解析中文数字（五百万 / 八十 / 十二 / 三）。解析不了返回 None。

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
    """抽出日期，并把原文里这些位置抹成空格（避免被当裸数字二次抽取）。"""
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
            masked = masked[: match.start()] + " " * (match.end() - match.start()) + masked[match.end() :]
    return found, masked


def extract_quantities(text: str) -> tuple[Quantity, ...]:
    """抽取一段文本里的全部可比量（按出现顺序去重）。

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
        value = parse_chinese_number(raw) if not raw[0].isdigit() else Decimal(raw)
        if value is None:
            continue
        if match.group("kind") == "千分之":
            value = value / Decimal(1000) * Decimal(100)
        unit_factor = UNIT_TABLE["%"]
        out.append(
            Quantity(value=value * unit_factor[1], unit=unit_factor[0], raw=match.group(0))
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
        out.append(Quantity(value=value * factor, unit=unit, raw=match.group(0)))
    masked = _CHINESE.sub(lambda m: " " * len(m.group(0)), masked)

    for match in _ARABIC.finditer(masked):
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
        out.append(Quantity(value=value * factor, unit=unit, raw=match.group(0)))

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
    """返回 (claim 里定位不到的, claim 里的全部)。空的第一项 = 全部定位成功。

    比对口径：**数值相等且规范单位一致**（§10.2 原话"要求数值相等且单位一致"）。
    """
    claim_items = extract_quantities(claim)
    if not claim_items:
        return (), ()
    known = {item.as_key() for item in extract_quantities(evidence_text)}
    missing = tuple(item for item in claim_items if item.as_key() not in known)
    return missing, claim_items


def extract_proper_nouns(text: str) -> tuple[str, ...]:
    """抽取专名（§10.1 第三道："专名包括部门名、系统名、条款号"）。

    只收三类：条款号、编码、机构/系统名。理由是不在这三类里的"专名"与普通名词
    无法机械区分，收进来只会让每条答案都判"定位失败"。
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
    for match in _ORG_SUFFIX.finditer(text):
        start = match.start()
        while start > 0 and len(match.group(0)) + (match.start() - start) < 8:
            char = text[start - 1]
            if not ("\u4e00" <= char <= "\u9fa5") or char in _FUNCTION_CHARS:
                break
            start -= 1
        item = text[start : match.end()].strip()
        # 两档长度下限：
        # - 单字后缀（部/处/科/组）要求前面至少两个字，"全部""本部""各处"这类
        #   普通词才不会被当成机构名；
        # - 其余后缀（中心/公司/系统…）两字即可。
        minimum = 3 if len(match.group(0)) == 1 else 2
        if len(item) < minimum or item in seen:
            continue
        seen.add(item)
        found.append(item)
    return tuple(found)


def locate_proper_nouns(
    claim: str, evidence_text: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """专名按**原文子串**定位：专名不是数，改写一个字就是另一个实体。"""
    items = extract_proper_nouns(claim)
    if not items:
        return (), ()
    missing = tuple(item for item in items if item not in evidence_text)
    return missing, items


# --- 第五道：同属性同单位不得出现两个值 ---------------------------------------
#
# "同一件事"的可操作口径：数字前面紧邻的那个中文短语（"住宿费限额为 **500** 元"里的
# 「住宿费限额」）。取前缀短语而不是让模型给实体，是因为这一道必须能用规则做
# （§10.1 第五道原话"不必再调一次模型"）。
#
# 抽取不到属性名（数字前面没有中文，如"共 500 元"）时不参与冲突判定：判它
# 和谁都冲突会让"总数 500 元 + 明细 500 元"这种正常表述被拒答。
_ATTR = re.compile(r"([\u4e00-\u9fa5]{2,12})\s*(?:为|是|按|不超过|最多|最少|不低于|达|约|：|:)?\s*$")
# 属性名尾部可能是连接词（"住宿费限额为" 里的「为」被贪婪的字符类吃进去了），
# 剥掉才好与另一个句子里的「住宿费限额」对上。
_ATTR_TRAILING = ("为", "是", "按", "达", "约", "的", "了", "和", "与", "及")
_ATTR_STRIP_PREFIX = (
    "每人每天",
    "每人每月",
    "每人每年",
    "每人",
    "每天",
    "每月",
    "每年",
    "每日",
    "单个",
    "其中",
    "合计",
    "共计",
    "总计",
)


@dataclass(frozen=True, slots=True)
class QuantityInContext:
    """带属性名的量。属性名是"这件事"的标识，单位是"同一量纲"的标识。"""

    attribute: str
    quantity: Quantity

    def as_key(self) -> tuple[str, str]:
        return (self.attribute, self.quantity.unit)


# 属性名前后的噪声。尾部要反复剥：`住宿费限额为每人每天` 里既有连接词又有量词前缀，
# 剥一次只解决一个。
def _clean_attribute(attribute: str) -> str:
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
    """抽出带属性上下文的量（第五道用）。

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
            attribute = _clean_attribute(attr_match.group(1) if attr_match else "")
            out.append(QuantityInContext(attribute=attribute, quantity=quantities[0]))
    return tuple(out)


def find_value_conflicts(
    claims: "list[str] | tuple[str, ...]",
) -> tuple[tuple[str, str, str, str], ...]:
    """找出"同一属性同一单位出现两个不同值"的冲突。

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
                    (item.attribute, item.quantity.unit or "（无单位）", str(previous[0]), str(value))
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
