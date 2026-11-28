"""切片：父子两级、尺寸边界、六项随行信息（§3.5 / 附录 A.3）。.

验收第三条是"切片字段缺失被校验拦截"。这条的可执行形式有两层：

1. `Chunk.missing_fields()` 逐项点名缺什么
   （`page_num` / `bbox` / `acl(...)`…）；
2. `Chunk.require_complete()` / `validate_chunks()` 在入库前抛
   `IncompleteChunk`。

两层都要测：只测"能抛"会让"缺的是哪一项"说不清，而排障时最需要的正是这一句。
六项逐项都要有**反例**——只测正向的话，某个字段的校验被删掉不会有人发现。
"""

from __future__ import annotations

import pytest

from api.parse.chunking import (
    CHILD_MAX_CHARS,
    CHILD_OVERLAP_CHARS,
    PARENT_MAX_CHARS,
    Acl,
    Chunk,
    IncompleteChunk,
    build_child_pieces,
    child_piece_count,
    chunk_id_for,
    image_path_for,
    parent_slices,
    split_long_text,
    split_sentences,
    validate_chunks,
)
from api.parse.geometry import PageSpace, Rect
from api.parse.model import Line, Region, RegionKind, TableCell
from api.parse.tables import build_table

BBOX = [[0.1, 0.1, 0.5, 0.2]]
ACL = Acl(is_public=False, allowed_depts=("fin",), allowed_roles=("staff",))


def _chunk(**overrides: object) -> Chunk:
    """Run _chunk."""
    base: dict[str, object] = {
        "chunk_id": "ch_aaaaaaaaaaaa",
        "doc_key": "policy/x",
        "version_id": "policy/x#abc123456789",
        "chunk_type": "text",
        "page_num": 3,
        "bbox": BBOX,
        "content_text": "差旅住宿标准：每人每天 500 元",
        "effective_date": 20250301,
        "effective_year": 2025,
        "date_confidence": "explicit",
        "is_public": False,
        "allowed_depts": ("fin",),
        "allowed_roles": ("staff",),
        "parent_id": "ch_bbbbbbbbbbbb",
    }
    base.update(overrides)
    return Chunk(**base)  # type: ignore[arg-type]


# --- 1. 六项随行信息：逐项反例 ------------------------------------------------

PERMISSION_CASES = [
    ("page_num", {"page_num": 0}),
    ("bbox", {"bbox": []}),
    ("bbox(invalid)", {"bbox": [[0.5, 0.1, 0.5, 0.2]]}),
    ("chunk_type", {"chunk_type": "unknown"}),
    ("date_confidence", {"date_confidence": "guessed"}),
    ("version_id", {"version_id": ""}),
    ("content_text", {"content_text": "   "}),
]


@pytest.mark.parametrize(("field", "overrides"), PERMISSION_CASES)
def test_each_accompanying_field_is_enforced(
    field: str, overrides: dict[str, object]
) -> None:
    """六项随行信息里每一项都要有反例：缺一项，后面就有一处功能不可用。."""
    chunk = _chunk(**overrides)
    assert field in chunk.missing_fields()
    with pytest.raises(IncompleteChunk):
        chunk.require_complete()


def test_complete_chunk_passes() -> None:
    """Run test_complete_chunk_passes."""
    chunk = _chunk()
    assert chunk.missing_fields() == []
    assert chunk.require_complete() is chunk


def test_acl_that_denies_everyone_is_rejected() -> None:
    """既不是公开、又没有任何放行项 = 谁都看不到。.

    这几乎一定是漏填，而不是"真的谁都不许看"（真那样就不该入库）。
    数据库拦不住这条：`allowed_depts` 的空数组是合法值。
    """
    chunk = _chunk(is_public=False, allowed_depts=(), allowed_roles=())
    assert "acl(everyone denied)" in chunk.missing_fields()


def test_public_chunk_needs_no_depts_or_roles() -> None:
    """Run test_public_chunk_needs_no_depts_or_roles."""
    chunk = _chunk(is_public=True, allowed_depts=(), allowed_roles=())
    assert chunk.missing_fields() == []


def test_explicit_confidence_without_a_date_is_rejected() -> None:
    """声称"这是原文显式时间"却拿不出日期 —— 时间过滤会把这个谎当成真。."""
    chunk = _chunk(
        effective_date=None, effective_year=None, date_confidence="explicit"
    )
    assert any("effective_date" in field for field in chunk.missing_fields())


def test_inferred_confidence_without_a_date_is_fine() -> None:
    """Run test_inferred_confidence_without_a_date_is_fine."""
    chunk = _chunk(
        effective_date=None, effective_year=None, date_confidence="inferred"
    )
    assert chunk.missing_fields() == []


def test_effective_year_must_match_the_date() -> None:
    """派生列必须与日期自洽，否则时间过滤会按错误的年去筛。."""
    chunk = _chunk(effective_date=20250301, effective_year=2024)
    assert "effective_year(mismatched)" in chunk.missing_fields()


def test_table_chunk_without_md_is_rejected() -> None:
    """Run test_table_chunk_without_md_is_rejected."""
    chunk = _chunk(chunk_type="table", table_md=None)
    assert "table_md" in chunk.missing_fields()


def test_figure_chunk_without_image_path_is_rejected() -> None:
    """Run test_figure_chunk_without_image_path_is_rejected."""
    chunk = _chunk(chunk_type="figure", image_path=None)
    assert "image_path" in chunk.missing_fields()


def test_self_parent_is_rejected() -> None:
    """Run test_self_parent_is_rejected."""
    chunk = _chunk(parent_id="ch_aaaaaaaaaaaa")
    assert "parent_id(self)" in chunk.missing_fields()


def test_validate_reports_the_total_count() -> None:
    """一次报全：修一条跑一次会让人以为是"修完这条还有下一条"。."""
    bad = [_chunk(page_num=0), _chunk(version_id="")]
    with pytest.raises(IncompleteChunk) as excinfo:
        validate_chunks(bad)
    assert "2 chunk(s)" in str(excinfo.value)
    assert excinfo.value.chunk_id == "2 chunk(s)"


def test_as_row_matches_the_database_columns() -> None:
    """`as_row()` 的键必须与迁移的列名逐字一致——它直接喂 INSERT。.

    `table_cells` 与 `notes` 在 004 迁移里补上了（001 漏了）：前者是 §3.3
    "三份表示"的第三份（校验阶段对齐数字用），后者是"这条证据的来源"。
    它们曾经只在内存对象上，`as_row()` 静默丢掉——那时验收脚本里
    "表格三表示一致"只对内存成立，入库剩两份。
    """
    row = _chunk().as_row()
    assert set(row) == {
        "chunk_id",
        "doc_key",
        "version_id",
        "chunk_type",
        "parent_id",
        "page_num",
        "bbox",
        "image_path",
        "content_text",
        "table_html",
        "table_md",
        "table_cells",
        "notes",
        "effective_date",
        "effective_year",
        "date_confidence",
        "is_public",
        "allowed_depts",
        "allowed_roles",
        "token_count",
    }
    assert row["allowed_depts"] == ["fin"]


def test_as_row_refuses_incomplete_chunks() -> None:
    """Run test_as_row_refuses_incomplete_chunks."""
    with pytest.raises(IncompleteChunk):
        _chunk(page_num=0).as_row()


# --- 2. chunk_id 的确定性 ----------------------------------------------------


def test_chunk_id_is_deterministic() -> None:
    """同一份文件重跑必须得到同一批编号，否则"两次解析是否一致"没法比。."""
    first = chunk_id_for("v#abc", page_num=2, kind="child", index=5, part=0)
    second = chunk_id_for("v#abc", page_num=2, kind="child", index=5, part=0)
    assert first == second


def test_chunk_id_differs_on_every_input() -> None:
    """Run test_chunk_id_differs_on_every_input."""
    base = chunk_id_for("v#abc", page_num=2, kind="child", index=5)
    variants = {
        chunk_id_for("v#xyz", page_num=2, kind="child", index=5),
        chunk_id_for("v#abc", page_num=3, kind="child", index=5),
        chunk_id_for("v#abc", page_num=2, kind="table", index=5),
        chunk_id_for("v#abc", page_num=2, kind="child", index=6),
        chunk_id_for("v#abc", page_num=2, kind="child", index=5, part=1),
    }
    assert len(variants | {base}) == 6


def test_chunk_id_is_filesystem_safe() -> None:
    """编号要进 `figures/{chunk_id}.webp` 这个**文件路径**。.

    第一版用 `version_id + 序号` 拼，得到 `policy/travel-cn#a91f#c001000-000`
    ——目录不存在，`#` 在 URL 里还是 fragment 分隔符，取图必 404。
    """
    chunk_id = chunk_id_for(
        "policy/travel-cn#a91f2b3c4d5e", page_num=1, kind="figure", index=0
    )
    assert "/" not in chunk_id
    assert "#" not in chunk_id
    assert chunk_id.startswith("ch_")
    assert len(chunk_id) == len("ch_") + 12


def test_image_path_is_relative_and_matches_the_doc_shape() -> None:
    """Run test_image_path_is_relative_and_matches_the_doc_shape."""
    assert image_path_for("ch_abc123def456") == "figures/ch_abc123def456.webp"


# --- 3. 子片：尺寸与重叠 -----------------------------------------------------


def _long_text(chars: int) -> str:
    """造一段可切的长文本：**每句都不一样**。.

    第一版用的是同一句话重复，结果测试里的"重叠长度探测"会在重复内容上匹配到
    一个假的长重叠（496 字），把"切分丢字"变成假阳性。夹具必须可辨识。
    """
    parts: list[str] = []
    index = 0
    while sum(len(part) for part in parts) < chars:
        parts.append(f"第{index}条：住宿标准按城市分档执行。")
        index += 1
    return "".join(parts)[:chars]


def test_short_text_is_one_piece() -> None:
    """Run test_short_text_is_one_piece."""
    assert build_child_pieces("短句。") == ["短句。"]


def test_pieces_respect_the_upper_bound() -> None:
    """Run test_pieces_respect_the_upper_bound."""
    pieces = build_child_pieces(_long_text(4000))
    assert all(len(piece) <= CHILD_MAX_CHARS for piece in pieces)


def test_pieces_cover_the_whole_text() -> None:
    """切分不能丢字：拼起来（去重叠）必须等于原文。."""
    text = _long_text(3000)
    pieces = build_child_pieces(text)
    rebuilt = pieces[0]
    for piece in pieces[1:]:
        overlap = _overlap(rebuilt, piece)
        assert (
            overlap >= CHILD_OVERLAP_CHARS - 5
        ), f"overlap {overlap} too small"
        rebuilt += piece[overlap:]
    assert rebuilt == text


def _overlap(built: str, piece: str) -> int:
    """`piece` 与已拼好的文本的重叠长度（最长公共后缀/前缀）。.

    只看最后 `CHILD_MAX_CHARS` 个字符：再长的重叠不可能是"回退 64 字"造成的，
    而且重复文本会让暴力搜索匹配到一个假的长重叠。
    """
    window = built[-CHILD_MAX_CHARS:]
    for size in range(min(len(window), len(piece)), 0, -1):
        if window.endswith(piece[:size]):
            return size
    return 0


def test_overlap_is_about_the_configured_size() -> None:
    """Run test_overlap_is_about_the_configured_size."""
    pieces = build_child_pieces(_long_text(2000))
    assert len(pieces) >= 4
    for previous, current in zip(pieces, pieces[1:], strict=False):
        overlap = _overlap(previous, current)
        assert CHILD_OVERLAP_CHARS - 5 <= overlap <= CHILD_OVERLAP_CHARS + 30


def test_absurd_overlap_degrades_to_at_most_half_a_piece() -> None:
    """`overlap` 配得比 `max_chars` 还大时，钳到一半，绝不退化成"每字一片"。.

    第一版只在 `overlap >= max_chars` 时取 1 字步进，实测把 1500 字切成 1401 片
    ——不是死循环，但等价于"每字一片"，既慢又完全丢掉语义。
    """
    pieces = build_child_pieces(_long_text(1500), max_chars=100, overlap=500)
    assert len(pieces) <= 1500 / 50 + 2  # 步长至少是 max_chars 的一半
    assert all(len(piece) <= 100 for piece in pieces)


def test_zero_max_chars_returns_nothing() -> None:
    """`max_chars=0` 是无效配置，返回空列表而不是切出单个字符。."""
    assert build_child_pieces("abc", max_chars=0, overlap=64) == []


def test_empty_text_yields_nothing() -> None:
    """Run test_empty_text_yields_nothing."""
    assert build_child_pieces("   ") == []


# --- 4. 句子边界 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("第一句。第二句！第三句？", ["第一句。", "第二句！", "第三句？"]),
        ("One. Two. Three.", ["One.", "Two.", "Three."]),
        ("分号；也算一句", ["分号；", "也算一句"]),
    ],
)
def test_split_sentences_keeps_the_punctuation(
    text: str, expected: list[str]
) -> None:
    """Run test_split_sentences_keeps_the_punctuation."""
    assert split_sentences(text) == expected


def test_split_long_text_prefers_soft_break_points() -> None:
    """硬切只在单句超长时发生，且尽量在逗号/空格处断。.

    把 `CNY 500` 切成 `CNY 5` + `00` 会让数字检索彻底失效。
    """
    text = "a" * 40 + "， " + "b" * 40
    pieces = split_long_text(text, max_chars=50)
    assert pieces[0].endswith("，") or pieces[0].endswith(" ")
    assert all(len(piece) <= 50 for piece in pieces)


# --- 5. 父片 -----------------------------------------------------------------


def test_parent_slices_respect_the_upper_bound() -> None:
    """Run test_parent_slices_respect_the_upper_bound."""
    text = "\n".join(_long_text(400) for _ in range(8))
    pieces = parent_slices(text, max_chars=PARENT_MAX_CHARS)
    assert all(len(piece) <= PARENT_MAX_CHARS for piece in pieces)


def test_parent_slices_prefer_paragraph_boundaries() -> None:
    """§3.5：按章节标题切，不按字数硬切。."""
    paragraphs = [f"段落{i}：" + "内容" * 60 for i in range(6)]
    pieces = parent_slices("\n".join(paragraphs), max_chars=PARENT_MAX_CHARS)
    for piece in pieces:
        for line in piece.splitlines():
            assert line in paragraphs


def test_parent_slices_split_an_oversized_paragraph_again() -> None:
    """一章超过 2048 字时要再切一层（§3.5 边界表）。."""
    single = "非常长的段落。" * 800
    pieces = parent_slices(single, max_chars=PARENT_MAX_CHARS)
    assert len(pieces) > 1
    assert all(len(piece) <= PARENT_MAX_CHARS for piece in pieces)


# --- 6. 儿子片关系（从区域建切片） --------------------------------------------


def _region(text: str, *, lines: int = 3) -> Region:
    """造一个有 `lines` 行的正文区域（正文按空格均分到各行）。."""
    words = text.split(" ")
    per_line = max(1, len(words) // lines)
    chunks = [
        " ".join(words[index * per_line : (index + 1) * per_line])
        for index in range(lines)
    ]
    line_objs = tuple(
        Line(
            text=chunk,
            rect=Rect(
                72.0,
                100.0 + index * 16,
                72.0 + len(chunk) * 5,
                110.0 + index * 16,
            ),
            page_num=1,
            font_size=10.0,
        )
        for index, chunk in enumerate(chunks)
    )
    return Region(
        kind=RegionKind.TEXT,
        page_num=1,
        rect=Rect(72.0, 100.0, 500.0, 100.0 + lines * 16),
        text=" ".join(chunks),
        lines=line_objs,
    )


def test_parent_and_child_relationship_is_well_formed() -> None:
    """Run test_parent_and_child_relationship_is_well_formed."""
    from api.parse.chunking import build_child_chunks, build_parent_chunk
    from api.parse.dates import BusinessDate

    space = PageSpace(0, 595.0, 842.0)
    region = _region("差旅住宿标准 每人每天 500 元 一线城市 其他城市 380 元")
    bboxes = [region.bbox(space)] * len(region.lines)
    parent_id = chunk_id_for("v#abc", page_num=1, kind="parent", index=0)
    parent = build_parent_chunk(
        doc_key="policy/x",
        version_id="v#abc",
        region=region,
        page_bboxes=bboxes,
        acl=ACL,
        date=BusinessDate(20250301, 2025, "explicit"),
        chunk_id=parent_id,
    )
    parts = range(child_piece_count(region))
    children = build_child_chunks(
        doc_key="policy/x",
        version_id="v#abc",
        region=region,
        page_bboxes=bboxes,
        acl=ACL,
        date=BusinessDate(20250301, 2025, "explicit"),
        parent_id=parent_id,
        child_ids=[
            chunk_id_for("v#abc", page_num=1, kind="child", index=0, part=part)
            for part in parts
        ],
    )
    assert parent.parent_id is None
    assert children, "region should produce at least one child"
    assert all(child.parent_id == parent.chunk_id for child in children)
    assert parent.require_complete() is parent


def test_child_chunks_need_matching_id_count() -> None:
    """Run test_child_chunks_need_matching_id_count."""
    from api.parse.chunking import build_child_chunks
    from api.parse.dates import BusinessDate

    space = PageSpace(0, 595.0, 842.0)
    region = _region("短文本 一句 两句 三句")
    with pytest.raises(ValueError):
        build_child_chunks(
            doc_key="policy/x",
            version_id="v#abc",
            region=region,
            page_bboxes=[region.bbox(space)],
            acl=ACL,
            date=BusinessDate(),
            parent_id=None,
            child_ids=[],
        )


def test_child_piece_count_matches_the_pieces() -> None:
    """Run test_child_piece_count_matches_the_pieces."""
    region = _region(_long_text(1200), lines=4)
    assert child_piece_count(region) == len(build_child_pieces(region.text))


def test_child_bbox_covers_only_its_own_lines() -> None:
    """子片的 bbox 取"它覆盖的那几行"的并集，不是整段。.

    整段的 bbox 会让前端高亮框住一大块，用户看不出是哪一句。
    """
    from api.parse.chunking import build_child_chunks
    from api.parse.dates import BusinessDate

    space = PageSpace(0, 595.0, 842.0)
    region = _region(_long_text(1200), lines=6)
    bboxes = [region.bbox(space)]
    children = build_child_chunks(
        doc_key="policy/x",
        version_id="v#abc",
        region=region,
        page_bboxes=[region.bbox(space)] * 6,
        acl=ACL,
        date=BusinessDate(),
        parent_id=None,
        child_ids=[
            chunk_id_for("v#abc", page_num=1, kind="child", index=0, part=part)
            for part in range(child_piece_count(region))
        ],
    )
    assert len(children) == child_piece_count(region)
    for child in children:
        assert child.bbox
        assert child.require_complete() is child
    del bboxes


# --- 7. 表格切片 -------------------------------------------------------------


def test_table_chunk_keeps_all_three_representations() -> None:
    """Run test_table_chunk_keeps_all_three_representations."""
    from api.parse.chunking import build_table_chunk
    from api.parse.dates import BusinessDate

    table = build_table(
        [
            TableCell(0, 0, "City", is_header=True),
            TableCell(0, 1, "Cap", is_header=True),
            TableCell(1, 0, "Shanghai"),
            TableCell(1, 1, "500"),
        ],
        row_count=2,
        col_count=2,
    )
    region = Region(
        kind=RegionKind.TABLE,
        page_num=1,
        rect=Rect(72, 100, 500, 200),
        table=table,
    )
    chunk = build_table_chunk(
        doc_key="policy/x",
        version_id="v#abc",
        region=region,
        bbox=[0.1, 0.1, 0.8, 0.3],
        acl=ACL,
        date=BusinessDate(),
        chunk_id="ch_tttttttttttt",
        content_text="City: Shanghai | Cap: 500",
    )
    assert chunk.table_html == table.html
    assert chunk.table_md == table.md
    assert chunk.table_cells is not None
    assert len(chunk.table_cells) == 4
    assert chunk.require_complete() is chunk
