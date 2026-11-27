"""表格三表示（详细设计.md §3.3）。.

一份存档、两份派生，分工写死在这里，别处不再做转换：

    table_html   保真存档：rowspan / colspan 原样保留，前端渲染与人工核对看它
    table_md     派生文本：喂模型、编码、词法索引（标签占 token 还污染语义）
    table_cells  单元格矩阵：校验阶段对齐数字用，比在 HTML 里找子串可靠

### 为什么 HTML 与 md 都从 cells 生成

如果 HTML 从 pdfplumber 的 `extract()` 直接拿、md 另写一套展开，两份逻辑迟早
分叉——真实事故形状是"HTML 里表头还是合并的、md 里只有第一列"。
这里的方向是单向的：

    cells  ──render_html──▶  html
      │                        │
      └────expand_matrix──▶  md  ◀──cells_from_html──┘

三条推导规则（§3.3，写死，不交给模型）：

1. **合并单元格按"左上角为值、其余格复制该值"展开**。注意这会让 md 里出现
   重复列名（Markdown 没有合并表头），这正是为什么 HTML 存档不能丢。
2. **空单元格填 `-`**，不是填空：md 里的空串会让"两列之间少一个分隔符"，
   解析回来列数就变了。
3. **表头行照留**；没有表头时首行当表头并标 `header_inferred`，这时校验阶段
   不拿首行当字段名（它可能只是一行数据）。

`cells_from_html` 的存在是为了让"三表示一致"可执行：往返一圈等于同一个矩阵，
才算一致。生产路径不解析 HTML（没必要），但测试与非预期来源（比如人工修正过的
table_html）都要靠它。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from html import escape
from html.parser import HTMLParser

from api.parse.model import TableCell, TableData, expand_to_matrix

# md 里空格的占位。用 `-` 而不是 `&nbsp;`：md 要喂模型与词法索引，
# HTML 实体在里面是噪声。
BLANK_CELL = "-"

# 单元格内换行合并成空格（§3.3 边界表：换行会让 md 表格断行）。
_NEWLINE = re.compile(r"\s*\r?\n\s*")
# 连续空白收成一个空格：PDF 抽出来的文字经常带多个空格，md 表格里会歪。
_WS = re.compile(r"[ \t\u00a0]+")


def clean_cell_text(text: str | None) -> str:
    """单元格文本归一：去换行、收空白、去首尾。.

    不做全角/半角转换、不做数字格式化——`table_cells` 是校验用的证据，
    改一个字符就可能让"数字能否在被引证据里定位"判错。
    """
    if text is None:
        return ""
    text = _NEWLINE.sub(" ", text)
    text = _WS.sub(" ", text)
    return text.strip()


# --- cells -> html -----------------------------------------------------------


def render_html(
    cells: Sequence[TableCell],
    row_count: int,
    col_count: int,
    *,
    header_inferred: bool = False,
) -> str:
    """Cells -> 保真 HTML。.

    合并格按 (row, col) 落在网格上，被覆盖的格**不输出 `<td>`**——这正是 HTML
    表达合并的方式（`rowspan`/`colspan` 占位，其余格让位）。反过来若把被覆盖的
    格也输出，浏览器会把它挤到下一行，渲染出来的表整个错位。

    表头 `<th>` 的条件：`is_header=True` 的格。`header_inferred`
    （首行推断成表头）时仍然用 `<th>`：保真的意思是"我以为它是表头"，
    不是"它一定是表头"，这个区分由 `header_inferred` 字段承载，不靠标签。
    """
    grid: list[list[TableCell | None]] = [
        [None] * col_count for _ in range(row_count)
    ]
    for cell in cells:
        if not (0 <= cell.row < row_count and 0 <= cell.col < col_count):
            raise ValueError(f"cell out of bounds: {cell.row},{cell.col}")
        grid[cell.row][cell.col] = cell

    taken: set[tuple[int, int]] = set()
    lines: list[str] = ['<table border="1">']
    for row in range(row_count):
        cells_html: list[str] = []
        for col in range(col_count):
            if (row, col) in taken:
                continue
            cell = grid[row][col]
            if cell is None:
                # 没有单元格覆盖到这一格：输出空 td 而不是跳过——跳过会让后面
                # 的格整体左移一列，而这个错误在 HTML 里完全看不出来。
                cells_html.append(f"<td>{escape(BLANK_CELL)}</td>")
                continue
            tag = "th" if cell.is_header else "td"
            attrs = []
            if cell.row_span > 1:
                attrs.append(f'rowspan="{cell.row_span}"')
            if cell.col_span > 1:
                attrs.append(f'colspan="{cell.col_span}"')
            for r in range(row, min(row_count, row + max(1, cell.row_span))):
                for c in range(
                    col, min(col_count, col + max(1, cell.col_span))
                ):
                    if (r, c) != (row, col):
                        taken.add((r, c))
            attr_text = (" " + " ".join(attrs)) if attrs else ""
            body = escape(clean_cell_text(cell.text) or BLANK_CELL)
            cells_html.append(f"<{tag}{attr_text}>{body}</{tag}>")
        lines.append("<tr>" + "".join(cells_html) + "</tr>")
    lines.append("</table>")
    del header_inferred  # 只影响字段，不影响标记（见 docstring）
    return "\n".join(lines)


# --- cells -> markdown ------------------------------------------------------


def render_md(
    cells: Sequence[TableCell], row_count: int, col_count: int
) -> str:
    """Cells -> Markdown 表。.

    先 `expand_to_matrix` 再排成 md：合并格展开、空格补 `-`。转义 `|`：
    单元格文本里真的有竖线（"标准 500|380"是真实写法），不转义会把一行
    切成更多列，再解析回来列数就对不上了。
    """
    if row_count <= 0 or col_count <= 0:
        return ""
    matrix = expand_to_matrix(cells, row_count, col_count, blank=BLANK_CELL)
    rows = [
        "| " + " | ".join(_escape_md_cell(value) for value in row) + " |"
        for row in matrix
    ]
    header_sep = "| " + " | ".join(["---"] * col_count) + " |"
    return "\n".join([rows[0], header_sep, *rows[1:]])


def _escape_md_cell(value: str) -> str:
    """Run _escape_md_cell."""
    return value.replace("\\", "\\\\").replace("|", "\\|")


def parse_md(md: str) -> list[list[str]]:
    """Md 表 -> 矩阵。往返测试用，也是"md 可重建 cells"的可执行检查。.

    只认标准 GFM 形状（首行表头 + 分隔行 + 数据行）。畸形输入抛 `ValueError`
    而不是尽量猜：md 的列数不对时猜出来的表一定是错的，早炸比晚炸好。
    """
    lines = [line.strip() for line in md.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(
            "markdown table needs a header and a separator, "
            f"got {len(lines)} line(s)"
        )
    header = _split_md_row(lines[0])
    separator = _split_md_row(lines[1])
    if len(header) != len(separator) or not all(
        set(cell) <= {"-", ":"} and cell for cell in separator
    ):
        raise ValueError(f"malformed markdown separator row: {lines[1]!r}")
    rows = [header]
    for line in lines[2:]:
        cells = _split_md_row(line)
        if len(cells) != len(header):
            raise ValueError(
                f"row has {len(cells)} cells, header has {len(header)}: "
                f"{line!r}"
            )
        rows.append(cells)
    return rows


def _split_md_row(line: str) -> list[str]:
    """Run _split_md_row."""
    if not line.startswith("|") or not line.endswith("|"):
        raise ValueError(f"markdown row must start and end with '|': {line!r}")
    body = line[1:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in body:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


# --- html -> cells ----------------------------------------------------------


class _TableParser(HTMLParser):
    """把 render_html 的输出还原成 cells（含 rowspan/colspan 的占位重建）。.

    只实现我们自己的输出形状，不做通用 HTML 表格解析：要通用就会引入
    "遇到不认识的标签怎么办"的一堆猜测，而这里的目标是往返一致。
    """

    def __init__(self) -> None:
        """Initialize the instance."""
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict[str, object]]] = []
        self._row: list[dict[str, object]] | None = None
        self._cell: dict[str, object] | None = None
        self._buffer: list[str] = []
        self._depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        """Run handle_starttag."""
        if tag == "table":
            self._depth += 1
            return
        if tag == "tr":
            self._row = []
            return
        if tag in {"td", "th"}:
            attr_map = dict(attrs)
            self._cell = {
                "is_header": tag == "th",
                "row_span": _as_int(attr_map.get("rowspan"), 1),
                "col_span": _as_int(attr_map.get("colspan"), 1),
            }
            self._buffer = []
            return
        if tag == "br" and self._cell is not None:
            self._buffer.append(" ")

    def handle_endtag(self, tag: str) -> None:
        """Run handle_endtag."""
        if tag in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._cell["text"] = "".join(self._buffer)
            self._row.append(self._cell)
            self._cell = None
            self._buffer = []
            return
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            return
        if tag == "table":
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        """Run handle_data."""
        if self._cell is not None:
            self._buffer.append(data)


def _as_int(value: str | None, default: int) -> int:
    """Run _as_int."""
    if value is None:
        return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def cells_from_html(html: str) -> tuple[list[TableCell], int, int]:
    """HTML -> (cells, row_count, col_count)。.

    被 `rowspan`/`colspan` 覆盖的位置在 HTML 里没有标签，这里按占位表补回来——
    还原出来的 cells 与 `render_html` 的输入逐个相等（同一位置、同一 span）。
    """
    parser = _TableParser()
    parser.feed(html)
    parser.close()

    occupied: dict[tuple[int, int], int] = {}
    cells: list[TableCell] = []
    max_col = 0
    for row_index, raw_cells in enumerate(parser.rows):
        col = 0
        for raw in raw_cells:
            while (row_index, col) in occupied:
                col += 1
            row_span = int(raw.get("row_span", 1))  # type: ignore[arg-type]
            col_span = int(raw.get("col_span", 1))  # type: ignore[arg-type]
            text = clean_cell_text(str(raw.get("text", "")))
            cells.append(
                TableCell(
                    row=row_index,
                    col=col,
                    text=text,
                    is_header=bool(raw.get("is_header", False)),
                    row_span=row_span,
                    col_span=col_span,
                )
            )
            for r in range(row_index, row_index + row_span):
                for c in range(col, col + col_span):
                    occupied[(r, c)] = 1
            col += col_span
            max_col = max(max_col, col)
    return cells, len(parser.rows), max_col


# --- 组装 -------------------------------------------------------------------


def build_table(
    cells: Iterable[TableCell],
    *,
    row_count: int,
    col_count: int,
    header_inferred: bool = False,
    continued: bool = False,
) -> TableData:
    """一次的入口：cells 进，三表示一起出。.

    `header_inferred`：首行被当表头用了（原文没有表头）。校验阶段据此决定
    要不要拿首行当字段名——它可能只是一行数据，拿它当字段名会让"数字定位"
    把 500 当成列名。
    """
    cell_list = tuple(cells)
    html = render_html(
        cell_list, row_count, col_count, header_inferred=header_inferred
    )
    md = render_md(cell_list, row_count, col_count)
    return TableData(
        cells=cell_list,
        row_count=row_count,
        col_count=col_count,
        html=html,
        md=md,
        header_inferred=header_inferred,
        continued=continued,
    )


def table_content_text(table: TableData, *, title: str | None = None) -> str:
    """表格切片的 `content_text`。.

    **不是 md 原文**：`content_text` 要进 BM25 与向量，md 的竖线与分隔行是
    纯噪声。这里按行拼成"自然语言"：
    `City: Shanghai | Lodging cap: 500 | Region: tier-1`，表头作为键——
    这样"上海住宿限额多少"这类问句在词法上能命中 `Shanghai` 与 `500`。

    表头推断出来的（`header_inferred`）就不加键：拿一行数据当列名会让文本变成
    `500: 380 | tier-2: tier-3` 这种胡说。退化成"逐行空格连接"更诚实。
    """
    matrix = table.matrix()
    if not matrix:
        return title or ""
    header = matrix[0] if not table.header_inferred else None
    lines: list[str] = []
    if title:
        lines.append(title)
    body = matrix[1:] if header is not None else matrix
    for row in body:
        if header is not None:
            pairs = [
                f"{key}: {value}"
                for key, value in zip(header, row, strict=False)
                if value and value != BLANK_CELL
            ]
            lines.append(" | ".join(pairs) if pairs else "")
        else:
            joined = " ".join(
                value for value in row if value and value != BLANK_CELL
            )
            lines.append(joined)
    return "\n".join(line for line in lines if line.strip())


def is_label_table(table: TableData) -> bool:
    """单行单列表格（§3.3 边界表）：这种"表格"其实是标签，该转成一句文本。."""
    return table.row_count <= 1 and table.col_count <= 1


__all__ = [
    "BLANK_CELL",
    "build_table",
    "cells_from_html",
    "clean_cell_text",
    "is_label_table",
    "parse_md",
    "render_html",
    "render_md",
    "table_content_text",
]
