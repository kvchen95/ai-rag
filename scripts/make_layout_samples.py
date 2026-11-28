#!/usr/bin/env python3
"""生成「有版面」的样本 PDF（纯 stdlib，字节确定）——A2 版面解析测试资产。.

`scripts/make_samples.py` 的样本只有几行正文，够验扫描/判重，但验不了 §3.2–§3.5
的任何一条：没有表格、没有图、没有页眉页脚、没有旋转页、没有扫描页。这个脚本
补上那五类，一页一个坑，**每个坑都能反证解析器错了**：

- `layout/table-demo.pdf`（2 页）：有线表格 4×3、跨页续表。错了会把表格当正文，
  检索不到单元格，三表示也对不上。
- `layout/figure-demo.pdf`（1 页）：矢量柱状图 + 图注。错了就把图拆成文字碎片，
  转译数值无法对账。
- `layout/scanned-demo.pdf`（2 页）：第 2 页只有位图、无文字层。不 OCR 就整页
  丢失（无坐标则无法溯源）。
- `layout/rotated-demo.pdf`（2 页）：第 2 页 `/Rotate 90`。不转坐标则高亮框全
  错位。
- `layout/two-column-demo.pdf`（1 页）：双栏排版。按行读会把两栏拼成一句。
- `layout/header-demo.pdf`（3 页）：每页同一行页眉 + 相同的中段条款编号。要么
  页眉进正文（噪声），要么连条款编号一起删掉（丢正文）。

**为什么自己写 PDF 结构**：仓库要能 `git clone` 就复现，不能依赖"我机器上那份
PDF"。这里只用到 stdlib：xref 偏移由代码算，图像用 `zlib` 压成 flate 原始像素，
不写时间戳——同一份代码在任何机器上生成**字节完全相同**，测试才能断言 bbox。

用法：

    python scripts/make_layout_samples.py --root api/tests/fixtures/pdf
    python scripts/make_layout_samples.py --root /tmp/a2 --force

生成物不进版本库（二进制 fixture 让 diff 无法评审）：测试在 tmp 目录现生成，
人要看时自己指定 `--root`。
"""

from __future__ import annotations

import argparse
import sys
import zlib
from pathlib import Path

if __package__:
    from .resources.make_layout_samples_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/make_layout_samples.py
    from resources.make_layout_samples_msgs import MESSAGES as M

PAGE_W = 595.0
PAGE_H = 842.0

FONT_ID = 1
CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"


def _esc(text: str) -> str:
    """Run _esc."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


class PdfBuilder:
    """最小 PDF 写入器：页 + 文本 + 矢量图元 + 位图。.

    刻意不做字形子集嵌入（用 Helvetica 标准 14 字体），这样生成物只有几 KB，
    测试里反复生成也不拖时间。
    """

    def __init__(self) -> None:
        """Initialize the instance."""
        self._objects: list[bytes] = []
        self._pages: list[int] = []
        self._font_id = self._add(
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        )
        self._add(b"")  # obj 2: Pages（最后回填）
        self._add(CATALOG)  # obj 3: Catalog

    def _add(self, body: bytes) -> int:
        """Run _add."""
        self._objects.append(body)
        return len(self._objects)

    # --- 图元：都拼成 content stream 片段 -------------------------------

    @staticmethod
    def text(x: float, y: float, size: float, body: str) -> str:
        """在页面坐标 (x, y)（左下原点，PDF 原生坐标系）画一行文本。."""
        return f"BT /F1 {size} Tf {x:.2f} {y:.2f} Td ({_esc(body)}) Tj ET"

    @staticmethod
    def line(
        x0: float, y0: float, x1: float, y1: float, width: float = 0.8
    ) -> str:
        """Run line."""
        return f"{width} w {x0:.2f} {y0:.2f} m {x1:.2f} {y1:.2f} l S"

    @staticmethod
    def rect(
        x0: float, y0: float, w: float, h: float, *, fill: bool = True
    ) -> str:
        """Run rect."""
        op = "f" if fill else "S"
        return f"{x0:.2f} {y0:.2f} {w:.2f} {h:.2f} re {op}"

    def add_page(
        self,
        ops: list[str],
        *,
        rotate: int = 0,
        image: tuple[int, int, bytes] | None = None,
        image_box: tuple[float, float, float, float] | None = None,
    ) -> None:
        """加一页。.

        `image` 是 (宽, 高, RGB 原始字节)，会压进 `/FlateDecode` XObject。.
        """
        resources = f"<< /Font << /F1 {self._font_id} 0 R >>"
        prefix: list[str] = []
        if image is not None:
            width, height, raw = image
            image_id = self._add(
                f"<< /Type /XObject /Subtype /Image /Width {width} "
                f"/Height {height}"
                f" /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                f"/Filter /FlateDecode"
                f" /Length {len(zlib.compress(raw, 9))} >>\nstream\n".encode()
                + zlib.compress(raw, 9)
                + b"\nendstream"
            )
            resources += f" /XObject << /Im1 {image_id} 0 R >>"
            x0, y0, x1, y1 = image_box or (0.0, 0.0, PAGE_W, PAGE_H)
            prefix.append(
                f"q {x1 - x0:.2f} 0 0 {y1 - y0:.2f} "
                f"{x0:.2f} {y0:.2f} cm /Im1 Do Q"
            )
        resources += " >>"

        stream = "\n".join([*prefix, *ops]).encode("latin-1", "replace")
        contents_id = self._add(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        page_id = self._add(b"")
        rotate_op = f" /Rotate {rotate}" if rotate else ""
        self._objects[page_id - 1] = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {PAGE_W:g} {PAGE_H:g}]"
            f" /Resources {resources} /Contents {contents_id} 0 R{rotate_op} >>"
        ).encode()
        self._pages.append(page_id)

    def build(self) -> bytes:
        """Run build."""
        kids = b" ".join(str(pid).encode() + b" 0 R" for pid in self._pages)
        self._objects[1] = (
            b"<< /Type /Pages /Kids ["
            + kids
            + b"] /Count "
            + str(len(self._pages)).encode()
            + b" >>"
        )
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: list[int] = []
        for number, body in enumerate(self._objects, start=1):
            offsets.append(len(out))
            out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
        xref_at = len(out)
        out += b"xref\n0 " + str(len(self._objects) + 1).encode() + b"\n"
        out += b"0000000000 65535 f \n"
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        out += (
            b"trailer\n<< /Size "
            + str(len(self._objects) + 1).encode()
            + b" /Root 3 0 R >>\nstartxref\n"
            + str(xref_at).encode()
            + b"\n%%EOF\n"
        )
        return bytes(out)


    # --- 六份样本 -----------------------------------------------------------


def merged_table_page() -> list[str]:
    """单页**带合并单元格**的表格：表头跨两列（colspan=2）、末行跨两列。.

    §3.3 的头号要求是"合并单元格、跨行表头、合计行原样保留"，所以 pdfplumber
    → `TableCell.col_span/row_span` 这条**真实 PDF 路径**必须有夹具。
    （合成的 `TableCell` + HTML 往返测的是模型与渲染，测不到 pdfplumber 怎么报
    合并格。）

    用"缺哪条线"表达合并——这也是真实排版的做法：

        +-------+---------------+
        | Role  |  Monthly cap  |   <- 第一行第 2 列处不画竖线 = 表头跨两列
        +-------+-------+-------+
        | staff | 2000  | 备注  |
        +-------+-------+-------+
        | audit |   12000      |   <- 末行第 2、3 列之间不画竖线 = 跨两列
        +-------+---------------+
    """
    col_x = [72.0, 232.0, 392.0, 523.0]
    ys = [700.0 - index * 30.0 for index in range(4)]
    ops = [
        PdfBuilder.text(72, 780, 14, "Reimbursement Matrix"),
        PdfBuilder.text(72, 756, 10, "Table 2. Caps by role"),
    ]
    # 横线：每行边界都画
    for y in ys:
        ops.append(PdfBuilder.line(col_x[0], y, col_x[-1], y))
    # 竖线：左右外框 + 第 2 条竖线只在表头以下
    ops.append(PdfBuilder.line(col_x[0], ys[0], col_x[0], ys[-1]))
    ops.append(PdfBuilder.line(col_x[-1], ys[0], col_x[-1], ys[-1]))
    ops.append(PdfBuilder.line(col_x[1], ys[1], col_x[1], ys[-1]))
    # 第 3 条竖线只画到最后一行以上（末行跨两列）
    ops.append(PdfBuilder.line(col_x[2], ys[1], col_x[2], ys[2]))
    ops.extend(
        [
            PdfBuilder.text(78, ys[0] - 20, 10, "Role"),
            PdfBuilder.text(250, ys[0] - 20, 10, "Monthly cap"),
            PdfBuilder.text(78, ys[1] - 20, 10, "staff"),
            PdfBuilder.text(238, ys[1] - 20, 10, "2000"),
            PdfBuilder.text(398, ys[1] - 20, 10, "per person"),
            PdfBuilder.text(78, ys[2] - 20, 10, "audit"),
            PdfBuilder.text(238, ys[2] - 20, 10, "12000"),
        ]
    )
    return ops


def prose_fragments_page() -> list[str]:
    """单栏、**长篇散文**排版：没有栏缝，但行距/对齐很不规则。.

    这份夹具专门用来逼住 `_looks_like_prose`：`text` 策略在这种版面上会按"空格
    对齐"切出一张表格，每格是半句话（`['Clause 4.2 app', 'lies to all']`）。
    双栏夹具能被"跨栏缝"判据抓住，所以抓不到这一条——必须用**单栏**版面。

    排版特点（都是真实公文里常见的样子）：
      * 左边界不对齐（有的行缩进）；
      * 行内有大空格（`Clause 4.2     applies to all staff.`）；
      * 没有表格线、没有图片。
    """
    ops = [PdfBuilder.text(72, 780, 14, "Long-form Policy Text")]
    paragraphs = [
        (
            72.0,
            "Clause 4.2 applies to all staff who travel on company business.",
        ),
        (
            72.0,
            "The lodging cap is CNY 500 per person per night in tier-1 cities.",
        ),
        (
            96.0,
            "This paragraph is indented, which breaks the left margin"
            " alignment.",
        ),
        (
            72.0,
            "Reimbursement requires itemised receipts for any single expense.",
        ),
        (72.0, "Claims must be submitted within 60 days of the expense date."),
    ]
    y = 740.0
    for x, text in paragraphs:
        ops.append(PdfBuilder.text(x, y, 10, text))
        y -= 18.0
    return ops


def table_pages() -> list[list[str]]:
    """两页带线表格：第 1 页表头 + 三行，第 2 页续表无表头。.

    第 1 页的表格刻意贴近页面底部。单元格坐标写死成网格，便于断言
    "bbox 还原出来正好落在这一格里"。

    第 1 页的表格刻意排到 y=118 起（页面底部 842，表格下沿落在页高 86% 处）——
    这是"跨页续表"判据的一半：上一页表格贴近底部、下一页表格贴近顶部、列数相同。
    第一版把表格排在页面中部（下沿只到 70%），于是续表判定永远不成立，测试也就
    测不到它声称在测的东西。
    """
    rows = [
        ["City", "Lodging cap", "Region"],
        ["Shanghai", "500", "tier-1"],
        ["Beijing", "500", "tier-1"],
        ["Hefei", "380", "tier-2"],
    ]
    col_x = [72.0, 232.0, 392.0]
    top = 118.0 + len(rows) * 28.0  # 表格上沿（用户空间 y）：下沿落在 118
    row_h = 28.0

    def draw_table(
        page_ops: list[str], data: list[list[str]], y_top: float
    ) -> None:
        """Run draw_table."""
        for index in range(len(data) + 1):
            y = y_top - index * row_h
            page_ops.append(PdfBuilder.line(72.0, y, 523.0, y))
        for x in [*col_x, 523.0]:
            page_ops.append(
                PdfBuilder.line(x, y_top, x, y_top - len(data) * row_h)
            )
        for r, row in enumerate(data):
            for c, cell in enumerate(row):
                page_ops.append(
                    PdfBuilder.text(
                        col_x[c] + 6.0,
                        y_top - (r + 1) * row_h + 9.0,
                        10,
                        cell,
                    )
                )

    page1 = [
        PdfBuilder.text(72, 780, 14, "Travel Expense Policy (CN)"),
        PdfBuilder.text(72, 762, 10, "Section 2. Lodging limits by city"),
        PdfBuilder.text(72, 738, 9, "Table 1. Lodging caps"),
        PdfBuilder.text(72, 724, 9, "The table below lists the cap per city."),
    ]
    draw_table(page1, rows, top)

    page2 = [
        PdfBuilder.text(72, 800, 14, "Travel Expense Policy (CN)"),
        PdfBuilder.text(72, 780, 10, "Section 2. Lodging limits (continued)"),
    ]
    cont = [["Chengdu", "380", "tier-2"], ["Urumqi", "320", "tier-3"]]
    top2 = (
        752.0  # 紧接标题下方 = 页面上部（页高 842，上沿 752 落在 89% 高度处）
    )
    for index in range(len(cont) + 1):
        y = top2 - index * row_h
        page2.append(PdfBuilder.line(72.0, y, 523.0, y))
    for x in [*col_x, 523.0]:
        page2.append(PdfBuilder.line(x, top2, x, top2 - len(cont) * row_h))
    for r, row in enumerate(cont):
        for c, cell in enumerate(row):
            page2.append(
                PdfBuilder.text(
                    col_x[c] + 6.0, top2 - (r + 1) * row_h + 9.0, 10, cell
                )
            )
    page2.append(
        PdfBuilder.text(72, 620, 10, "See Table 1 for the tier definitions.")
    )
    return [page1, page2]


def figure_page() -> list[str]:
    """一页矢量柱状图：纵轴 0/50/100 刻度、四根柱、图注。.

    数值对账要能验的两件事都在这里：每根柱的读数必须落在 0–100 的刻度范围内，
    四个百分比之和必须落在 99–101。
    """
    ops = [
        PdfBuilder.text(72, 780, 14, "Spend by category - 2025"),
        PdfBuilder.text(
            72, 758, 9, "Figure 1. Share of travel spend by category (percent)"
        ),
    ]
    # 坐标系：左下 (110, 300)，高 320（对应 0..100），宽 360（四个类别）
    x0, y0, height, width = 110.0, 300.0, 320.0, 360.0
    ops.append(PdfBuilder.line(x0, y0, x0, y0 + height))  # 纵轴
    ops.append(PdfBuilder.line(x0, y0, x0 + width, y0))  # 横轴
    for tick in (0, 50, 100):
        y = y0 + height * tick / 100.0
        ops.append(PdfBuilder.line(x0 - 6, y, x0, y))
        ops.append(PdfBuilder.text(x0 - 30, y - 4, 9, str(tick)))
    values = [("Transport", 32), ("Lodging", 28), ("Meals", 24), ("Other", 16)]
    bar_w = width / 4.0 * 0.6
    for index, (label, value) in enumerate(values):
        bx = x0 + width / 4.0 * index + (width / 4.0 - bar_w) / 2
        bh = height * value / 100.0
        ops.append(PdfBuilder.rect(bx, y0, bar_w, bh))
        ops.append(PdfBuilder.text(bx, y0 - 14, 9, label))
        ops.append(PdfBuilder.text(bx + 6, y0 + bh + 4, 9, str(value)))
    ops.append(
        PdfBuilder.text(
            72, 250, 9, "Y axis: percent of total spend. X axis: category."
        )
    )
    return ops


ImageTriple = tuple[
tuple[int, int, bytes], tuple[float, float, float, float] | None
]


def scanned_pages() -> list[tuple[list[str], ImageTriple | None]]:
    """两页：第 1 页正常文字层，第 2 页**只有位图**（扫描件的特征）。.

    第 2 页的位图里画的是真文字（用位图字体逐字画点阵），这样 OCR 兜底这条路
    才有可验证的内容——否则"OCR 有效"就只能靠"它没报错"来证明，那不叫验证。

    位图故意画得比页面小（390×300pt ≈ 页面的 23%）：第一版把"扫描页"的判据写成
    "位图覆盖 ≥ 25%
    页面"，这份夹具正好落在门槛之下，于是那一页被判成"不是扫描件"
    而一条切片都出不来。**这是真实场景**：传真件、单据贴图、印章页上的位图都只
    占一小块，但上面全是唯一的内容。
    """
    page1 = [
        PdfBuilder.text(72, 780, 14, "Scanned Policy Bundle"),
        PdfBuilder.text(72, 760, 10, "Section 1. This page has a text layer."),
    ]
    w, h = 480, 360
    px = bytearray()
    # 白底
    px += b"\xff\xff\xff" * (w * h)
    # 在位图里画三条"文字行"：每行是一串 8×10 的方块字块，肉眼与 OCR 都能认。
    # 不用真字体：这份夹具要的是"图上有可识别的文字块"，不是排版还原。
    glyph = [
        [
            0b01111110,
            0b10000001,
            0b10100101,
            0b10100101,
            0b10111101,
            0b10000001,
            0b01111110,
        ],
        [
            0b00011000,
            0b00111000,
            0b00011000,
            0b00011000,
            0b00011000,
            0b00011000,
            0b01111110,
        ],
    ]
    rows = [(60, "CY 500"), (140, "REF 2025"), (220, "TOTAL 1200")]
    for top, text in rows:
        x = 40
        for index, _ch in enumerate(text):
            g = glyph[index % 2]
            for gy, bits in enumerate(g):
                for gx in range(8):
                    if bits & (0b10000000 >> gx):
                        for sy in range(6):
                            for sx in range(6):
                                offset = (
                                    (top + gy * 6 + sy) * w + (x + gx * 6 + sx)
                                ) * 3
                                px[offset : offset + 3] = b"\x00\x00\x00"
            x += 8 * 6 + 6
    return [
        (page1, None),
        ([], ((w, h, bytes(px)), (72.0, 400.0, 462.0, 700.0))),
    ]


def rotated_pages() -> list[tuple[list[str], int]]:
    """两页：第 2 页标 `/Rotate 90`——与第 1 页内容相同，只是旋转了。."""
    body = [
        PdfBuilder.text(72, 780, 14, "Rotated Page Demo"),
        PdfBuilder.text(
            72, 758, 10, "Section 3. This sentence is on a rotated page."
        ),
        PdfBuilder.text(
            72, 742, 10, "The highlight box must still land on this line."
        ),
    ]
    return [(body, 0), (list(body), 90)]


def two_column_page() -> list[str]:
    """单页双栏：左栏先读完再读右栏（按行读会把两栏拼成一句）。."""
    ops = [PdfBuilder.text(72, 790, 14, "Two Column Policy")]
    left = [
        "Left column line one explains scope.",
        "Left column line two explains limits.",
        "Left column line three ends the scope.",
    ]
    right = [
        "Right column line one lists approvals.",
        "Right column line two lists receipts.",
        "Right column line three ends approvals.",
    ]
    for index, line in enumerate(left):
        ops.append(PdfBuilder.text(72, 750 - index * 16, 10, line))
    for index, line in enumerate(right):
        ops.append(PdfBuilder.text(320, 750 - index * 16, 10, line))
    return ops


def header_pages() -> list[list[str]]:
    """三页：页眉/页脚在页边带内，章节标题与条款在正文区。.

    版面刻意按真实制度文件排（单位 pt，页高 842）：

        页眉  y=812   -> top≈24   （2.9%，页边带内）
        章节  y=700   -> top≈135  （16%，正文区）
        条款  y=664   -> top≈171  （20%，正文区）
        正文  y=628   -> top≈207  （25%，正文区）
        页脚  y=24    -> top≈813  （96.6%，页边带内）

    **为什么要这样排**：§3.2 的"重复即页眉页脚"规则会误删"每页都出现的
    条款编号"，
    而 §3.2 的边界表给出的处置是"保留但标低置信"。这条夹具就是那条边界：
    `Clause 4.2 applies to all staff.` 三页重复、但不在页边带内 → **必须保留**。
    第一版夹具把所有行都堆在页面顶部 20% 里，于是页边带也盖住了章节标题，
    测出来的其实是"夹具不真实"，不是解析器错。
    """
    pages: list[list[str]] = []
    for page_no in range(1, 4):
        ops = [
            PdfBuilder.text(
                72, 812, 8, "ACME Internal - Travel Policy Handbook"
            ),
            PdfBuilder.text(72, 700, 14, f"Section {page_no}. Scope"),
            PdfBuilder.text(72, 664, 10, "Clause 4.2 applies to all staff."),
            PdfBuilder.text(
                72, 628, 10, f"Paragraph {page_no}: this text differs per page."
            ),
            PdfBuilder.text(72, 24, 8, f"Page {page_no} of 3"),
        ]
        pages.append(ops)
    return pages


SAMPLES = {
"layout/table-demo.pdf": "table",
"layout/merged-table-demo.pdf": "merged-table",
"layout/figure-demo.pdf": "figure",
"layout/scanned-demo.pdf": "scanned",
"layout/rotated-demo.pdf": "rotated",
"layout/two-column-demo.pdf": "two-column",
"layout/prose-demo.pdf": "prose",
"layout/header-demo.pdf": "header",
}


def build(name: str) -> bytes:
    """Run build."""
    kind = SAMPLES[name]
    builder = PdfBuilder()
    if kind == "table":
        for ops in table_pages():
            builder.add_page(ops)
    elif kind == "merged-table":
        builder.add_page(merged_table_page())
    elif kind == "figure":
        builder.add_page(figure_page())
    elif kind == "scanned":
        for ops, image in scanned_pages():
            builder.add_page(
                ops,
                image=image[0] if image else None,
                image_box=image[1] if image else None,
            )
    elif kind == "rotated":
        for ops, rotate in rotated_pages():
            builder.add_page(ops, rotate=rotate)
    elif kind == "two-column":
        builder.add_page(two_column_page())
    elif kind == "prose":
        builder.add_page(prose_fragments_page())
    elif kind == "header":
        for ops in header_pages():
            builder.add_page(ops)
    else:  # pragma: no cover
        raise SystemExit(f"unknown sample kind: {kind}")
    return builder.build()


def main() -> int:
    """Run main."""
    ap = argparse.ArgumentParser(
        description="generate layout sample PDFs (stdlib only)"
    )
    ap.add_argument(
        "--root",
        required=True,
        help=M["HELP_01"],
    )
    ap.add_argument("--force", action="store_true", help=M["HELP_02"])
    args = ap.parse_args()

    root = Path(args.root)
    written = kept = 0
    for rel in SAMPLES:
        target = root / rel
        data = build(rel)
        if target.exists() and not args.force and target.read_bytes() == data:
            kept += 1
            print(f"keep    : {rel} ({len(data)} bytes)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written += 1
        print(f"write   : {rel} ({len(data)} bytes)")
    print(f"\ndone    : wrote {written}, unchanged {kept} under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
