"""最终 Markdown 整合输出。"""

from __future__ import annotations

import re
from typing import Any


def _clean(text: str) -> str:
    return text.strip()


def render_final_markdown(blocks: list[dict[str, Any]]) -> str:
    """把富化后的块整合成一份可读的 Markdown 文档。"""
    lines: list[str] = []
    for block in blocks:
        block_type = str(block.get("type") or "")
        if block_type in {"header", "footer", "page_number", "page_footnote"}:
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        level = block.get("heading_level")
        if isinstance(level, int) and 1 <= level <= 6 and block_type not in {"table"}:
            lines.append(f"{'#' * level} {text}")
            lines.append("")
            continue
        if block_type == "table":
            lines.append(text)
            lines.append("")
            continue
        if block_type == "chart":
            lines.append("> [图表]")
            lines.append("")
            continue
        lines.append(text)
        lines.append("")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"
