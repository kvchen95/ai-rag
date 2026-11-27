"""Docling 表格结构化增强：把表格转成结构化信息块并写回解析 JSON。."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

TableRunner = Callable[[str], list[dict[str, Any]]]


def _default_runner(pdf_path: str) -> list[dict[str, Any]]:
    """Run _default_runner."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = True
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    result = converter.convert(pdf_path)
    tables: list[dict[str, Any]] = []
    for index, table in enumerate(result.document.tables):
        df = table.export_to_dataframe(doc=result.document)
        cells = [[str(value) for value in row] for row in df.values.tolist()]
        tables.append(
            {
                "docling_index": index,
                "page_num": table.prov[0].page_no if table.prov else None,
                "rows": len(df),
                "cols": len(df.columns),
                "columns": [str(col) for col in df.columns],
                "cells": cells,
                "markdown": df.to_markdown(index=False),
            }
        )
    return tables


def _context_of(
    blocks: list[dict[str, Any]], index: int, direction: int
) -> str:
    """Run _context_of."""
    cursor = index + direction
    while 0 <= cursor < len(blocks):
        if blocks[cursor].get("type") != "table":
            return str(blocks[cursor].get("text") or "").strip()
        cursor += direction
    return ""


def _match_docling(
    mineru_tables: list[dict[str, Any]], docling_tables: list[dict[str, Any]]
) -> list[dict[str, Any] | None]:
    """按页号和出现顺序匹配；数量不一致时缺的留 None，不编造。."""
    by_page: dict[int | None, list[dict[str, Any]]] = {}
    for table in docling_tables:
        by_page.setdefault(table.get("page_num"), []).append(table)
    matched: list[dict[str, Any] | None] = []
    for block in mineru_tables:
        page = (block.get("page_idx") or 0) + 1
        candidates = by_page.get(page) or by_page.get(None) or []
        matched.append(candidates.pop(0) if candidates else None)
    return matched


def enhance_tables(
    blocks: list[dict[str, Any]],
    pdf_path: str,
    *,
    runner: TableRunner | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (增强后的 blocks, docling 表格列表)。."""
    runner = runner or _default_runner
    docling_tables = runner(pdf_path)
    mineru_tables = [block for block in blocks if block.get("type") == "table"]

    matched = _match_docling(mineru_tables, docling_tables)
    table_positions = [
        i for i, block in enumerate(blocks) if block.get("type") == "table"
    ]

    for position, docling in zip(table_positions, matched, strict=False):
        block = blocks[position]
        page = (block.get("page_idx") or 0) + 1
        before = _context_of(blocks, position, -1)
        after = _context_of(blocks, position, 1)
        caption = " ".join(block.get("table_caption") or []).strip()
        footnote = " ".join(block.get("table_footnote") or []).strip()
        parts = [f"表格（第 {page} 页）"]
        if caption:
            parts.append(f"表题：{caption}")
        if docling and docling.get("markdown"):
            parts.append(docling["markdown"])
        elif block.get("table_body"):
            parts.append(str(block["table_body"]))
        if footnote:
            parts.append(f"注：{footnote}")
        if before:
            parts.append(f"前文：{before}")
        if after:
            parts.append(f"后文：{after}")
        block["text"] = "\n".join(parts)
        block["docling"] = docling or {}
        block["context_before"] = before
        block["context_after"] = after
        block["table_enhanced"] = True

    return blocks, docling_tables


__all__ = (
    ["enhance_tables", "result_origin_url"] if False else ["enhance_tables"]
)
