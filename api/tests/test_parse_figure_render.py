"""图表裁片的 DPI 与无损编码（§3.4：无损 WebP ≥ 200 DPI）。.

两条都是硬要求，而且都会**静默**出问题：

* DPI 不够：刻度、小字、细线被磨平，VLM 读出来的数是错的。错得很自然，
  因为图看起来"还行"。
* 有损压缩：`47%` 的刻度线被压成模糊一团，读成 `41%` 完全可能。而对账拦不住
  源头失真——它只能核对"模型说的数在不在图上"，图本身糊了就无从核对。

所以这里不测"能生成图"，测"这张图**够不够看清**"：像素宽 / 物理宽 ≥ 200 DPI，
并且编码是无损的（同一张图两次编码字节相同，有损编码做不到）。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from PIL import Image

from api.parse.figures import (
    MIN_CROP_PX,
    MIN_FIGURE_DPI,
    FigureRenderError,
    assert_min_dpi,
    crop_display,
    effective_dpi,
    scale_for_dpi,
)
from api.parse.geometry import Rect

W, H = 595.0, 842.0
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _solid(
    width: int, height: int, color: tuple[int, int, int] = (255, 255, 255)
) -> Image.Image:
    """Run _solid."""
    return Image.new("RGB", (width, height), color)


# --- 1. scale 与 DPI --------------------------------------------------------


def test_scale_for_dpi_uses_points_per_inch() -> None:
    """72pt = 1 英寸：200 DPI 就是每 pt 2.78 像素。."""
    assert scale_for_dpi(200) == pytest.approx(200 / 72)
    assert scale_for_dpi(72) == pytest.approx(1.0)


def test_scale_for_dpi_rejects_non_positive() -> None:
    """Run test_scale_for_dpi_rejects_non_positive."""
    with pytest.raises(ValueError):
        scale_for_dpi(0)


def test_effective_dpi_matches_the_physical_width() -> None:
    """2 英寸宽的图渲染成 480 像素 = 240 DPI。."""
    bbox = (0.0, 0.0, 144.0 / W, 0.5)  # 144pt = 2 英寸
    assert effective_dpi(480, bbox, W) == pytest.approx(240.0, abs=0.5)


# --- 2. 裁片：DPI 与最小像素宽 ----------------------------------------------


def _page_image(dpi: float) -> Image.Image:
    """按给定 DPI 渲染"整页"（白底），尺寸与真实渲染一致。."""
    scale = scale_for_dpi(dpi)
    return _solid(round(W * scale), round(H * scale))


def test_crop_at_200_dpi_meets_the_floor() -> None:
    """一个 2 英寸宽的图在 220 DPI 下裁出来，DPI 必须 ≥ 200。."""
    image = _page_image(220)
    rect = Rect(72.0, 100.0, 216.0, 200.0)  # 144pt = 2 英寸宽
    rendered = crop_display(image, rect, display_width=W, display_height=H)
    assert rendered.dpi >= MIN_FIGURE_DPI
    assert rendered.width_px >= MIN_CROP_PX


def test_crop_of_a_tiny_figure_is_upscaled_to_the_pixel_floor() -> None:
    """0.5 英寸宽的迷你图在 200 DPI 下只有 100px —— 刻度读不出来。.

    放大到 `MIN_CROP_PX` 是有意的：`§3.4` 的边界表写的是“放大后重裁，
    仍不行则标失败”。这里实现“放大”那一半。
    """
    image = _page_image(200)
    rect = Rect(72.0, 100.0, 108.0, 130.0)  # 36pt = 0.5 英寸
    rendered = crop_display(image, rect, display_width=W, display_height=H)
    assert rendered.width_px == MIN_CROP_PX
    assert rendered.dpi > MIN_FIGURE_DPI  # 放大之后 DPI 反而更高


def test_crop_rejects_an_aspect_mismatch() -> None:
    """渲染尺寸与页面尺寸不匹配时继续裁会得到错位切片，直接报错。."""
    wrong = _solid(100, 1000)  # 与 595×842 的宽高比不符
    with pytest.raises(FigureRenderError, match="aspect mismatch"):
        crop_display(
            wrong, Rect(0, 0, 100, 100), display_width=W, display_height=H
        )


def test_crop_of_a_tiny_rect_is_upscaled_not_rejected() -> None:
    """物理尺寸极小的矩形会裁出 1-2 像素，然后按 `MIN_CROP_PX` 放大。.

    "极小"不是拒绝的理由：`§3.4` 的边界表写的是"放大后重裁，仍不行则标失败"。
    放大到 `MIN_CROP_PX` 之后刻度能看清，就没有拒绝的必要；
    真拒绝发生在渲染失败或 DPI 不足（见
    `test_assert_min_dpi_rejects_a_low_resolution_crop`）。
    """
    image = _page_image(200)
    rendered = crop_display(
        image,
        Rect(100.0, 100.0, 100.2, 100.2),
        display_width=W,
        display_height=H,
    )
    assert rendered.width_px == MIN_CROP_PX


def test_assert_min_dpi_passes_above_the_floor() -> None:
    """Run test_assert_min_dpi_passes_above_the_floor."""
    image = _page_image(220)
    rendered = crop_display(
        image,
        Rect(72.0, 100.0, 216.0, 200.0),
        display_width=W,
        display_height=H,
    )
    assert_min_dpi(rendered, minimum=200)


def test_assert_min_dpi_rejects_a_low_resolution_crop() -> None:
    """120 DPI 的图必须被拒 —— 刻度在那个分辨率下不可信。.

    这条是 `figure-dpi` 变异的克星：把 `assert_min_dpi` 的判据改成恒真，
    它就会静默放行糊图。
    """
    image = _page_image(120)  # 整页只有 120 DPI
    rect = Rect(
        72.0, 100.0, 500.0, 400.0
    )  # 宽 428pt ≈ 5.9 英寸 → 714px ≈ 120 DPI
    rendered = crop_display(image, rect, display_width=W, display_height=H)
    assert (
        rendered.dpi < MIN_FIGURE_DPI
    ), f"fixture not low-res enough: {rendered.dpi:.0f}"
    with pytest.raises(FigureRenderError, match="DPI"):
        assert_min_dpi(rendered, minimum=MIN_FIGURE_DPI)


# --- 3. 无损编码 -------------------------------------------------------------


def test_pipeline_calls_assert_min_dpi(tmp_path: Path) -> None:
    """`build_figure` 必须真的调用 DPI 检查，而不是只定义了它。.

    第一版把"DPI 不足就报错"写成 `assert_min_dpi()`，但没有任何用例证明
    `build_figure` 调了它——把调用删掉，测试仍然全绿。

    做法：真渲染一份夹具 PDF（走完整路径），把 `assert_min_dpi` 换成会抛的桩，
    断言 `build_figure` 返回的是 `render: …` 错误。这证明调用链上真的走到它。
    """
    from api.parse import figure_pipeline  # noqa: PLC0415
    from api.parse.geometry import PageSpace  # noqa: PLC0415
    from api.parse.layout import DetectedFigure  # noqa: PLC0415
    from api.parse.reader import FigureBuildRequest  # noqa: PLC0415

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_layout_samples  # noqa: PLC0415

    pdf = tmp_path / "figure.pdf"
    pdf.write_bytes(make_layout_samples.build("layout/figure-demo.pdf"))

    calls: list[object] = []

    def _boom(rendered: object, *, minimum: float = 200) -> None:  # noqa: ARG001
        """Run _boom."""
        calls.append(rendered)
        raise figure_pipeline.fig.FigureRenderError("dpi too low (test stub)")

    original = figure_pipeline.fig.assert_min_dpi
    figure_pipeline.fig.assert_min_dpi = _boom  # type: ignore[assignment]
    try:
        request = FigureBuildRequest(
            page=object(),
            space=PageSpace(0, W, H),
            page_num=1,
            detected=DetectedFigure(
                rect=Rect(60.0, 178.0, 514.0, 594.0),
                core_rect=Rect(104.0, 222.0, 470.0, 542.0),
                kind="vector",
                source="vector",
            ),
            lines=(),
        )
        result = figure_pipeline.build_figure(
            request,
            vlm=_NullVLM(),
            config=figure_pipeline.FigurePipelineConfig(),
            source_path=pdf,
        )
    finally:
        figure_pipeline.fig.assert_min_dpi = original  # type: ignore[assignment]

    assert calls, "build_figure never called assert_min_dpi"
    assert result.error is not None and result.error.startswith("render:")
    assert "dpi too low" in result.error


class _NullVLM:
    """占位 VLM：DPI 检查在它之前就该抛，永远不该被调用。."""

    def transcribe(self, image: bytes, prompt: str) -> str:  # noqa: ARG002
        """Run transcribe."""
        raise AssertionError("VLM must not be called when the DPI check fails")


def test_lossless_webp_roundtrip_is_byte_identical(tmp_path: Path) -> None:
    """无损：解码出来的像素与原始完全一致（有损编码做不到）。."""
    original = Image.new("RGB", (120, 80), (255, 255, 255))
    # 画几根 1 像素的细线：有损压缩最先磨掉这种高频细节
    for x in range(0, 120, 7):
        for y in range(80):
            original.putpixel((x, y), (0, 0, 0))
    image = crop_display(
        _page_image(200),
        Rect(72.0, 100.0, 300.0, 250.0),
        display_width=W,
        display_height=H,
    )
    buffer = io.BytesIO()
    image.image.save(buffer, format="WEBP", lossless=True)
    with Image.open(io.BytesIO(buffer.getvalue())) as decoded:
        assert list(decoded.convert("RGB").getdata()) == list(
            image.image.getdata()
        )


def test_save_lossless_webp_writes_the_file(tmp_path: Path) -> None:
    """Run test_save_lossless_webp_writes_the_file."""
    image = crop_display(
        _page_image(220),
        Rect(72.0, 100.0, 216.0, 200.0),
        display_width=W,
        display_height=H,
    )
    target = tmp_path / "figures" / "ch_x.webp"
    size = image.save_lossless_webp(target)
    assert target.exists()
    assert size == target.stat().st_size
    with Image.open(target) as decoded:
        assert decoded.format == "WEBP"
        assert decoded.size == (image.width_px, image.height_px)
