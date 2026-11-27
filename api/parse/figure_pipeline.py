"""把「检测到的图区域」变成「已转译、已对账的 FigureData」的默认实现。.

这是 `reader.FigureBuilder` 的生产实现，四步顺序固定：

   裁图（≥200 DPI 无损 WebP 字节）→ VLM 转译 → 数值对账 → 交回字节 + FigureData

任何一步失败都返回 `FigureBuildResult(error=...)`，**图不进切片**：

| 失败点 | 原因码 | 为什么不入库 |
|---|---|---|
| 渲染/裁片 | `render: …` | 没有图就没有引用对象；路径悬空更糟 |
| 没配 VLM | `vlm_unavailable` | 空转译让检索命中一条没有内容的证据 |
| 转译无结构化输出 | `transcript: …` | 没有数值可对账 = 无法证明模型没读错 |
| 对账不过 | `reconcile: <检查名>` | §3.4 的硬要求：对不上就判失败 |

### 为什么图文件不在这里落盘

文件名要用 `chunk_id`，而 `chunk_id` 要等整页区域排完序、编号定下来才知道（图的
序号是本页 `regions` 里的下标）。所以这里只交 WebP **字节**，`pipeline` 在算好
编号之后统一下盘并回填 `image_path`。两处各拼一遍文件名，迟早一处写错、
库里的 `image_path` 指向一个不存在的文件——而那种错在检索阶段完全看不出来。

### 图里的"证据"从哪来

对账要拿**原始 PDF** 里的刻度/标签/柱高与模型的说法对撞，不能拿模型自己的
话说事。`collect_evidence` 做这件事：

* 图区域内的文字行 → `texts` 与 `numbers`（刻度、类别名、图注）；
* 矢量柱（`page.rects` 里落在图区域内的实心矩形）→ `bar_ratios`（高度 / 轴高）。

柱高比例这条很关键：矢量柱状图常常不印数值（只有刻度），此时唯一能对账的依据
就是"这根柱子的高度在轴上的位置"。
"""

from __future__ import annotations

import io
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from api.parse import figures as fig
from api.parse.figures_transcribe import (
    FigureEvidence,
    VLMClient,
    build_figure_data,
    extract_numbers,
    parse_transcript,
    prompt_for,
    reconcile,
)
from api.parse.geometry import Rect
from api.parse.layout import DetectedFigure
from api.parse.reader import FigureBuildRequest, FigureBuildResult

# 计算柱高比例时，轴高取"最高柱 × 这个系数"：柱顶通常比轴顶低一点（留白），
# 直接用最高柱当轴高会把百分比系统性放大。
AXIS_HEIGHT_SLACK = 1.15
# 柱子的最小尺寸（pt）：更小的矩形是刻度线、图例方块，不是柱。
BAR_MIN_WIDTH = 6.0
BAR_MIN_HEIGHT = 8.0


@dataclass(frozen=True, slots=True)
class FigurePipelineConfig:
    """裁图与转译参数。."""

    # 目标 DPI。`figures.MIN_FIGURE_DPI` 是硬下限（200），这里给 220 留余量
    # ——渲染出来的实际 DPI 会因取整与最小像素宽略有偏差，贴着 200 易掉线。
    dpi: float = 220.0
    # 裁片额外留白（pt）：让刻度与标签完整落进图里，不被切掉半个字。
    pad_pt: float = 6.0


def build_figure(
    request: FigureBuildRequest,
    *,
    vlm: VLMClient | None,
    config: FigurePipelineConfig,
    source_path: Path | None = None,
) -> FigureBuildResult:
    """`FigureBuilder` 的生产实现。.

    `source_path` 是 PDF 文件路径（pdfplumber 的 Page 上拿不可靠：
    它可能是内存流）。
    """
    if vlm is None:
        return FigureBuildResult(error="vlm_unavailable: no VLM configured")
    if source_path is None:
        return FigureBuildResult(error="render: source PDF path is unknown")

    try:
        rendered = _render(request, config, source_path)
        fig.assert_min_dpi(rendered)
    except fig.FigureRenderError as exc:
        return FigureBuildResult(error=f"render: {exc}")

    buffer = io.BytesIO()
    rendered.image.save(
        buffer, format="WEBP", lossless=True, method=fig.WEBP_EFFORT
    )
    image_bytes = buffer.getvalue()

    prompt = prompt_for(request.detected.kind)
    try:
        raw = vlm.transcribe(image_bytes, prompt)
    except Exception as exc:  # noqa: BLE001 - 模型侧的异常类型不固定
        return FigureBuildResult(
            error=f"vlm_error: {type(exc).__name__}: {exc}"
        )

    try:
        transcript = parse_transcript(raw)
    except ValueError as exc:
        return FigureBuildResult(error=f"transcript: {exc}")

    evidence = collect_evidence(request)
    result = reconcile(transcript, evidence)
    if not result.ok:
        # 对账不过：连字节都不交回去，调用方无从把它写进 figures/。
        return FigureBuildResult(error=f"reconcile: {result.reason()}")

    figure = build_figure_data(
        image_path="",  # 由 pipeline 在算出 chunk_id 后回填
        width_px=rendered.width_px,
        height_px=rendered.height_px,
        dpi=rendered.dpi,
        transcript=transcript,
        evidence=evidence,
    )
    # 字节挂在 FigureData 上一起交回去：`reader` 只认 `FigureData`，单独再传一份
    # 字节要走第二个通道（`FigureBuildResult.image_bytes`），两处容易断一个。
    figure = replace(figure, image_bytes=image_bytes)
    return FigureBuildResult(figure=figure, image_bytes=image_bytes)


def _render(
    request: FigureBuildRequest, config: FigurePipelineConfig, source_path: Path
) -> fig.RenderedFigure:
    """渲染整页并裁出这块图。.

    **裁的就是 `detected.rect`**（已外扩、已吸收刻度与图注的那个矩形），不是
    `core_rect` 再加一点 pad。理由：`Region.rect` 用的是 `detected.rect`，
    切片进库的 bbox 就是它；裁片若比它小，用户点角标会看到"框住一大块、
    图只占其中一小块"，而且图上缺刻度（VLM 读不出数）。两者必须是同一块地方。

    延迟 import pypdfium2：没装它的部署只要没有图就不受影响。
    """
    from pypdfium2 import PdfDocument  # noqa: PLC0415

    display_width, display_height = request.space.display_size
    display_rect = request.space.reading_to_display(request.detected.rect)
    # 裁片不能超出页面：越界部分会被 PIL 用黑边填充，
    # VLM 会把黑边当"图的一部分"。
    display_rect = Rect(
        max(0.0, display_rect.x0),
        max(0.0, display_rect.top),
        min(display_width, display_rect.x1),
        min(display_height, display_rect.bottom),
    )

    document = PdfDocument(str(source_path))
    try:
        pdfium_page = document[request.page_num - 1]
        image = fig.render_page(
            pdfium_page, scale=fig.scale_for_dpi(config.dpi)
        )
    finally:
        document.close()
    return fig.crop_display(
        image,
        display_rect,
        display_width=display_width,
        display_height=display_height,
    )


def collect_evidence(request: FigureBuildRequest) -> FigureEvidence:
    """从原始 PDF 收集对账依据（文字、数值、柱高比例）。."""
    region = request.detected.rect
    texts = tuple(
        line.text.strip()
        for line in request.lines
        if line.text.strip() and region.intersection(line.rect) is not None
    )
    numbers = extract_numbers(texts)
    bars = _bar_ratios(request.page, request.detected)
    return FigureEvidence(
        texts=texts,
        numbers=numbers,
        bar_ratios=bars,
        axis_height=0.0,
        area_rect=region,
    )


def _bar_ratios(
    page: Any, detected: DetectedFigure
) -> tuple[tuple[float, float], ...]:
    """矢量柱的（高度 / 轴高, x 中心）。.

    柱子识别：落在图区域内、宽 ≥ `BAR_MIN_WIDTH`、高 ≥ `BAR_MIN_HEIGHT` 的矩形
    （`page.rects`）。比例算的是"这根柱的高度占轴高的多少"，于是模型说的 `32%`
    能对上"柱高 ≈ 轴高 × 0.32"。

    轴高取最高柱 × `AXIS_HEIGHT_SLACK`：柱顶通常比轴顶低一点（留白），
    直接拿最高柱当轴高会把所有比例系统性放大——而"系统性放大"会让对账
    一致地通过，这正是对账要防的那类错（一致地错比随机错更危险）。
    """
    core = detected.core_rect
    bars: list[Rect] = []
    for primitive in getattr(page, "rects", ()):
        rect = Rect(
            float(primitive["x0"]),
            float(primitive["top"]),
            float(primitive["x1"]),
            float(primitive["bottom"]),
        )
        if rect.width < BAR_MIN_WIDTH or rect.height < BAR_MIN_HEIGHT:
            continue
        if not core.contains_point((rect.x0 + rect.x1) / 2.0, rect.center_y):
            continue
        bars.append(rect)
    if not bars:
        return ()
    tallest = max(bar.height for bar in bars)
    if tallest <= 0:
        return ()
    axis_height = tallest * AXIS_HEIGHT_SLACK
    return tuple(
        (bar.height / axis_height, (bar.x0 + bar.x1) / 2.0) for bar in bars
    )


def write_webp(image_bytes: bytes, figures_dir: Path, chunk_id: str) -> str:
    """把裁片写进 `figures/{chunk_id}.webp`，返回**相对路径**（进库的那一个）。.

    相对路径而不是绝对路径：绝对路径会让 `chunks.image_path` 跟着机器走，
    换部署位置库里的值就全废了。
    """
    figures_dir.mkdir(parents=True, exist_ok=True)
    (figures_dir / f"{chunk_id}.webp").write_bytes(image_bytes)
    return f"{figures_dir.as_posix().rstrip('/')}/{chunk_id}.webp"


def make_builder(
    vlm: VLMClient | None,
    config: FigurePipelineConfig,
    *,
    source_path: Path | None = None,
) -> Any:
    """返回一个可直接传给 `read_pdf(figure_builder=...)` 的可调用对象。."""

    def _builder(request: FigureBuildRequest) -> FigureBuildResult:
        """Run _builder."""
        return build_figure(
            request, vlm=vlm, config=config, source_path=source_path
        )

    return _builder


__all__ = [
    "AXIS_HEIGHT_SLACK",
    "BAR_MIN_HEIGHT",
    "BAR_MIN_WIDTH",
    "FigurePipelineConfig",
    "build_figure",
    "collect_evidence",
    "make_builder",
    "write_webp",
]
