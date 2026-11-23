"""过滤语义在 Qdrant 与 PG 两侧的一致性（需要真 PG + 真 Qdrant）。

这是本卡唯一一类"读代码读不出来"的证据。`test_filters.py` 断言的是结构
（该有的条件在、不该有的不在），但两边各自演进之后"结构看起来对、结果不同"
是真实发生过的形状：语义路带了权限、BM25 路漏了，两边都"看起来有过滤"。

做法：造一批**故意覆盖每种边界**的切片（公开/各部门/仅角色/无 ACL/已下线/
推断日期/显式日期/不同月份），分别灌 PG 与 Qdrant，然后用同一份
`RetrievalFilter` 在两边各查一次，比较 chunk_id 集合。

四类断言：
1. 每个身份形态下，两边集合完全相等（不是"都非空"，是集合相等）。
2. 无权限的切片在两边都不出现（防"两边错成一样"：如果两边都漏了 ACL，
   集合仍然相等，所以必须再单独验"该出现/不该出现"）。
3. 时间窗在两边都精确到月（专门针对"Qdrant 只有 effective_year"的退化）。
4. 变异守卫：把 Qdrant 侧的时间条件降级成整年过滤，对拍必须失败（证明对拍有牙）。

Qdrant 侧的 payload 由 `scripts/qdrant_common.build_payload` 生成，不在测试里手搓：
手搓就变成"测试与实现各写一遍"，而两边不一致正是本卡要防的事。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable

import psycopg
import pytest

from api.chunks_repo import select_ids
from api.clients.pg import PgDatabase, normalize_dsn
from api.filters import RetrievalFilter
from api.identity import IdentitySnapshot
from api.qdrant_repo import scroll_ids, scroll_rows
from api.time_range import TimeRange
from api.version_guard import drop_stale

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

QDRANT_URL = os.getenv("AI_RAG_TEST_QDRANT_URL") or os.getenv("QDRANT_URL", "")
COLLECTION = f"b2_parity_{uuid.uuid4().hex[:8]}"
DOC_KEY = "policy/b2-parity"
ACTIVE_VERSION = f"{DOC_KEY}#b2a1"
OLD_VERSION = f"{DOC_KEY}#b2old"
HASH_ACTIVE = "a" * 64
HASH_OLD = "b" * 64

# (chunk_id, status, is_public, depts, roles, effective_date, confidence, version)
ROWS: list[tuple[str, str, bool, list[str], list[str], int | None, str, str]] = [
    ("ch_public", "active", True, [], [], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_fin", "active", False, ["fin"], [], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_hr", "active", False, ["hr"], [], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_corp", "active", False, ["corp"], [], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_staff", "active", False, [], ["staff"], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_board", "active", False, [], ["board"], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_none", "active", False, [], [], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_inferred", "active", True, [], [], 20260310, "inferred", ACTIVE_VERSION),
    ("ch_feb", "active", True, [], [], 20260210, "explicit", ACTIVE_VERSION),
    ("ch_no_date", "active", True, [], [], None, "inferred", ACTIVE_VERSION),
    ("ch_old", "deprecated", True, [], [], 20260310, "explicit", ACTIVE_VERSION),
    ("ch_oldver", "active", True, [], [], 20260310, "explicit", OLD_VERSION),
]
ACTIVE_ROW_IDS = {r[0] for r in ROWS if r[1] == "active" and r[7] == ACTIVE_VERSION}


def _qdrant_common():
    """import scripts/qdrant_common.py（它不在包里，需要把 scripts/ 塞进 sys.path）。"""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import qdrant_common  # noqa: PLC0415

    return qdrant_common


def _qdrant_client():
    try:
        from qdrant_client import QdrantClient  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        pytest.skip("qdrant-client 未安装")
    if not QDRANT_URL:
        pytest.skip("QDRANT_URL 未设置（对拍需要真 Qdrant）")
    # trust_env=False：本机 ALL_PROXY=socks5 会把 127.0.0.1 也代理走，WSL 下直接失败。
    return QdrantClient(
        url=QDRANT_URL, timeout=10, check_compatibility=False, trust_env=False
    )


@pytest.fixture(scope="module")
def pg(pg_dsn: str):
    """真 PG，schema 走项目唯一的迁移入口（不自己建表）。"""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPTS_DIR / "db_migrate.py"), "--dsn", pg_dsn],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"db_migrate.py failed: {proc.stderr[-800:]}"
    database = PgDatabase(pg_dsn, min_size=1, max_size=2, connect_timeout_s=5)
    database.open()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(scope="module")
def seeded_pg(pg: PgDatabase, pg_dsn: str):
    """造数：两个版本（一个 active 一个 deprecated），12 个切片覆盖各种边界。"""
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    with psycopg.connect(normalize_dsn(pg_dsn), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO documents (doc_key, filename, page_count, status) "
            "VALUES (%s, %s, 1, 'active') ON CONFLICT (doc_key) DO NOTHING",
            (DOC_KEY, f"{DOC_KEY}.pdf"),
        )
        # ux_active_version 保证一份文档只能一个 active；老版本给 deprecated。
        conn.execute(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status, activated_at) "
            "VALUES (%s, %s, %s, 'active', now()), "
            "(%s, %s, %s, 'deprecated', now() - interval '1 day') "
            "ON CONFLICT (version_id) DO NOTHING",
            (ACTIVE_VERSION, DOC_KEY, HASH_ACTIVE, OLD_VERSION, DOC_KEY, HASH_OLD),
        )
        for (
            chunk_id,
            status,
            is_public,
            depts,
            roles,
            eff_date,
            confidence,
            version,
        ) in ROWS:
            conn.execute(
                """
                INSERT INTO chunks (chunk_id, doc_key, version_id, chunk_type, page_num, bbox,
                                    content_text, effective_date, effective_year,
                                    date_confidence, is_public, allowed_depts, allowed_roles, status)
                VALUES (%(cid)s, %(doc)s, %(ver)s, 'text', 1, %(bbox)s::jsonb,
                        %(text)s, %(ed)s::int,
                        CASE WHEN %(ed)s::int IS NULL THEN NULL ELSE %(ed)s::int / 10000 END,
                        %(conf)s, %(pub)s, %(depts)s::text[], %(roles)s::text[], %(st)s)
                ON CONFLICT (chunk_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    allowed_depts = EXCLUDED.allowed_depts,
                    allowed_roles = EXCLUDED.allowed_roles,
                    is_public = EXCLUDED.is_public,
                    effective_date = EXCLUDED.effective_date,
                    effective_year = EXCLUDED.effective_year,
                    date_confidence = EXCLUDED.date_confidence
                """,
                {
                    "cid": chunk_id,
                    "doc": DOC_KEY,
                    "ver": version,
                    "bbox": Jsonb([]),
                    "text": f"内容 {chunk_id}",
                    "ed": eff_date,
                    "conf": confidence,
                    "pub": is_public,
                    "depts": depts,
                    "roles": roles,
                    "st": status,
                },
            )
    try:
        yield pg
    finally:
        with psycopg.connect(normalize_dsn(pg_dsn), autocommit=True) as conn:
            conn.execute("DELETE FROM chunks WHERE doc_key = %s", (DOC_KEY,))
            conn.execute("DELETE FROM doc_versions WHERE doc_key = %s", (DOC_KEY,))
            conn.execute("DELETE FROM documents WHERE doc_key = %s", (DOC_KEY,))


@pytest.fixture(scope="module")
def seeded_qdrant(seeded_pg: PgDatabase, pg_dsn: str):
    """Qdrant 侧由 PG 的同一批行生成，payload 走项目的 build_payload，保证两边输入一致。"""
    from qdrant_client.models import Distance, PointStruct, VectorParams  # noqa: PLC0415

    qc = _qdrant_common()
    client = _qdrant_client()
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config={"dense": VectorParams(size=4, distance=Distance.COSINE)},
    )
    with psycopg.connect(normalize_dsn(pg_dsn)) as conn:
        rows = conn.execute(
            "SELECT chunk_id, doc_key, version_id, chunk_type, page_num, effective_year, "
            "effective_date, date_confidence, is_public, allowed_depts, allowed_roles, status "
            "FROM chunks WHERE doc_key = %s",
            (DOC_KEY,),
        ).fetchall()
    assert len(rows) == len(ROWS), f"PG 里应有 {len(ROWS)} 行，实际 {len(rows)}"
    client.upsert(
        collection_name=COLLECTION,
        points=[
            PointStruct(
                id=qc.point_id_for(r[0]),
                vector={"dense": [0.0, 0.0, 0.0, 0.0]},
                payload=qc.build_payload(
                    {
                        "chunk_id": r[0],
                        "doc_key": r[1],
                        "version_id": r[2],
                        "chunk_type": r[3],
                        "page_num": r[4],
                        "effective_year": r[5],
                        "effective_date": r[6],
                        "date_confidence": r[7],
                        "is_public": r[8],
                        "allowed_depts": r[9],
                        "allowed_roles": r[10],
                        "status": r[11],
                    }
                ),
            )
            for r in rows
        ],
    )
    try:
        yield client
    finally:
        client.delete_collection(COLLECTION)


@pytest.fixture(scope="module")
def qdrant_ids(seeded_qdrant) -> Callable[[RetrievalFilter], list[str]]:
    """Qdrant 侧候选：只做前置过滤，不做版本兜底（§7.4 的兜底在召回之后）。"""

    def _ids(flt: RetrievalFilter) -> list[str]:
        return scroll_ids(seeded_qdrant, COLLECTION, flt)

    return _ids


@pytest.fixture(scope="module")
def qdrant_guarded_ids(
    seeded_qdrant, seeded_pg: PgDatabase
) -> Callable[[RetrievalFilter], list[str]]:
    """Qdrant 侧候选 + §7.4 版本兜底：这才是"能进精排"的集合，可与 PG 视图对齐。"""

    def _ids(flt: RetrievalFilter) -> list[str]:
        raw = scroll_rows(seeded_qdrant, COLLECTION, flt)
        kept, _ = drop_stale(seeded_pg, raw)
        return sorted({item["chunk_id"] for item in kept})

    return _ids


def _both(
    pg: PgDatabase, qdrant_ids, flt: RetrievalFilter
) -> tuple[list[str], list[str]]:
    return select_ids(pg, flt), qdrant_ids(flt)


def _both_after_guard(
    pg: PgDatabase, qdrant_guarded_ids, flt: RetrievalFilter
) -> tuple[list[str], list[str]]:
    return select_ids(pg, flt), qdrant_guarded_ids(flt)


# --- 一致性 -----------------------------------------------------------------

IDENTITIES = {
    "no_depts": IdentitySnapshot(user_id="u_new"),
    "single_dept": IdentitySnapshot(user_id="u_fin", depts=("fin",)),
    "multi_depts": IdentitySnapshot(user_id="u_multi", depts=("fin", "corp")),
    "role_only": IdentitySnapshot(user_id="u_staff", roles=("staff",)),
    "dept_and_role": IdentitySnapshot(
        user_id="u_both", depts=("hr",), roles=("staff",)
    ),
    "admin": IdentitySnapshot(user_id="u_admin", admin=True),
}


@pytest.mark.parametrize("name", sorted(IDENTITIES))
def test_pg_and_qdrant_return_the_same_ids(
    seeded_pg: PgDatabase, qdrant_guarded_ids, name: str
) -> None:
    flt = RetrievalFilter(identity=IDENTITIES[name])
    pg_ids, qd_ids = _both_after_guard(seeded_pg, qdrant_guarded_ids, flt)
    assert pg_ids == qd_ids, f"{name}: PG 与 Qdrant 过滤结果不一致"

    # 光是相等不够：两边都漏 ACL 也会相等。所以再单独钉几条"该有/该无"。
    assert "ch_public" in pg_ids, "公开切片必须能被任何人召回"
    assert "ch_old" not in pg_ids, "已下线的切片不能进候选池（status=active）"
    assert "ch_oldver" not in pg_ids, "旧版本下的切片不能进候选池（版本生效口径）"


def test_no_depts_sees_only_public(seeded_pg: PgDatabase, qdrant_guarded_ids) -> None:
    """§5.2：无部门 = 空集 = 只能看公开。不能是"看到全部"，也不是"什么都没看到"。"""
    pg_ids, qd_ids = _both_after_guard(
        seeded_pg, qdrant_guarded_ids, RetrievalFilter(identity=IDENTITIES["no_depts"])
    )
    # 公开的活跃切片：ch_public / ch_feb / ch_inferred / ch_no_date（无时间条件时都在）
    expected = {"ch_public", "ch_feb", "ch_inferred", "ch_no_date"}
    assert set(pg_ids) == expected, f"多出来的：{set(pg_ids) - expected}"
    assert set(qd_ids) == expected


def test_multi_depts_is_union_not_intersection(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    """多部门是"任一命中"。若写成"必须属于全部部门"，ch_fin 与 ch_corp 都会消失。"""
    pg_ids, qd_ids = _both_after_guard(
        seeded_pg,
        qdrant_guarded_ids,
        RetrievalFilter(identity=IDENTITIES["multi_depts"]),
    )
    expected = {"ch_public", "ch_feb", "ch_inferred", "ch_no_date", "ch_fin", "ch_corp"}
    assert set(pg_ids) == expected, f"PG 少了 {expected - set(pg_ids)}"
    assert set(qd_ids) == expected, f"Qdrant 少了 {expected - set(qd_ids)}"


def test_dept_and_role_are_both_honoured(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    """部门或角色任一命中即可（§5.2 是三个分支取或，不是与）。"""
    pg_ids, qd_ids = _both_after_guard(
        seeded_pg,
        qdrant_guarded_ids,
        RetrievalFilter(identity=IDENTITIES["dept_and_role"]),
    )
    expected = {"ch_public", "ch_feb", "ch_inferred", "ch_no_date", "ch_hr", "ch_staff"}
    assert set(pg_ids) == expected and set(qd_ids) == expected


def test_admin_sees_every_active_chunk(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    pg_ids, qd_ids = _both_after_guard(
        seeded_pg, qdrant_guarded_ids, RetrievalFilter(identity=IDENTITIES["admin"])
    )
    assert pg_ids == qd_ids
    assert set(pg_ids) == ACTIVE_ROW_IDS


def test_unprivileged_chunk_never_appears(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    """ch_none（ACL 空且非公开）与 ch_board（仅 board 角色）对所有人都不该出现。"""
    for name in (
        "no_depts",
        "single_dept",
        "multi_depts",
        "role_only",
        "dept_and_role",
    ):
        pg_ids, qd_ids = _both_after_guard(
            seeded_pg, qdrant_guarded_ids, RetrievalFilter(identity=IDENTITIES[name])
        )
        assert (
            "ch_none" not in pg_ids and "ch_none" not in qd_ids
        ), f"{name} 看到了无 ACL 的切片"
        assert "ch_board" not in pg_ids and "ch_board" not in qd_ids, f"{name} 越权命中"


# --- 时间条件 ---------------------------------------------------------------

MARCH = TimeRange(20260301, 20260331, "month", "三月", "assumed_year", inferred=True)


def test_time_window_is_month_precise_on_both_sides(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    """这条专门防"Qdrant 只有 effective_year"的退化：若向量库侧按整年过滤，
    ch_feb（二月）会被多召回。"""
    flt = RetrievalFilter(identity=IDENTITIES["admin"], time_range=MARCH)
    pg_ids, qd_ids = _both_after_guard(seeded_pg, qdrant_guarded_ids, flt)
    assert pg_ids == qd_ids, f"时间过滤不一致：PG={pg_ids} Qdrant={qd_ids}"
    assert "ch_feb" not in pg_ids, "二月切片不该落在三月的窗口里"
    assert "ch_public" in pg_ids


def test_inferred_dates_are_excluded_from_time_filter(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    """§5.2：时间过滤只认 explicit。ch_inferred 有日期但置信度是 inferred。"""
    flt = RetrievalFilter(identity=IDENTITIES["admin"], time_range=MARCH)
    pg_ids, qd_ids = _both_after_guard(seeded_pg, qdrant_guarded_ids, flt)
    assert "ch_inferred" not in pg_ids and "ch_inferred" not in qd_ids


def test_no_time_filter_keeps_inferred_and_undated(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    """反方向：不加时间条件时，推断日期与无日期的切片都该在（证明上一条不是"永远排除"）。"""
    flt = RetrievalFilter(identity=IDENTITIES["admin"])
    pg_ids, qd_ids = _both_after_guard(seeded_pg, qdrant_guarded_ids, flt)
    assert {"ch_inferred", "ch_no_date"} <= set(pg_ids)
    assert {"ch_inferred", "ch_no_date"} <= set(qd_ids)


# --- scope ------------------------------------------------------------------


def test_scope_filter_matches_on_both_sides(
    seeded_pg: PgDatabase, qdrant_guarded_ids
) -> None:
    flt = RetrievalFilter(identity=IDENTITIES["admin"], doc_keys=(DOC_KEY,))
    pg_ids, qd_ids = _both_after_guard(seeded_pg, qdrant_guarded_ids, flt)
    assert pg_ids == qd_ids and pg_ids

    other = RetrievalFilter(identity=IDENTITIES["admin"], doc_keys=("policy/other",))
    assert _both_after_guard(seeded_pg, qdrant_guarded_ids, other) == ([], [])


# --- 变异守卫：证明对拍真的会红 ---------------------------------------------


def test_parity_detects_year_granularity_regression(
    seeded_pg: PgDatabase, qdrant_ids, seeded_qdrant
) -> None:
    """把时间条件降级成"整年"（模拟 Qdrant 只有 effective_year 的旧 payload），
    对拍必须失败。判据从没红过，和被删掉是一回事。"""
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        MatchAny,
        MatchValue,
    )

    from api.filters import ACTIVE_STATUS, P_DATE_CONFIDENCE, P_STATUS  # noqa: PLC0415

    degraded = Filter(
        must=[
            FieldCondition(key=P_STATUS, match=MatchValue(value=ACTIVE_STATUS)),
            FieldCondition(key="effective_year", match=MatchAny(any=[2026])),
            FieldCondition(key=P_DATE_CONFIDENCE, match=MatchValue(value="explicit")),
        ]
    )
    points, _ = seeded_qdrant.scroll(
        collection_name=COLLECTION, scroll_filter=degraded, limit=500, with_payload=True
    )
    degraded_ids = sorted({p.payload["chunk_id"] for p in points})
    # 前提：降级后确实多召回了二月切片，否则这条守卫什么也没证明。
    assert "ch_feb" in degraded_ids, "降级后二月切片必须被错误召回，否则守卫无效"

    pg_ids = select_ids(
        seeded_pg, RetrievalFilter(identity=IDENTITIES["admin"], time_range=MARCH)
    )
    assert pg_ids != degraded_ids, "对拍没有发现整年退化"
