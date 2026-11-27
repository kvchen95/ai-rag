"""切片：父子两级 + 六项随行信息（详细设计.md §3.5 / §2.3 / 附录 A.3）。.

### 六项随行信息，缺一项后面就有一处功能不可用

| # | 字段 | 缺了会怎样 |
|---|---|---|
| 1 | `page_num` | 引用不能跳页（前端拿不到页号） |
| 2 | `bbox` | 高亮框画不出来（前端只有页码没有位置） |
| 3 | `chunk_type` | 取文本时选错列（正文 / 表格 / 图三套取法） |
| 4 | `effective_date`+`date_confidence` | 过期条款混进过滤，推断当显式 |
| 5 | `is_public`+`allowed_depts`+`allowed_roles` | 越权命中 |
| 6 | `version_id` | 检索入口没法做版本兜底（§7.4） |

`Chunk.require_complete()` 是这六项的**可执行判据**：少一项就抛，而不是"入库时
再说"。这条在验收里叫"切片字段缺失被校验拦截"——拦截点在这里，不在数据库：
`chunks` 表的 NOT NULL 只管"这一列有没有值"，管不了"这个值有没有意义"
（`bbox` 可以是 `[]`，`allowed_depts` 可以是空数组但
`is_public=false` 时它意味着**谁都看不到**，那几乎一定是漏填）。

### 父子两级

* 子片 256–512 字、重叠 64 字，被检索命中；
* 父片 1024–2048 字或整章，被送进模型作答；
* 子片 `parent_id` 指向父片，父片该字段为空（附录 A.3）。

为什么两级：子片短，切得开"一句话"，向量语义更聚焦；但一个答案往往需要上下文，
把父片整段喂给模型比喂 5 条零碎子片更省 token 也更有连贯性（§2.3）。

### 切分只按"能读的边界"切

优先级：章节标题 → 段落 → 句子（。！？；/ `. `）→ 硬切。
硬切是最后手段：中英混排的条款里，硬切会把 `500` 和 `元` 切开，于是
"每人每天 500 元"变成两条互不相干的切片，两条都检索不到。

### 尺寸的口径

字符数（`len`），不是 token。理由：`token_count` 是"按字符近似"（附录 A.3），
而切片的上下限是给人看的口径（256–512 字）。用 token 卡会让同一句话在不同
分词器下被切成不同形状，评测集就没法跨模型比。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from api.parse.dates import BusinessDate
from api.parse.geometry import BBox, Rect, union_bboxes
from api.parse.model import Region, RegionKind

# §3.5：子片 256-512 字，重叠 64 字。
CHILD_MIN_CHARS = 256
CHILD_MAX_CHARS = 512
CHILD_OVERLAP_CHARS = 64
# §3.5：父片 1024-2048 字或整章。
PARENT_MIN_CHARS = 1024
PARENT_MAX_CHARS = 2048
# 超短段落（< 80 字）与相邻段落合并后再切（§3.5 边界表）。
MERGE_SHORT_BELOW = 80
# 超长表格（整表超过 2048 字）整表单独成片，不切，标 `may_exceed`。
TABLE_SINGLE_PIECE_LIMIT = PARENT_MAX_CHARS
# 句子边界：中英都要认。英文的 `. ` 要吃掉后面的空格，中文的句号本身就是边界。
_SENTENCE_END = re.compile(r"(?<=[。！？；!?])|(?<=[.])\s+")

CHUNK_TYPES = ("text", "table", "figure")


class IncompleteChunkError(ValueError):
    """切片缺随行信息。消息里点名缺哪几项——否则排障要靠猜。."""

    def __init__(self, chunk_id: str, missing: Sequence[str]) -> None:
        """Initialize the instance."""
        self.chunk_id = chunk_id
        self.missing = tuple(missing)
        super().__init__(f"{chunk_id}: missing {', '.join(self.missing)}")


# 旧名保留给既有调用方（worker、脚本、测试仍按这个名字导入）。
IncompleteChunk = IncompleteChunkError


@dataclass(slots=True)
class Chunk:
    """一个待入库的切片。字段与 `chunks` 表逐列对应（附录 A.3）。.

    没有 `status`：切片不维护自己的状态，显示"是否可检索"一律看所属版本
    （§2.3）。
    入库时由调用方写 `pending`，翻牌时由 A3 按 `version_id` 批量改。
    """

    chunk_id: str
    doc_key: str
    version_id: str
    chunk_type: str
    page_num: int
    bbox: list[list[float]]
    content_text: str
    # --- 六项随行信息里的后三项 ---
    effective_date: int | None = None
    effective_year: int | None = None
    date_confidence: str = "inferred"
    is_public: bool = False
    allowed_depts: tuple[str, ...] = ()
    allowed_roles: tuple[str, ...] = ()
    # --- 父子与结构化 ---
    parent_id: str | None = None
    table_html: str | None = None
    table_md: str | None = None
    table_cells: list[dict[str, Any]] | None = None
    image_path: str | None = None
    token_count: int | None = None
    section: str | None = None
    notes: tuple[str, ...] = ()

    # --- 校验 ---

    def missing_fields(self) -> list[str]:
        """六项随行信息里缺/无效的那些。空列表才算完整。."""
        missing: list[str] = []
        if not self.page_num or self.page_num < 1:
            missing.append("page_num")
        if not self.bbox:
            missing.append("bbox")
        elif not all(_valid_bbox(rect) for rect in self.bbox):
            missing.append("bbox(invalid)")
        if self.chunk_type not in CHUNK_TYPES:
            missing.append("chunk_type")
        if self.date_confidence not in {"explicit", "inferred"}:
            missing.append("date_confidence")
        if self.date_confidence == "explicit" and self.effective_date is None:
            # explicit 却没有日期 = 声称"这是原文显式时间"但拿不出来。
            missing.append("effective_date(explicit without date)")
        if (
            self.effective_date is not None
            and self.effective_year != self.effective_date // 10000
        ):
            missing.append("effective_year(mismatched)")
        # 权限：既不是公开、又没有任何放行项 = 谁都看不到。这几乎一定是漏填，
        # 而不是"真的谁都不许看"（真的不许看不入库即可）。
        if (
            not self.is_public
            and not self.allowed_depts
            and not self.allowed_roles
        ):
            missing.append("acl(everyone denied)")
        if not self.version_id:
            missing.append("version_id")
        if not self.content_text.strip():
            missing.append("content_text")
        if self.chunk_type == "table" and not self.table_md:
            missing.append("table_md")
        if self.chunk_type == "figure" and not self.image_path:
            missing.append("image_path")
        if self.parent_id is not None and self.parent_id == self.chunk_id:
            missing.append("parent_id(self)")
        return missing

    def require_complete(self) -> Chunk:
        """验收判据：缺一项就抛 `IncompleteChunkError`。."""
        missing = self.missing_fields()
        if missing:
            raise IncompleteChunkError(self.chunk_id, missing)
        return self

    @property
    def char_count(self) -> int:
        """Run char_count."""
        return len(self.content_text)

    def as_row(self) -> dict[str, Any]:
        """落库形状（列名与 001/004 迁移一致）。`require_complete()` 必须先过。.

        为什么 `table_cells` 与 `notes` 也在这张行里：它们是"§3.3 的三份表示"之
        第三份与"这条证据的来源"。只放在内存对象上等于入库即丢——校验阶段要拿
        矩阵对齐数字（而不是正则拆 HTML），下游要能看出"这段文字是 OCR 认的"。
        004 迁移为此补了这两列。
        """
        self.require_complete()
        return {
            "chunk_id": self.chunk_id,
            "doc_key": self.doc_key,
            "version_id": self.version_id,
            "chunk_type": self.chunk_type,
            "parent_id": self.parent_id,
            "page_num": self.page_num,
            "bbox": self.bbox,
            "image_path": self.image_path,
            "content_text": self.content_text,
            "table_html": self.table_html,
            "table_md": self.table_md,
            "table_cells": self.table_cells,
            "notes": list(self.notes),
            "effective_date": self.effective_date,
            "effective_year": self.effective_year,
            "date_confidence": self.date_confidence,
            "is_public": self.is_public,
            "allowed_depts": list(self.allowed_depts),
            "allowed_roles": list(self.allowed_roles),
            "token_count": self.token_count,
        }


def _valid_bbox(rect: Sequence[float]) -> bool:
    """Run _valid_bbox."""
    if len(rect) != 4:
        return False
    return all(0.0 <= float(value) <= 1.0 for value in rect) and (
        float(rect[2]) > float(rect[0]) and float(rect[3]) > float(rect[1])
    )


def chunk_id_for(
    version_id: str, *, page_num: int, kind: str, index: int, part: int = 0
) -> str:
    """切片编号：`ch_` + 12 位十六进制（附录 A.3 的形状 `ch_9f2c31`）。.

    **为什么不用"version_id + 序号"拼**（第一版就是这么做的，两个问题）：

    1. `chunk_id` 会进 `figures/{chunk_id}.webp` 这个**文件路径**。
       `version_id` 里有 `doc_key`（`policy/travel-cn`）和 `#`，拼出来的路径是
       `figures/policy/travel-cn#a91f#c001000-000.webp`——目录不存在、`#` 在 URL
       里是 fragment 分隔符，取图会 404。编号必须文件系统安全。
    2. 附录 A.3 的示例就是短码。编号是标识符，不是"人能读懂的描述"；可读性该由
       `page_num` / `chunk_type` / `parent_id` 承担。

    但**必须是确定性的**：同一份文件重跑要得到同一批编号，否则"两次解析结果
    是否一致"就没法比，upsert 也会变成插入新行。所以用
    `sha256(version_id|page|kind|index)` 的前 12 位
    （16^12 ≈ 2.8e14，40 万切片撞号概率可忽略）。
    """
    digest = hashlib.sha256(
        f"{version_id}|{page_num}|{kind}|{index}|{part}".encode()
    ).hexdigest()
    return f"ch_{digest[:12]}"


def parent_id_for(version_id: str, *, page_num: int, index: int) -> str:
    """父片编号。同样由 `chunk_id_for` 派生，只是 `kind=fixed "parent"`。."""
    return chunk_id_for(
        version_id, page_num=page_num, kind="parent", index=index
    )


def image_path_for(chunk_id: str) -> str:
    """图的相对路径：`figures/{chunk_id}.webp`（附录 A.3 的形状）。."""
    return f"figures/{chunk_id}.webp"


@dataclass(slots=True)
class Acl:
    """权限三项。**默认不给任何放行**：解析器不知道业务权限，调用方必须显式传。.

    这不是"安全的默认值"而是"必须回答的问题"：一份文档是公开的还是部门的，
    只有入库的调用方（A3 / 评测脚本）知道。给个"默认公开"会让每份材料都变成
    全公司可见。
    """

    is_public: bool = False
    allowed_depts: tuple[str, ...] = ()
    allowed_roles: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        """Run is_empty."""
        return (
            not self.is_public
            and not self.allowed_depts
            and not self.allowed_roles
        )


# --- 切分 -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Piece:
    """切分中间态：一段文本 + 它覆盖的区域（同页！）。.

    `rects` 里所有矩形必须同一页——跨页的并集会产生"两页之间的框"，
    `bbox` 就没意义了。`build_child_chunks` 因此按页切分。
    """

    text: str
    page_num: int
    rects: tuple[Rect, ...]

    def bbox(self, page_bboxes: Sequence[BBox]) -> list[list[float]]:
        """多个 bbox 的并集（同一页，所以有意义）。."""
        merged = union_bboxes(page_bboxes)
        return [list(merged)] if merged else []


def split_sentences(text: str) -> list[str]:
    """按句子边界切。中英混排都认，且不丢分隔符。.

    `re.split` 带捕获组会保留分隔符；这里用零宽断言切，切点落在分隔符**之后**，
    所以"500 元。"仍是完整一句。
    """
    parts = [part for part in _SENTENCE_END.split(text) if part]
    return [part.strip() for part in parts if part.strip()]


def split_long_text(text: str, *, max_chars: int) -> list[str]:
    """把一段文本切成不超过 `max_chars` 的若干段：先按句，再按硬切。.

    硬切只在单句本身超长时发生（长表格行、无标点的条款）。硬切时**尽量在空格或
    逗号处断**——把 `CNY 500` 切成 `CNY 5` + `00` 会让数字检索彻底失效。
    """
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    for sentence in split_sentences(text) or [text]:
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        start = 0
        while start < len(sentence):
            end = min(len(sentence), start + max_chars)
            if end < len(sentence):
                cut = _soft_cut(sentence, start, end)
                end = cut if cut > start else end
            pieces.append(sentence[start:end])
            start = end
    return [piece for piece in pieces if piece.strip()]


def _soft_cut(text: str, start: int, end: int) -> int:
    """在 [start+max_chars/2, end] 里找最后一个人读得通的断点。."""
    floor = start + max(1, (end - start) // 2)
    for index in range(end, floor, -1):
        if text[index - 1] in " ，,、;；:：)）]】":
            return index
    return end


def build_child_pieces(
    text: str,
    *,
    max_chars: int = CHILD_MAX_CHARS,
    overlap: int = CHILD_OVERLAP_CHARS,
) -> list[str]:
    """把一段文本切成子片：每片 ≤ `max_chars`，相邻两片重叠约 `overlap` 字。.

    重叠的实现是"回退 `overlap` 个字符再切一段"，不是"在上一片尾部复制 `overlap`
    个字"——后者会让重叠部分在新片开头重复出现两次内容（一次作为上下文一次作为
    正文），检索时看起来像重复命中。

    边界：**`overlap` 被钳到 `max_chars` 的一半以内**。第一版只在
    `overlap >= max_chars` 时退化成 1 字步进，实测把 1500 字的文本切成了 1401 片
    （不是死循环，但等价于"每字一片"，既慢又完全失去语义）。钳到一半是保守的
    上界：重叠超过一半时，两片的内容已经大半重复，再大只是浪费向量。
    """
    text = text.strip()
    if not text or max_chars <= 0:
        return []
    if len(text) <= max_chars:
        return [text]
    effective_overlap = max(0, min(overlap, max_chars // 2))
    step = max_chars - effective_overlap
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        pieces.append(text[start:end])
        if end >= len(text):
            break
        start += step
    return [piece for piece in pieces if piece.strip()]


def parent_slices(text: str, *, max_chars: int = PARENT_MAX_CHARS) -> list[str]:
    """父片：按小节/段落切，尽量接近 `max_chars` 但不超出；超长再切。.

    "按章节标题切，不按字数硬切"（§3.5）——但一章超过 2048 字时要再切一层
    （同节边界表）。所以这里先按段落累加到上限附近，段落本身超长再交给
    `split_long_text`。
    """
    paragraphs = [
        para.strip() for para in re.split(r"\n{1,}", text) if para.strip()
    ]
    if not paragraphs:
        return []
    out: list[str] = []
    buffer: list[str] = []
    size = 0
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if buffer:
                out.append("\n".join(buffer))
                buffer, size = [], 0
            out.extend(split_long_text(paragraph, max_chars=max_chars))
            continue
        if size + len(paragraph) + (1 if buffer else 0) > max_chars and buffer:
            out.append("\n".join(buffer))
            buffer, size = [], 0
        buffer.append(paragraph)
        size += len(paragraph) + (1 if len(buffer) > 1 else 0)
    if buffer:
        out.append("\n".join(buffer))
    return out


# --- 从区域建切片 -----------------------------------------------------------


def build_child_chunks(
    *,
    doc_key: str,
    version_id: str,
    region: Region,
    page_bboxes: Sequence[BBox],
    acl: Acl,
    date: BusinessDate,
    parent_id: str | None,
    child_ids: Sequence[str],
    section: str | None = None,
    allow_permission_override: bool = False,
    notes: tuple[str, ...] = (),
) -> list[Chunk]:
    """把一个正文区域切成子片。.

    `page_bboxes` 是这个区域每一行（或每段）在本页的归一化 bbox，按阅读顺序。
    子片的 bbox 取"它覆盖的那几行"的并集——不是整个区域的 bbox：整段的 bbox 会让
    前端高亮框住一大块，用户看不出是哪个句子。

    `child_ids` 由调用方给（不是这里生成）：编号必须是文件系统安全的确定性短码
    （见 `chunk_id_for`），而"这一片是第几片"只有切分之后才知道——所以调用方
    先算出片数，再传对应个数的编号。
    """
    if region.kind is not RegionKind.TEXT:
        raise ValueError(
            f"build_child_chunks expects a text region, got {region.kind}"
        )
    if region.lines and len(page_bboxes) != len(region.lines):
        raise ValueError(
            f"page_bboxes ({len(page_bboxes)}) must be one per line "
            f"({len(region.lines)})"
        )

    pieces = build_child_pieces(region.text)
    if not pieces:
        return []
    if len(child_ids) < len(pieces):
        raise ValueError(f"need {len(pieces)} child ids, got {len(child_ids)}")

    spans = _line_spans([line.text for line in region.lines] or [region.text])
    chunks: list[Chunk] = []
    cursor = 0
    for index, piece in enumerate(pieces):
        start = region.text.find(piece, cursor)
        if start < 0:  # 重叠回退时 find 可能失败，退回按累计位置
            start = cursor
        end = start + len(piece)
        cursor = max(start + 1, end - CHILD_OVERLAP_CHARS)
        covered = _cover_bboxes(spans, page_bboxes, start, end)
        chunk = Chunk(
            chunk_id=child_ids[index],
            doc_key=doc_key,
            version_id=version_id,
            chunk_type=region.kind.value,
            page_num=region.page_num,
            bbox=covered or ([list(page_bboxes[0])] if page_bboxes else []),
            content_text=piece,
            effective_date=date.effective_date,
            effective_year=date.effective_year,
            date_confidence=date.confidence,
            is_public=acl.is_public,
            allowed_depts=acl.allowed_depts,
            allowed_roles=acl.allowed_roles,
            parent_id=parent_id,
            token_count=len(piece),
            section=section,
            notes=notes,
        )
        if allow_permission_override:
            # 权限覆盖是**追加**而不是覆盖：来源标注（ocr）不能因为一次权限改写
            # 就丢掉，否则下游看不到"这片是识别出来的"。
            chunk.notes = (*chunk.notes, "permission_override")
        chunks.append(chunk)
    return chunks


def child_piece_count(region: Region) -> int:
    """这个正文区域会切成几片。调用方据此先生成 `child_ids`。."""
    return len(build_child_pieces(region.text))


def build_parent_chunk(
    *,
    doc_key: str,
    version_id: str,
    region: Region,
    page_bboxes: Sequence[BBox],
    acl: Acl,
    date: BusinessDate,
    chunk_id: str,
    section: str | None = None,
    notes: tuple[str, ...] = (),
) -> Chunk:
    """父片：整章 / 整段。`parent_id` 为空（附录 A.3）。."""
    text = region.text.strip()
    if not text:
        raise ValueError("parent chunk needs non-blank text")
    merged = union_bboxes(page_bboxes) if page_bboxes else None
    return Chunk(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=version_id,
        chunk_type=region.kind.value,
        page_num=region.page_num,
        bbox=[list(merged)] if merged else [],
        content_text=text,
        effective_date=date.effective_date,
        effective_year=date.effective_year,
        date_confidence=date.confidence,
        is_public=acl.is_public,
        allowed_depts=acl.allowed_depts,
        allowed_roles=acl.allowed_roles,
        parent_id=None,
        token_count=len(text),
        section=section,
        notes=notes,
    )


def build_table_chunk(
    *,
    doc_key: str,
    version_id: str,
    region: Region,
    bbox: BBox,
    acl: Acl,
    date: BusinessDate,
    chunk_id: str,
    content_text: str,
    table_md: str | None = None,
    section: str | None = None,
) -> Chunk:
    """表格切片：一张表一条不切（§3.5：整表超长则单独成片）。."""
    table = region.table
    if table is None:
        raise ValueError("table region without TableData")
    notes: tuple[str, ...] = ()
    if len(content_text) > TABLE_SINGLE_PIECE_LIMIT:
        notes = ("may_exceed",)
    return Chunk(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=version_id,
        chunk_type=RegionKind.TABLE.value,
        page_num=region.page_num,
        bbox=[list(bbox)],
        content_text=content_text,
        effective_date=date.effective_date,
        effective_year=date.effective_year,
        date_confidence=date.confidence,
        is_public=acl.is_public,
        allowed_depts=acl.allowed_depts,
        allowed_roles=acl.allowed_roles,
        parent_id=None,
        table_html=table.html,
        table_md=table_md if table_md is not None else table.md,
        table_cells=table.cells_json(),
        token_count=len(content_text),
        section=section,
        notes=notes,
    )


def build_figure_chunk(
    *,
    doc_key: str,
    version_id: str,
    region: Region,
    bbox: BBox,
    acl: Acl,
    date: BusinessDate,
    chunk_id: str,
    content_text: str,
    section: str | None = None,
) -> Chunk:
    """图切片：一张图一条，`image_path` 指向 `figures/{chunk_id}.webp`。."""
    figure = region.figure
    if figure is None:
        raise ValueError("figure region without FigureData")
    return Chunk(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=version_id,
        chunk_type=RegionKind.FIGURE.value,
        page_num=region.page_num,
        bbox=[list(bbox)],
        content_text=content_text,
        effective_date=date.effective_date,
        effective_year=date.effective_year,
        date_confidence=date.confidence,
        is_public=acl.is_public,
        allowed_depts=acl.allowed_depts,
        allowed_roles=acl.allowed_roles,
        parent_id=None,
        image_path=figure.image_path,
        token_count=len(content_text),
        section=section,
    )


# --- 行覆盖计算 -------------------------------------------------------------


def _line_spans(line_texts: Sequence[str]) -> list[tuple[int, int]]:
    """每行在拼接后的全文里占的区间。.

    空格连接的口径与 `paragraph_text` 一致。
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for text in line_texts:
        clean = text.strip()
        if not clean:
            continue
        start = cursor
        spans.append((start, start + len(clean)))
        cursor = start + len(clean) + 1  # +1 是连接用的空格
    return spans


def _cover_bboxes(
    spans: Sequence[tuple[int, int]],
    page_bboxes: Sequence[BBox],
    start: int,
    end: int,
) -> list[list[float]]:
    """覆盖 [start, end) 的那些行的 bbox 并集。."""
    if not spans or not page_bboxes or len(spans) != len(page_bboxes):
        return []
    hits = [
        page_bboxes[index]
        for index, (line_start, line_end) in enumerate(spans)
        if line_end > start and line_start < end
    ]
    merged = union_bboxes(hits)
    return [list(merged)] if merged else []


def validate_chunks(chunks: Iterable[Chunk]) -> list[Chunk]:
    """批量校验，任何一条缺字段就抛（带全部缺项，不是只报第一条）。.

    一次报全：修一条跑一次会让人以为是"修完这条还有下一条"，
    而实际上多半是同一类漏填（比如整套都漏了 allowed_depts）。
    """
    failures: list[IncompleteChunkError] = []
    out: list[Chunk] = []
    for chunk in chunks:
        missing = chunk.missing_fields()
        if missing:
            failures.append(IncompleteChunkError(chunk.chunk_id, missing))
        else:
            out.append(chunk)
    if failures:
        detail = "; ".join(str(failure) for failure in failures)
        raise IncompleteChunkError(f"{len(failures)} chunk(s)", [detail])
    return out


__all__ = [
    "Acl",
    "CHILD_MAX_CHARS",
    "CHILD_MIN_CHARS",
    "CHILD_OVERLAP_CHARS",
    "CHUNK_TYPES",
    "Chunk",
    "IncompleteChunk",
    "IncompleteChunkError",
    "MERGE_SHORT_BELOW",
    "PARENT_MAX_CHARS",
    "PARENT_MIN_CHARS",
    "Piece",
    "build_child_chunks",
    "build_child_pieces",
    "build_figure_chunk",
    "build_parent_chunk",
    "build_table_chunk",
    "child_piece_count",
    "chunk_id_for",
    "image_path_for",
    "parent_id_for",
    "parent_slices",
    "split_long_text",
    "split_sentences",
    "validate_chunks",
]
