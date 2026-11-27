"""从表格切片提取时序事实。

只处理结构化 Markdown 表格，不做自然语言抽取。报告期来自文档元数据；
列名负责区分本期、上年同期、年初至报告期末。无法确定的数值一律跳过。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

_METRIC = re.compile(
    r"营业收入|营收|营业成本|净利润|归母净利润|扣非净利润|研发投入|研发费用|"
    r"经营活动产生的现金流量净额|总资产|所有者权益|毛利率|净利率|基本每股收益"
)
_CURRENT = ("本期数", "本期金额", "本报告期", "本报告期金额", "期末余额", "本期末")
_PRIOR = ("上年同期数", "上年同期金额", "上年同期", "上期数", "上期", "上年末")
_YTD = ("年初至报告期末", "年初至报告期末金额", "本年累计")
_HEADER_SUFFIX = re.compile(r"[（(](?:元|万元|亿元|%|％|人民币)[）)]$")
_SKIP = {"", "-", "—", "/", "不适用", "n/a", "na"}


def _clean_header(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip()


def _header_kind(header: str) -> str | None:
    text = _HEADER_SUFFIX.sub("", _clean_header(header))
    if text in _YTD:
        return "ytd"
    if text in _PRIOR:
        return "prior"
    if text in _CURRENT:
        return "current"
    return None


@dataclass(frozen=True, slots=True)
class TemporalFact:
    """一条带报告期和来源的事实；value 为原始单位下的数值。"""

    fact_id: str
    subject: str
    metric: str
    value: Decimal
    unit: str
    period_start: str
    period_end: str
    period_granularity: str
    source_chunk_id: str
    doc_key: str
    role: str = "reported"
    confidence: str = "explicit"
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "subject": self.subject,
            "metric": self.metric,
            "value": str(self.value),
            "unit": self.unit,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "period_granularity": self.period_granularity,
            "source_chunk_id": self.source_chunk_id,
            "doc_key": self.doc_key,
            "role": self.role,
            "confidence": self.confidence,
            "provenance": dict(self.provenance),
        }


def _date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _prior_period(start: str, end: str) -> tuple[str, str]:
    s = _date(start)
    e = _date(end)
    return s.replace(year=s.year - 1).isoformat(), e.replace(year=e.year - 1).isoformat()


def _number(text: str) -> Decimal | None:
    raw = (text or "").strip().replace(",", "").replace("，", "")
    if raw.lower() in _SKIP:
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()")
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return -value if negative else value


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _header_kind(header: str) -> str | None:
    text = (header or "").strip()
    if any(marker in text for marker in ("变化幅度", "增减变动", "同比增速")):
        return None
    if "年初至报告期末" in text or "累计" in text:
        return "ytd"
    if any(marker in text for marker in _PRIOR):
        return "prior"
    if any(marker in text for marker in _CURRENT):
        return "current"
    return None


def _period_for(kind: str, start: str, end: str) -> tuple[str, str]:
    if kind == "prior":
        return _prior_period(start, end)
    if kind == "ytd":
        e = _date(end)
        return date(e.year, 1, 1).isoformat(), end
    return start, end


def _fact_id(chunk_id: str, metric: str, start: str, end: str) -> str:
    raw = f"{chunk_id}|{metric}|{start}|{end}".encode("utf-8")
    return "tf_" + hashlib.sha1(raw).hexdigest()[:16]


def extract_table_facts(
    *,
    chunk_id: str,
    doc_key: str,
    subject: str,
    report_period_start: str,
    report_period_end: str,
    table_md: str,
    unit: str = "",
    period_granularity: str = "range",
) -> list[TemporalFact]:
    """从 Markdown 表格提取收入/利润/研发投入等报告事实。

    只提取行首指标明确、列名可映射到期间的数值；变化幅度列不生成事实，
    避免把“同比增速”当成与收入同单位的值。
    """
    lines = [line for line in (table_md or "").splitlines() if line.strip().startswith("|")]
    if len(lines) < 2:
        return []
    header = _cells(lines[0])
    kinds = [_header_kind(cell) for cell in header]
    if "current" not in kinds:
        return []

    facts: list[TemporalFact] = []
    for line in lines[2:]:
        cells = _cells(line)
        if not cells or len(cells) < 2:
            continue
        metric = re.sub(r"\s+", "", cells[0])
        if not _METRIC.search(metric):
            continue
        for index, kind in enumerate(kinds):
            if kind not in ("current", "prior", "ytd") or index >= len(cells):
                continue
            value = _number(cells[index])
            if value is None:
                continue
            start, end = _period_for(kind, report_period_start, report_period_end)
            facts.append(
                TemporalFact(
                    fact_id=_fact_id(chunk_id, metric, start, end),
                    subject=subject,
                    metric=metric,
                    value=value,
                    unit=unit,
                    period_start=start,
                    period_end=end,
                    period_granularity=period_granularity,
                    source_chunk_id=chunk_id,
                    doc_key=doc_key,
                    provenance={"column": header[index], "kind": kind, "row": metric},
                )
            )
    return facts


__all__ = ["TemporalFact", "extract_table_facts"]
