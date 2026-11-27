"""结构化文档选择：company + report_type + report_period metadata 硬过滤。

LLM 只在硬过滤后的候选里做歧义消解，不能凭空返回不存在或跨主体的 doc_key；
硬过滤无命中时直接返回空元组，不做全库回退。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable


REPORT_TYPE_KEYWORDS = {
    "annual": ("年报", "年度报告", "annual"),
    "interim": ("半年报", "中期报告", "interim", "半年"),
    "quarterly": ("季报", "季度报告", "quarterly", "三季", "一季", "四季"),
}


def _date_int(value: Any) -> int | None:
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


def _year_of(value: Any) -> int | None:
    number = _date_int(value)
    if number is None or number < 19000101:
        return None
    return number // 10000


def _normalize_company_name(value: Any) -> str:
    return str(value or "").strip()


def _extract_company(question: str, documents: Iterable[dict[str, Any]]) -> str | None:
    companies = sorted(
        {_normalize_company_name(doc.get("company")) for doc in documents if doc.get("company")},
        key=len,
        reverse=True,
    )
    for company in companies:
        if company and company in (question or ""):
            return company
    return None


def _extract_report_type(question: str) -> str | None:
    text = question or ""
    for report_type, keywords in REPORT_TYPE_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return report_type
    return None


def _forecast_years(question: str) -> set[int]:
    import re

    result: set[int] = set()
    for match in re.finditer(r"(\d{4})(?:\s*年)?", question or ""):
        year = int(match.group(1))
        after = (question or "")[match.end() : match.end() + 12]
        window = (question or "")[max(0, match.start() - 12) : match.end() + 30]
        doc_markers = ("报告", "年报", "季报", "半年报", "月报")
        forecast_markers = ("预测", "预计", "展望", "规划", "未来", "市场规模", "到", "至")
        if any(marker in after for marker in doc_markers):
            continue
        if any(marker in window for marker in forecast_markers):
            result.add(year)
    return result


def _period_overlaps(doc: dict[str, Any], date_from: int | None, date_to: int | None) -> bool:
    if date_from is None or date_to is None:
        return True
    start = _date_int(doc.get("report_period_start"))
    end = _date_int(doc.get("report_period_end"))
    if start is None or end is None:
        # 没有报告期 metadata 的旧数据不参与精确报告期过滤，避免把历史数据全部筛掉。
        return True
    return not (end < date_from or start > date_to)


def _hard_filter(
    question: str,
    documents: list[dict[str, Any]],
    *,
    company: str | None = None,
    report_type: str | None = None,
    date_from: int | None = None,
    date_to: int | None = None,
    target_times: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    selected = list(documents)
    resolved_company = company or _extract_company(question, selected)
    if resolved_company:
        selected = [doc for doc in selected if _normalize_company_name(doc.get("company")) == resolved_company]

    resolved_report_type = report_type or _extract_report_type(question)
    if resolved_report_type:
        selected = [doc for doc in selected if str(doc.get("report_type") or "") == resolved_report_type]

    # 报告期硬过滤：只按 document_ranges，绝不把 target_time 年份带进来。
    if date_from is not None and date_to is not None:
        selected = [doc for doc in selected if _period_overlaps(doc, date_from, date_to)]

    # 无文档时间范围时，预测目标年份不能被误当成报告期年份。
    forecast_years = _forecast_years(question)
    if date_from is None and date_to is None and forecast_years:
        selected = [
            doc for doc in selected if _year_of(doc.get("report_period_start")) not in forecast_years
        ]

    if target_times:
        target_years = set()
        for item in target_times:
            try:
                target_years.add(int(str(item).strip()[:4]))
            except (TypeError, ValueError):
                continue
        if target_years and date_from is None and date_to is None:
            selected = [
                doc for doc in selected if _year_of(doc.get("report_period_start")) not in target_years
            ]

    return selected


def _llm_disambiguate(
    question: str,
    candidates: list[dict[str, Any]],
    client: Any,
) -> tuple[str, ...]:
    if client is None or len(candidates) <= 1:
        return tuple(str(doc["doc_key"]) for doc in candidates if doc.get("doc_key"))
    options = "\n".join(
        f"- {doc.get('doc_key')}: company={doc.get('company')}, "
        f"type={doc.get('report_type')}, period={doc.get('report_period_start')}..{doc.get('report_period_end')}, "
        f"name={doc.get('filename') or doc.get('doc_name')}"
        for doc in candidates
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是文档选择器。只能从候选 doc_key 中选择直接相关的文档；"
                "必须同时匹配公司、报告类型和报告期。输出 JSON：{\"doc_keys\":[]}。"
            ),
        },
        {"role": "user", "content": f"问句：{question}\n候选：\n{options}\n输出：{{\"doc_keys\":[]}}"},
    ]
    try:
        content = client.complete(messages, temperature=0.0)
    except Exception:  # noqa: BLE001
        return tuple(str(doc["doc_key"]) for doc in candidates if doc.get("doc_key"))
    try:
        from api.llm_json import parse_json_object

        data = parse_json_object(content)
        keys = data.get("doc_keys") if isinstance(data, dict) else None
        if not isinstance(keys, list):
            return tuple(str(doc["doc_key"]) for doc in candidates if doc.get("doc_key"))
        allowed = {str(doc.get("doc_key")) for doc in candidates if doc.get("doc_key")}
        return tuple(dict.fromkeys(str(key) for key in keys if str(key) in allowed))
    except Exception:  # noqa: BLE001
        return tuple(str(doc["doc_key"]) for doc in candidates if doc.get("doc_key"))


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
    target_times: Iterable[Any] | None = None,
    client: Any = None,
    llm: Any = None,
    **kwargs: Any,
) -> tuple[str, ...]:
    """按 metadata 硬过滤文档；LLM 只做候选内消歧。"""
    if documents is None:
        if pg is not None and hasattr(pg, "fetch_all"):
            try:
                documents = pg.fetch_all(
                    "SELECT doc_key, company, report_type, report_period_start, "
                    "report_period_end, filename FROM documents"
                )
            except Exception:  # noqa: BLE001
                documents = []
        else:
            documents = []

    docs = [dict(doc) for doc in (documents or [])]
    if time_range is not None:
        if date_from is None:
            date_from = getattr(time_range, "date_from", None)
        if date_to is None:
            date_to = getattr(time_range, "date_to", None)
        if target_times is None:
            target_times = getattr(time_range, "target_times", None)
    if date_from is None and isinstance(kwargs.get("report_period"), tuple):
        date_from, date_to = kwargs["report_period"]

    candidates = _hard_filter(
        question,
        docs,
        company=company,
        report_type=report_type,
        date_from=date_from,
        date_to=date_to,
        target_times=target_times,
    )
    if not candidates:
        return ()
    llm_client = client if client is not None else llm
    return _llm_disambiguate(question, candidates, llm_client)


# 兼容不同调用方/测试的命名。
select_doc_keys = select_documents
select_temporal_docs = select_documents
select_time_docs_structured = select_documents
filter_documents = select_documents
choose_documents = select_documents


def fetch_documents(pg: Any) -> list[dict[str, Any]]:
    if pg is None or not hasattr(pg, "fetch_all"):
        return []
    try:
        rows = pg.fetch_all(
            "SELECT doc_key, company, report_type, report_period_start, "
            "report_period_end, filename FROM documents"
        )
    except Exception:  # noqa: BLE001
        return []
    return [dict(row) for row in rows]
