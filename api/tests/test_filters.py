"""过滤表达式（§5.2 / §7.1）：结构、边界、以及两条渲染路径的语义一致。

三层验证：

1. **结构**：生成的 Qdrant filter 就是文档写的那三组条件，不多不少。
2. **边界**：无 depts / 多 depts / admin / 有时间 / 无时间 / scope，逐个验。
3. **一致性**：同一份条件渲染出的 Qdrant filter 与 PG WHERE，在**同一批数据**上
   必须选出**完全相同的** chunk_id 集合（`test_filter_parity.py` 真跑两边）。

第 3 层是重点。前两层都能靠"读代码觉得对"通过，而真正的裂缝恰恰是两边各自
演进后语义不同——所以一致性必须由数据对拍来证，不能由代码审阅来证。
"""

from __future__ import annotations

import pytest

from api.filters import (
    ACTIVE_STATUS,
    BRANCH_DEPTS,
    BRANCH_PUBLIC,
    BRANCH_ROLES,
    P_DATE_CONFIDENCE,
    P_DEPTS,
    P_DOC_KEY,
    P_EFFECTIVE_DATE,
    P_EFFECTIVE_YEAR,
    P_IS_PUBLIC,
    P_REPORT_PERIOD_END,
    P_REPORT_PERIOD_START,
    P_ROLES,
    P_STATUS,
    PgSource,
    RetrievalFilter,
    build_filter,
)
from api.identity import IdentitySnapshot
from api.time_range import TimeRange

USER = IdentitySnapshot(user_id="u_fin", depts=("fin",), roles=("staff",))
MULTI = IdentitySnapshot(
    user_id="u_multi", depts=("corp", "fin"), roles=("approver", "staff")
)
NO_DEPTS = IdentitySnapshot(user_id="u_new")
ADMIN = IdentitySnapshot(user_id="u_admin", depts=(), roles=(), admin=True)
MARCH = TimeRange(
    20260301, 20260331, "month", "三月", "assumed_year", inferred=True
)
YEAR_2026 = TimeRange(20260101, 20261231, "year", "2026年", "explicit")


# --- 结构 -------------------------------------------------------------------


def _conditions(flt: RetrievalFilter) -> dict[str, object]:
    """把 Qdrant filter 摊平成 {字段: 条件}，便于断言结构而不是断言类名。

    断言类名（`isinstance(x, FieldCondition)`）在实现换形状时会一起改，
    断言"哪个字段、什么匹配方式"才是真正想守的契约。

    ACL 那一组是 must 里的嵌套 Filter，所以它内部的字段也会被摊到顶层
    （`__should__` 保留分支顺序）；否则每条断言都要先手动钻进嵌套结构。
    """
    qdrant_filter = flt.to_qdrant()
    assert qdrant_filter.must, "must 不能为空：status=active 是恒有的一条"
    out: dict[str, object] = {}
    for item in qdrant_filter.must:
        if hasattr(item, "key"):
            out[item.key] = _condition_value(item)
            continue
        # 嵌套 Filter（ACL 那组或关系）
        assert item.should, "嵌套 Filter 只在 should 场景出现"
        out["__should__"] = [sub.key for sub in item.should]
        for sub in item.should:
            out[sub.key] = _condition_value(sub)
    return out


def _condition_value(condition: object) -> object:
    rng = getattr(condition, "range", None)
    if rng is not None:
        return (rng.gte, rng.lte)
    match = getattr(condition, "match", None)
    assert match is not None
    return match


def test_always_has_status_active() -> None:
    conds = _conditions(RetrievalFilter(identity=USER))
    assert P_STATUS in conds
    assert conds[P_STATUS].value == ACTIVE_STATUS  # type: ignore[union-attr]


def test_acl_group_has_public_depts_roles_for_normal_user() -> None:
    conds = _conditions(RetrievalFilter(identity=USER))
    assert conds["__should__"] == [P_IS_PUBLIC, P_DEPTS, P_ROLES]


def test_public_branch_is_always_present() -> None:
    """§5.2：无部门的人只能看公开文档，所以 public 这一支永远在。"""
    for identity in (USER, MULTI, NO_DEPTS):
        assert (
            BRANCH_PUBLIC in RetrievalFilter(identity=identity).acl_branches()
        )


def test_no_depts_and_no_roles_yields_public_only() -> None:
    flt = RetrievalFilter(identity=NO_DEPTS)
    assert flt.acl_branches() == (BRANCH_PUBLIC,)
    assert _conditions(flt)["__should__"] == [P_IS_PUBLIC]
    # 关键：不生成空的
    # MatchAny(any=[])——那要靠别人未成文的实现细节才对，而"省略"
    # 不需要任何人承诺什么。
    assert P_DEPTS not in _conditions(flt)


def test_multi_depts_all_go_into_one_any() -> None:
    """多部门是“任一命中”：一个 MatchAny 装两个值，不是两条 AND 条件。

    写成两条 AND 会让跨部门的人一条都命中不了（§5.2 点名要避免的坑）。
    """
    conds = _conditions(RetrievalFilter(identity=MULTI))
    match = conds[P_DEPTS]
    assert sorted(match.any) == ["corp", "fin"]  # type: ignore[union-attr]


def test_depts_only_without_roles_omits_roles_branch() -> None:
    identity = IdentitySnapshot(user_id="u_d", depts=("hr",))
    conds = _conditions(RetrievalFilter(identity=identity))
    assert conds["__should__"] == [P_IS_PUBLIC, P_DEPTS]
    assert BRANCH_ROLES not in RetrievalFilter(identity=identity).acl_branches()


def test_admin_drops_the_whole_acl_group() -> None:
    """§5.2 边界表：管理员跳过部门与角色。

    是“整条去掉”，不是“放宽成 public or 全部”——后者会让 is_public 与
    ACL 的语义纠缠在一起，日后无法单独调整。
    """
    flt = RetrievalFilter(identity=ADMIN)
    conds = _conditions(flt)
    assert flt.acl_branches() == ()
    assert "__should__" not in conds
    assert (
        P_IS_PUBLIC not in conds
        and P_DEPTS not in conds
        and P_ROLES not in conds
    )
    # 但 status=active 仍在：管理员也要看生效版本（版本兜底另算，§7.4）。
    assert P_STATUS in conds


def test_admin_marks_audit_flag() -> None:
    assert RetrievalFilter(identity=ADMIN).describe()["audit_flags"] == [
        "admin_access"
    ]


# --- 时间条件 ---------------------------------------------------------------


def test_no_time_word_adds_no_time_condition() -> None:
    conds = _conditions(RetrievalFilter(identity=USER))
    assert P_EFFECTIVE_DATE not in conds
    assert P_DATE_CONFIDENCE not in conds


def test_time_window_adds_inclusive_range_and_explicit_only() -> None:
    """§5.2：只认显式时间。

    少 date_confidence 那一半，推断出来的日期也会被召回。
    """
    conds = _conditions(RetrievalFilter(identity=USER, time_range=MARCH))
    assert conds[P_EFFECTIVE_DATE] == (20260301, 20260331)
    assert conds[P_DATE_CONFIDENCE].value == "explicit"  # type: ignore[union-attr]


def test_year_window_uses_effective_year_and_allows_inferred_dates() -> None:
    """报告级年份是 inferred；年份查询不能要求精确 explicit 日期。"""
    conds = _conditions(RetrievalFilter(identity=USER, time_range=YEAR_2026))
    assert conds[P_EFFECTIVE_YEAR] == (2026, 2026)
    assert P_EFFECTIVE_DATE not in conds
    assert P_DATE_CONFIDENCE not in conds


# --- scope ------------------------------------------------------------------


# --- report period 三层过滤
# ------------------------------------------------------


def _time_branches(flt: RetrievalFilter) -> list[object]:
    qdrant_filter = flt.to_qdrant()
    temporal = [
        item
        for item in qdrant_filter.must
        if hasattr(item, "should")
        and not any(
            getattr(sub, "key", None) == P_IS_PUBLIC for sub in item.should
        )
    ]
    assert len(temporal) == 1, "时间条件必须是一个 should 组"
    return list(temporal[0].should)


def _branch_conditions(branch: object) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in branch.must:  # type: ignore[union-attr]
        if getattr(item, "is_null", None) is not None:
            out[item.is_null.key] = ("is_null", True)  # type: ignore[union-attr]
        else:
            out[item.key] = _condition_value(item)
    return out


def _condition_value(condition: object) -> object:
    rng = getattr(condition, "range", None)
    if rng is not None:
        return (rng.gte, rng.lte)
    match = getattr(condition, "match", None)
    if match is not None:
        return match
    if getattr(condition, "is_null", None) is not None:
        return ("is_null", condition.is_null)  # type: ignore[union-attr]
    raise AssertionError("condition has neither range nor match nor is_null")


Q3_REPORT = TimeRange(
    20260701, 20260930, "quarter", "2026 Q3", "explicit", use_report_period=True
)


def test_report_period_filter_uses_three_temporal_branches() -> None:
    branches = _time_branches(
        RetrievalFilter(identity=ADMIN, time_range=Q3_REPORT)
    )
    assert len(branches) == 3
    exact = _branch_conditions(branches[0])
    assert exact[P_REPORT_PERIOD_START] == (20260701, 20260930)
    assert exact[P_REPORT_PERIOD_END] == (20260701, 20260930)
    for branch in branches[1:]:
        conditions = _branch_conditions(branch)
        assert conditions[P_REPORT_PERIOD_START] == ("is_null", True)
        assert conditions[P_REPORT_PERIOD_END] == ("is_null", True)
    fallback_date = _branch_conditions(branches[1])
    assert P_EFFECTIVE_DATE in fallback_date
    fallback_year = _branch_conditions(branches[2])
    assert P_EFFECTIVE_YEAR in fallback_year


def test_scope_adds_doc_key_any() -> None:
    conds = _conditions(
        RetrievalFilter(identity=USER, doc_keys=("policy/a", "policy/b"))
    )
    assert sorted(conds[P_DOC_KEY].any) == ["policy/a", "policy/b"]  # type: ignore[union-attr]


def test_empty_scope_is_treated_as_no_scope() -> None:
    """空 scope 的语义是“不限定文档”（附录 C.1）。

    拼成恒假条件会让一次请求拼错表现为“库里没有”，而不是“请求有问题”。
    """
    flt = RetrievalFilter(identity=USER, doc_keys=())
    assert flt.doc_keys is None
    assert P_DOC_KEY not in _conditions(flt)


def test_scope_is_deduped_and_blank_stripped() -> None:
    flt = RetrievalFilter(identity=USER, doc_keys=(" a ", "a", "", "b"))
    assert flt.doc_keys == ("a", "b")


# --- PG 渲染 ----------------------------------------------------------------


def test_pg_where_on_view_omits_status() -> None:
    """视图已经把“双 active”写进 WHERE，而且视图里没有 status 列。

    实测撞过：多加一条 `c.status = 'active'` 直接报 column does not exist。
    """
    where, params = RetrievalFilter(identity=USER).pg_where()
    assert "status" not in where
    # 参数里只剩 ACL 的部门与角色，没有状态值。
    assert ACTIVE_STATUS not in params
    assert params == [["fin"], ["staff"]]


def test_pg_where_on_chunks_table_adds_status() -> None:
    """反方向：直连原表时状态必须自己写，否则待转正/已下线的切片都会进来。"""
    where, params = RetrievalFilter(identity=USER).pg_where(
        source=PgSource.CHUNKS
    )
    assert "status = %s" in where
    assert params[0] == ACTIVE_STATUS


def test_pg_where_uses_intersection_not_containment() -> None:
    """`&&` 是"有交集"。

    写成 `<@`（被包含）会让多部门的人漏召回——§5.2 明确点名的坑。
    """
    where, params = RetrievalFilter(identity=MULTI).pg_where()
    assert "allowed_depts && %s::text[]" in where
    assert "allowed_roles && %s::text[]" in where
    assert "[" in where and "]" in where  # 是括号内取或
    assert " OR " in where
    assert params[0] == ["corp", "fin"]


def test_pg_where_for_public_only_user_has_no_acl_params() -> None:
    where, params = RetrievalFilter(identity=NO_DEPTS).pg_where()
    assert where.count("&&") == 0
    assert params == []


def test_pg_where_for_admin_has_no_acl_clause() -> None:
    where, params = RetrievalFilter(identity=ADMIN).pg_where()
    assert "is_public" not in where
    assert "allowed_depts" not in where
    assert params == []


def test_pg_where_time_clause_uses_between_and_explicit() -> None:
    where, params = RetrievalFilter(identity=USER, time_range=MARCH).pg_where()
    assert "effective_date BETWEEN %s AND %s" in where
    assert "date_confidence = %s" in where
    assert 20260301 in params and 20260331 in params
    assert "explicit" in params


def test_pg_where_year_clause_uses_effective_year_without_confidence_gate() -> (
    None
):
    where, params = RetrievalFilter(
        identity=USER, time_range=YEAR_2026
    ).pg_where()
    assert "effective_year BETWEEN %s AND %s" in where
    assert "effective_date" not in where
    assert "date_confidence" not in where
    assert 2026 in params


def test_pg_where_params_are_all_placeholders() -> None:
    """部门名直接拼进 SQL 会让 GIN 索引用不上，也给了注入面。"""
    where, _ = RetrievalFilter(identity=MULTI, doc_keys=("a",)).pg_where()
    assert "corp" not in where and "fin" not in where and "staff" not in where
    assert where.count("%s") >= 3


def test_pg_where_alias_is_applied_to_every_column() -> None:
    where, _ = RetrievalFilter(identity=USER, time_range=MARCH).pg_where(
        "chunks"
    )
    for column in (
        "is_public",
        "allowed_depts",
        "allowed_roles",
        "effective_date",
        "date_confidence",
    ):
        assert f"chunks.{column}" in where
    # 没有裸列名漏出来（会与 JOIN 里另一张表的同名列歧义）。
    assert "f date_confidence" not in where


# --- 两条路必须回答同一组问题 -----------------------------------------------


@pytest.mark.parametrize(
    "identity",
    [
        USER,
        MULTI,
        NO_DEPTS,
        ADMIN,
        IdentitySnapshot(user_id="u_r", roles=("staff",)),
    ],
)
def test_both_renderings_agree_on_which_branches_are_active(
    identity: IdentitySnapshot,
) -> None:
    """结构级的对拍：PG 里出现的每一支，Qdrant 里也必须出现。

    这是数据级对拍（test_filter_parity.py）的前置守卫——如果结构就不一致，
    那边会变成"两边都错成一样"才通得过，很危险。

    注意比较的是 **ACL 三个分支**，不是"条件条数"：PG 侧的生效状态来自视图里的
    JOIN（关系性），Qdrant 侧是 payload 里的扁平字段，条件形状本来就不可能一样。
    能且只能要求的是：谁该被放行、谁该被挡，两边答案相同。
    """
    flt = RetrievalFilter(identity=identity)
    where, _ = flt.pg_where()
    conds = _conditions(flt)
    branches = flt.acl_branches()

    assert ("is_public" in where) == (BRANCH_PUBLIC in branches)
    assert ("allowed_depts &&" in where) == (BRANCH_DEPTS in branches)
    assert ("allowed_roles &&" in where) == (BRANCH_ROLES in branches)
    if branches:
        assert conds["__should__"] == [
            name
            for name in (P_IS_PUBLIC, P_DEPTS, P_ROLES)
            if name in conds["__should__"]
        ]
    else:
        assert "__should__" not in conds


def test_describe_is_json_ready_and_has_no_secrets() -> None:
    import json

    payload = json.loads(
        json.dumps(RetrievalFilter(identity=USER, time_range=MARCH).describe())
    )
    assert payload["user_id"] == "u_fin"
    assert payload["time_range"]["from"] == 20260301
    assert payload["admin"] is False


def test_build_filter_logs_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    built = build_filter(USER, time_range=MARCH, doc_keys=["policy/a"])
    assert built.time_range is MARCH
    assert built.doc_keys == ("policy/a",)
