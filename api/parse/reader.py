"""PDF 读取器：一页 pdfplumber Page -> 一个 `PageLayout`（详细设计.md §3.2）。.

把 `layout.py` 的零件（行、表格、图）组装成一页的四类区域，并处理三件跨页的事：

1. **页眉页脚要先攒齐所有页再判**。"同一行在一半以上页面出现"天然是跨页判定，
   所以流程是两趟：第一趟抽行（顺带记下每页的原文），第二趟判页眉页脚，
   然后把被判为页眉页脚的行从页面里摘出去。
2. **表格跨页续表要合并**（§3.3）。判据不是"表头相同"，而是"上一页的最后一个
   表格贴着页面底部、下一页的第一个表格贴着顶部、列数相同"——表头相同的两张
   独立表很常见（比如每页都印的表），按表头合并会把它们错误地并成一张。
3. **没有文字层的页走 OCR 兜底**（§3.2，可配置）。OCR 之后的行照样参与页眉页脚
   判定与切分——不参与的话，扫描件的页眉会整片进正文。

这一层不做切片：切片要知道权限、版本号、doc_key，那些是调用方的事
（`pipeline.py`）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.parse import layout as lay
from api.parse.geometry import PageSpace, Rect
from api.parse.model import (
    DocLayout,
    FigureData,
    FigureFailure,
    Line,
    PageLayout,
    Region,
    RegionKind,
    TableData,
)
from api.parse.ocr import OcrConfig, OcrEngine, ocr_lines, text_layer_missing

# 整页位图渲染器：入参是 1-based 页码，返回 PIL 图或 None（渲染不可用）。
# 抽成类型是为了让 OCR 与图裁片两条路都别去碰对方那套页对象——它们不通用。
PageRenderer = Callable[[int], "Any"]

# 续表合并：上一页表格的下边界要落在页面底部这个比例内，下一页表格的上边界要落在
# 顶部这个比例内。太严会漏（表格下面还有一行注释），太松会把两张独立的表并起来。
CONTINUATION_BOTTOM_RATIO = 0.75
CONTINUATION_TOP_RATIO = 0.25


@dataclass(frozen=True, slots=True)
class ReaderConfig:
    """读取参数。默认值都是"按 §3.2 的口径"，改它们等于改设计。."""

    ocr: OcrConfig = field(default_factory=OcrConfig)
    # 标题取"首页字号最大的一行"：不需要真正的版面分析（那要训练模型），
    # 实测制度文件的标题就是首页最大的那行。取不到就 None，不猜。
    detect_title: bool = True
    # 是否合并跨页续表。关掉用于"想看清原始切分"的调试。
    merge_continued_tables: bool = True


# 图区域 -> FigureData（裁图 + 转译 + 对账）。
# 返回 None 表示这条图判失败、不入库。
# 做成回调而不是在 reader 里直接实现：转译要调外部模型、裁图要 pdfium 渲染，
# 两者都是"可能没有"的能力（没配 VLM / 没装 pypdfium2），reader 不该被它们拖住。
FigureBuilder = Callable[["FigureBuildRequest"], "FigureBuildResult"]


@dataclass(frozen=True, slots=True)
class FigureBuildRequest:
    """交给 `FigureBuilder` 的一切：它自己决定怎么裁、怎么转译。."""

    page: Any
    space: PageSpace
    page_num: int
    detected: lay.DetectedFigure
    lines: tuple[Line, ...]


@dataclass(frozen=True, slots=True)
class FigureBuildResult:
    """`FigureBuilder` 的结果。失败时 `figure` 为空、`error` 非空。.

    `image_bytes` 是裁片的无损 WebP 字节。**图文件不在这里落盘**：文件名要用
    `chunk_id`，而 `chunk_id` 要等整页区域排完序、编号定下来才知道（图的序号是
    本页 regions 里的下标，排序之后才确定）。所以这里只交字节，落盘由 `pipeline`
    在算好编号之后做——两处都拼一遍文件名，迟早一处写错、库里 `image_path` 指向
    一个不存在的文件。
    """

    figure: Any | None = None
    error: str | None = None
    image_bytes: bytes | None = None

    @property
    def ok(self) -> bool:
        """Run ok."""
        return self.figure is not None


def read_pdf(
    path: Path,
    *,
    doc_key: str,
    config: ReaderConfig | None = None,
    ocr_engine: OcrEngine | None = None,
    figure_builder: FigureBuilder | None = None,
    page_renderer: PageRenderer | None = None,
) -> DocLayout:
    """读一份 PDF，产出 `DocLayout`（区域已分类、页眉页脚已剔除、续表已合并）。.

    `ocr_engine=None` 且 `config.ocr.enabled` 时，OCR 会被跳过并在每页
    `ocr_skipped_reason` 里记原因——不静默降级。

    `figure_builder=None` 时图区域**不进 regions**：图没法转译就不该入库
    （空转译的图切片会让检索命中一条没有内容的证据）。原因写进 `doc.warnings`
    与该页 `notes`，并计入 `DocLayout.figure_failures`。
    """
    import pdfplumber  # noqa: PLC0415 - 延迟：只有解析才需要

    config = config or ReaderConfig()
    doc = DocLayout(doc_key=doc_key, page_count=0)

    # 渲染页与解析页是两种对象：pdfplumber 给字符与线段，位图只有
    # pypdfium2 能出。OCR 需要位图，所以要么调用方给 `page_renderer`，
    # 要么这里按 `path` 现开一个 pypdfium2 文档。**必须在 `with` 之外
    # 持有**：pdfplumber 的上下文管理器关掉后文件句柄就失效，而 OCR
    # 可能在被关掉之后才跑。
    with (
        page_renderer_for(path, page_renderer) as renderer,
        pdfplumber.open(str(path)) as pdf,
    ):
        doc.page_count = len(pdf.pages)
        drafts: list[_PageDraft] = []
        for index, page in enumerate(pdf.pages):
            draft = _draft_page(
                page,
                page_num=index + 1,
                config=config,
                ocr_engine=ocr_engine,
                render_page=renderer,
                warnings=doc.warnings,
            )
            if figure_builder is not None:
                _build_figures(
                    draft, page=page, builder=figure_builder, doc=doc
                )
            drafts.append(draft)

        # 第二趟：跨页判定页眉页脚（只在正文行上判，表格单元格不参与）。
        verdicts = lay.classify_header_footer(
            [draft.body_lines for draft in drafts]
        )
        doc.pages = [
            _publish_page(draft, verdicts=verdicts) for draft in drafts
        ]

        if config.merge_continued_tables:
            _merge_continued_tables(doc)
        if config.detect_title:
            doc.title = detect_title(doc.pages)
    return doc


@contextmanager
def page_renderer_for(
    path: Path, renderer: PageRenderer | None
) -> Iterator[PageRenderer | None]:
    """给 `read_pdf` 用的整页渲染器：调用方给了就用，没给就按 `path` 开一个。.

    返回 `None` 表示"没法渲染"（比如没装 pypdfium2）。此时 OCR 若需要位图，
    `ocr_lines` 会通过 `page.render` 失败路径报出来——不会静默。
    """
    if renderer is not None:
        yield renderer
        return
    try:
        from pypdfium2 import (
            PdfDocument,  # noqa: PLC0415 - 延迟：只有要渲染才需要
        )
    except ImportError:
        yield None
        return

    from api.parse.figures import render_page, scale_for_dpi  # noqa: PLC0415

    document = PdfDocument(str(path))
    try:

        def _render(index: int) -> Any:
            """Run _render."""
            return render_page(document[index - 1], scale=scale_for_dpi(150))

        yield _render
    finally:
        document.close()


def _build_figures(
    draft: _PageDraft,
    *,
    page: Any,
    builder: FigureBuilder,
    doc: DocLayout,
) -> None:
    """对每个检测到的图区域跑 builder，成则挂上 FigureData，败则记账。.

    失败的图**从 `draft.figures` 里摘掉**，但**它占的版面范围继续算作已占用**
    （`blocked_rects` 不重建）。理由：图区域里那些行是刻度与类别标签
    （`100` / `50` / `0` / `Transport`），单独变成正文切片就是一堆脱离上下文的
    数字碎片——它们能命中"500 是多少"这种问句，却拿不出任何依据。宁可让这块地方
    没有切片，也不产出一批可被引用但没有意义的证据。
    """
    kept: list[lay.DetectedFigure] = []
    for detected in draft.figures:
        request = FigureBuildRequest(
            page=page,
            space=draft.space,
            page_num=draft.page_num,
            detected=detected,
            lines=tuple(draft.lines),
        )
        try:
            result = builder(request)
        except Exception as exc:  # noqa: BLE001 - 不许中断整页
            result = FigureBuildResult(error=f"{type(exc).__name__}: {exc}")
        if result.ok and isinstance(result.figure, FigureData):
            kept.append(
                lay.DetectedFigure(
                    rect=detected.rect,
                    core_rect=detected.core_rect,
                    kind=detected.kind,
                    source=detected.source,
                    data=result.figure,  # type: ignore[call-arg]
                )
            )
        else:
            reason = result.error or "unknown"
            doc.warnings.append(
                f"page {draft.page_num}: figure rejected ({reason}) at "
                f"({detected.core_rect.x0:.0f},{detected.core_rect.top:.0f})"
            )
            doc.figure_failures.append(
                FigureFailure(
                    page_num=draft.page_num,
                    rect=detected.core_rect,
                    reason=reason,
                )
            )
    draft.figures = kept


# --- 第一趟：抽行 / 表格 / 图 -------------------------------------------------


@dataclass(slots=True)
class _PageDraft:
    """一页的中间态：还没判页眉页脚。.

    `body_lines` 是**不含表格/图区域内**的所有行（含表格单元格里的行）——先用它
    判页眉页脚会误伤（表格里的 `500`/`tier-1` 跨页重复），所以判之前还要用
    `blocked_rects` 过滤一遍：见 `body_line_texts`。
    """

    page_num: int
    space: PageSpace
    lines: list[Line]
    tables: list[lay.DetectedTable]
    figures: list[lay.DetectedFigure]
    ocr_applied: bool = False
    ocr_skipped_reason: str | None = None
    char_count: int = 0
    has_large_image: bool = False
    blocked_rects: list[Rect] = field(default_factory=list)

    @property
    def body_lines(self) -> list[tuple[str, bool]]:
        """参与页眉页脚判定的行：`(文本, 是否在页边带内)`，**排除**表格/图。.

        表格单元格的行在 `lines` 里也有（它们就是普通 char），不排除的话
        `500`/`tier-1` 这种跨页重复的单元格内容会被判成页眉页脚删掉，
        表格就被打散了。
        """
        out: list[tuple[str, bool]] = []
        width, height = self.space.reading_size
        for line in self.lines:
            if line.source == "vertical":
                continue
            if _inside_block(line, self.blocked_rects):
                continue
            out.append((line.text, lay.in_margin_band(line, width, height)))
        return out


def _inside_block(line: Line, blocks: Sequence[Rect]) -> bool:
    """Run _inside_block."""
    cx = (line.rect.x0 + line.rect.x1) / 2.0
    cy = line.rect.center_y
    return any(block.contains_point(cx, cy) for block in blocks)


def _draft_page(
    page: Any,
    *,
    page_num: int,
    config: ReaderConfig,
    ocr_engine: OcrEngine | None,
    render_page: PageRenderer | None = None,
    warnings: list[str],
) -> _PageDraft:
    """Run _draft_page."""
    space = PageSpace.from_display(page)
    chars = lay.reading_chars(page, space)
    lines = lay.extract_lines(chars, page_num)

    # 先判分栏：栏缝要用来否掉"无线候选表其实是分栏正文"，也必须让正文按栏排序。
    columns = lay.detect_columns(lines)
    gutters: list[tuple[float, float]] = []
    if columns is not None:
        for left_col, right_col in zip(columns, columns[1:], strict=False):
            gutters.append((left_col[1], right_col[0]))

    # 旋转页上的文字被拆成单字符行，`text` 策略会凭对齐造出一张假表格
    # （实测：19 行 × 3 列的矩阵，每格是一小段词）。这份"表格"没有任何线支撑，
    # 直接跳过无线策略——旋转页的表格在 PDF 里通常仍带线（线也跟着转），
    # 所以只跑 `lines` 策略不会漏掉真表。
    if space.rotation == 0:
        tables = lay.find_tables(
            page, space, column_gutters=gutters, lines=lines
        )
    else:
        tables = lay.find_tables(
            page,
            space,
            strategies=lay.TABLE_STRATEGIES[:1],
            column_gutters=gutters,
            lines=lines,
        )
    figures = lay.find_figures(
        page, space, lines, taken=[table.rect for table in tables]
    )
    blocked = [table.rect for table in tables] + [
        figure.rect for figure in figures
    ]

    ocr_applied = False
    skip_reason: str | None = None
    if text_layer_missing(page, config.ocr):
        if ocr_engine is None:
            skip_reason = "engine_unavailable"
            warnings.append(
                f"page {page_num}: OCR needed but engine unavailable"
            )
        else:
            from api.parse.figures import scale_for_dpi  # noqa: PLC0415

            ocr_result = ocr_lines(
                page,
                space,
                ocr_engine,
                page_num=page_num,
                config=config.ocr,
                render_scale=scale_for_dpi(config.ocr.render_dpi),
                render=(
                    None
                    if render_page is None
                    else lambda: render_page(page_num)
                ),
            )
            if ocr_result:
                lines = lay.sort_in_columns([*lines, *ocr_result])
                ocr_applied = True
            else:
                skip_reason = "no_text_recognised"
                warnings.append(
                    f"page {page_num}: OCR produced no text above "
                    "confidence floor"
                )
    elif config.ocr.enabled:
        skip_reason = "text_layer_present"
    else:
        skip_reason = "disabled"

    return _PageDraft(
        page_num=page_num,
        space=space,
        lines=lines,
        tables=tables,
        figures=figures,
        ocr_applied=ocr_applied,
        ocr_skipped_reason=skip_reason,
        char_count=sum(
            1 for char in page.chars if char.get("text", "").strip()
        ),
        has_large_image=lay.has_large_image(page),
        blocked_rects=blocked,
    )


# --- 第二趟：发布为 PageLayout ------------------------------------------------


def _publish_page(
    draft: _PageDraft, *, verdicts: dict[str, lay.HeaderFooterVerdict]
) -> PageLayout:
    """把草稿变成 `PageLayout`：摘掉页眉页脚行，把剩余行分成四类区域。."""
    header_footer: list[Line] = []
    body: list[Line] = []
    for line in draft.lines:
        verdict = verdicts.get(line.text)
        if verdict is not None and verdict.is_header_footer:
            header_footer.append(line)
        else:
            body.append(line)

    table_by_rect = list(draft.tables)
    regions: list[Region] = []
    for detected in table_by_rect:
        rect = detected.rect
        regions.append(
            Region(
                kind=RegionKind.TABLE,
                page_num=draft.page_num,
                rect=rect,
                table=detected.table,
                notes=("continued",) if detected.table.continued else (),
            )
        )
    for figure in draft.figures:
        if figure.data is None:
            continue
        regions.append(
            Region(
                kind=RegionKind.FIGURE,
                page_num=draft.page_num,
                rect=figure.rect,
                figure=figure.data,
                notes=(f"figure_source:{figure.source}",),
            )
        )

    # 用 `draft.blocked_rects` 而不是从 tables/figures 重算：被判失败的图
    # 已经从 `draft.figures` 里摘掉，但它占的版面**仍算已占用**
    # （见 `_build_figures`）。重算会让图里的刻度与类别标签
    # （`100`/`50`/`0`/`Transport`）变成正文切片——一批脱离上下文的
    # 数字碎片，能命中问句却拿不出依据。
    #
    # **但这页走了 OCR 时，图区域不再遮挡**：扫描页的整页内容就是那张位图，
    # OCR 出来的行全在它里面。继续遮挡会让 OCR 白跑——实测第 2 页
    # `ocr_applied=True` 却一条切片都没有。遮挡的本意是"别把图上的刻度
    # 当正文"，而 OCR 出来的正是那页的正文，不是图的装饰。
    blocked = [] if draft.ocr_applied else list(draft.blocked_rects)
    for rect, paragraph_lines in lay.build_text_regions(body, blocked=blocked):
        text = lay.paragraph_text(paragraph_lines)
        if not text.strip():
            continue
        regions.append(
            Region(
                kind=RegionKind.TEXT,
                page_num=draft.page_num,
                rect=rect,
                text=text,
                lines=tuple(paragraph_lines),
            )
        )

    # 区域按阅读顺序排：先上后下、先左后右。切片编号按这个顺序生成，
    # 于是 `ch_000..ch_00N` 与人在页面上看到的顺序一致（引用编号好解释）。
    regions.sort(
        key=lambda region: (round(region.rect.top, 1), round(region.rect.x0, 1))
    )
    layout = PageLayout(
        page_num=draft.page_num,
        space=draft.space,
        regions=regions,
        header_footer_lines=header_footer,
        ocr_applied=draft.ocr_applied,
        ocr_skipped_reason=draft.ocr_skipped_reason,
        char_count=draft.char_count,
        has_large_image=draft.has_large_image,
    )
    layout.warnings = []
    return layout


# --- 续表合并 ----------------------------------------------------------------


def _merge_continued_tables(doc: DocLayout) -> None:
    r"""跨页续表：标出"这张表是上一页某张表的延续"，**不把行搬过来**。.

    ### 为什么不做"按表号合并成一张"

    §3.3 的处理是"按表号合并；合不了标'续表'"。这里只做后半句，理由是本卡的
    验收硬线：**bbox 可映射原页**。把第 2 页的行并进第 1 页那条切片，它的
    `page_num=1` / `bbox=第1页那块` 就与 `content_text` 里的第 2 页数据
    不一致——用户点角标会跳到第 1 页并框住一个不含该数字的区域。那比
    "表被切成两条"糟得多，因为切开的表在检索时仍是两条独立证据（各自都能
    命中），而错的 bbox 是**假溯源**。

    合并本身在下游很容易做（`continued_of` 已经把它指向被延续的 chunk），
    而 bbox 一旦错，前端拿到的是无法修复的数据。

    判据不是"表头相同"（每页都印同样的表头很常见，那会把两张独立的表并起来），
    而是"上一页的表格贴着页面底部、当前页的表格贴着顶部、列数相同"。
    """
    for previous, current in zip(doc.pages, doc.pages[1:], strict=False):
        prev_tables = previous.regions_of(RegionKind.TABLE)
        curr_tables = current.regions_of(RegionKind.TABLE)
        if not prev_tables or not curr_tables:
            continue
        prev_table = prev_tables[-1]
        curr_table = curr_tables[0]
        if prev_table.table is None or curr_table.table is None:
            continue
        if prev_table.table.col_count != curr_table.table.col_count:
            continue
        tail = prev_table.rect.bottom / previous.page_height
        head = curr_table.rect.top / current.page_height
        if tail < CONTINUATION_BOTTOM_RATIO or head > CONTINUATION_TOP_RATIO:
            continue
        table = curr_table.table
        _replace_table(
            current,
            curr_table,
            TableData(
                cells=table.cells,
                row_count=table.row_count,
                col_count=table.col_count,
                html=table.html,
                md=table.md,
                header_inferred=table.header_inferred,
                continued=True,
            ),
            notes=(f"continued_of:page{previous.page_num}",),
        )


def _replace_table(
    page: PageLayout,
    target: Region,
    table: TableData,
    *,
    notes: tuple[str, ...] = (),
) -> None:
    """Run _replace_table."""
    for index, region in enumerate(page.regions):
        if region is target:
            page.regions[index] = Region(
                kind=region.kind,
                page_num=region.page_num,
                rect=region.rect,
                table=table,
                notes=notes,
            )
            return


# --- 标题 -------------------------------------------------------------------


def detect_title(pages: Sequence[PageLayout]) -> str | None:
    """标题 = 首页出现在页面顶部（上 20%）且**字号最大**的那一行。.

    两条边界（都是实测踩出来的）：
      * 只看首页顶部，不取"全文最大字号"——有些文件的页眉用了超大字号，
        那会变成标题；
      * 字号并列时取**最长**的那行：夹具里 `Section 2. Lodging limits by city`
        （10pt）与 `Table 1. Lodging caps`（9pt）字号不同，取 max 正确；但很多
        文件的标题与副标题同字号，取最短会得到 `2025` 这种副标题碎片。

    取不到就返回 None——猜标题会让"这份文档叫什么"变成一个假事实。
    """
    if not pages:
        return None
    first = pages[0]
    candidates: list[Line] = []
    for region in first.regions_of(RegionKind.TEXT):
        for line in region.lines:
            if (
                line.rect.top <= first.page_height * 0.2
                and len(line.text.strip()) >= 4
            ):
                candidates.append(line)
    if not candidates:
        return None
    best = max(
        candidates, key=lambda line: (round(line.font_size, 1), len(line.text))
    )
    return (best.text.strip() if best.text.strip() else None)


__all__ = [
    "CONTINUATION_BOTTOM_RATIO",
    "CONTINUATION_TOP_RATIO",
    "ReaderConfig",
    "detect_title",
    "read_pdf",
]
