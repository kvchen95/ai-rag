"""OCR 兜底（详细设计.md §3.2「扫描页：识别后按文本框位置算坐标」）。

**可配置**，默认关闭：OCR 慢（本机 CPU 实测 1.3 秒/页）、有错字（实测把
`CNY 500` 认成 `CNY 5o0`），所以它只在"这一页没有文字层"时才跑。开了也不会对
有文字层的页跑——那会把准确的文字层换成有错字的识别结果。

### 两个必须接住的行为

1. **识别结果的坐标是像素**，要按渲染缩放比转回阅读空间再归一化。直接拿像素当 pt
   会让 bbox 全部越界（被夹到 1.0），高亮框缩在右下角一小块。
2. **识别会漏字**。`5o0`（字母 o）这种不是靠替换能修的——所以 OCR 出来的行
   `source="ocr"`、`confidence` 取引擎给的分数，下游（数值校验）能据此放宽或拒绝。
   这里不做"猜着修"：把 `5o0` 改成 `500` 是造数据，不是识别。

### 引擎

`OcrEngine` 是协议，默认实现用 rapidocr-onnxruntime（纯 onnx，CPU 可跑，无需系统装
tesseract）。它不在 `requirements.txt` 里是**故意**的：那是 ~100MB 的 onnx 运行时，
而多数部署的样本有文字层，不该为少数扫描件让每个镜像都胖一圈。缺它时
`build_ocr_engine()` 返回 `None` 并把原因写进 `PageLayout.ocr_skipped_reason`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, Sequence

from api.parse.geometry import PageSpace, Rect
from api.parse.layout import extract_lines
from api.parse.model import Line


@dataclass(frozen=True, slots=True)
class OcrWord:
    """一条识别结果（阅读空间的 pt 坐标）。"""

    text: str
    rect: Rect
    confidence: float


class OcrEngine(Protocol):
    """唯一的接口：图片路径 -> 识别出的文字块。"""

    name: str

    def recognize(self, image: Any) -> list[OcrWord]: ...


# 一页"没有文字但有位图"时，这张位图至少占页面多少才认为它承载内容。
# 0.02 的依据：水印/logo 通常 < 1%，而 A4 上贴的一张单据哪怕只占三分之一宽也有
# 10% 以上。两者数量级差得远，所以不需要更细的启发式——只要别把装饰小图当扫描件。
CONTENT_IMAGE_MIN_RATIO = 0.02


@dataclass(frozen=True, slots=True)
class OcrConfig:
    """OCR 开关与参数。`enabled=False` 时整条兜底不跑（默认）。"""

    enabled: bool = False
    # 只有字符数低于这个阈值才认为"没有文字层"。给 10 是为了容忍"扫描件页眉被
    # 别的工具塞了少量文字层"的情况——那些字不足以重建版面。
    min_chars_for_text_layer: int = 10
    # 识别置信度低于它的块直接丢：OCR 的错字多半置信度也低，留着反而污染检索。
    min_confidence: float = 0.55
    # 渲染 DPI：识别质量随分辨率上升，150 是"够认字又不慢"的折中（200 DPI 会把
    # 单页耗时推到 2 秒以上）。
    render_dpi: int = 150


def text_layer_missing(page: Any, config: OcrConfig) -> bool:
    """这一页要不要走 OCR：没有文字层、且有**位图**（扫描件的特征）。

    ### 为什么判据是"有位图"而不是"位图覆盖半页"

    第一版要求位图覆盖 ≥ 25% 页面面积。实测 `scanned-demo` 的第 2 页：一张
    390×300pt 的位图，占页面的 **23%**，差两个百分点就被判成"不是扫描件"，
    于是那一页一条切片都出不来。而它千真万确是扫描页——没有文字、整页内容
    就是那张图。

    覆盖比例不是扫描件的判据：传真件、单据贴图、印章页的位图都可能只占一小块，
    但上面全是唯一的内容。判据只该是"这页没有文字层，但有位图——那文字只能
    在图里"。

    **唯一的例外是装饰性小图**（logo、分隔线、水印），它们上面没有正文。
    用相对面积筛掉它们可以，但门槛必须很低：0.02 就够区分"水印"与"半页单据"。
    """
    if not config.enabled:
        return False
    chars = [char for char in page.chars if char.get("text", "").strip()]
    if len(chars) >= config.min_chars_for_text_layer:
        return False
    return _has_content_image(page)


def _has_content_image(page: Any) -> bool:
    """页面上是否有一张**承载内容**的位图（而不是装饰性小图）。

    门槛取 2%：水印/logo 通常 < 1%，而一张 A4 上的单据贴图哪怕只占三分之一宽
    也有 10% 以上。这个数量级差异足够分开，不需要更细的启发式。
    """
    page_area = float(page.width) * float(page.height)
    if page_area <= 0:
        return False
    for image in page.images:
        area = abs(float(image["x1"]) - float(image["x0"])) * abs(
            float(image["bottom"]) - float(image["top"])
        )
        if area / page_area >= CONTENT_IMAGE_MIN_RATIO:
            return True
    return False


def ocr_lines(
    page: Any,
    space: PageSpace,
    engine: OcrEngine,
    *,
    page_num: int,
    config: OcrConfig,
    render_scale: float,
    render: Callable[[], Any] | None = None,
) -> list[Line]:
    """对一页做 OCR 并返回阅读空间的行。

    `render_scale` 是渲染用的"像素/pt"。识别结果是像素坐标，除以它才回到显示空间
    pt，再经 `space.display_to_reading` 到阅读空间——两步都不能省。

    `render` 是**整页位图的来源**。不给时按"`page` 本身能渲染"尝试（pypdfium2 的
    `PdfPage` 就满足）；给的话由调用方负责拿到 pypdfium2 页面并渲染。
    为什么要这个参数：解析走 pdfplumber（要字符与线段），渲染只有 pypdfium2 能做，
    两者的页对象不通用——第一版直接把 pdfplumber 的 `Page` 传进 `page.render()`，
    运行时报 `'Page' object has no attribute 'render'`。这条路径当时没有任何用例
    走过（OCR 默认关），所以一直是坏的。
    """
    image = render() if render is not None else _render_page(page, render_scale)
    words = engine.recognize(image)
    kept = [word for word in words if word.confidence >= config.min_confidence]
    if not kept:
        return []
    display_words: list[dict[str, Any]] = []
    for word in kept:
        display = Rect(
            word.rect.x0 / render_scale,
            word.rect.top / render_scale,
            word.rect.x1 / render_scale,
            word.rect.bottom / render_scale,
        )
        reading = space.display_to_reading(display)
        display_words.append(
            {
                "text": word.text,
                "x0": reading.x0,
                "top": reading.top,
                "x1": reading.x1,
                "bottom": reading.bottom,
                "upright": True,
                "_confidence": word.confidence,
            }
        )
    lines = extract_lines(display_words, page_num)
    return [
        Line(
            text=line.text,
            rect=line.rect,
            page_num=line.page_num,
            font_size=line.font_size,
            source="ocr",
            confidence=_mean(
                [float(w["_confidence"]) for w in display_words if w["text"] in line.text]
            ),
        )
        for line in lines
    ]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 1.0


def _render_page(page: Any, scale: float) -> Any:
    """渲染成 PIL 图。延迟 import：没装 pypdfium2 的部署不该连 import 都失败。"""
    from api.parse.figures import render_page  # noqa: PLC0415 - 延迟：避免顶层依赖

    return render_page(page, scale=scale)


def build_ocr_engine(config: OcrConfig) -> tuple[OcrEngine | None, str | None]:
    """构造 OCR 引擎。返回 `(engine, skip_reason)`。

    `enabled=False` 时返回 `(None, "disabled")`——把"没开"与"想开但装不上"分开报，
    否则排障时分不清是配置没开还是依赖没装。
    """
    if not config.enabled:
        return None, "disabled"
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: PLC0415
    except ImportError:
        return None, "engine_missing: rapidocr-onnxruntime is not installed"
    engine = _RapidOcrEngine(RapidOCR())
    return engine, None


class _RapidOcrEngine:
    """rapidocr-onnxruntime 的薄封装。

    它吃"图片路径或 ndarray"；这里用 ndarray（PIL -> numpy），省一次落盘。
    返回 `(boxes, texts, scores)`，boxes 是四点多边形（左上、右上、右下、左下）。
    """

    name = "rapidocr-onnxruntime"

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def recognize(self, image: Any) -> list[OcrWord]:
        import numpy as np  # noqa: PLC0415 - 只在真跑 OCR 时需要

        array = np.array(image.convert("RGB"))
        result, _elapsed = self._engine(array)
        if not result:
            return []
        words: list[OcrWord] = []
        for item in result:
            box, text, score = item[0], item[1], item[2]
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            words.append(
                OcrWord(
                    text=str(text),
                    rect=Rect(min(xs), min(ys), max(xs), max(ys)),
                    confidence=float(score),
                )
            )
        return words


__all__ = [
    "OcrConfig",
    "OcrEngine",
    "OcrWord",
    "build_ocr_engine",
    "ocr_lines",
    "text_layer_missing",
]
