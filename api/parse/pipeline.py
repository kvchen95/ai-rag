"""摄取流水线：一个待转正版本 -> 一批待入库切片（详细设计.md §3.2–§3.6）。

把前面几层串起来，并且**做三件别处做不了的事**：

1. **chunk_id 一次分配、按阅读顺序**。子片叫 `{version_id}#c{page:03d}{n:03d}`，
   表格 `#t{page:03d}{n:03d}`，图 `#f{page:03d}{n:03d}`。顺序稳定意味着重跑
   同一份文件得到同一批编号——比对两次解析结果时才看得出"有没有变"。
2. **图的转译与对账在这里被拦住**。`FigureBuilder` 返回失败时这张图**不进切片**，
   记进 `doc.figure_failures` 与 `ParseOutcome.rejected_figures`。这是验收第二条
   "图转译数值对不上标失败不入库"的落点。
3. **六项随行信息一次补齐**。权限来自调用方（`Acl`），日期来自切片文本 +
   文档级兜底，版本号来自 A1 的 `version_id`，页码/坐标/类型来自区域。

`ParseOutcome.chunks` 里的每一条都已经过 `require_complete()`：字段缺一项就在
这里炸，不会流到入库那一步。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from api.parse import chunking as ck
from api.parse.dates import BusinessDate, document_date, find_date, merge_dates
from api.parse.model import (
    DocLayout,
    FigureData,
    FigureFailure,
    PageLayout,
    Region,
    RegionKind,
)
from api.parse.reader import FigureBuilder, ReaderConfig, read_pdf
from api.parse.tables import table_content_text
from api.parse.figures_transcribe import figure_content_text


def _region_counts(page: PageLayout) -> dict[str, int]:
    """一页各类区域的条数，用于验收报告。

    页眉页脚**不在** `regions` 里（它们是行不是区域），但报告必须把它们算进来：
    否则"删了 3 行页眉"会在报告里显示 `header_footer: 0`，验收的人在读到
    "没有页眉页脚"时会以为这块根本没实现——报告说假话比不报告更糟。

    页眉页脚行是按"行"记的，与其他区域的"块"不同，所以计数口径也不同：
    这里如实返回行数，字段名 `header_footer_lines` 说明它是行数。
    """
    counts = {kind.value: len(page.regions_of(kind)) for kind in RegionKind}
    counts["header_footer_lines"] = len(page.header_footer_lines)
    return counts


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """切片参数。默认值就是 §3.5 的口径；改它们要同步改验收用例。"""

    child_min_chars: int = ck.CHILD_MIN_CHARS
    child_max_chars: int = ck.CHILD_MAX_CHARS
    child_overlap: int = ck.CHILD_OVERLAP_CHARS
    parent_max_chars: int = ck.PARENT_MAX_CHARS
    # 切父片：每个正文段落区域单独成父片（§3.5「按章节标题切，不按字数硬切」）。
    # 一段超长时 `parent_slices` 会再切一层。
    parent_per_region: bool = True


@dataclass(slots=True)
class ParseOutcome:
    """一次解析的完整结果。`chunks` 已通过六项校验。"""
    doc: DocLayout
    chunks: list[ck.Chunk] = field(default_factory=list)
    rejected_figures: list[FigureFailure] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for chunk in self.chunks:
            tally[chunk.chunk_type] = tally.get(chunk.chunk_type, 0) + 1
        return tally

    def as_report(self) -> dict[str, Any]:
        """给接口/脚本的摘要（不含正文）。"""
        return {
            "doc_key": self.doc.doc_key,
            "page_count": self.doc.page_count,
            "title": self.doc.title,
            "chunks": self.counts(),
            "total_chunks": len(self.chunks),
            "rejected_figures": len(self.rejected_figures),
            "warnings": [*self.doc.warnings, *self.warnings],
            "pages": [
                {
                    "page_num": page.page_num,
                    "rotation": page.rotation,
                    "regions": _region_counts(page),
                    "ocr_applied": page.ocr_applied,
                    "ocr_skipped_reason": page.ocr_skipped_reason,
                }
                for page in self.doc.pages
            ],
        }


class MissingAcl(ValueError):
    """调用方没给权限。默认放行会让每份材料都变成"全公司可见"。"""


def parse_document(
    path: Path,
    *,
    doc_key: str,
    version_id: str,
    acl: ck.Acl,
    reader_config: ReaderConfig | None = None,
    pipeline_config: PipelineConfig | None = None,
    ocr_engine: Any | None = None,
    figure_builder: FigureBuilder | None = None,
    figures_dir: Path | None = None,
) -> ParseOutcome:
    """解析一份 PDF 并切成待入库切片。

    `acl` 必须显式给出（`Acl()` 的空 ACL 会让所有切片在入库时被六项校验拦下——
    这是有意的：解析器不知道业务权限，猜一个"公开"比拒绝入库危险得多）。

    `figures_dir` 是图文件的落盘根目录。给了才写图：`figure_builder` 交回字节、
    这里在**算出 chunk_id 之后**写文件并回填 `image_path`。不给就只在内存里过一遍
    转译与对账（测试与 dry-run 用），此时图切片仍入库、`image_path` 指向未来的路径。
    """
    return _parse(
        path,
        doc_key=doc_key,
        version_id=version_id,
        acl=acl,
        reader_config=reader_config,
        pipeline_config=pipeline_config,
        ocr_engine=ocr_engine,
        figure_builder=figure_builder,
        figures_dir=figures_dir,
    )


def _parse(
    path: Path,
    *,
    doc_key: str,
    version_id: str,
    acl: ck.Acl,
    reader_config: ReaderConfig | None,
    pipeline_config: PipelineConfig | None,
    ocr_engine: Any | None,
    figure_builder: FigureBuilder | None,
    figures_dir: Path | None,
) -> ParseOutcome:
    config = pipeline_config or PipelineConfig()
    doc = read_pdf(
        path,
        doc_key=doc_key,
        config=reader_config,
        ocr_engine=ocr_engine,
        figure_builder=figure_builder,
    )
    if acl.is_empty():
        raise MissingAcl(
            "acl must be explicit: an empty ACL denies everyone, which is never intended"
        )
    outcome = _build_chunks(
        doc,
        version_id=version_id,
        acl=acl,
        config=config,
        figures_dir=figures_dir,
    )
    return outcome


def _build_chunks(
    doc: DocLayout,
    *,
    version_id: str,
    acl: ck.Acl,
    config: PipelineConfig,
    figures_dir: Path | None,
) -> ParseOutcome:
    fallback_date = document_date(doc.title, [_first_page_text(doc)])
    outcome = ParseOutcome(doc=doc, rejected_figures=list(doc.figure_failures))
    chunks: list[ck.Chunk] = []
    for page in doc.pages:
        for index, region in enumerate(page.regions):
            if region.kind is RegionKind.HEADER_FOOTER:
                continue
            date = merge_dates(
                find_date(region.text or _region_text(region)), fallback_date
            )
            if region.kind is RegionKind.TEXT:
                chunks.extend(
                    _text_chunks(
                        page,
                        region,
                        doc_key=doc.doc_key,
                        version_id=version_id,
                        acl=acl,
                        date=date,
                        config=config,
                        index=index,
                    )
                )
            elif region.kind is RegionKind.TABLE:
                chunks.append(
                    _table_chunk(
                        page,
                        region,
                        version_id=version_id,
                        acl=acl,
                        date=date,
                        doc_key=doc.doc_key,
                        index=index,
                    )
                )
            elif region.kind is RegionKind.FIGURE:
                chunk = _figure_chunk(
                    page,
                    region,
                    version_id=version_id,
                    acl=acl,
                    date=date,
                    doc_key=doc.doc_key,
                    index=index,
                    figures_dir=figures_dir,
                    warnings=outcome.warnings,
                )
                if chunk is not None:
                    chunks.append(chunk)
    # 一次全校验：缺字段在这里炸，不流到入库。
    outcome.chunks = ck.validate_chunks(chunks)
    return outcome


def _first_page_text(doc: DocLayout) -> str:
    if not doc.pages:
        return ""
    parts = [
        region.text
        for region in doc.pages[0].regions
        if region.kind is RegionKind.TEXT
    ]
    return "\n".join(parts)


def _region_text(region: Region) -> str:
    """区域的纯文本：正文取 text，表格取 md，图取转译原文。"""
    if region.kind is RegionKind.TABLE and region.table is not None:
        return region.table.md
    if region.kind is RegionKind.FIGURE and region.figure is not None:
        return region.figure.raw_markdown
    return region.text


# --- 正文：父子两级 ---------------------------------------------------------


def _text_chunks(
    page: PageLayout,
    region: Region,
    *,
    doc_key: str,
    version_id: str,
    acl: ck.Acl,
    date: BusinessDate,
    config: PipelineConfig,
    index: int,
) -> list[ck.Chunk]:
    """一个正文区域 -> 一个父片 + 若干子片。

    父片与子片**都是这个区域的文本**：父片整段（供模型作答），子片是它的切分
    （供检索命中）。子片 `parent_id` 指向父片（附录 A.3）。

    区域只有一行时不出父片：一个父片等于自己的子片，是纯粹的重复行，
    徒增切片规模（§12.4 的容量估算按子片算）。
    """
    bbox = region.bbox(page.space)
    line_bboxes = [page.bbox_for(line.rect) for line in region.lines]
    parent_id = ck.parent_id_for(version_id, page_num=page.page_num, index=index)

    out: list[ck.Chunk] = []
    parent_pieces = (
        ck.parent_slices(region.text, max_chars=config.parent_max_chars)
        if config.parent_per_region and len(region.lines) > 1
        else []
    )
    for piece_index, piece in enumerate(parent_pieces):
        out.append(
            ck.build_parent_chunk(
                doc_key=doc_key,
                version_id=version_id,
                region=Region(
                    kind=region.kind,
                    page_num=region.page_num,
                    rect=region.rect,
                    text=piece,
                    lines=region.lines,
                ),
                page_bboxes=line_bboxes or [bbox],
                acl=acl,
                date=date,
                chunk_id=ck.chunk_id_for(
                    version_id,
                    page_num=page.page_num,
                    kind="parent",
                    index=index,
                    part=piece_index,
                ),
                notes=_region_notes(region),
            )
        )

    # 子片编号一次算齐：编号是确定性短码（文件系统安全），"第几片"只有切分后才知道。
    child_count = ck.child_piece_count(region)
    child_ids = [
        ck.chunk_id_for(version_id, page_num=page.page_num, kind="child", index=index, part=part)
        for part in range(child_count)
    ]
    out.extend(
        ck.build_child_chunks(
            doc_key=doc_key,
            version_id=version_id,
            region=region,
            page_bboxes=line_bboxes or [bbox],
            acl=acl,
            date=date,
            parent_id=parent_id if parent_pieces else None,
            child_ids=child_ids,
            notes=_region_notes(region),
        )
    )
    return out


def _region_notes(region: Region) -> tuple[str, ...]:
    """把"这段文字是从哪来的"传给切片，让下游能区分 OCR 与文字层。

    为什么必须传下去：OCR 有错字（实测 `CNY 500` → `CNY 5o0`），而数值校验与
    引用展示都该知道这条证据的来源。只在 `Line.source` 上标记等于没标记——
    切片入库后 `Line` 就没了，下游（A3 翻牌、检索、前端引用）只看得到 `Chunk`。
    """
    sources = {line.source for line in region.lines}
    if sources == {"ocr"}:
        return ("source:ocr",)
    if "ocr" in sources:
        # 混合页（文字层 + OCR）：标注清楚，别让下游以为整段都是识别结果
        return ("source:mixed",)
    return ()


def _table_chunk(
    page: PageLayout,
    region: Region,
    *,
    version_id: str,
    acl: ck.Acl,
    date: BusinessDate,
    doc_key: str,
    index: int,
) -> ck.Chunk:
    table = region.table
    assert table is not None
    caption = _caption_for(region, page)
    unit = _unit_for(region, page)
    title = caption
    if unit:
        title = (title + " " + unit).strip() if title else unit
    content = table_content_text(table, title=title)
    if not content.strip():
        content = title or "table (empty)"
    chunk_id = ck.chunk_id_for(
        version_id, page_num=page.page_num, kind="table", index=index
    )
    table_md = table.md
    return ck.build_table_chunk(
        doc_key=doc_key,
        version_id=version_id,
        region=region,
        bbox=region.bbox(page.space),
        acl=acl,
        date=date,
        chunk_id=chunk_id,
        content_text=content,
        table_md=table_md,
    )


def _figure_chunk(
    page: PageLayout,
    region: Region,
    *,
    version_id: str,
    acl: ck.Acl,
    date: BusinessDate,
    doc_key: str,
    index: int,
    figures_dir: Path | None,
    warnings: list[str],
) -> ck.Chunk | None:
    """图切片：编号先定、图文件后写（文件名与 `image_path` 用同一个编号）。

    `image_bytes` 由 `FigureBuilder` 交回（转译与对账都过了才有字节）。没配
    `figures_dir` 时不写盘但仍给 `image_path`——那条路径是"将来落盘时它会在这里"，
    入库的版本要等真正的写盘流程跑过才可见（`figures/` 与 PG 由同一个作业写）。
    """
    figure = region.figure
    if figure is None:
        return None
    chunk_id = ck.chunk_id_for(
        version_id, page_num=page.page_num, kind="figure", index=index
    )
    image_path = ck.image_path_for(chunk_id)
    image_bytes = getattr(figure, "image_bytes", None)
    if figures_dir is not None and image_bytes:
        try:
            figures_dir.mkdir(parents=True, exist_ok=True)
            (figures_dir / f"{chunk_id}.webp").write_bytes(image_bytes)
        except OSError as exc:
            # 写图失败就不出这条切片：库里有行、盘上没图 = 角标点开是 404。
            warnings.append(f"page {page.page_num}: figure write failed ({exc})")
            return None
    figure = FigureData(
        image_path=image_path,
        width_px=figure.width_px,
        height_px=figure.height_px,
        dpi=figure.dpi,
        raw_markdown=figure.raw_markdown,
        kind=figure.kind,
        axes=figure.axes,
        series=figure.series,
        labels_in_region=figure.labels_in_region,
        numbers_in_region=figure.numbers_in_region,
        bar_heights=figure.bar_heights,
        legend_missing=figure.legend_missing,
    )
    caption = _caption_for(region, page)
    content = figure_content_text(figure, caption=caption)
    if not content.strip():
        return None
    return ck.build_figure_chunk(
        doc_key=doc_key,
        version_id=version_id,
        region=Region(
            kind=region.kind,
            page_num=region.page_num,
            rect=region.rect,
            figure=figure,
            notes=region.notes,
        ),
        bbox=region.bbox(page.space),
        acl=acl,
        date=date,
        chunk_id=chunk_id,
        content_text=content,
    )


def _caption_for(region: Region, page: PageLayout) -> str | None:
    """图注/表题：区域上方最近的一行以 `Table`/`Figure`/`表`/`图` 开头的文字。

    只在同页找，且必须在区域上方（图注在下的情况也有，但那种排版在本期样本里
    没有出现，猜它会把正文第一行当图注）。
    """
    keywords = ("table ", "figure ", "表 ", "图 ", "表：", "图：", "表1", "图1")
    best: tuple[float, str] | None = None
    for other in page.regions:
        if other is region or other.kind is not RegionKind.TEXT:
            continue
        if other.rect.bottom > region.rect.top + 2:
            continue
        for line in other.lines:
            text = line.text.strip()
            if not text:
                continue
            lowered = text.lower()
            if any(lowered.startswith(keyword) for keyword in keywords):
                gap = region.rect.top - line.rect.bottom
                if best is None or gap < best[0]:
                    best = (gap, text)
    return best[1] if best is not None else None


def _unit_for(region: Region, page: PageLayout) -> str | None:
    """表题附近的单位说明，如 "单位：元"。"""
    best: tuple[float, str] | None = None
    for other in page.regions:
        if other is region or other.kind is not RegionKind.TEXT:
            continue
        if other.rect.bottom > region.rect.top + 2:
            continue
        for line in other.lines:
            text = line.text.strip()
            if "单位" not in text:
                continue
            gap = region.rect.top - line.rect.bottom
            if best is None or gap < best[0]:
                best = (gap, text)
    return best[1] if best is not None else None


def assign_doc_key(chunks: Iterable[ck.Chunk], doc_key: str) -> list[ck.Chunk]:
    """把 `doc_key` 补上（`_build_chunks` 里由调用方统一填，避免每处都传）。

    单独一个函数而不是在 `_build_chunks` 内部填，是因为 `Chunk` 是 frozen 的
    数据对象、`doc_key` 属于"归属"而不是"内容"——两者分开，重跑同一版本时
    `chunk_id` 不变而归属可以变（比如 doc_key 归一化规则调整后重跑）。
    """
    out: list[ck.Chunk] = []
    for chunk in chunks:
        chunk.doc_key = doc_key
        out.append(chunk)
    return out


__all__ = [
    "MissingAcl",
    "ParseOutcome",
    "PipelineConfig",
    "assign_doc_key",
    "parse_document",
]
