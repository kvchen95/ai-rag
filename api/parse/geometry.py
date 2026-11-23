"""归一化坐标、旋转映射与矩形代数（详细设计.md §3.2 / 附录 A.3 的 `bbox`）。

`bbox` 的口径声明一次，下游全部按这个来：

    bbox = [[x0, y0, x1, y1], ...]     每个数在 0..1，**页面显示空间**（左上原点）
    x0 < x1, y0 < y1

为什么是"显示空间"而不是 PDF 用户空间：前端画框是 `left = x0 * viewport.width`
（附录 C.2），而 PDF.js / pypdfium2 的 viewport 都已经是"旋转之后、左上原点"的
画布。若把坐标存成用户空间（左下原点、未旋转），前端每处都要自己再转一次——
转一次的代码写对一次就够了，放在这里。

### 旋转为什么必须在这里处理

`/Rotate 90` 的页面，阅读方向与页面坐标轴差 90°。不转的后果不是"框偏一点"，
而是"框转着错"，且只在旋转页上错——测试里最难发现的一类。四个角度的映射
（均由"顺时针旋转后在显示画布上的落点"推出，括号内是逆变换）：

    旋转    用户空间 (u, v)  ->  显示空间 (x_d, y_d)        逆
    ----    --------------------------------------------    ---------------------
      0     x_d = u,            y_d = H_u - v               u = x_d, v = H_u - y_d
     90     x_d = v,            y_d = u                     u = y_d, v = x_d
    180     x_d = W_u - u,      y_d = v                     u = W_u - x_d, v = y_d
    270     x_d = H_u - v,      y_d = W_u - u               u = W_u - y_d, v = H_u - x_d

其中 `W_d = H_u`、`H_d = W_u`（90/270 时显示画布是转置的）。

这两条不是推导完就算数：`api/tests/test_parse_geometry.py` 把页面渲染成像素，
再按归一化 bbox 去裁图，断言"裁出来的区域确实有墨迹、平移一点就没有"。
公式写错了那个用例会红，而不是等到线上高亮框错位。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

# 归一化精度：1e-4 对应 A4 纸宽 595pt 上的 0.06pt，肉眼看不出，也够让前端画框。
DEFAULT_DIGITS = 4

BBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Rect:
    """显示空间（左上原点，单位 pt）的轴对齐矩形。"""

    x0: float
    top: float
    x1: float
    bottom: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.x0, self.top, self.x1, self.bottom)):
            raise ValueError(f"non-finite rect: {self}")
        if self.x1 < self.x0 or self.bottom < self.top:
            raise ValueError(f"inverted rect: {self}")

    # --- 派生量 ---

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def area(self) -> float:
        return self.width * self.height

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2.0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.top, self.x1, self.bottom)

    # --- 代数 ---

    def union(self, other: "Rect") -> "Rect":
        return Rect(
            min(self.x0, other.x0),
            min(self.top, other.top),
            max(self.x1, other.x1),
            max(self.bottom, other.bottom),
        )

    def expand(self, pad: float) -> "Rect":
        return Rect(self.x0 - pad, self.top - pad, self.x1 + pad, self.bottom + pad)

    def intersection(self, other: "Rect") -> "Rect | None":
        x0 = max(self.x0, other.x0)
        top = max(self.top, other.top)
        x1 = min(self.x1, other.x1)
        bottom = min(self.bottom, other.bottom)
        if x1 <= x0 or bottom <= top:
            return None
        return Rect(x0, top, x1, bottom)

    def overlap_ratio(self, other: "Rect") -> float:
        """交集面积 / 自身面积。用来判断"这行文字属于哪个区域"。"""
        inter = self.intersection(other)
        if inter is None or self.area == 0:
            return 0.0
        return inter.area / self.area

    def contains_point(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.top <= y <= self.bottom

    def horizontal_gap(self, other: "Rect") -> float:
        """水平间距（相交时为 0）。判断"两栏之间有没有缝"用它。"""
        return max(0.0, max(self.x0, other.x0) - min(self.x1, other.x1))


def union_rects(rects: Iterable[Rect]) -> Rect | None:
    iterator = iter(rects)
    first = next(iterator, None)
    if first is None:
        return None
    out = first
    for rect in iterator:
        out = out.union(rect)
    return out


# --- 归一化 ------------------------------------------------------------------


def _round(value: float, digits: int) -> float:
    return round(min(1.0, max(0.0, value)), digits)


def normalize(rect: Rect, page_width: float, page_height: float, *, digits: int = DEFAULT_DIGITS) -> BBox:
    """显示空间 pt -> 0..1 归一化。

    除以页宽页高并夹到 [0, 1]：PDF 里真有把文字画在页面边界外的文件（负坐标、
    超出 MediaBox），夹一下比让前端拿到 1.07 然后画到屏幕外面好。
    """
    if page_width <= 0 or page_height <= 0:
        raise ValueError(f"page size must be positive: {page_width}x{page_height}")
    return (
        _round(rect.x0 / page_width, digits),
        _round(rect.top / page_height, digits),
        _round(rect.x1 / page_width, digits),
        _round(rect.bottom / page_height, digits),
    )


def denormalize(bbox: Sequence[float], page_width: float, page_height: float) -> Rect:
    """0..1 -> 显示空间 pt。与 `normalize` 严格互逆（到 `digits` 位精度）。"""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    return Rect(x0 * page_width, y0 * page_height, x1 * page_width, y1 * page_height)


def to_pixels(bbox: Sequence[float], pixel_width: int, pixel_height: int) -> tuple[int, int, int, int]:
    """0..1 -> 渲染像素矩形（左, 上, 右, 下）。

    风格上取整用 floor/ceil 而不是 round：裁图时宁可多带一像素，也别把字切掉半个。
    """
    x0, y0, x1, y1 = (float(v) for v in bbox)
    left = max(0, math.floor(x0 * pixel_width))
    top = max(0, math.floor(y0 * pixel_height))
    right = min(pixel_width, math.ceil(x1 * pixel_width))
    bottom = min(pixel_height, math.ceil(y1 * pixel_height))
    return (left, top, right, bottom)


def union_bboxes(bboxes: Iterable[Sequence[float]]) -> BBox | None:
    """多个归一化 bbox 的并集。

    坐标都在同一页时才有意义——跨页的并集会得到一个"两页之间的框"，
    所以调用方必须保证同页（`chunk` 的构建按页切，正是为了这个）。
    """
    out: BBox | None = None
    for bbox in bboxes:
        cur = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        if out is None:
            out = cur
        else:
            out = (
                min(out[0], cur[0]),
                min(out[1], cur[1]),
                max(out[2], cur[2]),
                max(out[3], cur[3]),
            )
    return out


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """两个归一化 bbox 的交并比。去重与"同一区域被识别两次"的判定用它。"""
    ax0, ay0, ax1, ay1 = (float(v) for v in a)
    bx0, by0, bx1, by1 = (float(v) for v in b)
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def bbox_width_pt(bbox: Sequence[float], page_width: float) -> float:
    return (float(bbox[2]) - float(bbox[0])) * page_width


def effective_dpi(pixel_width: int, bbox: Sequence[float], page_width: float) -> float:
    """图表裁片的等效 DPI = 像素宽 / 物理宽（英寸）。

    §3.4 要求 ≥200 DPI。这里算出来而不是"相信渲染时设的 DPI"：如果将来有人
    给裁片加了缩放/压缩，这个数会立刻变小，而 `assert_dpi` 会拦住它。
    """
    width_in = bbox_width_pt(bbox, page_width) / 72.0
    if width_in <= 0:
        return 0.0
    return pixel_width / width_in


# --- 旋转映射 ----------------------------------------------------------------


_VALID_ROTATIONS = (0, 90, 180, 270)


def normalize_rotation(rotation: int) -> int:
    """任意角度折到 0/90/180/270。

    PDF 的 `/Rotate` 规范上只能是这四值，但真实文件里出现过 `-90` 和 `450`
    （有的生成器把累加值直接写进去）。折不了的值不是"按 0 处理"，是坏文件：
    `ValueError` 让它在解析阶段就暴露，而不是产出一批错位坐标。
    """
    value = int(rotation) % 360
    if value not in _VALID_ROTATIONS:
        raise ValueError(f"unsupported page rotation: {rotation}")
    return value


def display_to_user(
    rect: Rect, rotation: int, user_width: float, user_height: float
) -> tuple[float, float, float, float]:
    """显示空间（左上原点）-> PDF 用户空间（左下原点）的矩形。

    返回 `(x0, y0, x1, y1)`，`y0 < y1` 且以左下为原点——这是 PDF 内容流里
    真正用来定位的坐标，`原页可映射` 指的就是它。
    """
    rot = normalize_rotation(rotation)
    if rot == 0:
        u0, u1 = rect.x0, rect.x1
        v0, v1 = user_height - rect.bottom, user_height - rect.top
    elif rot == 90:
        u0, u1 = rect.top, rect.bottom
        v0, v1 = rect.x0, rect.x1
    elif rot == 180:
        u0, u1 = user_width - rect.x1, user_width - rect.x0
        v0, v1 = rect.top, rect.bottom
    else:  # 270
        u0, u1 = user_width - rect.bottom, user_width - rect.top
        v0, v1 = user_height - rect.x1, user_height - rect.x0
    return (min(u0, u1), min(v0, v1), max(u0, u1), max(v0, v1))


def user_to_display(
    rect: tuple[float, float, float, float],
    rotation: int,
    user_width: float,
    user_height: float,
) -> Rect:
    """PDF 用户空间 -> 显示空间。`display_to_user` 的逆，测试里用它做往返。"""
    rot = normalize_rotation(rotation)
    x0, y0, x1, y1 = rect
    if rot == 0:
        return Rect(x0, user_height - y1, x1, user_height - y0)
    if rot == 90:
        return Rect(y0, x0, y1, x1)
    if rot == 180:
        return Rect(user_width - x1, y0, user_width - x0, y1)
    return Rect(user_height - y1, user_width - x1, user_height - y0, user_width - x0)


class PageSpace:
    """一页的三种坐标系之间的换算，一次构造、到处用。

    为什么需要"阅读空间"这个第三空间：`/Rotate 90` 的页，pdfplumber 给的坐标是
    **显示空间**（转过的、左上原点）——正文在那里是竖着排的（实测：一行
    `The highlight box...` 变成 34 个各自独立的单字符行）。行聚类、分栏、表格
    全都要在"文字横着读"的空间里才有意义。

    所以：所有建模在**阅读空间**做，输出 bbox 时一次性换算回显示空间。

        显示空间（左上原点、已旋转，前端画框与 PDF.js viewport 都认这个）
            ⇄ 用户空间（左下原点、未旋转，PDF 内容流的原生坐标）
            ⇄ 阅读空间（左上原点、未旋转，文字按行读的那个空间）

    `rotation=0` 时三者重合，所有换算都是恒等——所以未旋转的页一条额外分支都不走。
    """

    __slots__ = ("rotation", "user_width", "user_height")

    def __init__(self, rotation: int, user_width: float, user_height: float) -> None:
        self.rotation = normalize_rotation(rotation)
        self.user_width = float(user_width)
        self.user_height = float(user_height)

    @classmethod
    def from_display(cls, page: Any) -> "PageSpace":
        """从 pdfplumber 的 page 构造。

        pdfplumber 的 `page.width/height` 是**显示空间**尺寸；旋转 90/270 时用户
        空间（未旋转页面）的宽高与它互换。实测：`/Rotate 90` 的 A4 页
        `page.width=842, height=595`，而内容流里的坐标范围是 0..595 × 0..842。
        """
        rotation = normalize_rotation(int(getattr(page, "rotation", 0) or 0))
        display_w, display_h = float(page.width), float(page.height)
        if rotation in (90, 270):
            return cls(rotation, display_h, display_w)
        return cls(rotation, display_w, display_h)

    @property
    def reading_size(self) -> tuple[float, float]:
        """阅读空间的宽高 = 未旋转页面的宽高（文字横着读的那个空间）。"""
        return (self.user_width, self.user_height)

    @property
    def display_size(self) -> tuple[float, float]:
        if self.rotation in (90, 270):
            return (self.user_height, self.user_width)
        return (self.user_width, self.user_height)

    def display_to_reading(self, rect: Rect) -> Rect:
        """显示空间 -> 阅读空间。`rotation=0` 恒等。"""
        if self.rotation == 0:
            return rect
        x0, y0 = self._point_to_reading(rect.x0, rect.top)
        x1, y1 = self._point_to_reading(rect.x1, rect.bottom)
        return Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def reading_to_display(self, rect: Rect) -> Rect:
        """阅读空间 -> 显示空间。`rotation=0` 恒等。"""
        if self.rotation == 0:
            return rect
        x0, y0 = self._point_to_display(rect.x0, rect.top)
        x1, y1 = self._point_to_display(rect.x1, rect.bottom)
        return Rect(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def _point_to_reading(self, x: float, y: float) -> tuple[float, float]:
        """显示空间点 -> 阅读空间点。

        四个角度的映射由"显示坐标 = 用户坐标按 /Rotate 顺时针转"反解而来，
        实测逐值对过（`scripts/probe_rotate_space.py` 的思路：在用户空间放一个已知
        方块，看 pdfplumber 报的显示坐标）：

            旋转    显示(x, y) -> 阅读(x', y')        阅读尺寸
            ----    -----------------------------     ----------
              0     (x, y)                             W_u × H_u
             90     (y, H_u - x)                       W_u × H_u
            180     (W_u - x, H_u - y)                 W_u × H_u
            270     (W_u - y, x)                       W_u × H_u
        """
        if self.rotation == 0:
            return (x, y)
        if self.rotation == 90:
            return (y, self.user_height - x)
        if self.rotation == 180:
            return (self.user_width - x, self.user_height - y)
        return (self.user_width - y, x)

    def _point_to_display(self, x: float, y: float) -> tuple[float, float]:
        """阅读空间点 -> 显示空间点（`_point_to_reading` 的逆）。"""
        if self.rotation == 0:
            return (x, y)
        if self.rotation == 90:
            return (self.user_height - y, x)
        if self.rotation == 180:
            return (self.user_width - x, self.user_height - y)
        return (y, self.user_width - x)


    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"PageSpace(rotation={self.rotation}, "
            f"user={self.user_width:g}x{self.user_height:g})"
        )


__all__ = [
    "BBox",
    "DEFAULT_DIGITS",
    "PageSpace",
    "Rect",
    "bbox_width_pt",
    "denormalize",
    "display_to_user",
    "effective_dpi",
    "iou",
    "normalize",
    "normalize_rotation",
    "to_pixels",
    "union_bboxes",
    "union_rects",
    "user_to_display",
]
