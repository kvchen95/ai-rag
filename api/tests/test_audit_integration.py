"""审计写入的集成测试（真 PG，由 AI_RAG_TEST_DATABASE_URL 指过去）。

表结构不在这里建：schema 的唯一权威是 `db/migrations/*.sql` +
`scripts/db_migrate.py`（DA1 卡）。这里先跑他们的 runner 把库准备好，
再验两件事：
1. 写入的列、数组、jsonb 能原样读回来（含身份快照）；
2. 库里已有的 CHECK 真的拦得住坏行——每条判据都喂一次坏数据，
   从没红过的判据和被删掉是一回事。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from api.audit import AuditWriter, QaLogEntry
from api.clients.pg import PgDatabase, normalize_dsn
from api.errors import ErrorCode

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "db_migrate.py"
TEST_USER = "u_b1_test"


@pytest.fixture(scope="module")
def migrated(pg_dsn: str) -> str:
    """用项目唯一的迁移入口把库准备好（幂等，已应用的文件会跳过）。"""
    proc = subprocess.run(  # noqa: S603 - 固定脚本路径，参数全部来自测试
        [sys.executable, str(MIGRATE_SCRIPT), "--dsn", pg_dsn],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"db_migrate.py failed: {proc.stderr[-800:]}"
    with psycopg.connect(normalize_dsn(pg_dsn)) as conn:
        exists = conn.execute("SELECT to_regclass('public.qa_log')").fetchone()[0]
    assert exists is not None, "qa_log 不存在：迁移没建出来，后面的审计断言无意义"
    return pg_dsn


@pytest.fixture
def db(migrated: str):
    database = PgDatabase(migrated, min_size=1, max_size=2, connect_timeout_s=3)
    database.open()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(autouse=True)
def _cleanup(pg_dsn: str):
    yield
    with psycopg.connect(normalize_dsn(pg_dsn), autocommit=True) as conn:
        conn.execute("DELETE FROM qa_log WHERE user_id LIKE %s", (TEST_USER + "%",))


def _entry(**overrides: object) -> QaLogEntry:
    payload: dict[str, object] = {
        "user_id": TEST_USER,
        "user_depts": ["fin", "corp"],
        "user_roles": ["staff"],
        "is_admin": False,
        "question": "2025年差旅住宿标准是多少",
        "retrieved_ids": ["ch_9f2c31", "ch_7b01"],
        "score_snapshot": {"top1": 0.62, "rerank": [0.62, 0.41]},
        "gate1_verdict": "pass",
        "gate2_verdict": "supported",
        "hop_count": 0,
        "answer": "每人每天 500 元",
        "citations": [{"n": 1, "chunk_id": "ch_9f2c31"}],
        "latency_ms": 1840,
        "model": "gw-qwen-max",
    }
    payload.update(overrides)
    return QaLogEntry(**payload)


def test_audit_row_round_trips(db: PgDatabase) -> None:
    result = AuditWriter(db, retention_days=365).record(_entry())
    assert result.ok is True and isinstance(result.audit_id, int)

    row = db.fetch_one("SELECT * FROM qa_log WHERE id = %s", (result.audit_id,))
    assert row is not None
    # 身份快照
    assert row["user_id"] == TEST_USER
    assert row["user_depts"] == ["fin", "corp"]
    assert row["user_roles"] == ["staff"]
    assert row["is_admin"] is False
    # 问答本身：数组与 jsonb 都要原样回来，不能变成字符串
    assert row["retrieved_ids"] == ["ch_9f2c31", "ch_7b01"]
    assert row["score_snapshot"]["top1"] == 0.62
    assert row["score_snapshot"]["rerank"] == [0.62, 0.41]
    assert row["citations"] == [{"n": 1, "chunk_id": "ch_9f2c31"}]
    assert row["latency_ms"] == 1840
    assert row["reject_reason"] is None


def test_admin_flag_is_recorded(db: PgDatabase) -> None:
    """§5.1：管理员跨部门访问必须在审计里可辨，否则事后无法还原。"""
    result = AuditWriter(db).record(_entry(is_admin=True, user_depts=[]))
    assert result.ok
    row = db.fetch_one(
        "SELECT is_admin, user_depts FROM qa_log WHERE id = %s", (result.audit_id,)
    )
    assert row == {"is_admin": True, "user_depts": []}


def test_reject_row_keeps_reason_code(db: PgDatabase) -> None:
    result = AuditWriter(db).record(
        _entry(
            gate1_verdict="reject_low",
            gate2_verdict=None,
            answer=None,
            citations=None,
            reject_reason=ErrorCode.LOW_CONFIDENCE,
        )
    )
    assert result.ok
    row = db.fetch_one(
        "SELECT gate1_verdict, reject_reason FROM qa_log WHERE id = %s",
        (result.audit_id,),
    )
    assert row == {"gate1_verdict": "reject_low", "reject_reason": "low_confidence"}


# 这些是 db/migrations/001_init.sql 里真实存在的单列 CHECK。
_BAD_ROWS = {
    "unknown_gate1": "INSERT INTO qa_log (user_id, question, gate1_verdict, latency_ms) "
    "VALUES ('u_b1_test', 'q', 'maybe', 1)",
    "unknown_gate2": "INSERT INTO qa_log (user_id, question, gate1_verdict, gate2_verdict, latency_ms) "
    "VALUES ('u_b1_test', 'q', 'pass', 'perhaps', 1)",
    "negative_latency": "INSERT INTO qa_log (user_id, question, gate1_verdict, latency_ms) "
    "VALUES ('u_b1_test', 'q', 'pass', -1)",
    "negative_hops": "INSERT INTO qa_log (user_id, question, gate1_verdict, hop_count, latency_ms) "
    "VALUES ('u_b1_test', 'q', 'pass', -1, 1)",
    "missing_question": "INSERT INTO qa_log (user_id, question, gate1_verdict, latency_ms) "
    "VALUES ('u_b1_test', NULL, 'pass', 1)",
}


@pytest.mark.parametrize("case", list(_BAD_ROWS))
def test_database_rejects_bad_rows(pg_dsn: str, case: str) -> None:
    with psycopg.connect(normalize_dsn(pg_dsn), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.IntegrityError):
            conn.execute(_BAD_ROWS[case])


def test_audit_failure_is_reported_not_raised() -> None:
    """审计写不进去时：调用方拿到 ok=False，业务结果不被改写，但日志里必须有 ERROR。"""
    broken = PgDatabase(
        "postgresql://nobody@127.0.0.1:1/none", connect_timeout_s=1, pool_timeout_s=0.5
    )
    result = AuditWriter(broken).record(_entry())
    assert result.ok is False
    assert result.error


def test_purge_expired_keeps_recent_rows(db: PgDatabase) -> None:
    writer = AuditWriter(db, retention_days=365)
    old = writer.record(_entry())
    fresh = writer.record(_entry())
    assert old.ok and fresh.ok
    assert old.audit_id is not None
    db.execute(
        "UPDATE qa_log SET created_at = now() - interval '400 days' WHERE id = %s",
        (old.audit_id,),
    )

    assert writer.purge_expired() >= 1
    assert db.fetch_one("SELECT id FROM qa_log WHERE id = %s", (old.audit_id,)) is None
    assert (
        db.fetch_one("SELECT id FROM qa_log WHERE id = %s", (fresh.audit_id,))
        is not None
    )
