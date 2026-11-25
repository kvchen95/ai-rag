"""检索侧取文：从 PG 按切片编号取正文、标题与定位（附录 B / §2.4）。

向量库只存编号、向量与过滤字段（附录 B 末段"正文不进向量库，避免第二份真相"），
所以候选进精排前必须回 PG 取一次正文。这个模块就是那一次。

三个刻意的选择：

1. **走 `v_active_chunks` 视图**，不直连 `chunks`。候选的版本兜底（§7.4）已经
   过了，但取文这一步再走一遍视图是免费的：它保证"取到的每一行仍是双 active"，
   而 §7.4 与取文之间还隔着一个精排（几百毫秒），窗口不是零。
2. **一次查一批**，不逐条查。精排窗口最多 50 条，逐条查是 50 次往返。
3. **取不到的行不编造**：缺失的 chunk_id 从结果里消失，调用方按"这条候选没正文"
   处理（降级成不进证据包）。用空串顶替会让一条空证据进证据包，生成层引用空内容
   反而更难查。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from api.retrieval.hybrid import Candidate
from api.clients.pg import PgDatabase

SELECT_TEXTS_SQL = """
SELECT c.chunk_id,
       c.doc_key,
       c.version_id,
       c.chunk_type,
       c.page_num,
       c.bbox,
       c.content_text,
       COALESCE(NULLIF(c.table_md, ''), c.content_text) AS retrieval_text,
       d.filename
FROM v_active_chunks AS c
JOIN documents AS d ON d.doc_key = c.doc_key
WHERE c.chunk_id = ANY(%s)
"""


class ChunkText:
    """一条候选的正文与展示信息。`retrieval_text` 已按类型取好（§9.1 第 1 条）。"""

    __slots__ = ("chunk_id", "doc_key", "doc_name", "page_num", "bbox", "text", "chunk_type")

    def __init__(
        self,
        *,
        chunk_id: str,
        doc_key: str,
        doc_name: str,
        page_num: int,
        bbox: list[Any],
        text: str,
        chunk_type: str,
    ) -> None:
        self.chunk_id = chunk_id
        self.doc_key = doc_key
        self.doc_name = doc_name
        self.page_num = page_num
        self.bbox = bbox
        self.text = text
        self.chunk_type = chunk_type


def fetch_texts(pg: PgDatabase, chunk_ids: Iterable[str]) -> dict[str, ChunkText]:
    """按编号取正文。返回 {chunk_id: ChunkText}，取不到的条目直接缺席。"""
    ids = list(dict.fromkeys(cid for cid in chunk_ids if cid))
    if not ids:
        return {}
    rows = pg.fetch_all(SELECT_TEXTS_SQL, [ids])
    out: dict[str, ChunkText] = {}
    for row in rows:
        text = _text_for(row)
        if not text:
            # 空正文不进结果：它在证据包里等于一条空引用（§9.1 的编号要能对上原文）。
            continue
        out[row["chunk_id"]] = ChunkText(
            chunk_id=row["chunk_id"],
            doc_key=row["doc_key"],
            doc_name=row["filename"] or row["doc_key"],
            page_num=int(row["page_num"]),
            bbox=list(row["bbox"] or []),
            text=text,
            chunk_type=row["chunk_type"],
        )
    return out


def _text_for(row: dict[str, Any]) -> str:
    """按类型取文本：表格优先取含表题/单位的自然语言正文，其余取原文。

    图的转译结果同样落在 `content_text` 里，解析阶段写的就是它，所以这条规则在
    数据层已经体现。
    """
    if row.get("chunk_type") == "table":
        return (row.get("content_text") or row.get("retrieval_text") or "").strip()
    return (row.get("content_text") or "").strip()


def doc_names(pg: PgDatabase, doc_keys: Sequence[str] | None = None) -> dict[str, str]:
    """{doc_key: 展示名}。`doc_keys` 为空时读取全部文档。"""
    keys = list(dict.fromkeys(key for key in (doc_keys or []) if key))
    if keys:
        rows = pg.fetch_all(
            "SELECT doc_key, filename FROM documents WHERE doc_key = ANY(%s)", [keys]
        )
    else:
        rows = pg.fetch_all("SELECT doc_key, filename FROM documents")
    return {row["doc_key"]: (row["filename"] or row["doc_key"]) for row in rows}


def all_doc_names(pg: PgDatabase) -> dict[str, str]:
    """读取全部文档展示名，用于在检索前识别问题点名的资料。"""
    return doc_names(pg, None)


def mentioned_doc_keys(question: str, names: dict[str, str]) -> tuple[str, ...]:
    """从展示文件名里找问题明确点名的文档；找不到时返回空元组。"""
    tokens = [token.strip() for token in (question or "").split() if token.strip()]
    if not tokens:
        return ()
    return tuple(
        sorted(
            doc_key
            for doc_key, name in names.items()
            if name and any(token in name for token in tokens)
        )
    )


def drop_repeated_boilerplate(
    candidates: Sequence[Candidate],
    texts: dict[str, ChunkText],
    *,
    min_pages: int = 3,
) -> tuple[list[Candidate], list[str]]:
    """去掉跨页重复的短样板文本（页眉、报告标题等）。

    真实 PDF 会把同一报告标题切成很多独立 chunk；它们语义上高度相似，精排会给
    接近满分的分数，把真正的数据表挤出 top-N。这里只处理“同一规范化文本出现在
    至少 min_pages 个不同页面”的候选，不按关键词猜测，避免误删正常短段落。
    """
    pages_by_text: dict[str, set[int]] = {}
    text_by_id: dict[str, str] = {}
    for candidate in candidates:
        item = texts.get(candidate.chunk_id)
        if item is None:
            continue
        normalised = "".join((item.text or "").split())
        if not normalised:
            continue
        text_by_id[candidate.chunk_id] = normalised
        pages_by_text.setdefault(normalised, set()).add(candidate.page_num)
    repeated = {text for text, pages in pages_by_text.items() if len(pages) >= min_pages}
    kept: list[Candidate] = []
    dropped: list[str] = []
    for candidate in candidates:
        if text_by_id.get(candidate.chunk_id) in repeated:
            dropped.append(candidate.chunk_id)
        else:
            kept.append(candidate)
    return kept, dropped


def drop_header_only_candidates(
    candidates: Sequence[Candidate],
    texts: dict[str, ChunkText],
    *,
    max_chars: int = 100,
) -> tuple[list[Candidate], list[str]]:
    """去掉短的页眉/章节标题 chunk，避免它们挤占 rerank top-N。

    判据不依赖“资产负债表”等具体关键词：短、单行、没有句末标点、也不是表格的
    文本，信息量不足以回答事实问题。表格与带完整句子的短注释放例保留。
    """
    kept: list[Candidate] = []
    dropped: list[str] = []
    for candidate in candidates:
        item = texts.get(candidate.chunk_id)
        if item is None:
            kept.append(candidate)
            continue
        text = (item.text or "").strip()
        normalised = "".join(text.split())
        header_only = (
            item.chunk_type != "table"
            and len(normalised) <= max_chars
            and "\n" not in text
            and " " not in text
            and "\t" not in text
            and not any(mark in text for mark in "。！？!?；;：:|")
        )
        if header_only:
            dropped.append(candidate.chunk_id)
        else:
            kept.append(candidate)
    return kept, dropped


def dedupe_candidate_texts(
    candidates: Sequence[Candidate],
    texts: dict[str, ChunkText],
) -> tuple[list[Candidate], list[str]]:
    """同一文档/版本内，相同规范化正文只保留第一条。

    这是通用去重，不按“标题/页眉”关键词猜；重复文本不会提供新证据，
    保留第一条即可，后续条目只浪费 rerank 预算和 top-N 名额。
    """
    seen: set[tuple[str, str, str]] = set()
    kept: list[Candidate] = []
    dropped: list[str] = []
    for candidate in candidates:
        item = texts.get(candidate.chunk_id)
        if item is None:
            kept.append(candidate)
            continue
        key = (item.doc_key, item.doc_name, "".join((item.text or "").split()))
        if key in seen:
            dropped.append(candidate.chunk_id)
        else:
            seen.add(key)
            kept.append(candidate)
    return kept, dropped


__all__ = ["ChunkText", "all_doc_names", "dedupe_candidate_texts", "doc_names", "drop_header_only_candidates", "drop_repeated_boilerplate", "fetch_texts", "mentioned_doc_keys"]
