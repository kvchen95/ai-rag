"""零宽字符与字宽兜底（§3.2 的隐含前提：坐标得能用）。

**这一组是被真实样本逼出来的**：`scripts/make_samples.py` 生成的三份制度
文件用标准 Helvetica 但没写 `/Widths`，pdfplumber 照实报每个字符 `w=0.00`
（实测 268 个字符全是零宽）。后果不是“坐标略偏”，而是：

* 行宽 = 0 → `bbox` 退化成一个竖条 → `Chunk.missing_fields()` 把 `bbox(invalid)`
  报成缺失 → **整份文档一条切片都出不来**；
* 相邻字符间距 = 0 → 拼出来的文本是 `TravelExpensePolicy(CN)`（连写），
  词法检索里 `travel expense` 一个词都命中不了。

兜底不是"猜内容"：字符本身没变，只是把没写下来的字宽按字体经验值估出来。
所以这里的判据是"文本完全正确、bbox 有实际宽度"，而不是"看起来差不多"。
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber
import pytest

from api.parse.chunking import Acl
from api.parse.geometry import PageSpace
from api.parse.layout import (
    FALLBACK_CJK_EM,
    FALLBACK_LATIN_EM,
    FALLBACK_SPACE_EM,
    extract_lines,
    fill_missing_widths,
    reading_chars,
)
from api.parse.pipeline import parse_document

REPO_ROOT = Path(__file__).resolve().parents[2]
PORTRAIT = REPO_ROOT / "samples" / "policy" / "travel-cn.pdf"
LANDSCAPE = REPO_ROOT / "samples" / "hr" / "leave-2025.pdf"

pytestmark = pytest.mark.skipif(
    not PORTRAIT.exists(),
    reason="samples/ 不在仓库里（先跑 scripts/make_samples.py）",
)


def _char(
    text: str, *, x: float, top: float, height: float = 11.0, width: float = 0.0
) -> dict:
    return {
        "text": text,
        "x0": x,
        "x1": x + width,
        "top": top,
        "bottom": top + height,
        "upright": True,
    }


# --- 1. 真实样本确实是零宽的（前提事实） --------------------------------------


def test_real_samples_have_zero_width_chars() -> None:
    """先确认这个坑真实存在，否则后面的兜底测试是在测一个不存在的问题。

    这条前提**只对 `make_samples.py` 造的样本成立**：它用标准 Helvetica 且不写
    `/Widths`，pdfplumber 照实报 `w=0.00`。真实财报/研报
    （`samples/pdf_reports/`）
    带完整字宽，这条断言在它们身上是假的——所以只在"样本确实是合成件"时才验。
    """
    with pdfplumber.open(str(PORTRAIT)) as pdf:
        chars = pdf.pages[0].chars
    assert chars
    zero_width = all(abs(float(c["x1"]) - float(c["x0"])) < 0.01 for c in chars)
    if not zero_width:
        pytest.skip(
            "样本已换成带字宽的真实 PDF，零宽兜底的前提不再成立；"
            "下面的兜底用例仍以合成样本为输入，不受影响"
        )


# --- 2. 兜底：文本与行宽都要恢复正常 -----------------------------------------


def test_fallback_restores_spaces_and_line_width() -> None:
    """`Travel Expense Policy (CN)` 不能被压成 `TravelExpensePolicy(CN)`。

    宽度也不能是 0。
    """
    with pdfplumber.open(str(PORTRAIT)) as pdf:
        page = pdf.pages[0]
        space = PageSpace.from_display(page)
        lines = extract_lines(reading_chars(page, space), 1)
    first = lines[0]
    assert first.text == "Travel Expense Policy (CN) - 2025 revision"
    assert first.rect.width > 100, f"line width collapsed: {first.rect.width}"


def test_fallback_keeps_the_words_separated_across_the_document() -> None:
    """整份文档的每一行都该有空格（不是只有第一行）。"""
    with pdfplumber.open(str(PORTRAIT)) as pdf:
        space = PageSpace.from_display(pdf.pages[0])
        lines = extract_lines(reading_chars(pdf.pages[0], space), 1)
    joined = " ".join(line.text for line in lines)
    assert "Lodging: CNY 500 per person per night" in joined
    assert "Effective date: 2025-03-01" in joined


def test_fallback_is_proportional_to_font_size() -> None:
    """估算宽度按字号走：11pt 的字按 0.5em = 5.5pt 宽。

    硬编码一个固定像素宽会在不同字号的页上让 bbox 全部失真——而字号差一倍的
    标题/正文在同一页上很常见。
    """
    chars = fill_missing_widths(
        [
            _char("a", x=100.0, top=10.0, height=20.0),
            _char("b", x=100.0, top=40.0, height=10.0),
        ]
    )
    by_text = {c["text"]: c for c in chars}
    assert by_text["a"]["x1"] - by_text["a"]["x0"] == pytest.approx(
        20.0 * FALLBACK_LATIN_EM
    )
    assert by_text["b"]["x1"] - by_text["b"]["x0"] == pytest.approx(
        10.0 * FALLBACK_LATIN_EM
    )


def test_fallback_uses_full_em_for_cjk() -> None:
    """中文按 1 em：汉字是方块字，用 0.5em 会让行宽只有一半。"""
    chars = fill_missing_widths([_char("住", x=100.0, top=10.0, height=12.0)])
    assert chars[0]["x1"] - chars[0]["x0"] == pytest.approx(
        12.0 * FALLBACK_CJK_EM
    )


def test_fallback_uses_a_narrow_space() -> None:
    chars = fill_missing_widths([_char(" ", x=100.0, top=10.0, height=10.0)])
    assert chars[0]["x1"] - chars[0]["x0"] == pytest.approx(
        10.0 * FALLBACK_SPACE_EM
    )


def test_fallback_advances_the_cursor_within_a_line() -> None:
    """同一行里字符必须依次排开，而不是全叠在同一个 x 上。

    `x0` 全相同的 PDF 里只给宽度不推进游标，拼出来的行宽还是 0。
    """
    chars = fill_missing_widths(
        [
            _char("a", x=72.0, top=10.0),
            _char("b", x=72.0, top=10.0),
            _char("c", x=72.0, top=10.0),
        ]
    )
    positions = [(c["x0"], c["x1"]) for c in chars]
    assert positions[0][0] == 72.0
    assert positions[1][0] == pytest.approx(positions[0][1])
    assert positions[2][0] == pytest.approx(positions[1][1])
    assert positions[2][1] > positions[0][1]


def test_fallback_keeps_different_lines_independent() -> None:
    """游标按 `top` 分开：上一行的游标不能把下一行推到右边去。"""
    chars = fill_missing_widths(
        [_char("a", x=72.0, top=10.0), _char("b", x=72.0, top=30.0)]
    )
    assert all(c["x0"] == 72.0 for c in chars)


def test_fallback_leaves_valid_widths_untouched() -> None:
    """有真实字宽的 PDF 不能被改写（那会把准确的坐标换成估算值）。"""
    valid = _char("a", x=72.0, top=10.0, width=6.0)
    out = fill_missing_widths([valid])
    assert (out[0]["x0"], out[0]["x1"]) == (72.0, 78.0)


def test_fallback_does_not_mutate_the_input() -> None:
    original = _char("a", x=72.0, top=10.0)
    snapshot = dict(original)
    fill_missing_widths([original])
    assert original == snapshot


# --- 3. 端到端：真实样本能产出完整切片 --------------------------------------


@pytest.mark.parametrize(
    "path", [PORTRAIT, LANDSCAPE], ids=lambda p: Path(p).stem
)
def test_zero_width_sample_produces_complete_chunks(path: Path) -> None:
    """零宽样本也必须产出**通过六项校验**的切片。

    这是"字段缺失被校验拦截"的另一面：如果兜底没做，`parse_document` 会直接抛
    `IncompleteChunk`（bbox 无效），整份文档一条切片都不出——那是比"bbox 略偏"
    严重得多的事故。
    """
    outcome = parse_document(
        path,
        doc_key="verify/" + path.stem,
        version_id=f"verify/{path.stem}#abcdef123456",
        acl=Acl(is_public=False, allowed_depts=("fin",)),
    )
    assert outcome.chunks, "no chunks produced from a zero-width sample"
    for chunk in outcome.chunks:
        assert chunk.missing_fields() == [], chunk.missing_fields()
        rect = chunk.bbox[0]
        assert rect[2] > rect[0], f"bbox has no width: {rect}"
        assert rect[3] > rect[1], f"bbox has no height: {rect}"


def test_zero_width_sample_bbox_covers_the_text() -> None:
    """行宽估算之后，切片的 bbox 必须是一个**落在页面内、有实际宽度**的矩形。

    判据只用两条硬边界（宽度 > 100pt 且 ≤ 页面宽）：再往上加"等于多少 pt"就是在
    断言估算系数的具体取值，而那是可以调的参数，不是正确性。
    第一条（> 100pt）足以拦住"零宽 bbox"，那才是这条测试要防的事故。
    """
    outcome = parse_document(
        PORTRAIT,
        doc_key="verify/portrait",
        version_id="verify/portrait#abcdef123456",
        acl=Acl(is_public=True),
    )
    chunk = outcome.chunks[0]
    width_pt = (chunk.bbox[0][2] - chunk.bbox[0][0]) * 595.0
    assert width_pt > 100, f"bbox width collapsed: {width_pt}"
    assert width_pt <= 595.0, f"bbox exceeds the page width: {width_pt}"
