"""版本兜底校验（详细设计.md §7.4）。

前置过滤表达不了"切片所属版本是不是该文档的当前生效版本"：

- PG 里这件事是关系性的——`chunks ⋈ doc_versions` 两个 status 都必须是 active，
  `v_active_chunks` 视图已经把它写成 JOIN；
- Qdrant payload 里只有一个扁平的 `status`，它是**切片**状态，不是**版本**状态。

于是翻牌同步的那一分钟里必然存在一个窗口：库里旧版本已下线，向量库里那些点
还写着 `status=active`，前置过滤照样能召回它们（实测：对拍用例里
`ch_oldver` 就是这种行，PG 视图不返回它，Qdrant 返回）。

§7.4 的处理是不把它塞进过滤条件（放进过滤会让 Qdrant 的选择性变差、查询退化），
而是在进精排之前**逐条剔除**：拿候选切片的 `version_id` 回查该文档的当前生效
版本，不一致就丢弃。代价是一次主键查询，收益是那两分钟的漏网被挡住。

为什么不做成"过滤里加 version_id"：那需要在每次检索前先查出全部生效版本号，
一是把一次查询变成两次，二是版本集合会随语料规模增长，过滤条件跟着膨胀——
而过滤条件的长度直接吃 Qdrant 的查询计划。
"""

from __future__ import annotations

from typing import Any, Iterable

from api.clients.pg import PgDatabase
from api.observability import get_logger, log_event

logger = get_logger("api.version_guard")

# 一次查出所有相关文档的当前生效版本。`ux_active_version` 保证每 doc_key 至多一行。
ACTIVE_VERSIONS_SQL = """
SELECT d.doc_key, v.version_id
FROM doc_versions AS v
JOIN documents AS d ON d.doc_key = v.doc_key
WHERE v.status = 'active' AND v.doc_key = ANY(%s)
"""


def active_version_map(pg: PgDatabase, doc_keys: Iterable[str]) -> dict[str, str]:
    """{doc_key: 当前生效 version_id}。没有生效版本的文档不出现在结果里。"""
    keys = sorted({key for key in doc_keys if key})
    if not keys:
        return {}
    rows = pg.fetch_all(ACTIVE_VERSIONS_SQL, [keys])
    return {row["doc_key"]: row["version_id"] for row in rows}


def drop_stale(
    pg: PgDatabase, candidates: Iterable[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """丢掉版本不是当前生效的候选。返回 (保留, 丢弃)，丢弃的带原因供日志与审计。

    入参每条只需要 `chunk_id` / `doc_key` / `version_id` 三个字段。
    不做"猜"：文档查不到生效版本时，它的候选**全部丢弃**——Fail-Closed。
    少召回会落进门控的低置信，多召回会把已废止条款送进答案，两者代价不对等。
    """
    items = list(candidates)
    versions = active_version_map(pg, (item.get("doc_key", "") for item in items))
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for item in items:
        doc_key = item.get("doc_key", "")
        expected = versions.get(doc_key)
        if expected is not None and item.get("version_id") == expected:
            kept.append(item)
        else:
            dropped.append(
                {
                    "chunk_id": item.get("chunk_id"),
                    "doc_key": doc_key,
                    "version_id": item.get("version_id"),
                    "expected_version_id": expected,
                }
            )
    if dropped:
        log_event(
            logger,
            "version_guard_dropped",
            level=30,
            count=len(dropped),
            chunks=[item["chunk_id"] for item in dropped][:20],
        )
    return kept, dropped
