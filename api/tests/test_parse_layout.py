"""版面还原的实测判据（§3.2）：行聚类、栏序、页眉页脚、表格/图检测。

用的是 `scripts/make_layout_samples.py` 现场生成的六份夹具——不是仓库里的二进制
文件。这样 fixture 的**意图**（哪一页埋了哪个坑）写在代码里，评审时看得见；
而且样本字节确定，断言可以钉到具体坐标。

三组判据对应 §3.2 边界表里的三条硬要求：

* **双栏**：一句话不能被拆成两栏拼起来；
* **旋转页**：坐标必须在阅读空间里，否则高亮框转着错；
* **页眉页脚**：只删页边带内重复的行，页中的重复条款必须保留。

另外一条是这个夹具逼出来的：**表格/图失败时不能把刻度标签漏成正文**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pdfplumber
import pytest

from api.parse.geometry import PageSpace, Rect
from api.parse.layout import (
    classify_header_footer,
    detect_columns,
    extract_lines,
    find_figures,
    find_tables,
    has_numeric_unit,
    in_margin_band,
    reading_chars,
    repeated_key,
)
from api.parse.model import Line

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """现场生成六份版面夹具（字节确定，随用随生成）。"""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_layout_samples  # noqa: PLC0415

    root = tmp_path_factory.mktemp("layout")
    for rel in make_layout_samples.SAMPLES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(make_layout_samples.build(rel))
    return root


def _lines_of(path: Path, page_index: int = 0) -> tuple[PageSpace, list[Line]]:
    with pdfplumber.open(str(path)) as pdf:
        page = pdf.pages[page_index]
        space = PageSpace.from_display(page)
        return space, extract_lines(reading_chars(page, space), page_index + 1)


def _line(text: str, *, top: float, x0: float = 72.0, x1: float = 300.0, size: float = 10.0) -> Line:
    return Line(text=text, rect=Rect(x0, top, x1, top + size), page_num=1, font_size=size)


# --- 1. 行聚类：行内空格 ------------------------------------------------


def test_line_join_inserts_spaces_from_coordinates(fixtures: Path) -> None:
    """PDF 里的"空格"经常不是空格字符，而是横向间隔。

    不补就会得到 `TravelExpensePolicy(CN)`——进 BM25 之后 "travel expense"
    一个词都命中不了。这是实测发现的（第一版就是连写的）。
    """
    _, lines = _lines_of(fixtures / "layout/table-demo.pdf")
    assert "Travel Expense Policy (CN)" in [line.text for line in lines]


def test_line_join_does_not_split_on_small_gaps(fixtures: Path) -> None:
    """词内的小间距不产生空格：`(CN)` 不该变成 `( C N )`。"""
    _, lines = _lines_of(fixtures / "layout/table-demo.pdf")
    assert not any("( C N" in line.text for line in lines)


def test_cjk_chars_are_not_separated_by_spaces() -> None:
    """中文之间不补空格：中文本来不用空格断词。

    补了会让"住宿标准"变成"住宿 标准"，与词典分词的结果对不上。
    """
    from api.parse.layout import CJK_GAP_PT, _join_segment  # noqa: PLC0415

    chars = [
        {"text": "住", "x0": 0.0, "x1": 10.0, "top": 0.0, "bottom": 10.0},
        {"text": "宿", "x0": 10.0 + CJK_GAP_PT * 0.5, "x1": 20.0, "top": 0.0, "bottom": 10.0},
    ]
    assert _join_segment(chars) == "住宿"


def test_cjk_gap_beyond_threshold_does_insert_a_space() -> None:
    """间隔大到像分栏时仍补空格：那是两个独立段落被同行排版。"""
    from api.parse.layout import CJK_GAP_PT, _join_segment  # noqa: PLC0415

    chars = [
        {"text": "住", "x0": 0.0, "x1": 10.0, "top": 0.0, "bottom": 10.0},
        {"text": "宿", "x0": 10.0 + CJK_GAP_PT * 2, "x1": 40.0, "top": 0.0, "bottom": 10.0},
    ]
    assert _join_segment(chars) == "住 宿"


# --- 2. 双栏 ---------------------------------------------------------------


def test_two_column_page_is_read_column_by_column(fixtures: Path) -> None:
    """§3.2 边界表：不按栏切，一句话会被拆成左右两栏拼起来。

    夹具里左右两栏各自三句，按行读会得到
    `Left column line one explains scope. Right column line one lists approvals.`。
    """
    _, lines = _lines_of(fixtures / "layout/two-column-demo.pdf")
    texts = [line.text for line in lines]
    # 每一行都只属于一栏（没有任何一行同时含左右两栏的说法）
    for text in texts:
        assert not ("Left column" in text and "Right column" in text), text
    assert any(text.startswith("Left column line one") for text in texts)
    assert any(text.startswith("Right column line one") for text in texts)


def test_detect_columns_finds_the_gutter(fixtures: Path) -> None:
    _, lines = _lines_of(fixtures / "layout/two-column-demo.pdf")
    columns = detect_columns(lines)
    assert columns is not None
    assert len(columns) == 2
    assert columns[0][1] < columns[1][0]  # 左栏右界 < 右栏左界 = 有缝


def test_single_column_page_has_no_gutter(fixtures: Path) -> None:
    _, lines = _lines_of(fixtures / "layout/table-demo.pdf")
    assert detect_columns(lines) is None


def test_column_order_puts_left_column_first(fixtures: Path) -> None:
    """栏序：左栏三行必须全部出现在右栏第一行之前。

    这条比"检测到缝"更强：缝检测对了但排序没按栏走，结果仍然是把两栏交错的。
    """
    _, lines = _lines_of(fixtures / "layout/two-column-demo.pdf")
    texts = [line.text for line in lines if "column line" in line.text]
    split = max(index for index, text in enumerate(texts) if text.startswith("Left"))
    first_right = min(index for index, text in enumerate(texts) if text.startswith("Right"))
    assert split < first_right


def test_detect_columns_needs_enough_lines() -> None:
    """两行的"页"不该被判成双栏：一行在左、一行在右完全可能是普通正文。"""
    lines = [_line("a", top=10, x0=72, x1=200), _line("b", top=30, x0=400, x1=550)]
    assert detect_columns(lines) is None


# --- 3. 旋转页 -------------------------------------------------------------


def test_rotated_page_lines_are_horizontal_after_conversion(fixtures: Path) -> None:
    """`/Rotate 90` 的页在显示空间里文字是竖排的，会被切成几十个单字符行。

    转换到阅读空间后必须恢复成正常的横排行（第一版没有转换，实测一行被切成
    34 个单字符行）。
    """
    _, lines = _lines_of(fixtures / "layout/rotated-demo.pdf", page_index=1)
    assert len(lines) <= 4
    assert any("This sentence is on a rotated page" in line.text for line in lines)


def test_rotated_page_bbox_maps_back_to_the_same_region(fixtures: Path) -> None:
    """同一段文字在第 1 页（未旋转）与第 2 页（旋转 90）应落在页面同一位置。"""
    with pdfplumber.open(str(fixtures / "layout/rotated-demo.pdf")) as pdf:
        page1, page2 = pdf.pages[0], pdf.pages[1]
        space1, space2 = PageSpace.from_display(page1), PageSpace.from_display(page2)
        lines1 = extract_lines(reading_chars(page1, space1), 1)
        lines2 = extract_lines(reading_chars(page2, space2), 2)

    target = "This sentence is on a rotated page"
    line1 = next(line for line in lines1 if target in line.text)
    line2 = next(line for line in lines2 if target in line.text)
    bbox1 = space1.reading_to_display(line1.rect)
    bbox2 = space2.reading_to_display(line2.rect)
    # 显示空间里两者的宽度/高度互换（页面被转了 90 度），但**阅读空间**必须一致。
    assert line1.rect.as_tuple() == pytest.approx(line2.rect.as_tuple(), abs=1.0)
    assert bbox1.as_tuple() != bbox2.as_tuple()


# --- 4. 页眉页脚 -----------------------------------------------------------


def test_header_footer_removed_only_from_the_margin_band(fixtures: Path) -> None:
    """页眉与页脚删掉；页中重复出现的 `Clause 4.2` 必须保留。

    §3.2 的"重复即页眉页脚"会误删每页都出现的条款编号——这里用页边带把误删
    收窄，同时保证真页眉仍然被删。夹具把两种行放在不同高度。
    """
    with pdfplumber.open(str(fixtures / "layout/header-demo.pdf")) as pdf:
        page_lines: list[list[tuple[str, bool]]] = []
        for index, page in enumerate(pdf.pages):
            space = PageSpace.from_display(page)
            width, height = space.reading_size
            lines = extract_lines(reading_chars(page, space), index + 1)
            page_lines.append(
                [(line.text, in_margin_band(line, width, height)) for line in lines]
            )
    verdicts = classify_header_footer(page_lines)
    assert verdicts["ACME Internal - Travel Policy Handbook"].is_header_footer
    assert verdicts["Page 1 of 3"].is_header_footer
    assert verdicts["Page 2 of 3"].is_header_footer
    clause = verdicts["Clause 4.2 applies to all staff."]
    assert not clause.is_header_footer
    assert clause.reason == "not_in_margin_band"


def test_margin_band_uses_the_line_centre() -> None:
    """骑在带边上的行按中心点判，不因一两个像素抖动改结论。

    页高 842 × 12% = 101.04pt 是上边界；行高 10pt，所以中心 = top + 5。
    用 top=92（中心 97，带内）与 top=100（中心 105，带外）各一个。
    """
    width, height = 595.0, 842.0
    inside = _line("x", top=92.0, size=10.0)
    outside = _line("y", top=100.0, size=10.0)
    assert in_margin_band(inside, width, height)
    assert not in_margin_band(outside, width, height)


def test_margin_band_also_covers_the_footer() -> None:
    """页脚带也要判（真实页脚在 96% 处）。

    页高 842，页脚带从 842 × 88% = 741pt 起。所以 top=800（中心 804）在带内，
    top=700（中心 705）在带外。
    """
    width, height = 595.0, 842.0
    footer = _line("page 1 of 3", top=800.0, size=8.0)
    body = _line("body", top=700.0, size=10.0)
    assert in_margin_band(footer, width, height)
    assert not in_margin_band(body, width, height)


def test_header_footer_requires_more_than_one_page() -> None:
    """单页文档没有"重复"可言。"""
    assert classify_header_footer([[("only page", True)]]) == {}


def test_repeated_key_flattens_digits() -> None:
    """真实页眉几乎都带页码，逐字比较一个都认不出来。

    但抹平数字有代价：`Clause 4.2` 与 `Clause 4.3` 归一后同键。这正是**必须**加
    "在页边带内"这条判据的根本原因——光靠重复，相邻条款会互相误伤。
    这个用例把代价钉住，免得有人以为 `Clause 4.2` 的保留是"碰巧"。
    """
    assert repeated_key("Page 1 of 3") == repeated_key("Page 2 of 3")
    assert repeated_key("第 1 页") == repeated_key("第 2 页")
    # 代价：不同条款号归一后同键（所以不能只靠重复判页眉）
    assert repeated_key("Clause 4.2") == repeated_key("Clause 4.3")


def test_clause_numbers_survive_because_of_the_margin_band() -> None:
    """相邻两条条款 `Clause 4.2` / `Clause 4.3` 都在正文区出现时，都不能被删。

    这条是"数字抹平 + 重复判定"这个组合的最坏情形：两条不同的条款归一成同一个
    键、又在两页上各出现一次。没有页边带判据，两条都会被当页眉删掉。
    """
    page_lines = [
        [
            ("Clause 4.2 applies to all staff.", False),
            ("Clause 4.3 covers approvals.", False),
        ],
        [
            ("Clause 4.2 applies to all staff.", False),
            ("Clause 4.3 covers approvals.", False),
        ],
    ]
    verdicts = classify_header_footer(page_lines)
    assert not any(verdict.is_header_footer for verdict in verdicts.values())


def test_repeated_key_flattens_whitespace_and_case() -> None:
    assert repeated_key("  ACME   Internal ") == repeated_key("acme internal")


def test_numeric_unit_lines_are_protected() -> None:
    """带数字单位的行即便重复也不删：它多半是每页都印的"标准值"提示。"""
    assert has_numeric_unit("每人每天 500 元")
    assert has_numeric_unit("CNY 500 per night")
    assert has_numeric_unit("30 个工作日")
    assert has_numeric_unit("80%")
    assert not has_numeric_unit("Clause 4.2 applies to all staff.")


def test_numeric_unit_line_survives_the_repeat_rule() -> None:
    lines = [[("限额 500 元", True)]] * 3
    verdicts = classify_header_footer(lines)
    assert not verdicts["限额 500 元"].is_header_footer
    assert verdicts["限额 500 元"].reason == "numeric_unit_protected"


def test_long_repeated_line_is_protected() -> None:
    long_line = "本文件由合规部发布，适用于全体正式员工与实习生，未经批准不得外传。" * 2
    lines = [[(long_line, True)]] * 2
    verdicts = classify_header_footer(lines)
    assert verdicts[long_line].reason == "too_long"


def test_pure_repeat_mode_is_available_for_comparison() -> None:
    """`require_margin=False` 复现 §3.2 的"纯重复"口径，用于对比实验。

    这个口眼下 `Clause 4.2` 会被误删——它是文档里写明的代价，测试把它钉住，
    免得有人以为收紧是"没必要的优化"。
    """
    lines = [
        [
            ("Clause 4.2 applies to all staff.", False),
            ("Section 1. Scope", False),
        ]
    ] * 3
    strict = classify_header_footer(lines)
    loose = classify_header_footer(lines, require_margin=False)
    assert not strict["Clause 4.2 applies to all staff."].is_header_footer
    assert loose["Clause 4.2 applies to all staff."].is_header_footer


# --- 5. 表格检测 -----------------------------------------------------------


def test_wired_table_is_detected_with_all_cells(fixtures: Path) -> None:
    with pdfplumber.open(str(fixtures / "layout/table-demo.pdf")) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        tables = find_tables(page, space)
    assert len(tables) == 1
    assert tables[0].table.matrix() == [
        ["City", "Lodging cap", "Region"],
        ["Shanghai", "500", "tier-1"],
        ["Beijing", "500", "tier-1"],
        ["Hefei", "380", "tier-2"],
    ]
    assert not tables[0].table.header_inferred


def test_table_bbox_is_in_reading_space(fixtures: Path) -> None:
    """表格矩形必须落在阅读空间（72..523 × 612..724），不是显示空间。

    夹具把第 1 页的表格排在页面底部（下沿 y=118pt = 页高 86%），所以阅读空间的
    `top` 是 842-724=118 起、到 842-612=230 止——这个数字与"页面底部"的物理位置
    对得上，说明坐标没被转错。
    """
    with pdfplumber.open(str(fixtures / "layout/table-demo.pdf")) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        tables = find_tables(page, space)
    rect = tables[0].rect
    assert (round(rect.x0), round(rect.top)) == (72, 612)
    assert (round(rect.x1), round(rect.bottom)) == (523, 724)


def test_two_column_prose_is_not_reported_as_a_table(fixtures: Path) -> None:
    """双栏正文在 `text` 策略下会被识成 7×7 的假表格（实测）。

    把它当表格会得到"一句话被拆成两栏拼起来"——正是 §3.2 点名的那类错。
    """
    with pdfplumber.open(str(fixtures / "layout/two-column-demo.pdf")) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        lines = extract_lines(reading_chars(page, space), 1)
        columns = detect_columns(lines)
        gutters = [(left[1], right[0]) for left, right in zip(columns, columns[1:])] if columns else []
        tables = find_tables(page, space, column_gutters=gutters, lines=lines)
    assert tables == []


def test_rotated_page_without_wires_produces_no_table(fixtures: Path) -> None:
    """旋转页的 `text` 策略会凭对齐造出 19 行 × 3 列的假表（实测）。

    reader 在旋转页上只跑 `lines` 策略，所以这份夹具必须没有任何表格。
    """
    with pdfplumber.open(str(fixtures / "layout/rotated-demo.pdf")) as pdf:
        page = pdf.pages[1]
        space = PageSpace.from_display(page)
        tables = find_tables(page, space, strategies=[{"vertical_strategy": "lines", "horizontal_strategy": "lines"}])
    assert tables == []


def test_fragmented_cells_are_rejected_as_prose() -> None:
    """同一行文字被切成两半的"表格"不是表格。

    实测形态：单栏正文在 `text` 策略下被切成 `['Clause 4.2 app', 'lies to all']`。
    真表格的单元格是完整字段值，不会出现"某行文字的一半"。
    """
    from api.parse.layout import _cells_are_fragments  # noqa: PLC0415
    from api.parse.model import TableCell  # noqa: PLC0415
    from api.parse.tables import build_table  # noqa: PLC0415

    table = build_table(
        [
            TableCell(0, 0, "Clause 4.2 app"),
            TableCell(0, 1, "lies to all"),
            TableCell(1, 0, "Paragraph 1: t"),
            TableCell(1, 1, "his text di"),
        ],
        row_count=2,
        col_count=2,
    )
    lines = [
        _line("Clause 4.2 applies to all staff.", top=10, x0=72, x1=300),
        _line("Paragraph 1: this text differs per page.", top=30, x0=72, x1=300),
    ]
    rect = Rect(70.0, 5.0, 310.0, 45.0)
    assert _cells_are_fragments(table, rect, lines)


# --- 6. 图检测 -------------------------------------------------------------


def test_vector_chart_is_detected_as_one_figure(fixtures: Path) -> None:
    """矢量柱状图的柱子、轴线、刻度线会被聚成**一块**图区域。

    不聚簇的话每根柱子都是一个候选，或者干脆被当成表格线。
    """
    with pdfplumber.open(str(fixtures / "layout/figure-demo.pdf")) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        lines = extract_lines(reading_chars(page, space), 1)
        figures = find_figures(page, space, lines)
    assert len(figures) == 1
    assert figures[0].source == "vector"


def test_figure_region_absorbs_axis_labels(fixtures: Path) -> None:
    """图区域要外扩到含刻度与类别名（VLM 没有刻度就无法对账）。"""
    with pdfplumber.open(str(fixtures / "layout/figure-demo.pdf")) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        lines = extract_lines(reading_chars(page, space), 1)
        figures = find_figures(page, space, lines)
    figure = figures[0]
    assert figure.rect.top < figure.core_rect.top  # 向上外扩（含 100 那个刻度）
    assert figure.rect.bottom > figure.core_rect.bottom  # 向下外扩（含类别名）
    absorbed = [line.text for line in lines if figure.rect.intersection(line.rect) is not None]
    assert "Transport" in absorbed
    assert "100" in absorbed or "0" in absorbed


def test_scanned_page_bitmap_is_detected(fixtures: Path) -> None:
    with pdfplumber.open(str(fixtures / "layout/scanned-demo.pdf")) as pdf:
        page = pdf.pages[1]
        space = PageSpace.from_display(page)
        figures = find_figures(page, space, [])
    assert len(figures) == 1
    assert figures[0].source == "image"


def test_prose_guard_flags_sentence_cells() -> None:
    """单元格以句末标点结尾占比高 → 判成散文（不是表格）。

    `test_two_column_prose_is_not_reported_as_a_table` 只能证明**结果**对；
    结果可能是被栏缝判据救回来的。这条直接测 `_looks_like_prose` 本身，
    让这个判据有独立的牙。
    """
    from api.parse.layout import _looks_like_prose  # noqa: PLC0415
    from api.parse.model import TableCell  # noqa: PLC0415
    from api.parse.tables import build_table  # noqa: PLC0415

    prose = build_table(
        [
            TableCell(0, 0, "Left column line one explains scope."),
            TableCell(0, 1, "Right column line one lists approvals."),
            TableCell(1, 0, "Left column line two explains limits."),
            TableCell(1, 1, "Right column line two lists receipts."),
        ],
        row_count=2,
        col_count=2,
    )
    assert _looks_like_prose(prose)


def test_prose_guard_flags_long_cells() -> None:
    """单个格子超过 `TABLE_CELL_MAX_CHARS` 字：真表格的字段值不会这么长。

    必须用**真的超过阈值**的文本：第一版用了 36 个字的句子，它其实是靠
    "以句号结尾"那条判据被判成散文的，长度这条分支根本没走到——测试名与实现不符。
    """
    from api.parse.layout import TABLE_CELL_MAX_CHARS, _looks_like_prose  # noqa: PLC0415
    from api.parse.model import TableCell  # noqa: PLC0415
    from api.parse.tables import build_table  # noqa: PLC0415

    long_text = "本表适用于全体正式员工与实习生，自二零二五年三月一日起执行，有效期三年" * 3
    assert len(long_text) > TABLE_CELL_MAX_CHARS
    table = build_table(
        [TableCell(0, 0, long_text), TableCell(0, 1, "x")],
        row_count=1,
        col_count=2,
    )
    assert _looks_like_prose(table)


def test_prose_guard_flags_cells_ending_with_sentence_punctuation() -> None:
    """格子**短**但一半以上以句号结尾 → 也是散文。

    这条与上一条是两个独立分支：短句靠标点判、长句靠长度判。
    两条都要有各自的用例，否则其中一个被删掉不会有人发现。
    """
    from api.parse.layout import TABLE_CELL_MAX_CHARS, _looks_like_prose  # noqa: PLC0415
    from api.parse.model import TableCell  # noqa: PLC0415
    from api.parse.tables import build_table  # noqa: PLC0415

    short_sentences = ["说明如下。", "自三月起。", "适用于全部。", "有效期三年。"]
    assert all(len(text) < TABLE_CELL_MAX_CHARS for text in short_sentences)
    table = build_table(
        [TableCell(row, 0, text) for row, text in enumerate(short_sentences)],
        row_count=len(short_sentences),
        col_count=1,
    )
    assert _looks_like_prose(table)


def test_prose_guard_lets_a_real_table_through() -> None:
    """字段值短、不以句号结尾 → 不是散文。这条防"判据太激进把真表毙掉"。"""
    from api.parse.layout import _looks_like_prose  # noqa: PLC0415
    from api.parse.model import TableCell  # noqa: PLC0415
    from api.parse.tables import build_table  # noqa: PLC0415

    table = build_table(
        [
            TableCell(0, 0, "City", is_header=True),
            TableCell(0, 1, "Cap", is_header=True),
            TableCell(1, 0, "Shanghai"),
            TableCell(1, 1, "500"),
            TableCell(2, 0, "Beijing"),
            TableCell(2, 1, "500"),
        ],
        row_count=3,
        col_count=2,
    )
    assert not _looks_like_prose(table)


def test_prose_document_produces_no_table_chunks(fixtures: Path) -> None:
    """单栏长文（无表格线、无栏缝）不该产出任何 table 切片。"""
    with pdfplumber.open(str(fixtures / "layout/prose-demo.pdf")) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        lines = extract_lines(reading_chars(page, space), 1)
        tables = find_tables(page, space, lines=lines)
    assert tables == []


def test_crosses_gutter_detects_a_rect_spanning_the_gap() -> None:
    """跨栏缝的矩形 = 横跨两栏 → 不是表格。

    单测判据本身：`test_two_column_prose_is_not_reported_as_a_table` 只能证明
    结果对（可能被别的判据救回来），这条让"栏缝判据"有独立的牙。
    """
    from api.parse.layout import _crosses_gutter  # noqa: PLC0415

    gutter = (233.0, 320.0)
    assert _crosses_gutter(Rect(72.0, 10.0, 500.0, 60.0), [gutter])
    # 完全在左栏内：不算跨
    assert not _crosses_gutter(Rect(72.0, 10.0, 230.0, 60.0), [gutter])
    # 完全在右栏内：不算跨
    assert not _crosses_gutter(Rect(325.0, 10.0, 500.0, 60.0), [gutter])
    # 边线压在缝上：正常排版，不算跨
    assert not _crosses_gutter(Rect(72.0, 10.0, 233.0, 60.0), [gutter])


def test_crosses_gutter_without_gutters_is_false() -> None:
    from api.parse.layout import _crosses_gutter  # noqa: PLC0415

    assert not _crosses_gutter(Rect(72.0, 10.0, 500.0, 60.0), [])


def test_small_decorations_are_not_figures(fixtures: Path) -> None:
    """一页只有几条分隔线时不产图表切片（`FIGURE_MIN_PRIMITIVES` 兜底）。"""
    with pdfplumber.open(str(fixtures / "layout/two-column-demo.pdf")) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        lines = extract_lines(reading_chars(page, space), 1)
        figures = find_figures(page, space, lines)
    assert figures == []
