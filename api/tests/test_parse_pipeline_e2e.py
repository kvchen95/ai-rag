"""端到端：真 PDF -> 解析 -> 切片（§3.2–§3.6 的验收面）。.

这是本卡的主验收文件。三条验收逐条落成断言：

1. **bbox 可映射原页**：每个切片给出的归一化 bbox，在人眼看来必须落在"那段文字
   真正所在的位置"。做法是拿 bbox 去裁原页渲染图，看那块区域里有没有墨迹；
   再把它整体平移 10% 页宽，断言"平移之后墨迹明显变少"——**这一半才是判据**，
   因为"有点墨迹"在 A4 页上到处都是。
2. **表格三表示一致可重建**：切片里的 `table_html` 反解出的矩阵 == `table_cells`
   展开的矩阵 == `table_md` 解析出的矩阵。
3. **字段缺失被校验拦截**：`parse_document` 出来的每条切片都必须
`require_complete()`；
   再加一条"人为抹掉一个字段，`validate_chunks` 必须抛"。

另外验四件只在端到端才看得见的事：页眉页脚没进切片、双栏没被拼成一句、
旋转页 bbox 与未旋转页一致、图为空转译时不入库。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from api.parse.chunking import Acl, IncompleteChunk, validate_chunks
from api.parse.geometry import PageSpace, denormalize, normalize
from api.parse.model import expand_to_matrix
from api.parse.pipeline import MissingAcl, parse_document
from api.parse.tables import cells_from_html, parse_md

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"

ACL = Acl(is_public=False, allowed_depts=("fin",), allowed_roles=("staff",))
VERSION = "policy/demo#abcdef123456"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run fixtures."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_layout_samples  # noqa: PLC0415

    root = tmp_path_factory.mktemp("e2e")
    for rel in make_layout_samples.SAMPLES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(make_layout_samples.build(rel))
    return root


def _parse(path: Path, **kwargs: object):
    """Run _parse."""
    return parse_document(
        path,
        doc_key="policy/demo",
        version_id=VERSION,
        acl=ACL,
        **kwargs,  # type: ignore[arg-type]
    )


# --- 1. 端到端结构 -----------------------------------------------------------


def test_table_document_produces_text_and_table_chunks(fixtures: Path) -> None:
    """Run test_table_document_produces_text_and_table_chunks."""
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    types = outcome.counts()
    assert types.get("table") == 2  # 两页各一张表
    assert types.get("text", 0) >= 2
    assert outcome.rejected_figures == []


def test_every_chunk_is_complete(fixtures: Path) -> None:
    """验收第三条：出流水线的每一条都必须带齐六项随行信息。."""
    for name in (
        "layout/table-demo.pdf",
        "layout/two-column-demo.pdf",
        "layout/header-demo.pdf",
    ):
        outcome = _parse(fixtures / name)
        for chunk in outcome.chunks:
            assert (
                chunk.missing_fields() == []
            ), f"{chunk.chunk_id}: {chunk.missing_fields()}"
            assert chunk.require_complete() is chunk


def test_acl_is_propagated_to_every_chunk(fixtures: Path) -> None:
    """权限是切片自带的（§2.3 六项之一），不是检索时再查。."""
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    for chunk in outcome.chunks:
        assert chunk.is_public is False
        assert chunk.allowed_depts == ("fin",)
        assert chunk.allowed_roles == ("staff",)


def test_empty_acl_is_refused(fixtures: Path) -> None:
    """解析器不知道业务权限：猜一个"公开"比拒绝入库危险得多。."""
    with pytest.raises(MissingAcl):
        parse_document(
            fixtures / "layout/table-demo.pdf",
            doc_key="policy/demo",
            version_id=VERSION,
            acl=Acl(),
        )


def test_header_footer_never_becomes_a_chunk(fixtures: Path) -> None:
    """页眉/页脚是噪声：进了库会让检索命中"ACME 内部文件"。."""
    outcome = _parse(fixtures / "layout/header-demo.pdf")
    texts = " ".join(chunk.content_text for chunk in outcome.chunks)
    assert "ACME Internal" not in texts
    assert "Page 1 of 3" not in texts


def test_repeated_clause_survives_as_a_chunk(fixtures: Path) -> None:
    """每页都出现的条款必须留在库里（它是正文，不是页眉）。."""
    outcome = _parse(fixtures / "layout/header-demo.pdf")
    texts = [chunk.content_text for chunk in outcome.chunks]
    assert any("Clause 4.2 applies to all staff." in text for text in texts)


def test_two_column_text_is_not_spliced_across_columns(fixtures: Path) -> None:
    """§3.2：不按栏切会把一句话拆成左右两栏拼起来。."""
    outcome = _parse(fixtures / "layout/two-column-demo.pdf")
    for chunk in outcome.chunks:
        assert not (
            "Left column" in chunk.content_text
            and "Right column" in chunk.content_text
        ), chunk.content_text
    texts = " ".join(chunk.content_text for chunk in outcome.chunks)
    assert "Left column line one explains scope." in texts
    assert "Right column line one lists approvals." in texts


# --- 2. bbox 可映射原页 ------------------------------------------------------


def _ink_ratio(image, bbox: tuple[float, float, float, float]) -> float:
    """一块区域里"非白像素"的占比。."""
    left = int(bbox[0] * image.width)
    top = int(bbox[1] * image.height)
    right = max(left + 1, int(bbox[2] * image.width))
    bottom = max(top + 1, int(bbox[3] * image.height))
    crop = image.crop((left, top, right, bottom)).convert("L")
    pixels = list(crop.getdata())
    if not pixels:
        return 0.0
    return sum(1 for value in pixels if value < 200) / len(pixels)


@pytest.fixture(scope="module")
def renderer(fixtures: Path):
    """按需渲染某一页为 RGB 位图（200 DPI，与图裁片一致）。."""
    from pypdfium2 import PdfDocument  # noqa: PLC0415

    from api.parse import figures as fig  # noqa: PLC0415

    cache: dict[tuple[str, int], object] = {}

    def _render(name: str, page_num: int):
        """Run _render."""
        key = (name, page_num)
        if key not in cache:
            document = PdfDocument(str(fixtures / name))
            try:
                page = document[page_num - 1]
                cache[key] = fig.render_page(page, scale=fig.scale_for_dpi(200))
            finally:
                document.close()
        return cache[key]

    return _render


def test_bbox_lands_on_the_text_it_describes(fixtures: Path, renderer) -> None:
    """切片 bbox 裁出来的区域里必须有墨迹，**平移之后墨迹明显变少**。.

    "有点墨迹"在 A4 页上到处都是（页边、表格线、相邻段落），所以要靠平移对照：
    如果 bbox 是对的，平移 10% 页宽就会离开这段文字；如果 bbox 是错的（比如
    x/y 搞反、旋转没转），平移前后一样多。
    """
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    chunk = next(
        c
        for c in outcome.chunks
        if c.chunk_type == "text" and len(c.content_text) > 30
    )
    image = renderer("layout/table-demo.pdf", chunk.page_num)
    bbox = tuple(chunk.bbox[0])
    here = _ink_ratio(image, bbox)
    shifted = (bbox[0] + 0.10, bbox[1], min(1.0, bbox[2] + 0.10), bbox[3])
    there = _ink_ratio(image, shifted)
    assert here > 0.02, f"bbox covers no ink at all ({here:.3f})"
    assert (
        here > there
    ), f"shifted region has as much ink: here={here:.3f} there={there:.3f}"


def test_bbox_of_a_table_lands_on_the_table(fixtures: Path, renderer) -> None:
    """Run test_bbox_of_a_table_lands_on_the_table."""
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    chunk = next(c for c in outcome.chunks if c.chunk_type == "table")
    image = renderer("layout/table-demo.pdf", chunk.page_num)
    # 表格区域有网格线，墨迹占比明显高于纯正文行
    assert _ink_ratio(image, tuple(chunk.bbox[0])) > 0.01


def test_bbox_is_normalized_and_well_formed(fixtures: Path) -> None:
    """Run test_bbox_is_normalized_and_well_formed."""
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    for chunk in outcome.chunks:
        assert chunk.bbox, chunk.chunk_id
        for rect in chunk.bbox:
            assert len(rect) == 4
            assert all(0.0 <= value <= 1.0 for value in rect), rect
            assert rect[2] > rect[0] and rect[3] > rect[1], rect


def test_rotated_page_bbox_matches_the_unrotated_page(
    fixtures: Path, renderer
) -> None:
    """旋转页上的 bbox 必须能映射回同一个物理位置。.

    旋转页与未旋转页上同一段文字的 bbox 应该**不同**（页面转了），但把它
    映射回用户空间后必须落在同一个物理位置。

    这是"bbox 可映射原页"在旋转页上的形式：前端拿显示空间 bbox 画框，
    后端拿用户空间坐标对原页——两者必须描述同一块地方。

    取**子片**（`chunk_type=text` 且 `parent_id` 为空）而不取父片：父片覆盖
    整段，子片覆盖“这一句”。这里要验的是句子级定位。
    """
    outcome = _parse(fixtures / "layout/rotated-demo.pdf")
    text_chunks = [
        c
        for c in outcome.chunks
        if c.chunk_type == "text"
        and "This sentence" in c.content_text
        and c.parent_id is None
    ]
    assert len(text_chunks) == 2, [c.chunk_id for c in text_chunks]
    first = next(c for c in text_chunks if c.page_num == 1)
    second = next(c for c in text_chunks if c.page_num == 2)
    assert first.bbox != second.bbox  # 显示空间里位置不同（页面转过了）

    # 未旋转页的 bbox 在阅读空间 == 显示空间；旋转页的要经 PageSpace 反推。
    space_rotated = PageSpace(90, 595.0, 842.0)
    display = denormalize(second.bbox[0], *space_rotated.display_size)
    reading = space_rotated.display_to_reading(display)
    reading_bbox = normalize(reading, *space_rotated.reading_size)
    assert reading_bbox == pytest.approx(tuple(first.bbox[0]), abs=0.02)


# --- 3. 表格三表示一致 -------------------------------------------------------


def test_table_three_representations_agree(fixtures: Path) -> None:
    """验收第二条：HTML 反解 == cells 展开 == md 解析。."""
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    tables = [c for c in outcome.chunks if c.chunk_type == "table"]
    assert len(tables) == 2
    for chunk in tables:
        assert chunk.table_html and chunk.table_md and chunk.table_cells

        from_cells = expand_to_matrix(
            [_cell_obj(cell) for cell in chunk.table_cells],
            row_count=len(parse_md(chunk.table_md)),
            col_count=len(parse_md(chunk.table_md)[0]),
        )
        from_html_cells, rows, cols = cells_from_html(chunk.table_html)
        from_html = expand_to_matrix(from_html_cells, rows, cols)
        from_md = parse_md(chunk.table_md)
        assert from_html == from_cells, "html 与 cells 不一致"
        assert from_md == from_cells, "md 与 cells 不一致"


def _cell_obj(raw: dict[str, object]):
    """Run _cell_obj."""
    from api.parse.model import TableCell  # noqa: PLC0415

    return TableCell(
        row=int(raw["row"]),  # type: ignore[arg-type]
        col=int(raw["col"]),  # type: ignore[arg-type]
        text=str(raw["text"]),
        is_header=bool(raw["is_header"]),
        row_span=int(raw["row_span"]),  # type: ignore[arg-type]
        col_span=int(raw["col_span"]),  # type: ignore[arg-type]
    )


def test_table_content_text_carries_the_numbers(fixtures: Path) -> None:
    """表格切片的检索文本必须带上单元格里的数字（否则"上海住宿限额"查不到）。."""
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    table = next(c for c in outcome.chunks if c.chunk_type == "table")
    assert "500" in table.content_text
    assert "Shanghai" in table.content_text


def test_continued_table_is_flagged_not_merged(fixtures: Path) -> None:
    """跨页续表：标出来，但**不合并**——合并会破坏 bbox 与原页的对应关系。.

    第 2 页那张表贴着页面顶部且与第 1 页同列数，所以它该被标成续表。
    """
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    page2 = next(page for page in outcome.doc.pages if page.page_num == 2)
    table_region = next(r for r in page2.regions if r.table is not None)
    assert table_region.table is not None
    assert table_region.table.continued is True
    assert any(
        note.startswith("continued_of:page1") for note in table_region.notes
    )
    # 第 2 页那条表切片的 page_num 与 bbox 仍指向第 2 页（没有被搬走）
    chunk = next(
        c for c in outcome.chunks if c.chunk_type == "table" and c.page_num == 2
    )
    assert chunk.page_num == 2
    assert chunk.bbox[0][1] < 0.5  # 落在第 2 页上半部分


# --- 4. 图：转译失败不入库 ---------------------------------------------------


class _FakeVLM:
    """_FakeVLM."""
    def __init__(self, response: str) -> None:
        """Initialize the instance."""
        self.response = response
        self.calls = 0

    def transcribe(self, image: bytes, prompt: str) -> str:
        """Run transcribe."""
        assert image.startswith(b"RIFF")  # 无损 WebP 容器头
        self.calls += 1
        return self.response


GOOD_TRANSCRIPT = """| 类别 | 百分比 |
| --- | --- |
| Transport | 32 |
| Lodging | 28 |
| Meals | 24 |
| Other | 16 |

JSON: {"kind":"bar","axes":{"y":{"unit":"%","min":0,"max":100}},
"series":[{"name":"share","unit":"%","points":[
{"label":"Transport","value":32},{"label":"Lodging","value":28},
{"label":"Meals","value":24},{"label":"Other","value":16}]}],
"legend_missing":false}"""

BAD_TRANSCRIPT = GOOD_TRANSCRIPT.replace(
    "| Transport | 32 |", "| Transport | 74 |"
).replace('"value":32', '"value":74')


def _figure_builder(response: str, path: Path):
    """Run _figure_builder."""
    from api.parse.figure_pipeline import (  # noqa: PLC0415
        FigurePipelineConfig,
        make_builder,
    )

    return make_builder(
        _FakeVLM(response), FigurePipelineConfig(), source_path=path
    )


def test_figure_with_matching_numbers_is_indexed(
    fixtures: Path, tmp_path: Path
) -> None:
    """Run test_figure_with_matching_numbers_is_indexed."""
    path = fixtures / "layout/figure-demo.pdf"
    figures_dir = tmp_path / "figures"
    outcome = parse_document(
        path,
        doc_key="policy/demo",
        version_id=VERSION,
        acl=ACL,
        figure_builder=_figure_builder(GOOD_TRANSCRIPT, path),
        figures_dir=figures_dir,
    )
    figure_chunks = [c for c in outcome.chunks if c.chunk_type == "figure"]
    assert len(figure_chunks) == 1
    chunk = figure_chunks[0]
    assert chunk.image_path is not None
    assert (figures_dir / Path(chunk.image_path).name).exists()
    assert "Transport" in chunk.content_text
    assert outcome.rejected_figures == []


def test_figure_with_wrong_number_is_rejected(
    fixtures: Path, tmp_path: Path
) -> None:
    """验收第二条的后半句：数值对不上 → 标失败 → **不入库**。.

    74 与图的真实值 32 对不上：占比求和不拢（142），而且图里找不到 74 的依据。
    这条图既不产出 figure 切片，也不留下图文件。
    """
    path = fixtures / "layout/figure-demo.pdf"
    figures_dir = tmp_path / "figures"
    outcome = parse_document(
        path,
        doc_key="policy/demo",
        version_id=VERSION,
        acl=ACL,
        figure_builder=_figure_builder(BAD_TRANSCRIPT, path),
        figures_dir=figures_dir,
    )
    assert [c for c in outcome.chunks if c.chunk_type == "figure"] == []
    assert len(outcome.rejected_figures) == 1
    failure = outcome.rejected_figures[0]
    assert failure.reason.startswith("reconcile:")
    assert (
        "percent_sum" in failure.reason
        or "value_not_in_figure" in failure.reason
    )
    assert (
        not list(figures_dir.glob("*.webp")) if figures_dir.exists() else True
    )
    # 图区域里的刻度/标签也不该变成正文切片（它们是脱离上下文的数字碎片）
    assert not any(
        c.content_text.strip() in {"100", "50", "0"} for c in outcome.chunks
    )


def test_figure_without_vlm_is_not_indexed(fixtures: Path) -> None:
    """没配 VLM 时不产出图切片：空转译的图切片是"没有内容的证据"。."""
    path = fixtures / "layout/figure-demo.pdf"
    outcome = parse_document(
        path,
        doc_key="policy/demo",
        version_id=VERSION,
        acl=ACL,
        figure_builder=_figure_builder_without_vlm(path),
    )
    assert [c for c in outcome.chunks if c.chunk_type == "figure"] == []
    assert any(
        "vlm_unavailable" in failure.reason
        for failure in outcome.rejected_figures
    )


def _figure_builder_without_vlm(path: Path):
    """Run _figure_builder_without_vlm."""
    from api.parse.figure_pipeline import (  # noqa: PLC0415
        FigurePipelineConfig,
        make_builder,
    )

    return make_builder(None, FigurePipelineConfig(), source_path=path)


def test_figure_image_meets_the_dpi_floor(
    fixtures: Path, tmp_path: Path
) -> None:
    """§3.4：无损 WebP ≥ 200 DPI。."""
    from PIL import Image  # noqa: PLC0415

    path = fixtures / "layout/figure-demo.pdf"
    figures_dir = tmp_path / "figures"
    outcome = parse_document(
        path,
        doc_key="policy/demo",
        version_id=VERSION,
        acl=ACL,
        figure_builder=_figure_builder(GOOD_TRANSCRIPT, path),
        figures_dir=figures_dir,
    )
    chunk = next(c for c in outcome.chunks if c.chunk_type == "figure")
    image_path = figures_dir / Path(chunk.image_path or "").name
    assert image_path.exists()
    with Image.open(image_path) as image:
        assert image.format == "WEBP"
        assert image.width >= 480  # MIN_CROP_PX
    # 库里的 bbox 与图的实际尺寸要能算出 ≥200 DPI
    from api.parse.geometry import effective_dpi  # noqa: PLC0415

    pdf_width = 595.0
    dpi = effective_dpi(image.width, chunk.bbox[0], pdf_width)
    assert dpi >= 200, f"figure rendered at {dpi:.0f} DPI"


# --- 5. 校验拦截的"再验一次"（反例） -----------------------------------------


def test_validate_catches_a_chunk_with_a_deleted_field(fixtures: Path) -> None:
    """人为抹掉一个字段，`validate_chunks` 必须抛。.

    这是"检查真的在跑"的反证：如果它只在 happy path 上被调用、从没见过坏输入，
    那么它是否有效就没人知道。
    """
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    victim = outcome.chunks[0]
    victim.page_num = 0  # 抹掉页码
    with pytest.raises(IncompleteChunk) as excinfo:
        validate_chunks(outcome.chunks)
    assert "page_num" in str(excinfo.value)
    del victim


def test_parse_report_has_no_chunk_text(fixtures: Path) -> None:
    """`as_report()` 给接口/日志用，不该带正文（会把日志撑爆）。."""
    outcome = _parse(fixtures / "layout/table-demo.pdf")
    report = outcome.as_report()
    assert "chunks" in report and isinstance(report["chunks"], dict)
    assert "content_text" not in str(report)
    assert report["total_chunks"] == len(outcome.chunks)
