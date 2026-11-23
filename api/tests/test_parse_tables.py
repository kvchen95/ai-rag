"""表格三表示（§3.3）：HTML 保真、md 派生、cells 矩阵，以及三者的往返一致。

验收第二条是"表格三表示一致可重建"。这条要变成可执行的判据，只有一条路：
**往返**。所以本文件的核心是

    cells --render_html--> html --cells_from_html--> cells'   必须相等
    cells --render_md--->  md  --parse_md-->        matrix'   必须等于 matrix

两条往返都不需要真 PDF，跑得快，而且改坏展开规则（比如合并格填 `-` 而不是复制
左上角的值）会立刻红。

另外单独钉住三件容易写错的事：
* 被 `rowspan`/`colspan` 覆盖的格在 HTML 里**不输出标签**（输出了浏览器会整体错位）；
* md 里的 `|` 要转义（单元格文本真的会带竖线："500|380"）；
* 表头推断（`header_inferred`）只影响字段语义，不影响 HTML 标记。
"""

from __future__ import annotations

import pytest

from api.parse.model import TableCell, expand_to_matrix
from api.parse.tables import (
    BLANK_CELL,
    build_table,
    cells_from_html,
    clean_cell_text,
    is_label_table,
    parse_md,
    render_html,
    render_md,
    table_content_text,
)

# 一个 4×3 的普通表：表头 + 三行数据。
PLAIN = [
    TableCell(0, 0, "City", is_header=True),
    TableCell(0, 1, "Lodging cap", is_header=True),
    TableCell(0, 2, "Region", is_header=True),
    TableCell(1, 0, "Shanghai"),
    TableCell(1, 1, "500"),
    TableCell(1, 2, "tier-1"),
    TableCell(2, 0, "Beijing"),
    TableCell(2, 1, "500"),
    TableCell(2, 2, "tier-1"),
    TableCell(3, 0, "Hefei"),
    TableCell(3, 1, "380"),
    TableCell(3, 2, "tier-2"),
]

# 带合并的表：表头跨两列（colspan=2），第一列有一格跨两行（rowspan=2）。
# 刻意做成"4 行 × 3 列全部被覆盖"：有洞的表往返回来会把洞实化成占位格，
# 那是另一个用例（见 `test_html_writes_placeholder_for_missing_cells`）。
MERGED = [
    TableCell(0, 0, "Role", is_header=True),
    TableCell(0, 1, "Monthly limit", is_header=True, col_span=2),
    TableCell(1, 0, "staff"),
    TableCell(1, 1, "2000"),
    TableCell(1, 2, "per person"),
    TableCell(2, 0, "approver", row_span=2),
    TableCell(2, 1, "8000"),
    TableCell(2, 2, "per team"),
    TableCell(3, 1, "12000"),
    TableCell(3, 2, "per dept"),
]


# --- 1. HTML 保真 ------------------------------------------------------------


def test_html_keeps_span_attributes() -> None:
    html = render_html(MERGED, 4, 3)
    assert 'colspan="2"' in html
    assert 'rowspan="2"' in html


def test_html_omits_cells_covered_by_a_span() -> None:
    """被覆盖的位置不输出 `<td>`——输出了浏览器会把后面的格整体挤到下一行。

    4 行 × 3 列 = 12 个位置，减掉被 colspan 覆盖的 1 个、被 rowspan 覆盖的 1 个，
    HTML 里应该正好 10 个 `<td>/<th>`。
    """
    html = render_html(MERGED, 4, 3)
    assert html.count("<td") + html.count("<th") == 10


def test_html_uses_th_only_for_header_cells() -> None:
    html = render_html(PLAIN, 4, 3)
    assert html.count("<th") == 3
    assert html.count("<td") == 9


def test_html_escapes_special_characters() -> None:
    cells = [TableCell(0, 0, "a < b & c", is_header=True)]
    html = render_html(cells, 1, 1)
    assert "&lt;" in html and "&amp;" in html
    assert "< b" not in html


def test_html_writes_placeholder_for_missing_cells() -> None:
    """没有单元格覆盖到的位置输出空 td，而不是跳过。

    跳过会让后面的格左移一列，而这个错误在 HTML 里完全看不出来，
    只有把 HTML 渲染出来、或反解回 cells 时才会暴露。
    """
    html = render_html([TableCell(0, 0, "only", is_header=True)], 2, 2)
    assert html.count("<td") + html.count("<th") == 4


# --- 2. cells <-> HTML 往返 --------------------------------------------------


@pytest.mark.parametrize("name,cells", [("plain", PLAIN), ("merged", MERGED)])
def test_cells_to_html_to_cells_roundtrip(name: str, cells: list[TableCell]) -> None:
    del name
    html = render_html(cells, 4, 3)
    back, rows, cols = cells_from_html(html)
    assert (rows, cols) == (4, 3)
    assert [(c.row, c.col, c.text, c.is_header, c.row_span, c.col_span) for c in back] == [
        (c.row, c.col, clean_cell_text(c.text), c.is_header, c.row_span, c.col_span)
        for c in cells
    ]


def test_roundtrip_preserves_the_reconstructed_matrix() -> None:
    """真正要一致的不是 cells 列表，而是**展开后的矩阵**。

    同一张表可以用不同的 span 组合表达（跨两列 vs 两个相同值），矩阵才是它的
    语义。验收口径"三表示一致可重建"指的是这个层面的一致。
    """
    html = render_html(MERGED, 4, 3)
    back, rows, cols = cells_from_html(html)
    assert expand_to_matrix(back, rows, cols) == expand_to_matrix(MERGED, 4, 3)


# --- 3. md 派生 --------------------------------------------------------------


def test_md_has_header_separator_and_rows() -> None:
    md = render_md(PLAIN, 4, 3)
    lines = md.splitlines()
    assert lines[0] == "| City | Lodging cap | Region |"
    assert lines[1] == "| --- | --- | --- |"
    assert len(lines) == 5  # 表头 + 分隔 + 3 行


def test_md_expands_colspan_into_repeated_column_names() -> None:
    """§3.3 规则 3：Markdown 不支持合并表头，展开成重复列名。

    填 `-` 会让人以为那格是空的——它不是空的，它是被合并格覆盖的同一列。
    """
    md = render_md(MERGED, 4, 3)
    header = md.splitlines()[0]
    assert header.count("Monthly limit") == 2


def test_md_fills_empty_cells_with_placeholder() -> None:
    cells = [TableCell(0, 0, "a", is_header=True), TableCell(0, 1, "", is_header=True)]
    md = render_md(cells, 1, 2)
    assert BLANK_CELL in md


def test_md_escapes_pipes_inside_cells() -> None:
    """"标准 500|380" 是真实写法。不转义会把一行切成更多列。"""
    cells = [TableCell(0, 0, "500|380", is_header=True)]
    md = render_md(cells, 1, 1)
    assert "\\|" in md
    assert parse_md(md) == [["500|380"]]


def test_md_of_empty_table_is_empty_string() -> None:
    assert render_md([], 0, 0) == ""


# --- 4. md -> matrix 往返 ----------------------------------------------------


@pytest.mark.parametrize("name,cells", [("plain", PLAIN), ("merged", MERGED)])
def test_md_parses_back_to_the_same_matrix(name: str, cells: list[TableCell]) -> None:
    del name
    md = render_md(cells, 4, 3)
    assert parse_md(md) == expand_to_matrix(cells, 4, 3)


def test_parse_md_rejects_ragged_rows() -> None:
    """列数不对时猜出来的表一定是错的，早炸比晚炸好。"""
    with pytest.raises(ValueError):
        parse_md("| a | b |\n| --- | --- |\n| only-one |")


def test_parse_md_rejects_malformed_separator() -> None:
    with pytest.raises(ValueError):
        parse_md("| a | b |\n| not-a-separator | --- |")


def test_parse_md_requires_header_and_separator() -> None:
    with pytest.raises(ValueError):
        parse_md("| a | b |")


# --- 5. 矩阵展开规则 ---------------------------------------------------------


def test_expand_copies_the_top_left_value_across_a_span() -> None:
    """§3.3 规则 1：合并单元格按"左上角为值、其余格复制该值"展开。"""
    cells = [TableCell(0, 0, "merged", col_span=2, row_span=2)]
    assert expand_to_matrix(cells, 2, 2) == [["merged", "merged"], ["merged", "merged"]]


def test_expand_does_not_overwrite_existing_values() -> None:
    """展开时"先到先得"：后面的格不能盖掉前面已经写进去的值。

    合并格的展开顺序决定了谁是"左上角"，写反了就会拿到被覆盖格的值。
    """
    cells = [
        TableCell(0, 0, "left", col_span=2),
        TableCell(0, 1, "should-not-win"),
    ]
    assert expand_to_matrix(cells, 1, 2) == [["left", "left"]]


def test_expand_rejects_out_of_bounds_cells() -> None:
    with pytest.raises(ValueError):
        expand_to_matrix([TableCell(5, 0, "x")], 2, 2)


# --- 6. 单元格文本归一 -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("  Shanghai  ", "Shanghai"),
        ("two\nlines", "two lines"),
        ("  a \t b  ", "a b"),
        ("\u00a0nbsp\u00a0", "nbsp"),
        (None, ""),
    ],
)
def test_clean_cell_text(raw: str | None, want: str) -> None:
    assert clean_cell_text(raw) == want


def test_clean_cell_text_does_not_touch_numbers() -> None:
    """不做全角转换、不做数字格式化：table_cells 是校验用的证据。

    改一个字符就可能让"数字能否在被引证据里定位"判错。
    """
    assert clean_cell_text("５００") == "５００"
    assert clean_cell_text("5,000") == "5,000"


# --- 7. content_text ---------------------------------------------------------


def test_table_content_text_uses_the_header_as_keys() -> None:
    """表切片的检索文本按行拼成 "列名: 值"，让"上海住宿限额多少"能词法命中。

    直接把 md 塞进 content_text 会让竖线与分隔行变成纯噪声，还占 token。
    """
    table = build_table(PLAIN, row_count=4, col_count=3)
    text = table_content_text(table)
    assert "City: Shanghai" in text
    assert "Lodging cap: 500" in text
    assert "---" not in text


def test_table_content_text_without_header_does_not_invent_keys() -> None:
    """表头是推断出来的（首行其实是数据）就不加键。

    拿一行数据当列名会拼出 `500: 380 | tier-2: tier-3` 这种胡说。
    """
    cells = [TableCell(0, 0, "Chengdu"), TableCell(0, 1, "380")]
    table = build_table(cells, row_count=1, col_count=2, header_inferred=True)
    assert table_content_text(table) == "Chengdu 380"


def test_table_content_text_keeps_the_title_line() -> None:
    table = build_table(PLAIN, row_count=4, col_count=3)
    text = table_content_text(table, title="Table 1. Lodging caps")
    assert text.splitlines()[0] == "Table 1. Lodging caps"


def test_label_table_detection() -> None:
    """单行单列的"表格"其实是标签（§3.3 边界表）。"""
    assert is_label_table(build_table([TableCell(0, 0, "x")], row_count=1, col_count=1))
    assert not is_label_table(build_table(PLAIN, row_count=4, col_count=3))


# --- 8. build_table 的一致性 -------------------------------------------------


def test_build_table_produces_all_three_representations() -> None:
    table = build_table(PLAIN, row_count=4, col_count=3)
    assert table.html.startswith("<table")
    assert table.md.startswith("| City |")
    assert len(table.cells) == 12
    assert len(table.cells_json()) == 12


def test_cells_json_matches_the_database_shape() -> None:
    """`chunks.table_cells` 是 jsonb 数组，键名必须与落库口径一致。"""
    table = build_table(PLAIN, row_count=4, col_count=3)
    first = table.cells_json()[0]
    assert set(first) == {"row", "col", "text", "is_header", "row_span", "col_span"}
    assert first == {
        "row": 0,
        "col": 0,
        "text": "City",
        "is_header": True,
        "row_span": 1,
        "col_span": 1,
    }
