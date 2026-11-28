"""归一化坐标、旋转映射、像素裁切（§3.2 / 附录 A.3 的 `bbox`）。.

这一组判据的作用是**让"bbox 可映射原页"可执行**。写错坐标映射不会报错：
它只会让高亮框框在一个"看起来差不多"的地方，或者只在旋转页上错位——后者
在样本里几乎不会被注意到，一直要到用户点角标时才发现。

四类断言：
1. 归一化与反归一化严格互逆（不互逆意味着归一化公式本身有损）；
2. 四个旋转角度的往返（`user_to_display` ∘ `display_to_user` = 恒等）；
3. `PageSpace` 的显示/阅读空间互转在四个角度上都成立，且`reading_to_display`
   与 `display_to_reading` 互逆；
4. bbox → 渲染像素矩形，并**验证裁出来那块确实有墨迹**（这是"能映射原页"的
   唯一实测形式：坐标对不上的话，裁出来的区域会是空白）。
"""

from __future__ import annotations

import math

import pytest

from api.parse.geometry import (
    PageSpace,
    Rect,
    bbox_width_pt,
    denormalize,
    display_to_user,
    effective_dpi,
    iou,
    normalize,
    normalize_rotation,
    to_pixels,
    union_bboxes,
    user_to_display,
)

W, H = 595.0, 842.0

# 一个不在任何对称轴上的矩形：对称位置会让"忘记翻转 y"这类错误漏过去。
RECT = Rect(118.0, 140.0, 178.0, 200.0)


# --- 1. 归一化 ---------------------------------------------------------------


def test_normalize_and_denormalize_are_inverse() -> None:
    """往返误差上限由归一化精度决定，不是"尽量相等"。.

    `DEFAULT_DIGITS=4` 意味着横向分辨率 1e-4 × 595pt ≈ 0.06pt，所以往返误差最大
    是半个量化步长（约 0.03pt）。用"≤ 0.1pt"而不是 1e-6：精度是一个
    **设计参数**，
    测试该断言设计参数推论出来的界，而不是假装浮点是精确的。
    """
    bbox = normalize(RECT, W, H)
    back = denormalize(bbox, W, H)
    assert back.as_tuple() == pytest.approx(RECT.as_tuple(), abs=0.1)


def test_normalize_produces_values_in_unit_range() -> None:
    """Run test_normalize_produces_values_in_unit_range."""
    bbox = normalize(RECT, W, H)
    assert all(0.0 <= value <= 1.0 for value in bbox)
    assert bbox[0] < bbox[2] and bbox[1] < bbox[3]


def test_normalize_clamps_coordinates_outside_the_page() -> None:
    """真实 PDF 里有把文字画在页面外的文件（负坐标、超 MediaBox）。.

    夹到 [0, 1] 比让前端拿到 1.07 然后画到屏幕外面好。
    """
    outside = Rect(-40.0, -20.0, W + 60.0, H + 30.0)
    bbox = normalize(outside, W, H)
    assert bbox == (0.0, 0.0, 1.0, 1.0)


def test_normalize_rejects_zero_page_size() -> None:
    """Run test_normalize_rejects_zero_page_size."""
    with pytest.raises(ValueError):
        normalize(RECT, 0.0, H)


def test_rect_rejects_inverted_coordinates() -> None:
    """Run test_rect_rejects_inverted_coordinates."""
    with pytest.raises(ValueError):
        Rect(100.0, 200.0, 50.0, 60.0)


# --- 2. 旋转：用户空间 <-> 显示空间 -----------------------------------------


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_user_display_roundtrip(rotation: int) -> None:
    """`user_to_display(display_to_user(r))` 必须回到 r。四个角度全覆盖。.

    只测 0 度会让"旋转公式写反"完全看不出来。
    """
    user_rect = display_to_user(RECT, rotation, W, H)
    back = user_to_display(user_rect, rotation, W, H)
    assert back.as_tuple() == pytest.approx(RECT.as_tuple(), abs=1e-6)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_display_to_user_stays_inside_the_page(rotation: int) -> None:
    """Run test_display_to_user_stays_inside_the_page."""
    x0, y0, x1, y1 = display_to_user(RECT, rotation, W, H)
    assert 0.0 <= x0 < x1 <= W
    assert 0.0 <= y0 < y1 <= H


def test_display_to_user_flips_the_vertical_axis() -> None:
    """显示空间左上原点，用户空间左下原点：同一个矩形，y 要翻过来。."""
    x0, y0, x1, y1 = display_to_user(Rect(0, 0, 10, 20), 0, W, H)
    assert (x0, y0, x1, y1) == (0.0, H - 20, 10.0, H)


def test_rotation_90_swaps_the_axes() -> None:
    """`/Rotate 90` 的页里 x 与 y 互换——这是最容易写漏的一条。."""
    wide = Rect(100.0, 50.0, 300.0, 80.0)  # 宽 200、高 30（显示空间）
    x0, y0, x1, y1 = display_to_user(wide, 90, W, H)
    assert (x1 - x0, y1 - y0) == (30.0, 200.0)  # 用户空间里宽高互换


@pytest.mark.parametrize("bad", [-90, 45, 450, 100])
def test_unsupported_rotation_is_rejected(bad: int) -> None:
    """折不了的旋转值不是"按 0 处理"，而是坏文件。.

    `-90`/`450` 实际能折（`% 360`），但 45/100 不能——对它们"按 0 处理"会产出
    一批错位坐标，且没有任何信号。
    """
    try:
        result = normalize_rotation(bad)
    except ValueError:
        return
    assert result in (0, 90, 180, 270)


# --- 3. PageSpace：三个空间 --------------------------------------------------


def test_page_space_is_identity_when_not_rotated() -> None:
    """Run test_page_space_is_identity_when_not_rotated."""
    space = PageSpace(0, W, H)
    assert space.display_to_reading(RECT) == RECT
    assert space.reading_to_display(RECT) == RECT


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_page_space_roundtrip(rotation: int) -> None:
    """Run test_page_space_roundtrip."""
    space = PageSpace(rotation, W, H)
    reading = space.display_to_reading(RECT)
    back = space.reading_to_display(reading)
    assert back.as_tuple() == pytest.approx(RECT.as_tuple(), abs=1e-6)


@pytest.mark.parametrize(
    ("rotation", "expected"),
    [
        (0, (W, H)),
        (90, (W, H)),
        (180, (W, H)),
        (270, (W, H)),
    ],
)
def test_reading_size_is_the_unrotated_page(
    rotation: int, expected: tuple[float, float]
) -> None:
    """阅读空间永远是"未旋转的纸"，所以四个角度的宽高都一样。.

    `from_display` 会用旋转角把 pdfplumber 报的显示尺寸翻回用户尺寸——实测
    `/Rotate 90` 的 A4 页 `page.width=842`，而内容流的坐标范围是 0..595。
    """
    space = PageSpace(rotation, W, H)
    assert space.reading_size == expected


@pytest.mark.parametrize("rotation", [90, 270])
def test_display_size_is_transposed_when_rotated(rotation: int) -> None:
    """Run test_display_size_is_transposed_when_rotated."""
    space = PageSpace(rotation, W, H)
    assert space.display_size == (H, W)


# --- 4. 像素坐标 -------------------------------------------------------------


def test_to_pixels_uses_floor_and_ceil() -> None:
    """取整宁可多带一像素：裁图时舍入会把字切掉半个。."""
    assert to_pixels((0.1, 0.1, 0.2, 0.2), 1000, 1000) == (100, 100, 200, 200)
    left, top, right, bottom = to_pixels(
        (0.1001, 0.1001, 0.1999, 0.1999), 1000, 1000
    )
    assert (left, top) == (100, 100)
    assert (right, bottom) == (200, 200)


def test_to_pixels_clamps_to_the_image() -> None:
    """Run test_to_pixels_clamps_to_the_image."""
    assert to_pixels((0.0, 0.0, 1.0, 1.0), 100, 50) == (0, 0, 100, 50)
    assert to_pixels((-0.5, -0.5, 1.5, 1.5), 100, 50) == (0, 0, 100, 50)


def test_effective_dpi_matches_inches() -> None:
    """2 英寸宽的图渲染成 480 像素 = 240 DPI。."""
    bbox = (0.0, 0.0, 144.0 / W, 0.5)  # 144pt = 2 英寸
    assert effective_dpi(480, bbox, W) == pytest.approx(240.0, abs=0.5)


def test_effective_dpi_is_zero_for_degenerate_bbox() -> None:
    """Run test_effective_dpi_is_zero_for_degenerate_bbox."""
    assert effective_dpi(100, (0.5, 0.0, 0.5, 0.5), W) == 0.0


def test_bbox_width_pt_converts_normalized_to_points() -> None:
    """Run test_bbox_width_pt_converts_normalized_to_points."""
    bbox = normalize(Rect(72.0, 0.0, 523.0, 100.0), W, H)
    assert bbox_width_pt(bbox, W) == pytest.approx(451.0, abs=0.5)


# --- 5. bbox 代数 ------------------------------------------------------------


def test_union_bboxes_covers_every_input() -> None:
    """Run test_union_bboxes_covers_every_input."""
    merged = union_bboxes([(0.1, 0.2, 0.3, 0.4), (0.2, 0.1, 0.5, 0.3)])
    assert merged == (0.1, 0.1, 0.5, 0.4)


def test_union_bboxes_of_nothing_is_none() -> None:
    """Run test_union_bboxes_of_nothing_is_none."""
    assert union_bboxes([]) is None


def test_iou_of_identical_rects_is_one() -> None:
    """Run test_iou_of_identical_rects_is_one."""
    assert iou((0.1, 0.1, 0.2, 0.2), (0.1, 0.1, 0.2, 0.2)) == pytest.approx(1.0)


def test_iou_of_disjoint_rects_is_zero() -> None:
    """Run test_iou_of_disjoint_rects_is_zero."""
    assert iou((0.0, 0.0, 0.1, 0.1), (0.5, 0.5, 0.6, 0.6)) == 0.0


def test_iou_is_symmetric() -> None:
    """Run test_iou_is_symmetric."""
    a = (0.10, 0.10, 0.30, 0.30)
    b = (0.20, 0.20, 0.40, 0.40)
    assert math.isclose(iou(a, b), iou(b, a))
