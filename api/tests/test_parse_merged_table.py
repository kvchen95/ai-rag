"""带合并单元格的真实 PDF 表格（§3.3 头号要求：合并单元格原样保留）。

`test_parse_tables.py` 用**合成的** `TableCell` 测模型与渲染，测不到
"pdfplumber 到底怎么报合并格"。这一组补上那段真实路径：

    PDF 里缺一条竖线  ->  pdfplumber 给一个跨列的 bbox  ->  我们的 `col_span=2`
                      ->  HTML 输出 `colspan="2"`、被覆盖位置不输出 `<td>`
                      ->  md 把合并值铺满被覆盖的格

三段都要一致，否则 §3.3 的"HTML 保真 + md 派生"就是空话。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pdfplumber
import pytest

from api.parse.chunking import Acl
from api.parse.geometry import PageSpace
from api.parse.layout import find_tables
from api.parse.model import expand_to_matrix
from api.parse.pipeline import parse_document
from api.parse.tables import cells_from_html, parse_md

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


@pytest.fixture(scope="module")
def merged_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_layout_samples  # noqa: PLC0415

    root = tmp_path_factory.mktemp("merged")
    rel = "layout/merged-table-demo.pdf"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(make_layout_samples.build(rel))
    return target


def test_pdfplumber_reports_the_merged_cell_as_a_wide_bbox(merged_pdf: Path) -> None:
    """前提事实：表头那个格被报成跨三列的一个 bbox（不是三个格）。"""
    with pdfplumber.open(str(merged_pdf)) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        tables = find_tables(page, space)
    assert len(tables) == 1
    table = tables[0].table
    header = next(cell for cell in table.cells if cell.row == 0)
    assert header.col_span == 3
    assert header.text == "Role Monthly cap"


def test_span_reaches_the_model_and_the_html(merged_pdf: Path) -> None:
    """`col_span` 必须一路传到 `TableCell` 与 HTML 的 `colspan`。

    这条是 `table-span` 变异的克星：把 `col_span` 写死成 1，HTML 里就没有
    colspan 了，而渲染出来会把"Monthly cap"塞进一列。
    """
    with pdfplumber.open(str(merged_pdf)) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        table = find_tables(page, space)[0].table
    assert 'colspan="3"' in table.html
    assert 'colspan="2"' in table.html  # 末行跨两列


def test_merged_header_fills_every_covered_column_in_md(merged_pdf: Path) -> None:
    """§3.3 规则 3：Markdown 不支持合并表头，展开成重复列名。"""
    with pdfplumber.open(str(merged_pdf)) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        table = find_tables(page, space)[0].table
    matrix = table.matrix()
    assert matrix[0] == ["Role Monthly cap"] * 3
    assert parse_md(table.md) == matrix


def test_merged_table_three_representations_agree_on_real_pdf(merged_pdf: Path) -> None:
    """真实 PDF 上跑一遍三表示往返（HTML / md / cells 都从 pdfplumber 的结果出发）。"""
    with pdfplumber.open(str(merged_pdf)) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        table = find_tables(page, space)[0].table

    from_cells = expand_to_matrix(table.cells, table.row_count, table.col_count)
    from_html_cells, rows, cols = cells_from_html(table.html)
    from_html = expand_to_matrix(from_html_cells, rows, cols)
    from_md = parse_md(table.md)
    assert from_html == from_cells
    assert from_md == from_cells


def test_merged_table_chunk_carries_the_span(merged_pdf: Path) -> None:
    """落库的 `table_cells` 必须带 span —— 校验阶段靠它对齐数字。"""
    outcome = parse_document(
        merged_pdf,
        doc_key="layout/merged",
        version_id="layout/merged#abcdef123456",
        acl=Acl(is_public=True),
    )
    table_chunks = [c for c in outcome.chunks if c.chunk_type == "table"]
    assert len(table_chunks) == 1
    cells = table_chunks[0].table_cells
    assert cells is not None
    header = next(cell for cell in cells if cell["row"] == 0)
    assert header["col_span"] == 3
    assert any(cell["col_span"] == 2 for cell in cells)
