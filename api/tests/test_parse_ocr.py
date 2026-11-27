"""OCR 兜底（§3.2「扫描页：识别后按文本框位置算坐标」）。

这一组的存在理由：OCR 默认关，所以它的整条链路**从来没被真跑过**。本轮把
`OcrConfig(enabled=True)` 打开后连出三个 bug：

1. `text_layer_missing` 要求位图覆盖 ≥ 25% 页面 —— 真实扫描件常小于这个值；
2. `ocr_lines` 拿 pdfplumber 的 `Page` 去调 `page.render()` —— 那个方法属于
   pypdfium2，运行时报 `'Page' object has no attribute 'render'`；
3. OCR 认出的行落在图区域内，被 `blocked` 扣掉 —— 那页 `ocr_applied=True`
   却一条切片都没有。

所以这里的用例不是"跑一遍不报错"，而是逐条钉住这三件事，以及"不猜着修"。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from api.parse.chunking import Acl
from api.parse.ocr import (
    CONTENT_IMAGE_MIN_RATIO,
    OcrConfig,
    OcrWord,
    build_ocr_engine,
    text_layer_missing,
)
from api.parse.pipeline import parse_document
from api.parse.reader import ReaderConfig, read_pdf

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


@pytest.fixture(scope="module")
def scanned_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_layout_samples  # noqa: PLC0415

    root = tmp_path_factory.mktemp("scanned")
    target = root / "layout" / "scanned-demo.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(make_layout_samples.build("layout/scanned-demo.pdf"))
    return target


class _FakePage:
    """最小的 page 替身：只需要 `chars` / `images` / `width` / `height`。"""

    def __init__(
        self, chars: list[str], images: list[tuple[float, float, float, float]]
    ) -> None:
        self.width = 595.0
        self.height = 842.0
        self.chars = [{"text": ch} for ch in chars]
        self.images = [
            {"x0": x0, "top": top, "x1": x1, "bottom": bottom}
            for x0, top, x1, bottom in images
        ]


class _StubEngine:
    """固定返回若干 `OcrWord` 的替身引擎。

    用它把 OCR 的**后处理**（坐标换算、置信度过滤、行聚类）与引擎本身解耦
    ——真实引擎的识别质量不该影响这些判据的测试。
    """

    name = "stub"

    def __init__(self, words: list[OcrWord]) -> None:
        self.words = words
        self.calls = 0

    def recognize(self, image: object) -> list[OcrWord]:  # noqa: ARG002
        self.calls += 1
        return list(self.words)


# --- 1. 判据：什么时候该走 OCR ----------------------------------------------


def test_disabled_config_never_ocrs() -> None:
    page = _FakePage([], [(72.0, 100.0, 462.0, 400.0)])
    assert not text_layer_missing(page, OcrConfig(enabled=False))


def test_page_with_a_real_text_layer_never_ocrs() -> None:
    """有文字层的页一旦 OCR，会把准确的文字换成有错字的识别结果。"""
    page = _FakePage(list("x" * 200), [(72.0, 100.0, 462.0, 400.0)])
    assert not text_layer_missing(page, OcrConfig(enabled=True))


def test_a_small_but_real_scan_is_still_ocrd() -> None:
    """**这条是本轮修的第一个 bug**：位图只占页面 23% 的扫描页必须走 OCR。

    原判据要求覆盖 ≥ 25%，`scanned-demo` 第 2 页（390×300pt/595×842pt ≈ 23%）
    就被判成"不是扫描件"，那一页一条切片都出不来。传真件、单据贴图、印章页
    都是这种尺寸。
    """
    page = _FakePage([], [(72.0, 400.0, 462.0, 700.0)])  # 390×300 = 23%
    assert text_layer_missing(page, OcrConfig(enabled=True))


def test_a_blank_page_with_a_tiny_logo_is_not_ocrd() -> None:
    """装饰性小图（logo/水印 < 2%）不该触发 OCR。

    它上面没有正文，只会产生噪声。
    """
    # 40×40pt on 595×842 = 0.3%
    page = _FakePage([], [(72.0, 100.0, 112.0, 140.0)])
    assert not text_layer_missing(page, OcrConfig(enabled=True))


def test_a_blank_page_with_no_image_is_not_ocrd() -> None:
    assert not text_layer_missing(_FakePage([], []), OcrConfig(enabled=True))


def test_content_image_ratio_threshold_is_low_enough_for_documents() -> None:
    """阈值本身：半页单据（~15%）必须过线，水印（~0.5%）必须不过。"""
    assert CONTENT_IMAGE_MIN_RATIO < 0.15
    assert CONTENT_IMAGE_MIN_RATIO > 0.001


# --- 2. 引擎构造：把"没开"与"装不上"分开 --------------------------------


def test_disabled_engine_reports_the_reason() -> None:
    engine, reason = build_ocr_engine(OcrConfig(enabled=False))
    assert engine is None
    assert reason == "disabled"


def test_enabled_engine_is_either_available_or_says_why() -> None:
    """装了 rapidocr 就必须给出引擎，没装就必须给出原因——不许两者都空。"""
    engine, reason = build_ocr_engine(OcrConfig(enabled=True))
    assert (engine is None) != (
        reason is None
    ), "engine 与 reason 必须恰好有一个"
    if engine is None:
        assert "rapidocr" in (reason or "")


# --- 3. 端到端：扫描页真的进切片，且带来源标记 -----------------------------


def test_scanned_page_produces_chunks_with_the_ocr_source_note(
    scanned_pdf: Path,
) -> None:
    """**这条是本轮第二、三个 bug 的克星。**

    它要求三件事同时成立：
      * 走的是真引擎（不是替身）—— 否则渲染路径的 `'Page' object has no
        attribute 'render'` 抓不到；
      * 扫描页真的产出切片 —— 否则"OCR 跑了但被图区域扣掉"抓不到；
      * 切片带 `source:ocr` —— 否则下游分不清文字层与识别结果。
    """
    engine, _ = build_ocr_engine(OcrConfig(enabled=True))
    if engine is None:
        pytest.skip("rapidocr-onnxruntime not installed")

    outcome = parse_document(
        scanned_pdf,
        doc_key="layout/scanned",
        version_id="layout/scanned#abcdef123456",
        acl=Acl(is_public=True),
        reader_config=ReaderConfig(ocr=OcrConfig(enabled=True)),
        ocr_engine=engine,
    )
    page2 = next(page for page in outcome.doc.pages if page.page_num == 2)
    assert page2.ocr_applied, "the scanned page was not OCR'd"
    assert page2.ocr_skipped_reason is None

    page2_chunks = [chunk for chunk in outcome.chunks if chunk.page_num == 2]
    assert (
        page2_chunks
    ), "OCR ran but produced no chunk (blocked by the figure region?)"
    assert all("source:ocr" in chunk.notes for chunk in page2_chunks)
    assert all(chunk.content_text.strip() for chunk in page2_chunks)


def test_scanned_page_without_an_engine_records_why(scanned_pdf: Path) -> None:
    """引擎不可用时要**说出来**，不能静默少一页。

    静默降级最坏的情况是：一页扫描件没入库，而报告里看不出少了什么。
    """
    doc = read_pdf(
        scanned_pdf,
        doc_key="layout/scanned",
        config=ReaderConfig(ocr=OcrConfig(enabled=True)),
        ocr_engine=None,
    )
    page2 = next(page for page in doc.pages if page.page_num == 2)
    assert page2.ocr_skipped_reason == "engine_unavailable"
    assert any(
        "OCR needed but engine unavailable" in warning
        for warning in doc.warnings
    )


def test_ocr_off_by_default(scanned_pdf: Path) -> None:
    """默认配置下绝不 OCR：慢（1.3s/页）且有错字，不该是默认行为。"""
    doc = read_pdf(scanned_pdf, doc_key="layout/scanned")
    assert all(not page.ocr_applied for page in doc.pages)
    assert {page.ocr_skipped_reason for page in doc.pages} == {"disabled"}


# --- 4. OCR 行的来源标记 ----------------------------------------------------


def test_ocr_lines_are_not_silently_corrected() -> None:
    """`5o0` 不许改成 `500`。

    这是 `§3.2` 的硬要求：识别错字是事实的一部分，下游（数值校验、引用展示）
    要能看到原文并自行判断。改成"看起来对"的数字等于造数据。
    """
    engine = _StubEngine([OcrWord("CNY 5o0", _rect(100, 200, 160, 212), 0.91)])
    lines = _run_stub(engine)
    assert len(lines) == 1
    assert lines[0].text == "CNY 5o0"
    assert "500" not in lines[0].text


def test_ocr_lines_carry_confidence_and_source() -> None:
    engine = _StubEngine(
        [OcrWord("Total 1200", _rect(100, 200, 200, 212), 0.77)]
    )
    lines = _run_stub(engine)
    assert lines[0].source == "ocr"
    assert lines[0].confidence == pytest.approx(0.77, abs=0.01)


def test_low_confidence_words_are_dropped() -> None:
    """置信度低于门槛的块丢掉：那多半就是错字，留着只会污染检索。"""
    engine = _StubEngine(
        [
            OcrWord("keep this", _rect(100, 200, 200, 212), 0.90),
            OcrWord("drp", _rect(100, 240, 140, 252), 0.20),
        ]
    )
    lines = _run_stub(engine, min_confidence=0.55)
    assert len(lines) == 1
    assert "keep this" in lines[0].text


def test_ocr_pixel_coordinates_are_converted_to_points() -> None:
    """**像素坐标必须除以渲染缩放比。**

    不除的话 bbox 全部越界（被夹到 1.0），高亮框会缩在页面右下角一小块。

    渲染 150 DPI = 2.083 px/pt。识别出的 200px → 96pt。
    """
    scale = 150 / 72
    engine = _StubEngine([OcrWord("x", _rect(200.0, 400.0, 300.0, 420.0), 0.9)])
    lines = _run_stub(engine, render_scale=scale)
    rect = lines[0].rect
    assert rect.x0 == pytest.approx(200.0 / scale, abs=0.5)
    assert rect.top == pytest.approx(400.0 / scale, abs=0.5)
    assert rect.x1 == pytest.approx(300.0 / scale, abs=0.5)


def _rect(x0: float, top: float, x1: float, bottom: float):
    from api.parse.geometry import Rect  # noqa: PLC0415

    return Rect(x0, top, x1, bottom)


def _run_stub(
    engine: _StubEngine,
    *,
    render_scale: float = 150 / 72,
    min_confidence: float = 0.55,
):
    """用固定位图跑 `ocr_lines`（不碰真 PDF，只验后处理）。"""
    from PIL import Image  # noqa: PLC0415

    from api.parse.geometry import PageSpace  # noqa: PLC0415
    from api.parse.ocr import ocr_lines  # noqa: PLC0415

    page = _FakePage([], [])
    space = PageSpace(0, page.width, page.height)
    return ocr_lines(
        page,
        space,
        engine,
        page_num=1,
        config=OcrConfig(enabled=True, min_confidence=min_confidence),
        render_scale=render_scale,
        render=lambda: Image.new("RGB", (100, 100), (255, 255, 255)),
    )
