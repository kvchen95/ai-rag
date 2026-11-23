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
    """按类型取文本（§9.1 第 1 条）：正文取原文、表格取派生文本、图取转译结果。

    图的转译结果同样落在 `content_text` 里（解析阶段写的就是它），所以这条规则在
    数据层已经体现为"表格优先 `table_md`，其余取 `content_text`"。
    """
    if row.get("chunk_type") == "table":
        return (row.get("retrieval_text") or "").strip()
    return (row.get("content_text") or "").strip()


def doc_names(pg: PgDatabase, doc_keys: Sequence[str]) -> dict[str, str]:
    """{doc_key: 展示名}。澄清选项与引用列表用它，缺回退到 doc_key 本身。"""
    keys = list(dict.fromkeys(key for key in doc_keys if key))
    if not keys:
        return {}
    rows = pg.fetch_all(
        "SELECT doc_key, filename FROM documents WHERE doc_key = ANY(%s)", [keys]
    )
    return {row["doc_key"]: (row["filename"] or row["doc_key"]) for row in rows}


__all__ = ["ChunkText", "doc_names", "fetch_texts"]
