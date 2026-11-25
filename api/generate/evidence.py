"""证据包组装（详细设计.md §9.1）。

证据包不是表，是这次问答临时组装的数组，用完即弃（§2.4）。它只有一个消费者：
提示词。所以这里的每一条规则都能翻译成"模型会看到什么"，而不是"库里存什么"。

五条规则按 §9.1 的顺序执行，顺序不能换：

1. **按类型取文本**：正文取原文、表格取派生文本、图取转译结果。这件事在取文那一层
   已经做了（`api/retrieval/texts.py` 的 `_text_for`），这里只负责不破坏它——
   证据包里不许再出现 `table_html`。理由在 §9.1 末段：标签占 token，还会让模型把
   属性名当内容。
2. **同一父片的多条子片合并成一条证据**，页码取子片里最靠前的一页。不合并的话，
   五行证据其实是同一段话，模型会重复引用，而角标会变成 5 个指向同一处的编号。
3. **总长度封顶 6000 字**，超了按精排分从低往高丢，**不截断单条证据**。
4. **编号从 1 开始、按分数降序**；用户看到的角标就是这个顺序。
5. **每条证据带一行来源标记**（标题、页码、切片编号），供模型抄引用。

两条实现细节是被"合并"这条规则逼出来的：

- **合并要去重叠**。子片之间本来有 64 字重叠（§2.3），直接拼接会让同一句出现两次。
  拼完不删重叠的后果不是"多几个字"，而是第 3 条的长度预算被重复内容吃掉，
  于是同一段话能挤掉的真实证据更少。
- **代表性切片单独保留**。合并后的证据有多个 `chunk_id`，但角标只能指向一个：
  取命中它的那条子片（分数最高的那条），`bbox` 与 `snippet` 也跟着它——否则
  高亮框会落在"最早的一页"，而用户点开时看到的是另一段话。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from api.retrieval.hybrid import Candidate
from api.retrieval.texts import ChunkText

# 相邻子片的已知重叠是 64 字（§2.3）。这里允许到 128，是为了容忍解析侧调整重叠
# 参数后旧数据仍在库里的过渡期；再大就不是重叠，而是两条独立证据了。
_MAX_OVERLAP = 128
# 重叠判定的下限：短于 8 字的重合多半是"的""应当"这类巧合，删掉会破坏句子。
_MIN_OVERLAP = 8
# 引用片段长度：前端在 bbox 缺失时只展示页码 + 片段，一行能放下就够。
SNIPPET_CHARS = 120
# 来源标记占的长度上限。它要进证据包，所以必须算进 6000 字预算——
# 不算的话"封顶"就是假的（8 条 × 一行来源 ≈ 300 字）。
SOURCE_LINE_CHARS = 80


@dataclass(frozen=True, slots=True)
class Evidence:
    """一条证据。编号是**本次问答内**的局部编号，不是切片编号（§2.4）。"""

    n: int
    # 代表性切片：角标指向它，bbox 与 snippet 也来自它。
    chunk_id: str
    # 合并进来的全部子片（同父片）。单条证据时它就是 (chunk_id,)。
    chunk_ids: tuple[str, ...]
    doc_key: str
    doc_name: str
    page_num: int
    bbox: list[Any]
    text: str
    chunk_type: str
    rerank_score: float | None = None
    fused_rank: int = 0
    dense_rank: int | None = None
    sparse_rank: int | None = None

    @property
    def snippet(self) -> str:
        """引用片段：单条证据的第一句。"""
        return _clip(self.text, SNIPPET_CHARS)

    def source_line(self) -> str:
        """§9.1 第 5 条：一行来源标记，供模型引用时抄。"""
        return _clip(f"《{self.doc_name}》第 {self.page_num} 页 · {self.chunk_id}", SOURCE_LINE_CHARS)

    def as_prompt_block(self) -> str:
        """提示词里的一段：编号 + 来源 + 正文。

        编号写成 `[n]` 而不是「第 n 条」：提示词要求"句末标引用编号 [n]"，而模型最
        常犯的格式错就是抄成「第1条」。让证据里出现的形式与要求输出的形式一致，
        能少一类可避免的失败。
        """
        return f"[{self.n}] 来源：{self.source_line()}\n{self.text}"

    def as_citation(self) -> dict[str, Any]:
        """附录 C.2 的 `citations.items[]`：n / doc_key / doc_name / page_num / bbox / snippet / chunk_id。"""
        return {
            "n": self.n,
            "doc_key": self.doc_key,
            "doc_name": self.doc_name,
            "page_num": self.page_num,
            "bbox": [list(item) if isinstance(item, (list, tuple)) else item for item in self.bbox],
            "snippet": self.snippet,
            "chunk_id": self.chunk_id,
        }


@dataclass(frozen=True, slots=True)
class EvidencePack:
    """一次问答的取材（§2.4 的第四个对象）。"""

    items: tuple[Evidence, ...]
    # 因为超长被丢掉的切片编号（按精排分从低到高丢）。
    dropped: tuple[str, ...] = ()
    max_chars: int = 6000
    # 装进提示词的字符数（含来源标记）。
    used_chars: int = 0
    # 是否因为封顶丢了证据。§9.3 要求这时候在答案里说明"仅依据前 N 条"。
    truncated: bool = False

    def __bool__(self) -> bool:
        return bool(self.items)

    @property
    def n_items(self) -> int:
        return len(self.items)

    def by_number(self) -> dict[int, Evidence]:
        return {item.n: item for item in self.items}

    def numbers(self) -> tuple[int, ...]:
        return tuple(item.n for item in self.items)

    def chunk_ids(self) -> tuple[str, ...]:
        """本次问答用到的全部切片编号（含合并进来的子片），进审计用。"""
        return tuple(cid for item in self.items for cid in item.chunk_ids)

    def citation_items(self) -> list[dict[str, Any]]:
        return [item.as_citation() for item in self.items]

    def notice(self) -> str | None:
        """给用户的裁量说明（§9.3"证据超长被截 → 在答案里说明仅依据前 N 条"）。

        返回 None 表示没截，不该在答案里多说一句——每句话都带一句免责声明，
        读起来就像系统在不停找借口。
        """
        if not self.truncated:
            return None
        return f"（证据超出长度上限，本次仅依据前 {self.n_items} 条证据作答）"

    def render(self) -> str:
        """提示词里的【证据包】整段。"""
        return "\n\n".join(item.as_prompt_block() for item in self.items)

    def as_audit(self) -> dict[str, Any]:
        """审计口径：编号 + 切片编号 + 分数，**不含正文**（§11.2）。"""
        return {
            "items": [
                {
                    "n": item.n,
                    "chunk_id": item.chunk_id,
                    "chunk_ids": list(item.chunk_ids),
                    "doc_key": item.doc_key,
                    "page_num": item.page_num,
                    "rerank_score": None if item.rerank_score is None else round(item.rerank_score, 6),
                }
                for item in self.items
            ],
            "dropped": list(self.dropped),
            "used_chars": self.used_chars,
            "max_chars": self.max_chars,
            "truncated": self.truncated,
        }


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _overlap_length(tail: str, head: str) -> int:
    """`tail` 的后缀与 `head` 的前缀的最长重合长度（限制在 [_MIN, _MAX] 内）。"""
    limit = min(len(tail), len(head), _MAX_OVERLAP)
    for size in range(limit, _MIN_OVERLAP - 1, -1):
        if tail[-size:] == head[:size]:
            return size
    return 0


def _group_by_parent(candidates: Sequence[Candidate]) -> list[list[Candidate]]:
    """把同父片的子片归到一组，组的顺序 = 组内最高分那条的名次（§9.1 第 4 条）。

    没有 `parent_id` 的候选（父片本身、或解析时未切父子的文本）各自成组：
    把没有父片的候选也归到一起，等于把互不相干的段落拼成一条证据。
    """
    groups: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for candidate in candidates:
        # 父片本身（parent_id 为空）与它的子片必须落进同一组：父片的编号
        # 就是子片的 parent_id。把父片放进 `__self__:chunk_id` 会把它和子片拆开，
        # 导致证据包里父段与子段重复出现，角标也对不上。
        key = candidate.parent_id or candidate.chunk_id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(candidate)
    return [groups[key] for key in order]


def _merge_texts(texts: Sequence[str]) -> str:
    """按顺序拼接并删掉相邻重叠。空文本已被上游过滤，这里不处理空串。"""
    merged = texts[0]
    for text in texts[1:]:
        if not text:
            continue
        overlap = _overlap_length(merged, text)
        merged = merged + text[overlap:]
    return merged


def build_evidence_pack(
    candidates: Sequence[Candidate],
    texts: dict[str, ChunkText],
    *,
    max_chars: int,
    cap: int | None = None,
) -> EvidencePack:
    """按 §9.1 的五条规则组装证据包。

    `candidates` 必须已按精排分降序（`gate.decide()` 给出的 `evidence` 就是这个顺序）。
    `cap` 是进证据包的条数上限（§7.5"进证据包 5–8"），由调用方给；
    不给就只受 6000 字约束——那是"没配上限"，不是"不限"。
    """
    if max_chars <= 0:
        raise ValueError(f"max_chars 必须 > 0，实际 {max_chars}")

    # 取不到正文的候选直接跳过：空证据在提示词里等于一条什么都支持不了的引用，
    # 而它还会占掉一个编号，让角标指向空白。
    usable = [c for c in candidates if c.chunk_id in texts]
    if cap is not None:
        if cap < 0:
            raise ValueError(f"cap 必须 >= 0，实际 {cap}")
        usable = usable[:cap]

    merged: list[tuple[Candidate, tuple[str, ...], str, ChunkText]] = []
    for group in _group_by_parent(usable):
        representative = group[0]
        ordered = sorted(group, key=lambda c: c.page_num)
        # 组内按页码升序拼接：§9.1 第 2 条要求页码取"子片里最靠前的一页"，
        # 文本顺序也按页码排，否则合并出来的段落读起来是倒的。
        members = tuple(c.chunk_id for c in ordered)
        body = _merge_texts([texts[c.chunk_id].text for c in ordered])
        if not body.strip():
            continue
        primary = min(
            (texts[c.chunk_id] for c in ordered),
            key=lambda item: (item.page_num, item.doc_name),
        )
        merged.append((representative, members, body, primary))

    # --- 第 3 条：封顶 6000 字，按精排分从低往高丢，不截断单条 ---
    #
    # 长度必须把来源标记算进去：它真的会进提示词，不算就等于"封顶 6000 实际发 6300"。
    # 第一条无论如何都留下——把它也丢掉会让一次本来有证据的问答变成"无证据"，
    # 而真相是"证据太长"。这时置 `truncated`，让答案按 §9.3 说明只依据了前 N 条。
    kept: list[tuple[Candidate, tuple[str, ...], str, ChunkText]] = []
    dropped: list[str] = []
    used = 0
    for index, entry in enumerate(merged):
        representative, members, body, primary = entry
        block_chars = len(body) + SOURCE_LINE_CHARS + 8  # 8 = 编号与换行的余量
        if kept and used + block_chars > max_chars:
            dropped.extend(members)
            continue
        if not kept and block_chars > max_chars:
            # 单条就超了：留（不截断单条证据是硬规则），但记账。
            kept.append(entry)
            used += block_chars
            continue
        kept.append(entry)
        used += block_chars

    items = tuple(
        Evidence(
            n=position + 1,
            chunk_id=representative.chunk_id,
            chunk_ids=members,
            doc_key=primary.doc_key,
            doc_name=primary.doc_name,
            page_num=primary.page_num,
            bbox=list(primary.bbox or []),
            text=body,
            chunk_type=primary.chunk_type,
            rerank_score=representative.rerank_score,
            fused_rank=representative.fused_rank,
            dense_rank=representative.dense_rank,
            sparse_rank=representative.sparse_rank,
        )
        for position, (representative, members, body, primary) in enumerate(kept)
    )
    return EvidencePack(
        items=items,
        dropped=tuple(dict.fromkeys(dropped)),
        max_chars=max_chars,
        used_chars=used,
        # 单条就超预算也算"截过"：此时 drooped 为空但实际只给了 1 条，
        # 而 §9.3 要求的说明是"仅依据前 N 条"——N=1 也是 N。
        truncated=bool(dropped) or used > max_chars,
    )


__all__ = [
    "Evidence",
    "EvidencePack",
    "SNIPPET_CHARS",
    "build_evidence_pack",
]
