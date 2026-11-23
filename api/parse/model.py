"""版面对象模型（详细设计.md §3.2 / §3.3 / §3.4 / §3.5）。

四个层级，上一层只依赖下一层，方便各自单测：

    Line       一行文字（原文 + 矩形 + 字号）
    Region     一块版面区域：正文 / 表格 / 图 / 页眉页脚
    PageLayout 一页：区域列表 + 页眉页脚行 + 是否 OCR 过 + 本页坐标系
    DocLayout  整份文档：页列表 + 标题 + 警告

### 坐标口径（读这块之前先看这个）

**模型内部的矩形一律是「阅读空间」**：未旋转、左上原点、文字横着读的那个空间。
旋转页（`/Rotate 90` 等）的 pdfplumber 坐标是转过的，在那里一行横排文字会变成
几十个各自独立的单字符行（实测），行聚类、分栏、段落全都做不了。

**对外输出的 `bbox` 一律是「显示空间」归一化坐标**（前端画框、PDF.js viewport 都认
这个）。两者之间只由 `PageSpace` 换算一次：`Region.bbox()` 与 `PageLayout.bbox_for()`
是唯一的出口。

为什么不做成"模型里存显示空间，需要时转阅读空间"：那要在每个聚类函数里各转一次，
而漏转一处只在旋转页上出错——这是最难被测试发现的一类 bug。转过一次、存一处，
不给自己留漏的机会。

### 为什么不把 chunk 放进来

切片是解析结果的下游（还要叠权限、版本号、父子关系），放同一个模块会让"解析"
与"入库"的边界糊掉。切片模型在 `api/parse/chunking.py`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Sequence

from api.parse.geometry import BBox, PageSpace, Rect, iou, normalize, to_pixels, union_rects

# 文本清洗后为空的行不留：`chunks.content_text` 有非空 CHECK，空切片进不了库。
MIN_LINE_CHARS = 1


class RegionKind(StrEnum):
    """§3.2 的四类区域。前三类进库，页眉页脚丢掉。"""

    TEXT = "text"
    TABLE = "table"
    FIGURE = "figure"
    HEADER_FOOTER = "header_footer"

    @property
    def indexable(self) -> bool:
        """能不能变成切片。页眉页脚不能——它是噪声，进了库会让检索命中"ACME 内部文件"。"""
        return self is not RegionKind.HEADER_FOOTER


@dataclass(frozen=True, slots=True)
class Line:
    """一行文字（阅读空间）。

    `font_size` 取字符高度的中位数（判断标题层级与行距容差）。
    `source` 三个值：`pdf`（有文字层）、`ocr`（识别出来的，坐标有 ±2pt 抖动）、
    `vertical`（竖排字符，单独处理）。下游据此决定要不要放宽容差。
    """

    text: str
    rect: Rect
    page_num: int
    font_size: float = 0.0
    source: str = "pdf"
    confidence: float = 1.0

    @property
    def is_blank(self) -> bool:
        return len(self.text.strip()) < MIN_LINE_CHARS

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "rect": self.rect.as_tuple(),
            "page_num": self.page_num,
            "font_size": round(self.font_size, 2),
            "source": self.source,
            "confidence": round(self.confidence, 3),
        }


@dataclass(frozen=True, slots=True)
class TableCell:
    """一个单元格。合并信息只有 `row_span` / `col_span`。

    `table_cells` 矩阵里的值靠展开规则（左上角为值、其余格复制该值，
    见 `tables.expand_to_matrix`）。
    """

    row: int
    col: int
    text: str
    is_header: bool = False
    row_span: int = 1
    col_span: int = 1


@dataclass(frozen=True, slots=True)
class TableData:
    """表格三表示一起产出，是**同一个对象**的三个投影。

    分开产出（解析一次出 cells、另一次出 html）就一定会分叉。这里 `html` 与 `md`
    都由 `cells` 派生，而 `cells` 可以从 `html` 反解，这条往返就是"三表示一致"的
    可执行定义。
    """

    cells: tuple[TableCell, ...]
    row_count: int
    col_count: int
    html: str
    md: str
    header_inferred: bool = False
    continued: bool = False

    def cells_json(self) -> list[dict[str, Any]]:
        """落库形状（jsonb 数组，按 row/col 排序）。"""
        return [
            {
                "row": cell.row,
                "col": cell.col,
                "text": cell.text,
                "is_header": cell.is_header,
                "row_span": cell.row_span,
                "col_span": cell.col_span,
            }
            for cell in self.cells
        ]

    def matrix(self) -> list[list[str]]:
        """展开后的二维矩阵（合并格已复制、空格补 `-`）。"""
        return expand_to_matrix(self.cells, self.row_count, self.col_count)


@dataclass(frozen=True, slots=True)
class FigureData:
    """图表。

    `transcript` 是 VLM 的**结构化**输出（不是自由文本）：只有结构化之后"数值对账"
    才有东西可对。`raw_markdown` 保留模型原文，进 `content_text` 供检索与引用。

    `image_bytes` 是裁片的无损 WebP 字节，**不进库**：它是 `FigureBuilder` 交给
    `pipeline` 用于落盘的一次性数据（`chunks.image_path` 只存相对路径）。
    """

    image_path: str
    width_px: int
    height_px: int
    dpi: float
    raw_markdown: str = ""
    kind: str = "unknown"
    axes: dict[str, Any] = field(default_factory=dict)
    series: tuple[dict[str, Any], ...] = ()
    labels_in_region: tuple[str, ...] = ()
    numbers_in_region: tuple[float, ...] = ()
    bar_heights: tuple[float, ...] = ()
    legend_missing: bool = False
    image_bytes: bytes | None = None


@dataclass(frozen=True, slots=True)
class Region:
    """一块版面区域。矩形是**阅读空间**（见模块 docstring）。"""

    kind: RegionKind
    page_num: int
    rect: Rect
    text: str = ""
    lines: tuple[Line, ...] = ()
    table: TableData | None = None
    figure: FigureData | None = None
    confidence: float = 1.0
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind is RegionKind.TABLE and self.table is None:
            raise ValueError("table region must carry TableData")
        if self.kind is RegionKind.FIGURE and self.figure is None:
            raise ValueError("figure region must carry FigureData")
        if self.kind is RegionKind.TEXT and not self.text.strip():
            raise ValueError("text region must carry non-blank text")

    def bbox(self, space: PageSpace) -> BBox:
        """阅读空间矩形 -> 显示空间归一化 bbox。这是**唯一**的坐标出口。"""
        width, height = space.display_size
        return normalize(space.reading_to_display(self.rect), width, height)

    def bbox_pixels(self, space: PageSpace, pixel_width: int, pixel_height: int) -> tuple[int, int, int, int]:
        return to_pixels(self.bbox(space), pixel_width, pixel_height)


@dataclass(slots=True)
class PageLayout:
    """一页的解析结果。`space` 记录本页旋转与用户空间尺寸，bbox 全靠它换算。"""

    page_num: int
    space: PageSpace
    regions: list[Region] = field(default_factory=list)
    header_footer_lines: list[Line] = field(default_factory=list)
    ocr_applied: bool = False
    ocr_skipped_reason: str | None = None
    char_count: int = 0
    has_large_image: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def rotation(self) -> int:
        return self.space.rotation

    @property
    def page_width(self) -> float:
        """阅读空间宽（= 未旋转页面宽）。归一化坐标请用 `bbox_for`，别自己乘。"""
        return self.space.reading_size[0]

    @property
    def page_height(self) -> float:
        return self.space.reading_size[1]

    @property
    def is_scanned(self) -> bool:
        """这一页是不是"没有文字层"。

        判据不是"有没有图片"（图文混排的正文页也有图片），而是"几乎没有字符且有
        覆盖大半页的图"。阈值给得很宽（10 个字符）：预先 OCR 过的扫描件页眉会有
        少量文字层，那不算"有文字层"。
        """
        return self.char_count < 10 and self.has_large_image

    def regions_of(self, kind: RegionKind) -> list[Region]:
        return [region for region in self.regions if region.kind is kind]

    def bbox_for(self, rect: Rect) -> BBox:
        width, height = self.space.display_size
        return normalize(self.space.reading_to_display(rect), width, height)


@dataclass(slots=True)
class DocLayout:
    """整份文档的解析结果。`warnings` 给人看（日志、验收报告），不进库。"""

    doc_key: str
    page_count: int
    pages: list[PageLayout] = field(default_factory=list)
    title: str | None = None
    warnings: list[str] = field(default_factory=list)
    # 被拒的图（转译失败 / 数值对账不过）。**不入库**，但必须留下可查的记录：
    # "这张图为什么没进来"是验收时要回答的问题，光有 warnings 字符串不够。
    figure_failures: list["FigureFailure"] = field(default_factory=list)

    def iter_regions(
        self, *, kinds: Iterable[RegionKind] | None = None
    ) -> Iterable[tuple[PageLayout, Region]]:
        allowed = None if kinds is None else set(kinds)
        for page in self.pages:
            for region in page.regions:
                if allowed is None or region.kind in allowed:
                    yield page, region

    def count(self, kind: RegionKind) -> int:
        return sum(len(page.regions_of(kind)) for page in self.pages)

    def region_by_id(self) -> dict[tuple[int, int], Region]:
        """{(page_num, 序号): region}，序号是本页 regions 里的下标。"""
        return {
            (page.page_num, index): region
            for page in self.pages
            for index, region in enumerate(page.regions)
        }


# --- 矩阵展开 / 比对 ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FigureFailure:
    """一条被拒的图。`reason` 是机器可读前缀 + 细节（`reconcile: value_outside_axis…`）。"""

    page_num: int
    rect: Rect
    reason: str


def expand_to_matrix(
    cells: Sequence[TableCell], row_count: int, col_count: int, *, blank: str = "-"
) -> list[list[str]]:
    """把（可能带合并的）单元格列表展开成 row_count × col_count 矩阵。

    §3.3 的三条展开规则：
      1. 合并单元格按"左上角为值、其余格复制该值"展开；
      2. 空单元格填 `-`；
      3. 表头照留（Markdown 不支持合并表头，展开成重复列名）。

    规则 3 与规则 1 是同一件事：表头跨列时被覆盖的格复制左上的值，于是 md 里会
    看到 `Monthly limit | Monthly limit`。填 `-` 会让人以为那格是空的。
    """
    grid: list[list[str | None]] = [[None] * col_count for _ in range(row_count)]
    for cell in cells:
        if not (0 <= cell.row < row_count and 0 <= cell.col < col_count):
            raise ValueError(f"cell out of bounds: row={cell.row} col={cell.col}")
        text = cell.text.strip() or blank
        row_end = min(row_count, cell.row + max(1, cell.row_span))
        col_end = min(col_count, cell.col + max(1, cell.col_span))
        for r in range(cell.row, row_end):
            for c in range(cell.col, col_end):
                if grid[r][c] is None:
                    grid[r][c] = text
    return [[blank if value is None else value for value in row] for row in grid]


def matrix_equal(left: Sequence[Sequence[str]], right: Sequence[Sequence[str]]) -> bool:
    if len(left) != len(right):
        return False
    return all(
        len(lrow) == len(rrow) and list(lrow) == list(rrow)
        for lrow, rrow in zip(left, right)
    )


def first_difference(
    left: Sequence[Sequence[str]], right: Sequence[Sequence[str]]
) -> str | None:
    """两矩阵的第一处差异（给人看的诊断）。相等返回 None。"""
    if len(left) != len(right):
        return f"row_count {len(left)} != {len(right)}"
    for r, (lrow, rrow) in enumerate(zip(left, right)):
        if len(lrow) != len(rrow):
            return f"row {r}: col_count {len(lrow)} != {len(rrow)}"
        for c, (lval, rval) in enumerate(zip(lrow, rrow)):
            if lval != rval:
                return f"({r},{c}): {lval!r} != {rval!r}"
    return None


# --- 区域代数 ----------------------------------------------------------------


def subtract_regions(rect: Rect, taken: Sequence[Rect]) -> list[Rect]:
    """`rect` 减去一组已占用矩形，返回剩下的矩形块（按横向条带切）。

    用途：算出"表格与图之外还剩哪块地方是正文"。不做完整矩形布尔运算——
    正文区域本来就是用来切行的，切成横向条带最直观，也最不容易切出碎块。
    """
    pieces = [rect]
    for blocker in taken:
        next_pieces: list[Rect] = []
        for piece in pieces:
            if piece.intersection(blocker) is None:
                next_pieces.append(piece)
                continue
            if blocker.top > piece.top:
                next_pieces.append(
                    Rect(piece.x0, piece.top, piece.x1, min(blocker.top, piece.bottom))
                )
            if blocker.bottom < piece.bottom:
                next_pieces.append(
                    Rect(piece.x0, max(blocker.bottom, piece.top), piece.x1, piece.bottom)
                )
        pieces = next_pieces
    return [piece for piece in pieces if piece.width > 1.0 and piece.height > 1.0]


def dedupe_overlapping(rects: Sequence[Rect], *, threshold: float = 0.9) -> list[Rect]:
    """去掉几乎相同的矩形（同一张图被识别成两个 image 对象是常见现象）。"""
    kept: list[Rect] = []
    for rect in rects:
        if any(iou_rect(rect, other) >= threshold for other in kept):
            continue
        kept.append(rect)
    return kept


def iou_rect(a: Rect, b: Rect) -> float:
    return iou(a.as_tuple(), b.as_tuple())


def merge_nearby(rects: Sequence[Rect], *, max_gap: float) -> list[Rect]:
    """把间距小于 `max_gap` 的矩形聚成簇（矢量图的碎片合成一块区域）。

    每轮从 `remaining` 里真删已并入的项；不在遍历原列表的同时 pop——那样索引会失效
    （`IndexError: pop index out of range`，本模块第一版就是这么错的）。
    """
    clusters: list[Rect] = []
    remaining = list(rects)
    while remaining:
        current = remaining.pop(0)
        changed = True
        while changed:
            changed = False
            kept: list[Rect] = []
            for candidate in remaining:
                if _gap(current, candidate) <= max_gap:
                    current = current.union(candidate)
                    changed = True
                else:
                    kept.append(candidate)
            remaining = kept
        clusters.append(current)
    return clusters


def _gap(a: Rect, b: Rect) -> float:
    dx = max(0.0, max(a.x0, b.x0) - min(a.x1, b.x1))
    dy = max(0.0, max(a.top, b.top) - min(a.bottom, b.bottom))
    return max(dx, dy)


def union_or_none(rects: Sequence[Rect]) -> Rect | None:
    return union_rects(rects)


__all__ = [
    "DocLayout",
    "FigureData",
    "FigureFailure",
    "Line",
    "PageLayout",
    "Region",
    "RegionKind",
    "TableData",
    "TableCell",
    "dedupe_overlapping",
    "expand_to_matrix",
    "first_difference",
    "matrix_equal",
    "merge_nearby",
    "subtract_regions",
    "union_or_none",
]
