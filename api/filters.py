"""前置过滤表达式（详细设计.md §5.2 / §7.1）。

一句话：**同一份归一条件，渲染成 Qdrant filter 和 PG WHERE 两条路。**
两条路各自手写一遍是这类系统最典型的裂缝——实测过的形状是"语义路带了权限、
BM25 路漏了"，于是没权限的文档从另一条路漏进候选池，而日志里一切都正常。

所以这里没有 `to_qdrant()` 和 `to_pg()` 两个各自做判断的函数。只有：

    RetrievalFilter(归一条件)  ->  .to_qdrant()   .pg_where()

两边的判断全部来自 `acl_branches()` 这一个地方。要改口径只改它，
改完两条路一起变；如果只改了一边，`test_filter_parity` 里那批对拍用例会红。

表达式的形状（§5.2）：

    status = 'active'
    AND ( is_public OR depts 交集 OR roles 交集 )      # admin 时整条去掉
    AND ( effective_date BETWEEN .. AND .. AND date_confidence='explicit' )  # 有时间条件时
    AND ( doc_key = ANY(..) )                          # 请求带 scope 时

三条刻意写进代码的取舍：

1. **部门/角色是"有交集"，不是"被包含"**。写成"用户部门必须在文档部门列表里"，
   多部门的人就漏——§5.2 明确点名了这个坑。
2. **空集合的分支直接不生成**，而不是生成一个恒假条件。`MatchAny(any=[])` 在
   Qdrant 里确实什么都不匹配（实测过），但依赖这个未成文的语义，等于把正确性押在
   别人的实现细节上；两边都省略，语义反而无需对齐。
3. **admin 去掉整条 ACL**，不是"放宽成 public or 全部"。§5.2 的边界表写的是
   "跳过部门与角色"，同时"仍校验版本"（版本校验不在这里，见 §7.4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Sequence

from api.identity import IdentitySnapshot
from api.observability import get_logger, log_event
from api.time_range import TimeRange
from api.trace import trace as trace_event

logger = get_logger("api.filters")


class PgSource(StrEnum):
    """PG 侧取数的两个来源。语义不同，不能混用（见 `pg_where` 的说明）。"""

    ACTIVE_VIEW = "v_active_chunks"  # 视图已含"双 active"，不要再写 status
    CHUNKS = "chunks"  # 直连原表，必须自己写 status


# --- Qdrant payload 字段名 -------------------------------------------------
# 与 scripts/qdrant_common.py 的 PAYLOAD_INDEXES 必须一致；这里写常量而不是散落
# 字符串，是因为字段名拼错时 Qdrant 不会报错，它只是"什么都不匹配"。
P_DOC_KEY = "doc_key"
P_STATUS = "status"
P_IS_PUBLIC = "is_public"
P_DEPTS = "allowed_depts"
P_ROLES = "allowed_roles"
P_EFFECTIVE_DATE = "effective_date"
P_EFFECTIVE_YEAR = "effective_year"
P_DATE_CONFIDENCE = "date_confidence"

# 只有显式日期才参与时间过滤（§5.2：不认推断出来的时间）。
EXPLICIT_CONFIDENCE = "explicit"
ACTIVE_STATUS = "active"

# ACL 的三个分支，名字用于日志与审计（不给字符串常量就直接拼日志，
# 迟早出现 "dept" / "depts" / "departments" 三种写法）。
BRANCH_PUBLIC = "public"
BRANCH_DEPTS = "depts"
BRANCH_ROLES = "roles"


@dataclass(frozen=True, slots=True)
class RetrievalFilter:
    """一次检索的完整前置条件。构造后不可变，两条渲染路都只读它。"""

    identity: IdentitySnapshot
    time_range: TimeRange | None = None
    doc_keys: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.doc_keys is not None:
            cleaned = tuple(
                dict.fromkeys(k.strip() for k in self.doc_keys if k.strip())
            )
            object.__setattr__(self, "doc_keys", cleaned)
            if not cleaned:
                # 空 scope 的语义是"不限定文档"，不是"一份都看不到"。
                # 让调用方拿到 None 的语义，而不是一个恒假条件——后者会让
                # 一次 scope 拼错的请求表现为"库里没有"，而不是"请求有问题"。
                object.__setattr__(self, "doc_keys", None)

    # --- 唯一的判断入口 ---

    @property
    def is_admin(self) -> bool:
        return self.identity.admin

    def acl_branches(self) -> tuple[str, ...]:
        """ACL 生效的分支名。空元组表示不加 ACL 条件（管理员）。"""
        if self.is_admin:
            return ()
        branches = [BRANCH_PUBLIC]
        if self.identity.depts:
            branches.append(BRANCH_DEPTS)
        if self.identity.roles:
            branches.append(BRANCH_ROLES)
        return tuple(branches)

    @property
    def has_time_filter(self) -> bool:
        return self.time_range is not None

    # --- Qdrant 渲染 ---

    def to_qdrant(self) -> Any:
        """渲染成 qdrant_client 的 Filter。

        延迟 import：`api.filters` 要能在没装 qdrant-client 的解释器里被 import
        （迁移脚本、离线工具只需要 PG 那一半）。
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchAny,
            MatchValue,
            Range,
        )

        must: list[Any] = [
            FieldCondition(key=P_STATUS, match=MatchValue(value=ACTIVE_STATUS))
        ]

        branches = self.acl_branches()
        should: list[Any] = []
        if BRANCH_PUBLIC in branches:
            should.append(FieldCondition(key=P_IS_PUBLIC, match=MatchValue(value=True)))
        if BRANCH_DEPTS in branches:
            should.append(
                FieldCondition(
                    key=P_DEPTS, match=MatchAny(any=list(self.identity.depts))
                )
            )
        if BRANCH_ROLES in branches:
            should.append(
                FieldCondition(
                    key=P_ROLES, match=MatchAny(any=list(self.identity.roles))
                )
            )
        if should:
            # 嵌套 Filter 放在 must 里 = "这一组内部按 should 取或，整体作为与条件"。
            must.append(Filter(should=should))

        if self.time_range is not None:
            tr = self.time_range
            if tr.granularity in ("year", "half_year", "quarter"):
                # 报告/半年报级时间窗按年份过滤：切片的 effective_date 常为 NULL（正文
                # 没写“生效”引导词），按精确日期过滤会把现金流量表等整段筛没。
                # inferred 年份也保留，避免“半年报”整份文档被时间条件打空。
                must.append(
                    FieldCondition(
                        key=P_EFFECTIVE_YEAR,
                        range=Range(gte=min(tr.years), lte=max(tr.years)),
                    )
                )
            else:
                must.append(
                    FieldCondition(
                        key=P_EFFECTIVE_DATE, range=Range(gte=tr.date_from, lte=tr.date_to)
                    )
                )
                must.append(
                    FieldCondition(
                        key=P_DATE_CONFIDENCE,
                        match=MatchValue(value=EXPLICIT_CONFIDENCE),
                    )
                )

        if self.doc_keys is not None:
            must.append(
                FieldCondition(key=P_DOC_KEY, match=MatchAny(any=list(self.doc_keys)))
            )

        return Filter(must=must)

    # --- PG 渲染 ---

    def pg_where(
        self, alias: str = "c", source: "PgSource | str" = "v_active_chunks"
    ) -> tuple[str, list[Any]]:
        """渲染成 PG 的 WHERE 片段（不含 `WHERE` 关键字）与参数。

        **`source` 不是可有可无的开关，而是这条路的语义前提**：PG 的"生效"是关系性的
        （chunk 与 version 双 active，要 JOIN 两张表），Qdrant 的"生效"是扁平字段
        （`payload.status`）。所以两边**不可能条件列表一一对应**，只能做到结果集合相同。

        - `v_active_chunks`（默认）：视图已经把"双 active"写进 WHERE，
          这边再加 `status = 'active'` 不但冗余，而且视图里根本没有这一列
          （直接报 `column c.status does not exist`——这是实测撞出来的）。
        - `chunks`：直连原表时必须自己写状态，否则待转正与已下线的切片都会进来。

        `alias` 由调用方给，默认 `c`。参数顺序与占位符顺序严格对应，全部走 %s 占位符——
        部门/角色列表直接拼进 SQL 会让 GIN 索引用不上，也给了注入面。
        """
        resolved_source = PgSource(source)
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if resolved_source is PgSource.CHUNKS:
            clauses.append(f"{prefix}status = %s")
            params.append(ACTIVE_STATUS)

        branches = self.acl_branches()
        or_parts: list[str] = []
        if BRANCH_PUBLIC in branches:
            # 写成 `is_public` 而不是 `is_public = true`：列本身 NOT NULL，
            # 这样 PG 的计划器能把它当成部分索引/常量条件处理。
            or_parts.append(f"{prefix}is_public")
        if BRANCH_DEPTS in branches:
            # `&&` 是数组"有交集"，正是 §5.2 要的语义；GIN 索引直接支撑它。
            or_parts.append(f"{prefix}allowed_depts && %s::text[]")
            params.append(list(self.identity.depts))
        if BRANCH_ROLES in branches:
            or_parts.append(f"{prefix}allowed_roles && %s::text[]")
            params.append(list(self.identity.roles))
        if or_parts:
            clauses.append("(" + " OR ".join(or_parts) + ")")

        if self.time_range is not None:
            tr = self.time_range
            if tr.granularity in ("year", "half_year", "quarter"):
                clauses.append(f"{prefix}effective_year BETWEEN %s AND %s")
                params.extend([min(tr.years), max(tr.years)])
            else:
                clauses.append(f"{prefix}effective_date BETWEEN %s AND %s")
                params.extend([tr.date_from, tr.date_to])
                clauses.append(f"{prefix}date_confidence = %s")
                params.append(EXPLICIT_CONFIDENCE)

        if self.doc_keys is not None:
            clauses.append(f"{prefix}doc_key = ANY(%s)")
            params.append(list(self.doc_keys))

        # 管理员 + 无时间 + 无 scope 时一条条件都没有。返回 "TRUE" 而不是空串：
        # 空串会拼出 `WHERE ORDER BY ...`（实测报 syntax error at or near "ORDER"）。
        # 用 "TRUE" 而不是让调用方判空，是因为调用方判空迟早有一处忘了判。
        return (" AND ".join(clauses) or "TRUE"), params

    # --- 日志 / 审计 ---

    def describe(self) -> dict[str, Any]:
        """给日志与审计的一句话描述。不含原文、不含问题，只有口径。"""
        return {
            "user_id": self.identity.user_id,
            "admin": self.is_admin,
            "acl_branches": list(self.acl_branches()),
            "public_only": self.identity.is_public_only,
            "time_range": None
            if self.time_range is None
            else self.time_range.as_dict(),
            "doc_keys": None if self.doc_keys is None else list(self.doc_keys),
            "audit_flags": list(self.identity.audit_flags()),
        }

    def log(self) -> None:
        log_event(logger, "retrieval_filter_built", **self.describe())
        trace_event("filter.built", **self.describe())


def build_filter(
    identity: IdentitySnapshot,
    *,
    time_range: TimeRange | None = None,
    doc_keys: Sequence[str] | None = None,
) -> RetrievalFilter:
    """构造过滤条件并留一条日志。

    留日志不是仪式：出线上问题时第一个要回答的是"这次到底按什么权限搜的"，
    而这个问题没法从结果反推。管理员访问另有一条 `admin_access` 级别更高的日志。
    """
    built = RetrievalFilter(
        identity=identity,
        time_range=time_range,
        doc_keys=None if doc_keys is None else tuple(doc_keys),
    )
    built.log()
    return built
