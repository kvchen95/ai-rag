"""版面还原：行、栏序、表格、图、页眉页脚（详细设计.md §3.2）。

输入是 pdfplumber 的一页 + 本页的 `PageSpace`，输出「阅读空间里的行、表格区域、
图区域」。五个决定写在这里，别处不再重复：

### 1. 先把字符搬进阅读空间，再做任何聚类

pdfplumber 给的是**显示空间**坐标。`/Rotate 90` 的页在显示空间里一行横排文字是
竖着排的，实测会被切成 34 个单字符行（`The highlight box...` → `T S R` / `h e` / …）。
所以第一步统一转成阅读空间（未旋转、左上原点），之后所有聚类、分栏、表格判断
都在那里面做。输出 bbox 时由 `Region.bbox(space)` 一次性转回去。

### 2. 行：自己从 char 聚，不用 `extract_text_lines()`

pdfplumber 的行是按 y 聚完直接横向拼接的，双栏页会把左右两栏拼成一句（实测
`Left column line one explains scope. Right column line one lists appro`）。
这里两步：先按 y 聚行，再按"横向大间距"切成若干段。

### 3. 行内空格要按坐标补

PDF 里的"空格"经常不是空格字符而是横向间隔。不补就会得到
`TravelExpensePolicy(CN)`——进 BM25 之后 `travel expense` 一个都命中不了。

### 4. 表格先占位，图再占位，剩下的才是正文

表格线在矢量图元里长得像"图"，矢量柱状图的柱子与网格线也长得像"表格框"。
先认表格（线段是硬证据），把范围占掉，再在剩余图元里找图，最后把没被占用的行
归正文。这样"同一段文字既是表格又是正文"的双计不会发生。

### 5. 页眉页脚只靠重复判，且只在"正文行"上判

§3.2：同一段文字在一半以上页面出现即判页眉页脚，删掉，但只删长度 < 40 字且
不含数字单位的行。比较前把连续数字换成 `#`——真实页眉几乎都带页码
（`Page 1 of 3`），逐字比较一个都认不出来；抹平只用于判定，删除的仍是原文。

**只在正文行上判**这一条是必须的：表格里的 `500`、`tier-1` 也跨页重复，
不排除就会把表格单元格从正文里删掉（更糟的是把整行表格键值对打散）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from api.parse.geometry import PageSpace, Rect, iou
from api.parse.model import (
    Line,
    TableCell,
    TableData,
    dedupe_overlapping,
    merge_nearby,
    subtract_regions,
)
from api.parse.tables import build_table, clean_cell_text

# --- 行的聚类参数（单位 pt）-------------------------------------------------
#
# 同一行的字符 top 会有 ±0.3pt 抖动（不同字号的基线对齐），所以容差取字号的一半：
# 10pt 正文给 5pt，14pt 标题给 7pt，都不会把相邻行并进来。
LINE_Y_TOLERANCE_RATIO = 0.5
# 一行内切成两段的横向间距阈值。正文词距约 2-4pt，栏间距通常 > 20pt；
# 取 18pt 既能切开双栏，又不会把对齐表格切成两行（那类间距一般 < 15pt，
# 而且表格在更早的一步已经被 pdfplumber 认走了）。
LINE_X_GAP_SPLIT = 18.0
# 行内补空格的阈值：两个字符的横向间隔超过它就补一个空格。
WORD_GAP_PT = 1.6
# 中日韩字符之间不补空格（中文本来不用空格断词），除非间隔大到像分栏。
CJK_GAP_PT = 6.0
# --- 行内文字宽度兜底 -------------------------------------------------------
#
# 有些 PDF（包括本项目 `scripts/make_samples.py` 生成的那三份：标准 14 字体但没写
# `/Widths`）的字符宽度是 0：泵出来的每个 char 的 `x0 == x1`。pdfplumber 照实报，
# 于是"行宽 = 0"——bbox 退化成一个竖条，`require_complete()` 会把它判成非法
# （`x1 > x0` 不成立），整份文档一条切片都出不来。
#
# 实测：`samples/policy/travel-cn.pdf` 的 268 个字符全是 `w=0.00`。
#
# 兜底做法：**活的 PDF 里字宽确实存在，只是没写下来**——按字体的经验宽度
# （西文 0.5 em、CJK 1 em）估一个，并按估算值推进游标。这与"猜内容"是两件事：
# 内容的准确性不依赖字宽，依赖的是字符本身；字宽只影响 bbox 的横向范围。
# 估算值不写回 `chars`，只用于拼行时算矩形——原始数据保持原样。
FALLBACK_LATIN_EM = 0.5
FALLBACK_CJK_EM = 1.0
# 字面量空格字符的宽度（em）：0.25 是常见值，用它把空格也算进行宽。
FALLBACK_SPACE_EM = 0.25

# --- 表格识别参数 -----------------------------------------------------------
#
# 两组线检测参数，顺序有意义（见 `find_tables`）：`lines` 权威，`text` 兜底。
TABLE_STRATEGIES: tuple[dict[str, Any], ...] = (
    {"vertical_strategy": "lines", "horizontal_strategy": "lines"},
    {"vertical_strategy": "text", "horizontal_strategy": "text"},
)
TABLE_MIN_ROWS = 2
TABLE_MIN_COLS = 2
TABLE_MIN_WIDTH = 60.0
TABLE_MIN_HEIGHT = 20.0
# 一份候选表有多大比例落在"已认表格或已占用区域"里就算重复。
TABLE_COVERED_RATIO = 0.3
# 单元格最大字符数：超过它就不像表格了。
TABLE_CELL_MAX_CHARS = 60
# 超长格占比超过这个数就判"这不是表格，是分栏正文"。
TABLE_LONG_CELL_RATIO = 0.25
# 以句末标点结尾的格占比超过这个数，同样判成"分栏正文"。真实表格的格很少以句号
# 结尾；而分栏排版被识成表格时，每格都是一句话（实测双栏夹具：
# `['Left column', 'line', 'one explains scope.', ...]` 7 列 × 7 行）。
TABLE_SENTENCE_CELL_RATIO = 0.3
_SENTENCE_END_CHARS = "。！？；.!?;"

# --- 图识别参数 -------------------------------------------------------------
#
# 位图小于这个尺寸忽略：图标、印章、装饰线不是"图"。§3.4 的图要能被 VLM 读出数值，
# 120×80pt（约 1.7×1.1 英寸）是能读出刻度的下限。
FIGURE_MIN_WIDTH = 120.0
FIGURE_MIN_HEIGHT = 80.0
# 矢量图元聚簇间距：柱状图的柱子之间、折线图的线段之间都是几个到几十 pt。
FIGURE_CLUSTER_GAP = 24.0
# 一页里矢量图元少于这个数就不当图：单独几条线更可能是分隔线。
FIGURE_MIN_PRIMITIVES = 4
# 图区域外扩：坐标轴刻度、类别标签、图注都在图的矩形外侧，VLM 转译要看到它们，
# 数值对账（§3.4）也要拿它们当参照。按图的尺寸比例外扩，比固定 pt 更稳。
FIGURE_LABEL_PAD_RATIO = 0.12
FIGURE_LABEL_PAD_MIN = 12.0
FIGURE_LABEL_PAD_MAX = 48.0
# 位图占页面面积比例 —— 判断"这页是不是扫描件"。
SCANNED_IMAGE_AREA_RATIO = 0.25

# --- 页眉页脚参数 -----------------------------------------------------------
HEADER_FOOTER_MIN_PAGES = 2  # 单页文档没有"重复"可言
HEADER_FOOTER_PAGE_RATIO = 0.5  # "一半以上页面"
HEADER_FOOTER_MAX_CHARS = 40
# 页边带（页面高度的比例）：上 12% 与下 12%。重复且落在带内才算页眉页脚
# ——只看重复会删掉每页的章节标题（实测三页夹具全丢），理由见
# `classify_header_footer` 的 docstring。
HEADER_BAND_RATIO = 0.12
FOOTER_BAND_RATIO = 0.12
# 数字+单位：金额、比例、期限、日期。带这些的行即便重复也不删——它多半是每页都印的
# "标准值"提示，删掉就是丢正文（§3.2 的原话）。
_NUMERIC_UNIT = re.compile(
    r"\d[\d,.]*\s*"
    r"(?:元|块|万|亿|%|％|天|日|个工作日|工作日|个月|月|年|小时|分钟|次|人|页|pt|px|CNY|RMB|USD|¥|\$)"
    r"|(?:CNY|RMB|USD|¥|\$)\s*\d",
    re.IGNORECASE,
)
_DIGITS = re.compile(r"\d+")
_CJK = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
)


def _is_cjk(char: str) -> bool:
    return bool(_CJK.match(char))


# --- 行提取 -----------------------------------------------------------------


def _char_rect(char: dict[str, Any]) -> Rect:
    return Rect(
        float(char["x0"]),
        float(char["top"]),
        float(char["x1"]),
        float(char["bottom"]),
    )


def _estimated_width(char: dict[str, Any]) -> float | None:
    """字宽为 0 时的估算宽度（pt）。字宽有效时返回 None。

    触发条件：`x1 - x0 <= 0.01`（不是严格的 0——浮点误差会有 1e-6 级的零宽）。
    """
    width = float(char["x1"]) - float(char["x0"])
    if width > 0.01:
        return None
    text = str(char.get("text") or "")
    if not text:
        return None
    height = float(char["bottom"]) - float(char["top"])
    if height <= 0:
        return None
    em = height  # 字号 ≈ 字高
    first = text[0]
    if first.isspace():
        return em * FALLBACK_SPACE_EM
    if _is_cjk(first):
        return em * FALLBACK_CJK_EM
    return em * FALLBACK_LATIN_EM


def fill_missing_widths(chars: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """给字宽为 0 的字符补上估算宽度，并把**后续字符的 x0/x1 依次推进**。

    为什么必须推进（而不是只给每个字符自己一个宽度）：字宽为 0 的 PDF 里所有
    字符的 `x0` 都是同一个值（实测 `travel-cn.pdf` 268 个字符全在 x=72）。
    只给宽度不推进，拼出来的行宽还是 0；推进之后行宽 = 估算总宽，
    bbox 才是"这段文字占的那一块"。

    推进只影响**同一行内**（同一个 `top`）的字符，且不修改入参：返回副本。
    有效宽度的字符原样保留，游标按它的真实 x1 走。
    """
    out: list[dict[str, Any]] = []
    cursor: dict[float, float] = {}  # top -> 下一个可用 x
    for char in sorted(chars, key=lambda c: (round(float(c["top"]), 1), float(c["x0"]))):
        clone = dict(char)
        estimated = _estimated_width(char)
        if estimated is None:
            out.append(clone)
            cursor[round(float(clone["top"]), 1)] = float(clone["x1"])
            continue
        key = round(float(clone["top"]), 1)
        start = max(float(clone["x0"]), cursor.get(key, float("-inf")))
        clone["x0"] = start
        clone["x1"] = start + estimated
        cursor[key] = clone["x1"]
        out.append(clone)
    # 恢复页内原始顺序（按 top 再按 x）：下游的行聚类自己会排序，
    # 但保持稳定顺序让调试输出可读。
    out.sort(key=lambda c: (float(c["top"]), float(c["x0"])))
    return out


def reading_chars(page: Any, space: PageSpace) -> list[dict[str, Any]]:
    """页面字符 -> 阅读空间副本（顺带补齐字宽为 0 的字符）。

    只搬坐标，不改其它键（`upright` 等仍可用于竖排判断）。`rotation=0` 时不做
    坐标变换，但**仍然要补字宽**：实测 `samples/` 里的三份制度文件全是零宽字符。

    顺序：先补字宽（在原坐标系里推进，逻辑最直观），再转空间（一次旋转不改变
    相邻关系）。
    """
    chars = fill_missing_widths(page.chars)
    if space.rotation == 0:
        return chars

    out: list[dict[str, Any]] = []
    for char in chars:
        rect = space.display_to_reading(_char_rect(char))
        clone = dict(char)
        clone["x0"], clone["top"], clone["x1"], clone["bottom"] = rect.as_tuple()
        out.append(clone)
    return out


def extract_lines(chars: Sequence[dict[str, Any]], page_num: int) -> list[Line]:
    """把（已经在阅读空间的）字符聚成行。

    **没有"竖排"分支**——这是实测改出来的（本模块第一版有，结果把旋转页的整段
    正文拆成了几十个单字符行）。原因：pdfplumber 的 `char["upright"]` 表达的是
    "这个字形相对**页面显示方向**是否正立"，`/Rotate 90` 的页上正常横排文字会被
    报成 `upright=False`。等我们把坐标搬进阅读空间之后，它们已经是正的横排了，
    再按 `upright` 判竖排就把整页拆散了。

    真正竖排的文字（从右到左的竖排中文、侧边栏骑缝章）在制度类 PDF 里极少，
    且本设计不做竖排重排：它们的字符会各自成行、仍可被检索到，但阅读顺序不保证。
    这是一个**已知限制**，写在 `api/README.md` 里，不假装能处理。
    """
    # 零面积字符不参与建行：真实研报（华泰中芯国际 page 5）里有 size=0 的
    # Helvetica 字符被从 PDF 内嵌流当文字层抽出来，坐标全在 (0, -0.08)、
    # x1==x0 且 bottom==top。它们会聚成一条宽高都为 0 的 Line，最终产出
    # bbox=[0,0,0,0] 的切片，让整份文档在 validate_chunks 终态失败。
    # 校验器（_valid_bbox 要求严格递增）是对的，脏数据要在这里就丢掉。
    usable = [
        char
        for char in chars
        if char.get("text", "").strip()
        and float(char["x1"]) > float(char["x0"])
        and float(char["bottom"]) > float(char["top"])
    ]
    if not usable:
        return []
    return sort_in_columns(_group_by_row(usable, page_num))


def _group_by_row(chars: Sequence[dict[str, Any]], page_num: int) -> list[Line]:
    """按 y 聚行 + 按 x 大缝隙切段。"""
    if not chars:
        return []
    ordered = sorted(chars, key=lambda c: (round(float(c["top"]), 1), float(c["x0"])))
    buckets: list[list[dict[str, Any]]] = []
    for char in ordered:
        height = float(char["bottom"]) - float(char["top"])
        tolerance = max(1.0, height * LINE_Y_TOLERANCE_RATIO)
        placed = False
        for bucket in reversed(buckets[-3:]):  # 只需回看最近几行
            center = sum(float(c["top"]) + float(c["bottom"]) for c in bucket) / (2 * len(bucket))
            if abs((float(char["top"]) + float(char["bottom"])) / 2 - center) <= tolerance:
                bucket.append(char)
                placed = True
                break
        if not placed:
            buckets.append([char])

    lines: list[Line] = []
    for bucket in buckets:
        for segment in _split_on_gap(bucket):
            text = _join_segment(segment)
            if not text:
                continue
            rect = Rect(
                min(float(c["x0"]) for c in segment),
                min(float(c["top"]) for c in segment),
                max(float(c["x1"]) for c in segment),
                max(float(c["bottom"]) for c in segment),
            )
            heights = sorted(float(c["bottom"]) - float(c["top"]) for c in segment)
            lines.append(
                Line(
                    text=text,
                    rect=rect,
                    page_num=page_num,
                    font_size=heights[len(heights) // 2],
                )
            )
    return lines


def _split_on_gap(chars: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(chars, key=lambda c: float(c["x0"]))
    if not ordered:
        return []
    segments: list[list[dict[str, Any]]] = [[ordered[0]]]
    for prev, char in zip(ordered, ordered[1:]):
        gap = float(char["x0"]) - float(prev["x1"])
        if gap > LINE_X_GAP_SPLIT:
            segments.append([char])
        else:
            segments[-1].append(char)
    return segments


def _join_segment(segment: Sequence[dict[str, Any]]) -> str:
    """按坐标拼接一行。

    两种"空格"分开处理：
      * 字面量空格字符：原样保留（PDF 里真有）；
      * 只有坐标间隔没有空格字符：按 `WORD_GAP_PT` 补一个空格。

    中文（CJK）之间不补：中文本来不用空格断词，补了反而让"住宿标准"变成
    "住宿 标准"，与词典分词对不上。间隔大到 `CJK_GAP_PT` 时仍补——那通常是两个
    独立段落被同行排版。
    """
    ordered = sorted(segment, key=lambda c: float(c["x0"]))
    out: list[str] = []
    previous: dict[str, Any] | None = None
    for char in ordered:
        text = char["text"]
        if previous is not None and text.strip() and str(previous["text"]).strip():
            gap = float(char["x0"]) - float(previous["x1"])
            prev_char = str(previous["text"])[-1]
            cur_char = text[0]
            if _is_cjk(prev_char) or _is_cjk(cur_char):
                if gap > CJK_GAP_PT:
                    out.append(" ")
            elif gap > WORD_GAP_PT:
                out.append(" ")
        out.append(text)
        previous = char
    return re.sub(r"\s+", " ", "".join(out)).strip()


# --- 分栏 -------------------------------------------------------------------


def detect_columns(
    lines: Sequence[Line], *, min_gutter: float = 12.0
) -> list[tuple[float, float]] | None:
    """检测竖向栏缝，返回各栏的 x 区间；单栏返回 None。

    三条同时成立才算双栏：
      1. 存在宽度 ≥ `min_gutter` 的竖带，**没有任何行**横穿它；
      2. 竖带两侧各有至少两行；
      3. 两侧的行都不越界。

    只看"缝在哪"不够：两端对齐的正文在中间也可能留出空隙。所以还要第 3 条——
    真的分成两栏时，左右两栏各自有稳定的边界。
    """
    if len(lines) < 4:
        return None
    ordered = sorted(lines, key=lambda line: line.rect.x0)
    best: tuple[float, float, int, int] | None = None
    for left, right in zip(ordered, ordered[1:]):
        gap_start = left.rect.x1
        gap_end = right.rect.x0
        if gap_end - gap_start < min_gutter:
            continue
        crossing = [
            line
            for line in lines
            if line.rect.x0 < gap_end - 0.5 and line.rect.x1 > gap_start + 0.5
        ]
        if crossing:
            continue
        left_lines = [line for line in lines if line.rect.x1 <= gap_start + 0.5]
        right_lines = [line for line in lines if line.rect.x0 >= gap_end - 0.5]
        if len(left_lines) < 2 or len(right_lines) < 2:
            continue
        score = len(left_lines) + len(right_lines)
        if best is None or score > best[2] + best[3]:
            best = (gap_start, gap_end, len(left_lines), len(right_lines))
    if best is None:
        return None
    gap_start, gap_end, _, _ = best
    left_lines = [line for line in lines if line.rect.x1 <= gap_start + 0.5]
    right_lines = [line for line in lines if line.rect.x0 >= gap_end - 0.5]
    return [
        (min(line.rect.x0 for line in left_lines), gap_start),
        (gap_end, max(line.rect.x1 for line in right_lines)),
    ]


def sort_in_columns(lines: Sequence[Line]) -> list[Line]:
    """按栏序 + 行序排序；检测不出双栏就只按行序排。"""
    if not lines:
        return []
    columns = detect_columns(lines)
    if columns is None:
        return sorted(lines, key=lambda line: (round(line.rect.top, 1), line.rect.x0))

    def key(line: Line) -> tuple[int, float, float]:
        for index, (x0, x1) in enumerate(columns):
            if line.rect.x0 >= x0 - 0.5 and line.rect.x1 <= x1 + 0.5:
                return (index, round(line.rect.top, 1), line.rect.x0)
        # 落不进任何一栏（跨栏标题、通栏页眉）：排在最前，按 y 走
        return (-1, round(line.rect.top, 1), line.rect.x0)

    return sorted(lines, key=key)


# --- 表格 -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectedTable:
    rect: Rect
    table: TableData


def find_tables(
    page: Any,
    space: PageSpace,
    *,
    taken: Sequence[Rect] = (),
    strategies: Sequence[dict[str, Any]] | None = None,
    column_gutters: Sequence[tuple[float, float]] = (),
    lines: Sequence[Line] = (),
) -> list[DetectedTable]:
    """找出页面上的表格，产出三表示（矩形已换算到阅读空间）。

    两份策略都要用，但顺序与去重规则不一样（实测出来的）：

      * `lines`（有网格线的表）先跑，它权威——线段是硬证据；
      * `text`（无线、靠对齐的表）后跑，只接受"落在已认表格之外"的结果。

    去重不能只看 IoU：实测 `text` 策略会把同一张有线的表识成更小的一块
    （7 行 × 2 列，只盖住左半边），与 `lines` 结果 IoU 只有 0.19，用 IoU 拦不住，
    于是一张表变两张、切片里出现两条互相矛盾的"同一个表"。用"这份结果有多大比例
    落在已认表格里"（> 0.3 就丢）才拦得住。

    表格交给 pdfplumber 在**显示空间**里检测，检测完再把 bbox 换算到阅读空间：
    pdfplumber 的线检测与字符位置同在显示空间，混两个空间去比会全错。

    `column_gutters` 是 `detect_columns` 的结果：**无线的候选表如果跨过栏缝，
    它就不是表格而是分栏正文**。这条判据比"单元格像不像句子"可靠得多——实测
    双栏夹具被识成 7×7 的表，但只有 3 个格带句号（12.5%），按句子比例判不出来，
    而它确确实实横跨了 233–320pt 这条栏缝。
    """
    accepted: list[Rect] = []
    out: list[DetectedTable] = []
    taken_reading = list(taken)
    for settings in strategies or TABLE_STRATEGIES:
        # 只有 `lines` 策略是"有网格线"（硬证据）。传进来的 strategies 可能是子集
        # （旋转页只用 lines），此时第一个就是 wired。
        wired = settings.get("vertical_strategy") == "lines"
        try:
            found = page.find_tables(table_settings=settings)
        except Exception:  # noqa: BLE001 - pdfplumber 在畸形页上会抛
            continue
        for table in found:
            display_rect = Rect(*[float(v) for v in _display_bbox(table.bbox)])
            reading_rect = space.display_to_reading(display_rect)
            if reading_rect.width < TABLE_MIN_WIDTH or reading_rect.height < TABLE_MIN_HEIGHT:
                continue
            if _covered_by(reading_rect, [*accepted, *taken_reading]):
                continue
            data = _table_data(table)
            if data is None:
                continue
            if not wired:
                # 只看无线候选：有线的表哪怕格子很长、跨着栏缝也是表（线段是硬证据）。
                if _crosses_gutter(reading_rect, column_gutters) or _looks_like_prose(data):
                    continue
                if _cells_are_fragments(data, reading_rect, lines):
                    # 每个"格"都是一行文字被**横向截断**的两半（实测：单栏正文在
                    # text 策略下被切成 ['Clause 4.2 app', 'lies to all']）。
                    # 真表格不会把同一行文字从中间劈开。
                    continue
                if _contains_a_known_table(reading_rect, accepted):
                    # 这份候选表把一张**已认的**表格整个包在里面 —— 它一定是
                    # "标题 + 说明 + 整张表"被拼成的一张假表（实测形态：15×4，
                    # 前 8 行是标题与说明，后 4 行是真表格）。
                    # `_covered_by` 只查"候选在已认表格里面"，查不到这个反方向。
                    continue
                if _has_too_many_blank_rows(data):
                    # 超过 15% 的行整行是占位符 —— 真表格里不会有大量空行；
                    # 这是 `text` 策略在段落之间按行距加出来的填充行。
                    continue
                if _swallows_the_page(reading_rect, space):
                    # 整页被框成一张表。真表格可以很大，但那种形态是"版面"。
                    continue
            accepted.append(reading_rect)
            out.append(DetectedTable(rect=reading_rect, table=data))
    return out


def _contains_a_known_table(rect: Rect, accepted: Sequence[Rect], *, ratio: float = 0.8) -> bool:
    """`rect` 是否把某张已认表格的 80% 以上包在里面。

    这是"假表格 = 标题 + 说明 + 真表格"这种形态的直接判据。反向的
    "候选落在已认表格里"由 `_covered_by` 负责，两个方向都要查：
    只查一个方向时，实测会出现"整页假表把真表吞掉、两张表都进库"的结果。
    """
    for known in accepted:
        inter = rect.intersection(known)
        if inter is None or known.area <= 0:
            continue
        if inter.area / known.area >= ratio:
            return True
    return False


def _has_too_many_blank_rows(data: TableData, *, threshold: float = 0.15) -> bool:
    if data.row_count < 3:
        return False
    matrix = data.matrix()
    blank = sum(1 for row in matrix if all(value in {"-", ""} for value in row))
    return blank / data.row_count > threshold


def _swallows_the_page(rect: Rect, space: PageSpace, *, ratio: float = 0.6) -> bool:
    """这份候选表是否占掉了页面的大部分面积。

    真表格可以很大，但不会同时占掉**大半页的宽度与大半个页高**——那种形态是
    "这一页的版面"，不是表格。实测：夹具里的表格贴近页面底部之后，`text` 策略把
    "标题 + 说明 + 整张表"识成一张 15×4 的假表，覆盖页面的 76%×85% = 65%。
    """
    width, height = space.reading_size
    if width <= 0 or height <= 0:
        return False
    return (rect.width / width) * (rect.height / height) >= ratio


def _cells_are_fragments(
    data: TableData, rect: Rect, lines: Sequence[Line], *, threshold: float = 0.4
) -> bool:
    """表格的格是否是"同一行文字被截断的两半"。

    判据：占比 ≥ `threshold` 的格，其文本恰好是某一整行的**真前缀/真后缀**（且
    不是整行本身）。这只能是无线的 `text` 策略在正文上按对齐切出来的假表格——
    真实表格的单元格是完整的字段值，不会出现"某行文字的一半"。

    用 `rect` 限定在表格范围内的行，避免拿页面别处的文字误配。
    """
    texts = [cell.text.strip() for cell in data.cells if cell.text.strip()]
    if not texts or not lines:
        return False
    candidates = [
        line.text.strip()
        for line in lines
        if rect.intersection(line.rect) is not None and len(line.text.strip()) > 6
    ]
    if not candidates:
        return False
    fragments = 0
    for text in texts:
        for line_text in candidates:
            if text != line_text and (line_text.startswith(text) or line_text.endswith(text)):
                fragments += 1
                break
    return fragments / len(texts) >= threshold


def _crosses_gutter(rect: Rect, gutters: Sequence[tuple[float, float]]) -> bool:
    """`rect` 是否横跨某条栏缝（两头都在缝外）。

    只要有一端落在缝里就不算"跨"——表格边线压在栏缝上是正常的排版。
    """
    for gap_start, gap_end in gutters:
        if rect.x0 < gap_start - 0.5 and rect.x1 > gap_end + 0.5:
            return True
    return False


def _looks_like_prose(table: TableData) -> bool:
    """这张"表"是不是其实是分栏正文。

    两条判据，命中任一即判成正文：
      * 超长单元格（> `TABLE_CELL_MAX_CHARS`）占比 ≥ `TABLE_LONG_CELL_RATIO`；
      * 以句末标点结尾的格占比 ≥ `TABLE_SENTENCE_CELL_RATIO`。

    都用比例而不是"有就否"：正常表格里也会有一两个描述格（"备注：本表自 2025 年起
    执行……"）或一个带句号的说明，按"有就否"会把真表格也毙掉。

    第二条是针对实测形态的：双栏排版被 `text` 策略识成 7×7 的格，每格很短
    （`Left column` / `line` / `one explains scope.`），只靠长度判不出来；
    但其中一半的格以句号结尾，而真表格的格几乎不会。
    """
    texts = [cell.text for cell in table.cells if cell.text.strip()]
    if not texts:
        return False
    long_cells = sum(1 for text in texts if len(text) > TABLE_CELL_MAX_CHARS)
    if long_cells / len(texts) >= TABLE_LONG_CELL_RATIO:
        return True
    sentence_cells = sum(1 for text in texts if text.rstrip()[-1:] in _SENTENCE_END_CHARS)
    return sentence_cells / len(texts) >= TABLE_SENTENCE_CELL_RATIO


def _display_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    """pdfplumber 的 bbox 是 `(x0, top, x1, bottom)`（显示空间）。"""
    x0, top, x1, bottom = bbox
    return (min(x0, x1), min(top, bottom), max(x0, x1), max(top, bottom))


def _covered_by(rect: Rect, blocks: Sequence[Rect], *, threshold: float = TABLE_COVERED_RATIO) -> bool:
    """`rect` 有多大比例落在已占用区域里（超过阈值就算重复/不是独立区域）。"""
    if not blocks or rect.area <= 0:
        return False
    covered = 0.0
    for block in blocks:
        inter = rect.intersection(block)
        if inter is not None:
            covered += inter.area
    return covered / rect.area > threshold


def _table_data(table: Any) -> TableData | None:
    """pdfplumber 的 Table -> 我们的 TableData。

    合并单元格**不靠猜**：pdfplumber 已经把跨行/跨列的格给成一个"大 bbox"，被覆盖
    的位置是 `None`（实测：表头跨三列时 row0 只有 cell0，bbox 撑满整行宽）。

    两件事分开取，来源不同：
      * **几何**（span）：从 `table.rows[..].cells` 的 bbox 反推网格线——bbox 跨了
        几条边界就是跨了几行/列。这里不能用 `.text`：`cells` 里放的是纯 `tuple`
        （实测没有 `.text`，第一版按 `.text` 取，整张表全空）。
      * **文本**：只能从 `table.extract()` 拿——它按网格返回矩阵，合并格的文本落在
        左上角那一格，其余位置是 `None`。自己按 bbox 过滤 chars 再拼是第二套实现，
        与 pdfplumber 的空格处理必然有细微差异。
    """
    rows = getattr(table, "rows", None)
    if not rows:
        return None

    row_boxes: list[list[tuple[float, float, float, float] | None]] = []
    boxes: list[tuple[float, float, float, float]] = []
    for row in rows:
        parsed: list[tuple[float, float, float, float] | None] = []
        for cell in getattr(row, "cells", None) or ():
            if cell is None:
                parsed.append(None)
                continue
            box = (float(cell[0]), float(cell[1]), float(cell[2]), float(cell[3]))
            parsed.append(box)
            boxes.append(box)
        row_boxes.append(parsed)
    if not boxes:
        return None

    try:
        matrix = table.extract()
    except Exception:  # noqa: BLE001 - pdfplumber 在畸形表上会抛
        matrix = []

    def edges(values: Iterable[float]) -> list[float]:
        collected: list[float] = []
        for value in sorted(values):
            if not collected or abs(value - collected[-1]) > 1.0:
                collected.append(value)
        return collected

    x_edges = edges([b[0] for b in boxes] + [b[2] for b in boxes])
    y_edges = edges([b[1] for b in boxes] + [b[3] for b in boxes])
    if len(x_edges) < 2 or len(y_edges) < 2:
        return None
    col_count = len(x_edges) - 1
    row_count = len(y_edges) - 1
    if row_count < TABLE_MIN_ROWS or col_count < TABLE_MIN_COLS:
        return None

    placed: dict[tuple[int, int], TableCell] = {}
    for source_row, parsed in enumerate(row_boxes):
        for source_col, box in enumerate(parsed):
            if box is None:
                continue
            col = _edge_index(x_edges, box[0])
            col_end = _edge_index(x_edges, box[2])
            row = _edge_index(y_edges, box[1])
            row_end = _edge_index(y_edges, box[3])
            text = _matrix_text(matrix, row, col, fallback=(source_row, source_col))
            placed.setdefault(
                (row, col),
                TableCell(
                    row=row,
                    col=col,
                    text=clean_cell_text(text),
                    row_span=max(1, row_end - row),
                    col_span=max(1, col_end - col),
                ),
            )

    cells = [placed[key] for key in sorted(placed)]
    if not cells:
        return None
    header_texts = [cell.text for cell in cells if cell.row == 0]
    header_inferred = not _looks_like_header(header_texts)
    with_header = [
        TableCell(
            row=cell.row,
            col=cell.col,
            text=cell.text,
            is_header=cell.row == 0,
            row_span=cell.row_span,
            col_span=cell.col_span,
        )
        for cell in cells
    ]
    return build_table(
        with_header,
        row_count=row_count,
        col_count=col_count,
        header_inferred=header_inferred,
    )


def _matrix_text(
    matrix: Sequence[Sequence[str | None]],
    row: int,
    col: int,
    *,
    fallback: tuple[int, int],
) -> str:
    """从 `table.extract()` 的矩阵取文本：先按网格坐标，取不到按行内位置。"""
    for r, c in ((row, col), fallback):
        if 0 <= r < len(matrix) and 0 <= c < len(matrix[r]):
            value = matrix[r][c]
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _edge_index(edges: Sequence[float], value: float) -> int:
    """`value` 落在第几条网格线上（取最近的一条；`edges` 已按 1pt 合并过）。"""
    best = 0
    best_delta = float("inf")
    for index, edge in enumerate(edges):
        delta = abs(edge - value)
        if delta < best_delta:
            best, best_delta = index, delta
    return best


def _looks_like_header(texts: Sequence[str]) -> bool:
    """首行是不是表头：**没有一格是纯数值**。

    第一版写成"不是所有格都是数值就当表头"，实测立刻错：跨页续表的首行是
    `['Chengdu', '380', 'tier-2']`，只有 `380` 是数值（1/3），于是被判成表头，
    然后 `table_content_text` 拿它当列名拼出 `Chengdu: Urumqi | 380: 320 |
    tier-2: tier-3` 这种胡说。

    改成"有任何一格是纯数值就不是表头"。代价是：真表头里带数字（`2025年标准`）
    时它仍算表头（`2025年标准` 不是纯数值），符合预期。
    """
    if not texts:
        return False
    return not any(_bare_number(text) for text in texts)


def _bare_number(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    cleaned = re.sub(r"[,\s¥$%％]", "", stripped)
    cleaned = re.sub(
        r"(元|块|万|亿|天|日|个月|年|小时|分钟|次|人|页|CNY|RMB|USD)$", "", cleaned, flags=re.I
    )
    try:
        float(cleaned)
    except ValueError:
        return False
    return True


# --- 图 ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetectedFigure:
    """一块图区域（阅读空间）。`rect` 已外扩到含刻度/标签/图注。

    `core_rect` 是外扩之前的紧致矩形，裁图时用**它 + 一点余量**（把刻度也裁进去是
    有意的：VLM 要读刻度才能对账）。

    `data` 是转译 + 对账都通过之后挂上的 `FigureData`（由 `pipeline` 填）。为空的
    `DetectedFigure` 永远不该进 regions——那是一条没有内容的图切片。
    """

    rect: Rect
    core_rect: Rect
    kind: str
    source: str  # image / vector
    data: Any | None = None


def find_figures(
    page: Any,
    space: PageSpace,
    lines: Sequence[Line],
    *,
    taken: Sequence[Rect] = (),
) -> list[DetectedFigure]:
    """在没被表格占用的地方找图（矩形换算到阅读空间）。

    两类来源：
      * 位图对象（`page.images`）：扫描件、贴进去的截图；
      * 矢量图元（rects/curves/lines）：柱状图、折线图、流程图。按间距聚簇。

    矢量图元的难点是"它也可能是表格线或分隔线"：表格已被 `taken` 占掉，剩下的零散
    线要求个数 ≥ `FIGURE_MIN_PRIMITIVES` 才当图——正文里的两条分隔线不该产出图表切片。

    找到之后按 `FIGURE_LABEL_PAD_RATIO` 外扩，并把外扩带内的**文字行**一并吸收：
    刻度的 `0/50/100`、类别的 `Transport`、图注都在图的矩形外（实测柱状图的
    刻度在 x0-30 的位置）。不外扩，VLM 只能看到柱子看不到刻度，数值对账必然失败。
    """
    out: list[DetectedFigure] = []

    image_rects: list[Rect] = []
    for image in page.images:
        rect = space.display_to_reading(
            Rect(
                float(image["x0"]),
                float(image["top"]),
                float(image["x1"]),
                float(image["bottom"]),
            )
        )
        if rect.width < FIGURE_MIN_WIDTH or rect.height < FIGURE_MIN_HEIGHT:
            continue
        if _covered_by(rect, taken, threshold=0.5):
            continue
        image_rects.append(rect)
    for rect in dedupe_overlapping(image_rects):
        out.append(
            DetectedFigure(
                rect=_absorb_labels(rect, lines),
                core_rect=rect,
                kind="image",
                source="image",
            )
        )

    primitives: list[Rect] = []
    for collection in (page.curves, page.rects, page.lines):
        for primitive in collection:
            rect = space.display_to_reading(
                Rect(
                    float(primitive["x0"]),
                    float(primitive["top"]),
                    float(primitive["x1"]),
                    float(primitive["bottom"]),
                )
            )
            if rect.width < 1.0 and rect.height < 1.0:
                continue  # 退化成一个点
            if _center_inside(rect, taken):
                continue
            primitives.append(rect)

    if len(primitives) >= FIGURE_MIN_PRIMITIVES:
        for cluster in merge_nearby(primitives, max_gap=FIGURE_CLUSTER_GAP):
            if cluster.width < FIGURE_MIN_WIDTH or cluster.height < FIGURE_MIN_HEIGHT:
                continue
            if any(iou(cluster.as_tuple(), other.core_rect.as_tuple()) > 0.5 for other in out):
                continue
            out.append(
                DetectedFigure(
                    rect=_absorb_labels(cluster, lines),
                    core_rect=cluster,
                    kind="vector",
                    source="vector",
                )
            )
    return out


def _center_inside(rect: Rect, blocks: Sequence[Rect]) -> bool:
    """`rect` 的**中心点**是否落在某个已占用区域里。

    表格的网格线与矢量图的图元经常共用同一条线（柱状图的轴线就是表格的下边框），
    按面积重叠判会把它们互相吃掉；中心点只落在一处。
    """
    cx = (rect.x0 + rect.x1) / 2.0
    cy = rect.center_y
    return any(block.contains_point(cx, cy) for block in blocks)


def _absorb_labels(core: Rect, lines: Sequence[Line]) -> Rect:
    """外扩 + 吸收外扩带内的文字行（刻度、类别名、图注）。

    迭代两轮：第一轮吸进来的图注可能把区域撑大到能覆盖更多标签，第二轮再吸一次。
    两轮足够（再远就不是这张图的标签了），固定次数比"循环到收敛"更容易预测。
    """
    pad = min(FIGURE_LABEL_PAD_MAX, max(FIGURE_LABEL_PAD_MIN, max(core.width, core.height) * FIGURE_LABEL_PAD_RATIO))
    region = core.expand(pad)
    for _ in range(2):
        grown = region
        for line in lines:
            if line.source == "vertical":
                continue
            if _line_touches(line.rect, region, pad):
                grown = grown.union(line.rect)
        if grown.as_tuple() == region.as_tuple():
            break
        region = grown
    return region


def _line_touches(line: Rect, region: Rect, pad: float) -> bool:
    """这行文字是否贴着区域（在区域里，或在 pad 带内）。"""
    if region.contains_point((line.x0 + line.x1) / 2.0, line.center_y):
        return True
    return region.expand(pad).intersection(line) is not None


def has_large_image(page: Any) -> bool:
    """这一页有没有占面积 ≥ 1/4 的位图（扫描页判定的必要条件）。"""
    page_area = float(page.width) * float(page.height)
    if page_area <= 0:
        return False
    for image in page.images:
        area = abs(float(image["x1"]) - float(image["x0"])) * abs(
            float(image["bottom"]) - float(image["top"])
        )
        if area / page_area >= SCANNED_IMAGE_AREA_RATIO:
            return True
    return False


# --- 页眉页脚 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeaderFooterVerdict:
    """一行的页眉页脚判定结果。`reason` 用于日志与验收报告（别处不再解释）。"""

    is_header_footer: bool
    reason: str


def repeated_key(text: str) -> str:
    """比较用的键：数字抹平 + 大小写归一 + 空白收拢。

    抹平数字是必须的：真实页眉/页脚几乎都带页码，逐字比较一个都认不出来。
    """
    collapsed = re.sub(r"\s+", " ", text).strip().lower()
    return _DIGITS.sub("#", collapsed)


def has_numeric_unit(text: str) -> bool:
    return bool(_NUMERIC_UNIT.search(text))


def in_margin_band(line: Line, page_width: float, page_height: float) -> bool:
    """这行是否落在页边带（上 12% 或下 12%）。

    用**行的中心点**而不是"任意一部分进入带内"：一行骑在 12% 线上时，
    按中心点判才不会因为一两个像素的抖动改变结论。
    """
    del page_width  # 只按纵向判；横向的页边（左右 2cm）在双栏排版里没有意义
    if page_height <= 0:
        return False
    center = line.rect.center_y / page_height
    return center <= HEADER_BAND_RATIO or center >= 1.0 - FOOTER_BAND_RATIO


def classify_header_footer(
    page_lines: Sequence[Sequence[tuple[str, bool]]],
    *,
    min_pages: int = HEADER_FOOTER_MIN_PAGES,
    page_ratio: float = HEADER_FOOTER_PAGE_RATIO,
    max_chars: int = HEADER_FOOTER_MAX_CHARS,
    require_margin: bool = True,
) -> dict[str, "HeaderFooterVerdict"]:
    """跨页判定：返回 {原文: 判定}。

    输入是每页的 `[(行文本, 是否在页边带内), ...]`。带"页边带"这条不是可选的实
    现细节，是本设计对 §3.2 的**一处收紧**（理由见下）。

    三步：
      1. 统计每个键出现在多少**不同页**上（同一页重复出现不算——目录页里同一个
         标题出现两次不是页眉）；
      2. 页数 ≥ max(min_pages, ceil(总页数 × page_ratio)) 的键进候选；
      3. 候选再按"长度 < `max_chars` 且不含数字单位"筛一遍。

    ### 为什么加"必须在页边带内"

    §3.2 的规则是"同一段文字在一半以上页面出现即判页眉页脚"，并承认它会误删
    "每页都出现的条款编号"。实测（三页的 `header-demo` 夹具 + 两页的
    `table-demo`）：这条规则还会删掉**每页的章节标题**——
    `Section 1/2/3. Scope` 抹平数字后同键，三页都命中，于是三页的正文标题全被删，
    合计丢掉整篇文档的结构。这不是"误删一个条款编号"的量级。

    收紧的判据是"重复 **且** 位于页边带（上/下 12%）"。页眉页脚的定义本来就包含
    位置；而"重复"仍然是主判据（只看位置会把正文顶部的第一行删掉）。
    两页文档里 `Cover` 那种"通栏大标题在顶部"的情况仍会被删——那是要删的。

    `require_margin=False` 只用于复现"纯重复"口径（对比实验、回归用例）。
    """
    pages = len(page_lines)
    if pages < min_pages:
        return {}
    threshold = max(min_pages, _ceil(pages * page_ratio))
    pages_by_key: dict[str, set[int]] = {}
    originals_by_key: dict[str, set[str]] = {}
    in_margin_by_original: dict[str, bool] = {}
    for page_index, lines in enumerate(page_lines):
        for text, in_margin in lines:
            key = repeated_key(text)
            if not key:
                continue
            pages_by_key.setdefault(key, set()).add(page_index)
            originals_by_key.setdefault(key, set()).add(text)
            # 同一原文只要有一处在页边带外，就不能靠"在页边带内"的那次判它。
            in_margin_by_original[text] = in_margin_by_original.get(text, True) and in_margin

    verdicts: dict[str, HeaderFooterVerdict] = {}
    for key, hit_pages in pages_by_key.items():
        if len(hit_pages) < threshold:
            continue
        for original in originals_by_key[key]:
            if len(original.strip()) >= max_chars:
                verdicts[original] = HeaderFooterVerdict(False, "too_long")
                continue
            if has_numeric_unit(original):
                verdicts[original] = HeaderFooterVerdict(False, "numeric_unit_protected")
                continue
            if require_margin and not in_margin_by_original.get(original, False):
                verdicts[original] = HeaderFooterVerdict(False, "not_in_margin_band")
                continue
            verdicts[original] = HeaderFooterVerdict(
                True, f"repeated_on_{len(hit_pages)}_pages"
            )
    return verdicts


def _ceil(value: float) -> int:
    integer = int(value)
    return integer if integer == value else integer + 1


# --- 正文区域组装 -----------------------------------------------------------


def build_text_regions(
    lines: Sequence[Line],
    *,
    blocked: Sequence[Rect],
    paragraph_gap_ratio: float = 0.9,
) -> list[tuple[Rect, list[Line]]]:
    """把正文行按段落聚成区域（每段一块矩形 + 行列表）。

    `blocked` 是表格与图的矩形：落进去的行不属于正文（已经被表格/图吃掉）。
    用"行中心点在不在块里"而不是面积重叠——一行横跨表格边线时面积会一半在内
    一半在外，中心点只会落在一侧。

    段落判据是"相邻两行的间距 > 行高的 0.9 倍"：这就是 PDF 里的空行。按内容判
    （比如句号结尾）会把"1. 范围"这种短行单独成段，反而更碎。
    """
    in_region = [line for line in lines if not _line_blocked(line, blocked)]
    if not in_region:
        return []

    ordered = sort_in_columns(in_region)
    paragraphs: list[list[Line]] = []
    for line in ordered:
        if paragraphs:
            previous = paragraphs[-1][-1]
            gap = line.rect.top - previous.rect.bottom
            tolerance = max(previous.rect.height, line.rect.height, 1.0) * paragraph_gap_ratio
            same_font = abs(previous.font_size - line.font_size) <= 1.5
            # 同一段要求：同一栏、间距不过大、字号同级。字号变了就是标题/正文切换，
            # 并进同一段会让"标题 + 正文"变成一块 bbox，高亮时框住一屏。
            if same_font and gap <= tolerance and gap > -previous.rect.height:
                paragraphs[-1].append(line)
                continue
        paragraphs.append([line])

    out: list[tuple[Rect, list[Line]]] = []
    for paragraph in paragraphs:
        rect = Rect(
            min(line.rect.x0 for line in paragraph),
            min(line.rect.top for line in paragraph),
            max(line.rect.x1 for line in paragraph),
            max(line.rect.bottom for line in paragraph),
        )
        out.append((rect, paragraph))
    return out


def _line_blocked(line: Line, blocked: Sequence[Rect]) -> bool:
    cx = (line.rect.x0 + line.rect.x1) / 2.0
    cy = line.rect.center_y
    return any(block.contains_point(cx, cy) for block in blocked)


def paragraph_text(lines: Sequence[Line]) -> str:
    """段落文本：同段内用空格连接。

    不用 `\\n`：这些文本要进 BM25 与向量，换行在词法上是额外分隔符，会让
    "同一句话被切成两段"变成一个无法解释的召回下降。
    """
    return " ".join(line.text.strip() for line in lines if line.text.strip())


def page_rect(space: PageSpace) -> Rect:
    """整页矩形（**阅读空间**）。"""
    width, height = space.reading_size
    return Rect(0.0, 0.0, width, height)


def remaining_rects(space: PageSpace, taken: Sequence[Rect]) -> list[Rect]:
    """页面减去已占用区域的剩余块。"""
    return subtract_regions(page_rect(space), taken)


__all__ = [
    "DetectedFigure",
    "DetectedTable",
    "HeaderFooterVerdict",
    "build_text_regions",
    "classify_header_footer",
    "detect_columns",
    "extract_lines",
    "find_figures",
    "find_tables",
    "has_large_image",
    "has_numeric_unit",
    "page_rect",
    "paragraph_text",
    "reading_chars",
    "remaining_rects",
    "repeated_key",
    "sort_in_columns",
]
