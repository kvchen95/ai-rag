"""切片仓储：把过滤条件落到 PG 上。.

这里只有读的一条查询，作用是**给 Qdrant 那条路做对照**。系统真正的检索走
Qdrant（§7.1），但"过滤条件在两边语义一致"这件事必须能被证明，而证明的办法
不是读代码，是拿同一份数据、同一份条件，在两边各跑一次再比结果。

所以 `select_ids` 的存在有两个理由：
1. 对拍（`api/tests/test_filter_parity.py`）：两边返回的 chunk_id 集合必须相同。
2. 兜底重建与对账脚本按同一口径取数，避免各自写一遍 WHERE 写歪（§4.2）。

取数一律走 `v_active_chunks` 视图（version 与 chunk 双 active），
再叠加 ACL 与时间条件。视图已经保证了版本口径，所以这里不再重复写状态判断——
两处都写迟早会分叉。
"""

from __future__ import annotations

from typing import Any

from api.clients.pg import PgDatabase
from api.filters import PgSource, RetrievalFilter

SELECT_IDS_SQL = """
SELECT chunk_id
FROM {source} AS {alias}
WHERE {where}
ORDER BY chunk_id
"""


def select_ids(
    pg: PgDatabase,
    flt: RetrievalFilter,
    *,
    limit: int | None = None,
    source: PgSource | str = PgSource.ACTIVE_VIEW,
) -> list[str]:
    """按过滤条件取可检索切片编号。只读，不开事务写。.

    默认走 `v_active_chunks`：它是"双 active"的唯一取数口径，检索、重建、对账
    都该按它取数（§4.2）。传 `PgSource.CHUNKS` 时 `pg_where` 会自己补状态条件。
    """
    resolved = PgSource(source)
    where, params = flt.pg_where("c", source=resolved)
    sql = SELECT_IDS_SQL.format(source=resolved.value, alias="c", where=where)
    if limit is not None:
        sql += " LIMIT %s"
        params = [*params, limit]
    rows = pg.fetch_all(sql, params)
    return [row["chunk_id"] for row in rows]


def explain(pg: PgDatabase, flt: RetrievalFilter) -> list[Any]:
    """`EXPLAIN (ANALYZE, BUFFERS)` 的原始计划行。.

    给排障用：过滤条件写错（比如把 `&&` 写成 `@>`）在功能测试里可能看不出来，
    但在执行计划里会立刻表现为 GIN 索引没被使用、或者扫描行数等于全表。
    """
    where, params = flt.pg_where("c", source=PgSource.ACTIVE_VIEW)
    sql = "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + SELECT_IDS_SQL.format(
        source=PgSource.ACTIVE_VIEW.value, alias="c", where=where
    )
    rows = pg.fetch_all(sql, params)
    return [row.get("QUERY PLAN") for row in rows]
