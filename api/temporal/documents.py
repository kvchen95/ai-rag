"""结构化文档选择：company + report_type + report_period metadata 硬过滤。.

只使用调用方显式传入或文档 metadata 中的结构化字段；不再从问句关键词
推断报告类型/预测年份。硬过滤无命中时返回空元组，不回退全库。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _date_int(value: Any) -> int | None:
    """Run _date_int."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("-", "")
    try:
        return int(text)
    except ValueError:
        return None


def _normalize_company_name(value: Any) -> str:
    """Run _normalize_company_name."""
    return str(value or "").strip()


def _extract_company(
    question: str, documents: Iterable[dict[str, Any]]
) -> str | None:
    """Run _extract_company."""
    companies = sorted(
        {
            _normalize_company_name(doc.get("company"))
            for doc in documents
            if doc.get("company")
        },
        key=len,
        reverse=True,
    )
    for company in companies:
        if company and company in (question or ""):
            return company
    return None


def _period_overlaps(
    doc: dict[str, Any], date_from: int | None, date_to: int | None
) -> bool:
    """Run _period_overlaps."""
    if date_from is None or date_to is None:
        return False
    start = _date_int(doc.get("report_period_start"))
    end = _date_int(doc.get("report_period_end"))
    if start is None or end is None:
        # 缺失 metadata 时不能证明报告期匹配；按 fail-closed 处理。
        return False
    return not (end < date_from or start > date_to)


def _hard_filter(
    question: str,
    documents: list[dict[str, Any]],
    *,
    company: str | None = None,
    report_type: str | None = None,
    date_from: int | None = None,
    date_to: int | None = None,
) -> list[dict[str, Any]]:
    """Run _hard_filter."""
    selected = list(documents)
    resolved_company = company or _extract_company(question, selected)
    if resolved_company:
        selected = [
            doc
            for doc in selected
            if _normalize_company_name(doc.get("company")) == resolved_company
        ]

    if report_type:
        selected = [
            doc
            for doc in selected
            if str(doc.get("report_type", "")) == report_type
        ]

    # 报告期硬过滤：只按 document metadata，不用问句里的预测年份。
    if date_from is not None and date_to is not None:
        selected = [
            doc for doc in selected if _period_overlaps(doc, date_from, date_to)
        ]

    return selected


def select_documents(
    question: str,
    documents: Iterable[dict[str, Any]] | None = None,
    *,
    pg: Any = None,
    time_range: Any = None,
    date_from: int | None = None,
    date_to: int | None = None,
    company: str | None = None,
    report_type: str | None = None,
    client: Any = None,
    llm: Any = None,
    **kwargs: Any,
) -> tuple[str, ...]:
    """按 metadata 硬过滤文档；候选过多时返回全部精确匹配，不做 LLM 消歧。."""
    if documents is None:
        if pg is None or not hasattr(pg, "fetch_all"):
            raise ValueError(
                "select_documents requires documents or a pg with fetch_all"
            )
        documents = pg.fetch_all(
            "SELECT doc_key, company, report_type, report_period_start, "
            "report_period_end, filename FROM documents"
        )

    docs = [dict(doc) for doc in (documents or [])]
    if time_range is not None:
        if date_from is None:
            date_from = getattr(time_range, "date_from", None)
        if date_to is None:
            date_to = getattr(time_range, "date_to", None)
    if date_from is None and isinstance(kwargs.get("report_period"), tuple):
        date_from, date_to = kwargs["report_period"]

    candidates = _hard_filter(
        question,
        docs,
        company=company,
        report_type=report_type,
        date_from=date_from,
        date_to=date_to,
    )
    if not candidates:
        return ()
    return tuple(
        str(doc["doc_key"]) for doc in candidates if doc.get("doc_key")
    )


# 兼容不同调用方/测试的命名。
select_doc_keys = select_documents
select_temporal_docs = select_documents
select_time_docs_structured = select_documents
filter_documents = select_documents
choose_documents = select_documents


def fetch_documents(pg: Any) -> list[dict[str, Any]]:
    """Run fetch_documents."""
    if pg is None or not hasattr(pg, "fetch_all"):
        raise ValueError("fetch_documents requires a pg with fetch_all")
    rows = pg.fetch_all(
        "SELECT doc_key, company, report_type, report_period_start, "
        "report_period_end, filename FROM documents"
    )
    return [dict(row) for row in rows]
