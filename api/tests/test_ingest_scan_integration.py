"""扫描 + ingest_jobs 的集成测试（真 PG）。

只验文档与库层说了算的东西——判定表、纯函数、状态机已经在单元用例里测过，
这里再测一遍是重复。这里验四件离开真库就没意义的事：

1. **重复扫描跳过**：第二轮一个版本都不新增、一个 job 都不新增（验收第一条）。
2. **并发扫描**：第二个扫描拿到 `ScanInProgress`（验收第一条）。
3. **指纹变化建 pending 新版本、旧 active 完全不受影响**（验收第二条）——
   包括 `documents.status`、`active_version`、切片可见性三处都不许动。
4. **数据库层的约束真的拦得住坏行**：002 里的 CHECK 与部分唯一索引。

样本用临时目录现造，不依赖仓库里的 `samples/`：样本内容会被测试改写
（要造"指纹变化"），动仓库样本会让下一次 `make test-api` 的期望值变味。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from api.clients.pg import PgDatabase, normalize_dsn
from api.ingest import jobs as job_store
from api.ingest.decide import SkipReason
from api.ingest.scan import (
    SamplesRootMissing,
    ScanInProgress,
    _acquire_scan_lock,
    get_document,
    scan_samples,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "db_migrate.py"

# 测试自己的命名空间：绝不碰真实样本的 doc_key（policy/travel-cn 等）。
# 6c6cb7a 起 doc_key 走拼音归一化（pypinyin 是硬依赖），但 SAMPLE_* 保留中文原名。
# 这两个常量曾经写死中文 key，重装依赖后 9 条用例连环红——期望值必须跟算法同源。
DOC_A = "itest/a-ban-fa"
DOC_B = "itest/b-ban-fa"
SAMPLE_A = "itest/a-办法-2025版.pdf"
SAMPLE_B = "itest/b-办法-2025版.pdf"


@pytest.fixture(scope="module")
def migrated(pg_dsn: str) -> str:
    proc = subprocess.run(  # noqa: S603 - 固定脚本路径
        [sys.executable, str(MIGRATE_SCRIPT), "--dsn", pg_dsn],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"db_migrate.py failed: {proc.stderr[-800:]}"
    with psycopg.connect(normalize_dsn(pg_dsn)) as conn:
        assert conn.execute("SELECT to_regclass('public.ingest_jobs')").fetchone()[0]
    return pg_dsn


@pytest.fixture
def db(migrated: str):
    database = PgDatabase(migrated, min_size=1, max_size=4, connect_timeout_s=3)
    database.open()
    try:
        yield database
    finally:
        database.close()


@pytest.fixture(autouse=True)
def _cleanup(pg_dsn: str):
    yield
    with psycopg.connect(normalize_dsn(pg_dsn), autocommit=True) as conn:
        # 顺序：切片 -> 版本 -> 任务 -> 文档（FK 是 RESTRICT）。
        conn.execute("DELETE FROM chunks WHERE doc_key LIKE 'itest/%'")
        conn.execute("DELETE FROM doc_versions WHERE doc_key LIKE 'itest/%'")
        conn.execute("DELETE FROM ingest_jobs WHERE doc_key LIKE 'itest/%'")
        conn.execute("DELETE FROM documents WHERE doc_key LIKE 'itest/%'")


@pytest.fixture
def samples(tmp_path: Path) -> Path:
    """两个样本文件的临时目录。

    文件名带年份/版本后缀，顺带验归一化在真路径上成立；放在 `itest/` 子目录下，
    让 doc_key 落在测试命名空间里（清理与断言都按 `itest/%` 走）。
    """
    root = tmp_path / "samples"
    (root / "itest").mkdir(parents=True)
    (root / SAMPLE_A).write_bytes(b"%PDF-1.4\n% A rev 1\n")
    (root / SAMPLE_B).write_bytes(b"%PDF-1.4\n% B rev 1\n")
    return root


def _counts(db: PgDatabase) -> dict[str, int]:
    row = db.fetch_one(
        """
        SELECT (SELECT count(*) FROM documents   WHERE doc_key LIKE 'itest/%') AS docs,
               (SELECT count(*) FROM doc_versions WHERE doc_key LIKE 'itest/%') AS versions,
               (SELECT count(*) FROM ingest_jobs WHERE doc_key LIKE 'itest/%') AS jobs
        """
    )
    assert row is not None
    return {k: int(v) for k, v in row.items()}


# --- 1. 冷扫 + 重复扫描 -------------------------------------------------------


def test_cold_scan_creates_document_version_and_job(
    db: PgDatabase, samples: Path
) -> None:
    report = scan_samples(db, samples, max_retries=3)

    assert report.counts == {"add": 2, "change": 0, "skip": 0, "fail": 0}
    assert {item.doc_key for item in report.added} == {DOC_A, DOC_B}

    counts = _counts(db)
    assert counts == {"docs": 2, "versions": 2, "jobs": 2}

    with db.session() as conn:
        doc = get_document(conn, DOC_A)
    assert doc is not None
    assert doc["filename"] == "a-办法-2025版.pdf"  # 展示名保留原文件名
    assert doc["status"] == "queued"  # 还没有 active 版本
    assert doc["active_version_id"] is None
    assert doc["pending_count"] == 1
    assert doc["version_count"] == 1

    # 版本是 pending（待转正），不是 active —— 翻牌是 A3 的事（§4.1）。
    row = db.fetch_one(
        "SELECT status, activated_at, file_hash FROM doc_versions WHERE doc_key = %s",
        (DOC_A,),
    )
    assert row is not None
    assert row["status"] == "pending"
    assert row["activated_at"] is None
    assert len(row["file_hash"]) == 64

    job = db.fetch_one(
        "SELECT status, retry_count, version_id FROM ingest_jobs WHERE doc_key = %s",
        (DOC_A,),
    )
    assert job is not None
    assert (job["status"], job["retry_count"]) == ("queued", 0)
    assert job["version_id"] is not None


def test_rescan_skips_everything_and_writes_nothing(
    db: PgDatabase, samples: Path
) -> None:
    """验收第一条：重复扫描跳过。

    光看计数不够——跳过必须是"真的没写库"，所以版本数与任务数也要一模一样。
    """
    scan_samples(db, samples)
    before = _counts(db)

    report = scan_samples(db, samples)

    assert report.counts == {"add": 0, "change": 0, "skip": 2, "fail": 0}
    assert report.as_dict()["reasons"] == {SkipReason.UNCHANGED.value: 2}
    assert _counts(db) == before


def test_rescan_after_version_failed_reports_the_reason(
    db: PgDatabase, samples: Path
) -> None:
    """同内容但上次解析失败：跳过，但原因码必须是 version_failed。

    §2.2 里 failed 是终态，同一内容不自动重解析；报告里混成 unchanged 的话，
    运维只会看到"skipped 一直很大"而看不出这个文件一直失败。
    """
    scan_samples(db, samples)
    db.execute("UPDATE doc_versions SET status = 'failed' WHERE doc_key = %s", (DOC_A,))
    # failed 是版本终态，任务那条也标成 failed（进度口径一致）。
    db.execute(
        "UPDATE ingest_jobs SET status = 'failed', started_at = now(), finished_at = now(),"
        " error = 'parse_failed: synthetic' WHERE doc_key = %s",
        (DOC_A,),
    )

    report = scan_samples(db, samples)

    assert report.counts == {"add": 0, "change": 0, "skip": 2, "fail": 0}
    assert report.as_dict()["reasons"] == {
        SkipReason.UNCHANGED.value: 1,
        SkipReason.VERSION_FAILED.value: 1,
    }
    failed_item = next(
        i for i in report.skipped if i.reason is SkipReason.VERSION_FAILED
    )
    assert failed_item.doc_key == DOC_A


def test_content_revert_to_deprecated_version_is_reported(
    db: PgDatabase, samples: Path
) -> None:
    """内容回退到已下线版本：不能建新版本（唯一约束会拒），要报 content_revert。

    这时唯一的正解是走 §4.4 的回滚（把旧版翻回 active），扫描不替人做这个决定。
    """
    scan_samples(db, samples)
    # 把 DOC_A 的版本标成 deprecated（模拟它被某个更新版本取代过）。
    db.execute(
        "UPDATE doc_versions SET status = 'deprecated' WHERE doc_key = %s", (DOC_A,)
    )
    db.execute(
        "UPDATE ingest_jobs SET status = 'succeeded', started_at = now(),"
        " finished_at = now() WHERE doc_key = %s",
        (DOC_A,),
    )
    before = _counts(db)

    report = scan_samples(db, samples)

    assert report.as_dict()["reasons"] == {
        SkipReason.UNCHANGED.value: 1,
        SkipReason.CONTENT_REVERT.value: 1,
    }
    assert _counts(db) == before  # 一个版本都没新增


# --- 2. 并发扫描 ---------------------------------------------------------------


def test_concurrent_scan_raises_scan_in_progress(db: PgDatabase, samples: Path) -> None:
    """验收第一条：并发 scan 返回 409（库层异常，接口映射成 409）。

    锁挂在另一条连接上模拟"另一个进程正在扫"——同一进程里用两个 PgDatabase
    也行，但那样测的更像"同进程重入"，而真实并发是两个 api 副本/一个副本 + 手跑脚本。
    """
    blocker = psycopg.connect(normalize_dsn(db.dsn), autocommit=True)
    try:
        _acquire_scan_lock(blocker)
        with pytest.raises(ScanInProgress):
            scan_samples(db, samples)
    finally:
        blocker.execute("SELECT pg_advisory_unlock_all()")
        blocker.close()

    # 锁释放后能正常扫：证明 409 是锁导致的，不是别的地方坏了。
    assert scan_samples(db, samples).counts["add"] == 2


def test_scan_does_not_start_when_samples_dir_is_missing(
    db: PgDatabase, tmp_path: Path
) -> None:
    """目录不存在 != 目录为空。前者是挂载/路径配错，报 0 条会让运维以为一切正常。"""
    with pytest.raises(SamplesRootMissing):
        scan_samples(db, tmp_path / "nope")


def test_empty_samples_dir_is_a_clean_noop(db: PgDatabase, tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    report = scan_samples(db, root)
    assert report.counts == {"add": 0, "change": 0, "skip": 0, "fail": 0}
    assert report.items == []


# --- 3. 指纹变化：新 pending 版本，旧 active 不受影响 --------------------------


def test_fingerprint_change_creates_pending_version_and_leaves_active_alone(
    db: PgDatabase, samples: Path
) -> None:
    """验收第二条。这是整张卡最关键的一条，所以四处都断言：

    - 新版本出现且状态是 pending（等 A3 翻牌）；
    - 旧版本的 status / activated_at 一个字节都没变；
    - documents.status 仍是 active（有生效版本在服务）；
    - 旧版本名下的切片状态没被动过（切片可见性跟版本走，§2.3）。
    """
    scan_samples(db, samples)

    # 把 DOC_A 的第一个版本置成 active 并挂一条切片（模拟 A3 已经翻过牌）。
    old_version = db.fetch_one(
        "SELECT version_id FROM doc_versions WHERE doc_key = %s", (DOC_A,)
    )["version_id"]
    db.execute(
        "UPDATE doc_versions SET status = 'active', activated_at = now() "
        "WHERE version_id = %s",
        (old_version,),
    )
    db.execute("UPDATE documents SET status = 'active' WHERE doc_key = %s", (DOC_A,))
    db.execute(
        """
        INSERT INTO chunks (chunk_id, doc_key, version_id, chunk_type, page_num,
                            bbox, content_text, status)
        VALUES ('itest_ch_1', %s, %s, 'text', 1, '[[0.1,0.1,0.9,0.2]]'::jsonb,
                '住宿标准：每人每天 500 元', 'active')
        """,
        (DOC_A, old_version),
    )
    before_version = db.fetch_one(
        "SELECT status, activated_at, file_hash FROM doc_versions WHERE version_id = %s",
        (old_version,),
    )
    before_chunk = db.fetch_one(
        "SELECT status FROM chunks WHERE chunk_id = 'itest_ch_1'"
    )
    assert before_chunk == {"status": "active"}

    # 同一路径换内容 = 改版（路径不变、指纹变）。
    (samples / SAMPLE_A).write_bytes(b"%PDF-1.4\n% A rev 2\n")
    report = scan_samples(db, samples)

    assert report.counts == {"add": 0, "change": 1, "skip": 1, "fail": 0}
    changed = report.changed[0]
    assert changed.doc_key == DOC_A
    assert changed.version_id != old_version

    rows = db.fetch_all(
        "SELECT version_id, status, activated_at, superseded_by FROM doc_versions "
        "WHERE doc_key = %s ORDER BY created_at",
        (DOC_A,),
    )
    assert len(rows) == 2
    new = next(r for r in rows if r["version_id"] != old_version)
    assert new["status"] == "pending"
    assert new["activated_at"] is None

    # 旧 active 完全没动。
    after_version = db.fetch_one(
        "SELECT status, activated_at, file_hash FROM doc_versions WHERE version_id = %s",
        (old_version,),
    )
    assert after_version == before_version
    assert (
        db.fetch_one("SELECT status FROM chunks WHERE chunk_id = 'itest_ch_1'")
        == before_chunk
    )

    # 文档仍然 active：新版本待转正不影响旧版本继续服务。
    doc = db.fetch_one("SELECT status FROM documents WHERE doc_key = %s", (DOC_A,))
    assert doc["status"] == "active"

    # 新版本有一条 queued 任务；旧版本那条任务不受影响（它可能已经 succeeded）。
    jobs = db.fetch_all(
        "SELECT version_id, status FROM ingest_jobs WHERE doc_key = %s ORDER BY job_id",
        (DOC_A,),
    )
    assert {j["version_id"] for j in jobs} == {old_version, new["version_id"]}


def test_reverting_content_back_to_previous_fingerprint_is_not_a_new_version(
    db: PgDatabase, samples: Path
) -> None:
    """内容改回旧版（指纹撞上历史版本）：不能建第三个版本（唯一约束会拒），
    也不能当 unchanged 混过去——要报 content_revert，让人去走 §4.4 的回滚翻牌。

    这里必须先模拟一次真实的翻牌（rev1 -> deprecated，rev2 -> active），
    否则 rev1 还是 pending，两次扫描都只是 unchanged，测不到这个分支。
    """
    path = samples / SAMPLE_A
    rev1 = path.read_bytes()
    scan_samples(db, samples)
    rev1_version = db.fetch_one(
        "SELECT version_id FROM doc_versions WHERE doc_key = %s", (DOC_A,)
    )["version_id"]

    path.write_bytes(b"%PDF-1.4\n% A rev 2\n")
    scan_samples(db, samples)
    rev2_version = db.fetch_one(
        "SELECT version_id FROM doc_versions WHERE doc_key = %s AND version_id <> %s",
        (DOC_A, rev1_version),
    )["version_id"]

    # 模拟 A3 的翻牌事务：旧版下线、新版生效。
    db.execute(
        "UPDATE doc_versions SET status = 'deprecated', superseded_by = %s "
        "WHERE version_id = %s",
        (rev2_version, rev1_version),
    )
    db.execute(
        "UPDATE doc_versions SET status = 'active', activated_at = now() "
        "WHERE version_id = %s",
        (rev2_version,),
    )
    before = _counts(db)

    path.write_bytes(rev1)
    report = scan_samples(db, samples)

    assert report.counts == {"add": 0, "change": 0, "skip": 2, "fail": 0}
    assert report.as_dict()["reasons"] == {
        SkipReason.CONTENT_REVERT.value: 1,
        SkipReason.UNCHANGED.value: 1,
    }
    # 没有新版本，active 也没被自动翻回去——回滚是人的决定，不是扫描的。
    assert _counts(db) == before
    active = db.fetch_one(
        "SELECT version_id FROM doc_versions WHERE doc_key = %s AND status = 'active'",
        (DOC_A,),
    )
    assert active["version_id"] == rev2_version


# --- 4. 失败路径 ---------------------------------------------------------------


def test_unreadable_file_records_failed_job_without_touching_versions(
    db: PgDatabase, samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3.1「文件读不到 -> 记失败，不动已有版本」。

    用 monkeypatch 让其中一份文件的 `file_hash` 抛 PermissionError，而不是 chmod
    0o000：测试在 root 容器里跑，root 能读 000 文件，权限位在那里根本造不出
    「读不到」，用例会静默变成假绿。
    """
    import api.ingest.scan as scan_module  # noqa: PLC0415

    scan_samples(db, samples)
    before = _counts(db)
    real_hash = scan_module.file_hash

    def _unreadable(path, **kwargs):
        if path.name == Path(SAMPLE_A).name:
            raise PermissionError(13, "Permission denied", str(path))
        return real_hash(path, **kwargs)

    monkeypatch.setattr(scan_module, "file_hash", _unreadable)
    report = scan_samples(db, samples)

    assert report.counts["fail"] == 1
    failed = report.failed[0]
    assert failed.reason is SkipReason.UNREADABLE
    assert failed.error and "PermissionError" in failed.error

    # 已经有版本/任务，不该因为一次读失败而改动它们；只多一条 failed 任务。
    after = _counts(db)
    assert after["versions"] == before["versions"]
    assert after["docs"] == before["docs"]
    assert after["jobs"] == before["jobs"] + 1

    job = db.fetch_one(
        "SELECT status, error, version_id FROM ingest_jobs WHERE doc_key = %s "
        "ORDER BY job_id DESC LIMIT 1",
        (DOC_A,),
    )
    assert job["status"] == "failed"
    assert job["version_id"] is None  # 没有版本可挂
    assert "unreadable" in job["error"]


def test_doc_key_collision_fails_both_files_and_keeps_others(
    db: PgDatabase, tmp_path: Path
) -> None:
    """§4.4「两份文档 doc_key 撞车 -> 拒绝入库，不改编号规则」。

    撞车的两份都判失败（不只后来那份）：谁先谁后取决于文件名排序，
    只拒后者会让结果随改名而变。其余文件照常入库——一个命名冲突不该废掉整轮。
    """
    root = tmp_path / "samples"
    (root / "itest").mkdir(parents=True)
    (root / "itest" / "制度-2025版.pdf").write_bytes(b"%PDF-1.4\n% one\n")
    (root / "itest" / "制度-2024版.pdf").write_bytes(b"%PDF-1.4\n% two\n")
    (root / "itest" / "其它.pdf").write_bytes(b"%PDF-1.4\n% three\n")

    report = scan_samples(db, root)

    assert report.counts == {"add": 1, "change": 0, "skip": 0, "fail": 2}
    assert report.as_dict()["reasons"] == {SkipReason.DOC_KEY_COLLISION.value: 2}
    assert {item.doc_key for item in report.added} == {"itest/qi-ta"}
    assert (
        db.fetch_one(
            "SELECT count(*) AS n FROM documents WHERE doc_key = %s", ("itest/zhi-du",)
        )["n"]
        == 0
    )


# --- 5. ingest_jobs 状态机落库 -------------------------------------------------


def test_job_lifecycle_with_retry_then_success(db: PgDatabase, samples: Path) -> None:
    """queued -> running -> (失败 x3) -> succeeded，四次之后成功。

    重试是同一行原地累加 retry_count，不是新插一行：否则"失败过几次"这个数
    会散在多行里，统计口径就说不清了。
    """
    scan_samples(db, samples)
    with db.session() as conn:
        job = job_store.claim_next_job(conn, doc_key=DOC_A)
        assert job is not None
        job_id = job["job_id"]
        conn.commit()

        for attempt in range(3):
            status, retries = job_store.fail_job(
                conn, job_id, f"boom {attempt}", max_retries=3
            )
            conn.commit()
            # 还有额度 -> 回到 queued，计数 +1
            assert status == job_store.JobStatus.QUEUED
            assert retries == attempt + 1
            claimed = job_store.claim_next_job(conn, doc_key=DOC_A)
            conn.commit()
            assert claimed is not None and claimed["job_id"] == job_id

        # 第 4 次失败：额度用完，终态 failed
        status, retries = job_store.fail_job(conn, job_id, "boom final", max_retries=3)
        conn.commit()
        assert status == job_store.JobStatus.FAILED
        assert retries == 3

        row = job_store.get_job(conn, job_id)
        assert row["status"] == "failed"
        assert row["finished_at"] is not None
        assert row["error"] == "boom final"

        # failed 之后不能再被 claim，也不能再 fail（幂等：不重复累加计数）。
        assert job_store.claim_next_job(conn, doc_key=DOC_A) is None
        with pytest.raises(job_store.IllegalTransition):
            job_store.fail_job(conn, job_id, "again", max_retries=3)
        conn.rollback()


def test_job_success_clears_previous_error(db: PgDatabase, samples: Path) -> None:
    scan_samples(db, samples)
    with db.session() as conn:
        job = job_store.claim_next_job(conn, doc_key=DOC_A)
        assert job is not None
        job_id = job["job_id"]
        conn.commit()

        job_store.fail_job(conn, job_id, "transient", max_retries=3)
        conn.commit()
        claimed = job_store.claim_next_job(conn, doc_key=DOC_A)
        assert claimed is not None
        conn.commit()

        job_store.succeed_job(conn, job_id)
        conn.commit()
        row = job_store.get_job(conn, job_id)
        assert row["status"] == "succeeded"
        # 成功行不能留着上次的失败原因：排障的人会以为现在还在失败。
        assert row["error"] is None
        assert row["finished_at"] is not None


def test_cancel_only_applies_to_open_jobs(db: PgDatabase, samples: Path) -> None:
    scan_samples(db, samples)
    with db.session() as conn:
        # 必须按 DOC_A 定向领取：全局 claim 会领到库里别的（真实）queued job，
        # 把别人的摄取任务判成 canceled——测试跑一次就污染一次生产队列。
        job = job_store.claim_next_job(conn, doc_key=DOC_A)
        assert job is not None
        job_id = job["job_id"]
        conn.commit()

        job_store.cancel_job(conn, job_id)  # running 也能取消
        conn.commit()
        assert job_store.get_job(conn, job_id)["status"] == "canceled"

        with pytest.raises(job_store.IllegalTransition):
            job_store.cancel_job(conn, job_id)  # 终态不能再取消
        conn.rollback()


def test_open_job_count_tracks_queue(db: PgDatabase, samples: Path) -> None:
    with db.session() as conn:
        assert job_store.open_job_count(conn, doc_key=DOC_A) == 0
    scan_samples(db, samples)
    with db.session() as conn:
        assert job_store.open_job_count(conn, doc_key=DOC_A) == 1
        assert job_store.open_job_count(conn, doc_key=DOC_B) == 1
        assert job_store.open_job_count(conn) >= 2  # 全局至少包含这两条


# --- 6. 002 迁移的约束真的拦得住 -----------------------------------------------


UNDEFINED_STATUS_SQL = (
    "INSERT INTO ingest_jobs (doc_key, status) VALUES ('itest/x', 'done')"
)
RUNNING_WITHOUT_STARTED_SQL = (
    "INSERT INTO ingest_jobs (doc_key, status) VALUES ('itest/x', 'running')"
)
SUCCEEDED_WITHOUT_FINISHED_SQL = (
    "INSERT INTO ingest_jobs (doc_key, status, started_at) "
    "VALUES ('itest/x', 'succeeded', now())"
)
FAILED_WITHOUT_ERROR_SQL = "INSERT INTO ingest_jobs (doc_key, status, error) VALUES ('itest/x', 'failed', NULL)"
BAD_ROWS = {
    "undefined_status": UNDEFINED_STATUS_SQL,
    "running_without_started_at": RUNNING_WITHOUT_STARTED_SQL,
    "succeeded_without_finished_at": SUCCEEDED_WITHOUT_FINISHED_SQL,
    "failed_without_error": FAILED_WITHOUT_ERROR_SQL,
}


@pytest.mark.parametrize("case", list(BAD_ROWS))
def test_002_constraints_reject_bad_rows(pg_dsn: str, case: str) -> None:
    """每条 CHECK 都喂一次坏数据。没红过的约束和被删掉是一回事。"""
    with psycopg.connect(normalize_dsn(pg_dsn), autocommit=True) as conn:
        with pytest.raises(psycopg.errors.IntegrityError):
            conn.execute(BAD_ROWS[case])


def test_one_open_job_per_version(pg_dsn: str) -> None:
    """部分唯一索引：同一版本同时只能有一个未结束任务。

    重复扫描靠指纹判重已经不会入队两次，这条是兜底——并发扫描、重放、worker
    重投都不该让同一版本被解析两遍（两遍会写出两份切片）。
    """
    dsn = normalize_dsn(pg_dsn)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO documents (doc_key, filename) VALUES ('itest/dup', 'd.pdf')"
        )
        conn.execute(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash) "
            "VALUES ('itest/dup#v1', 'itest/dup', %s)",
            ("c" * 64,),
        )
        conn.execute(
            "INSERT INTO ingest_jobs (doc_key, version_id) VALUES ('itest/dup', 'itest/dup#v1')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO ingest_jobs (doc_key, version_id) "
                "VALUES ('itest/dup', 'itest/dup#v1')"
            )
        # 前一条结束后，同一版本可以再入队（重试/重跑）。
        conn.execute(
            "UPDATE ingest_jobs SET status = 'succeeded', started_at = now(),"
            " finished_at = now() WHERE version_id = 'itest/dup#v1'"
        )
        conn.execute(
            "INSERT INTO ingest_jobs (doc_key, version_id) VALUES ('itest/dup', 'itest/dup#v1')"
        )
        conn.execute("DELETE FROM ingest_jobs WHERE doc_key = 'itest/dup'")
        conn.execute("DELETE FROM doc_versions WHERE doc_key = 'itest/dup'")
        conn.execute("DELETE FROM documents WHERE doc_key = 'itest/dup'")


def test_version_id_is_derived_so_same_content_cannot_get_two_versions(
    db: PgDatabase, samples: Path
) -> None:
    """同内容 -> 同 version_id，加上 `UNIQUE (doc_key, file_hash)`：
    两条独立的约束都指向"同一内容只有一个版本行"。"""
    scan_samples(db, samples)
    path = samples / SAMPLE_A
    original = path.read_bytes()
    path.write_bytes(original + b"\n% touch\n")
    scan_samples(db, samples)
    path.write_bytes(original)
    report = scan_samples(db, samples)

    assert report.counts["fail"] == 0
    assert _counts(db)["versions"] == 3  # A x2 + B x1


@pytest.fixture(autouse=True)
def _has_chmod(tmp_path: Path) -> None:
    """在读不到文件那条用例里改权限位；Windows 挂载盘上 chmod 不生效，跳过更诚实。"""
    probe = tmp_path / "probe"
    probe.write_text("x")
    probe.chmod(0o000)
    readable = probe.stat().st_mode & 0o777
    probe.chmod(0o644)
    if readable != 0o000:
        pytest.skip(
            "filesystem ignores permission bits (chmod cannot make a file unreadable)"
        )


def test_samples_are_clean_after_the_suite(pg_dsn: str) -> None:
    """本测试套件不许在库里留下 itest/ 前缀之外的东西。

    留脏数据最典型的后果是"下一张卡（A2/A3）跑起来发现版本数对不上"，
    而那时已经很难判断是谁写的。
    """
    with psycopg.connect(normalize_dsn(pg_dsn)) as conn:
        stray = conn.execute(
            "SELECT count(*) FROM doc_versions WHERE doc_key LIKE 'itest/%'"
        ).fetchone()[0]
    assert stray == 0
