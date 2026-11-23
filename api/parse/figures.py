"""图表裁图：渲染、裁片、无损 WebP、DPI 保证（详细设计.md §3.4）。

三条硬要求，缺一条后面都会读到错数：

1. **≥200 DPI**。刻度、小字、细线在低分辨率下会糊，糊了就会被读成别的数。
   `MIN_FIGURE_DPI = 200`，渲染时按"让裁片正好达到 200 DPI"算缩放比；如果图的
   物理尺寸太小、算出来的像素宽 < `MIN_CROP_PX`，就放大到 `MIN_CROP_PX`——
   这时 DPI 会超过 200（是有意的：宁可多给像素）。
2. **无损 WebP**。有损压缩会把刻度线磨平、把 `47%` 糊成 `41%`，而"数值对账"
   拦不住源头失真（§3.4 的原话）。`lossless=True` 是必须的。
3. **裁片路径进库**（`chunks.image_path`）。相对路径，`figures/{chunk_id}.webp`；
   绝对路径会让库跟着机器走。

### 为什么用 pypdfium2 而不是 pdfplumber 的 `to_image`

pdfplumber 的 `to_image(resolution=)` 内部也是拿 pypdfium2 渲染，但它返回的是
整页 image，再自己裁；而我们要的是"只渲染需要的那一块、只编码一次"。这里的做法
是 `page.render(scale=...)` 整页渲染一次然后裁——比逐图渲染省得多（一张 A4 @200DPI
约 1650×2340，编码一次几十毫秒），也让"同一页上多张图"共用一次渲染。

**渲染坐标**：pypdfium2 渲染的位图是**显示空间**（按 `/Rotate` 转过、左上原点），
与 pdfplumber 的 `page.width/height` 一致。旋转页的裁片因此只需要"显示空间矩形 ×
缩放比"，不需要再转一次。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from api.parse.geometry import Rect, effective_dpi, to_pixels

# §3.4：至少 200 DPI。
MIN_FIGURE_DPI = 200
# 裁片最小像素宽：图本身很小（比如 1 英寸宽的迷你图）时，200 DPI 只有 200px 宽，
# 刻度根本读不出来。放大到 480px 是"糊图也得能看清刻度"的下限。
MIN_CROP_PX = 480
# WebP 无损编码质量参数在 lossless 模式下只影响压缩努力度，不影响像素。
WEBP_EFFORT = 6


class FigureRenderError(RuntimeError):
    """渲染失败（坏页、字体缺失、内存不足）。调用方据此把这条图判失败、不入库。"""


@dataclass(frozen=True, slots=True)
class RenderedFigure:
    """一次裁片的结果。`dpi` 是按物理尺寸反算出来的，不是"我要求了多少"。"""

    image: Image.Image
    width_px: int
    height_px: int
    dpi: float
    scale: float

    def save_lossless_webp(self, path: Path) -> int:
        """写无损 WebP，返回字节数。目录不存在就建。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        self.image.save(buffer, format="WEBP", lossless=True, method=WEBP_EFFORT)
        data = buffer.getvalue()
        path.write_bytes(data)
        return len(data)


def render_page(page: Any, *, scale: float) -> Image.Image:
    """把一页渲染成 RGB 位图（显示空间，左上原点）。

    `page` 是 pypdfium2 的 `PdfPage`。`scale` 是"像素 / pt"。
    """
    try:
        bitmap = page.render(scale=scale)
        return bitmap.to_pil().convert("RGB")
    except Exception as exc:  # noqa: BLE001 - pdfium 的异常类型不固定
        raise FigureRenderError(f"render failed: {type(exc).__name__}: {exc}") from exc


def crop_display(
    image: Image.Image,
    display_rect: Rect,
    *,
    display_width: float,
    display_height: float,
    min_width_px: int = MIN_CROP_PX,
) -> RenderedFigure:
    """按显示空间矩形裁片。

    `image` 必须是整页渲染的结果（尺寸对应 `display_width × display_height`）。
    """
    if image.width <= 0 or image.height <= 0:
        raise FigureRenderError("empty render")
    scale = image.width / display_width
    if abs(scale - image.height / display_height) > 0.02:
        # 宽高比不一致说明渲染尺寸与传入的页面尺寸不匹配——继续裁会得到错位切片。
        raise FigureRenderError(
            "render/page aspect mismatch: "
            f"{image.width}x{image.height} vs {display_width:g}x{display_height:g}"
        )
    left, top, right, bottom = to_pixels(
        (
            display_rect.x0 / display_width,
            display_rect.top / display_height,
            display_rect.x1 / display_width,
            display_rect.bottom / display_height,
        ),
        image.width,
        image.height,
    )
    if right - left < 1 or bottom - top < 1:
        # 理论上不可达（`Rect` 拒绝零面积，`to_pixels` 用 floor/ceil 保证至少 1 像素），
        # 但留着这一行是因为它把"裁片必须有面积"写成显式契约：
        # 将来若有调用方绕过 `Rect` 直接传 bbox，这里能拦住 0 像素的裁片。
        raise FigureRenderError(f"crop too small: {(right - left)}x{(bottom - top)}px")
    cropped = image.crop((left, top, right, bottom))
    if cropped.width < min_width_px:
        # 放大而不是放弃：糊图读出来的数不可信，但"图太小"本身不是判失败的理由
        # （§3.4 边界表：放大后重裁，仍不行则标失败——本函数实现了"放大"这一半）。
        factor = min_width_px / cropped.width
        cropped = cropped.resize(
            (min_width_px, max(1, round(cropped.height * factor))),
            resample=Image.Resampling.LANCZOS,
        )
    dpi = effective_dpi(cropped.width, _normalized(display_rect, display_width, display_height), display_width)
    return RenderedFigure(
        image=cropped,
        width_px=cropped.width,
        height_px=cropped.height,
        dpi=dpi,
        scale=scale,
    )


def _normalized(rect: Rect, width: float, height: float) -> tuple[float, float, float, float]:
    return (rect.x0 / width, rect.top / height, rect.x1 / width, rect.bottom / height)


def scale_for_dpi(dpi: float) -> float:
    """给定目标 DPI，算出 pypdfium2 的 scale（像素/pt）。72pt = 1 英寸。"""
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    return dpi / 72.0


def assert_min_dpi(rendered: RenderedFigure, *, minimum: float = MIN_FIGURE_DPI) -> None:
    """DPI 不足就报错。这是"无损 ≥200 DPI"的可执行判据，别只在文档里写。"""
    if rendered.dpi < minimum - 0.5:
        raise FigureRenderError(
            f"figure rendered at {rendered.dpi:.1f} DPI < {minimum:g} "
            f"({rendered.width_px}x{rendered.height_px}px)"
        )


__all__ = [
    "MIN_CROP_PX",
    "MIN_FIGURE_DPI",
    "FigureRenderError",
    "RenderedFigure",
    "assert_min_dpi",
    "crop_display",
    "render_page",
    "scale_for_dpi",
]
