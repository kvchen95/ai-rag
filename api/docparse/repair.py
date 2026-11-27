"""确定性语义与结构修复（不调 LLM）。.

规则只做三件事：合并被错误拆断的段落/断句、白名单 OCR 纠错、还原标题与列表层级。
所有改动写进每块的 `repairs[]`，供人工复核；修不动的块原样保留。
"""

from __future__ import annotations

import html
import re
from typing import Any

_TERMINAL = ("。", "！", "？", "；", ".", "!", "?", ";", "）", ")")
_LIST_RE = re.compile(
    r"^(?:[●⚫•\-–\*]\s+|\d+[.)]\s+|[（(]?[一二三四五六七八九十]+[、.)]\s*)"
)


def _strip_tags(value: str) -> str:
    """Run _strip_tags."""
    return re.sub(r"<[^>]+>", "", value).strip()


def html_table_to_markdown(html_text: str) -> str:
    """把 MinerU 的 HTML 表格转成 Markdown 表格；失败时返回原文。."""
    if not html_text or "<table" not in html_text.lower():
        return html_text.strip()
    rows = re.findall(
        r"<tr[^>]*>(.*?)</tr>", html_text, flags=re.IGNORECASE | re.DOTALL
    )
    parsed_rows: list[list[str]] = []
    for row in rows:
        cells = re.findall(
            r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.IGNORECASE | re.DOTALL
        )
        parsed_rows.append(
            [
                html.unescape(_strip_tags(cell)).replace("|", "\\|")
                for cell in cells
            ]
        )
    if not parsed_rows:
        return html_text.strip()
    width = max(len(row) for row in parsed_rows)
    parsed_rows = [row + [""] * (width - len(row)) for row in parsed_rows]
    lines = [
        "| " + " | ".join(parsed_rows[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in parsed_rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _repair_table(block: dict[str, Any]) -> tuple[str, list[str]]:
    """Run _repair_table."""
    repairs: list[str] = []
    source = str(block.get("table_body") or block.get("text") or "").strip()
    if "<table" in source.lower():
        markdown = html_table_to_markdown(source)
        if markdown != source:
            repairs.append("table_html_to_markdown")
        return markdown, repairs
    text, inner_repairs = _clean_text(source)
    return text, inner_repairs


def _clean_text(text: str) -> tuple[str, list[str]]:
    """Run _clean_text."""
    repairs: list[str] = []
    original = text
    text = (
        text.replace("\u200b", "").replace("\u200d", "").replace("\ufeff", "")
    )
    text = re.sub(r"[ \t]{2,}", " ", text)
    if text != original:
        repairs.append("whitespace_or_zero_width")
    # OCR 白名单：只修高置信度的字符混淆，不做语义猜测。
    typo_map = {
        "０": "0",
        "Ｏ": "O",
        "ｌ": "l",
        "１": "1",
        "。": "。",
        "，": "，",
    }
    for bad, good in typo_map.items():
        if bad in text and bad != good:
            text = text.replace(bad, good)
            repairs.append(f"ocr_typo:{bad}->{good}")
    return text, repairs


def _heading_level(block: dict[str, Any]) -> int | None:
    """Run _heading_level."""
    if block.get("type") == "header":
        level = block.get("text_level")
        return int(level) if isinstance(level, int) and level > 0 else 1
    level = block.get("text_level")
    if isinstance(level, int) and 1 <= level <= 6:
        return level
    return None


def _is_list_item(text: str) -> bool:
    """Run _is_list_item."""
    return bool(_LIST_RE.match(text.strip()))


def repair_blocks(raw_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """返回带 block_id / heading_level / list_group_id / repairs 的修复块。."""
    repaired: list[dict[str, Any]] = []
    current_heading_id: str | None = None
    list_group: int | None = None
    list_counter = 0

    for index, raw in enumerate(raw_blocks):
        block = dict(raw)
        block["block_id"] = f"b{index + 1:04d}"
        if block.get("type") == "table":
            text, repairs = _repair_table(block)
        else:
            text, repairs = _clean_text(str(block.get("text") or ""))
        block["text"] = text
        block["repairs"] = repairs
        level = _heading_level(block)
        block["heading_level"] = level
        if level is not None:
            current_heading_id = block["block_id"]
            list_group = None

        is_list = _is_list_item(text)
        block["is_list_item"] = is_list
        if is_list:
            if list_group is None:
                list_counter += 1
                list_group = list_counter
            block["list_group_id"] = list_group
        else:
            block["list_group_id"] = None
            if block.get("type") not in {"table", "header"}:
                list_group = None

        # 合并被错误拆断的普通段落/断句：上一块未以终止符结束，
        # 且下一块不是标题/列表/表格。
        if (
            block.get("type") not in {"header", "table"}
            and not is_list
            and repaired
            and repaired[-1].get("type") not in {"header", "table"}
            and not repaired[-1].get("is_list_item")
            and not str(repaired[-1].get("text") or "")
            .rstrip()
            .endswith(_TERMINAL)
            and not str(text).lstrip().startswith(("●", "⚫", "-", "1.", "2."))
        ):
            previous = repaired[-1]
            previous["text"] = f"{previous.get('text', '')}\n{text}".strip()
            previous["repairs"] = [
                *previous.get("repairs", []),
                "merged_split_paragraph",
            ]
            previous["merged_from"] = [
                *previous.get("merged_from", []),
                block["block_id"],
            ]
            continue

        block["merged_from"] = []
        block["heading_id"] = current_heading_id
        repaired.append(block)

    return repaired


__all__ = ["repair_blocks"]
