"""摄取 worker 的链路测试（真 PG + 假 parser）。.

为什么用真 PG：`claim_next_job` 的 `FOR UPDATE SKIP LOCKED`、chunks 的
jsonb/text[] 落库形状、`succeed_job` 的事务边界都是 PG 行为，sqlite 替身
测不出“能不能真的入库”。

为什么 parser 用假的：这里验的是 worker 的编排（领任务 -> 解析 -> 落库 ->
翻状态），解析质量归 A2 的用例；假 parser 还能把“ACL 到底传了什么”变成
可断言的值。

测试命名空间固定 `pdf_reports/demo`，结束即删，绝不碰真实研报样本的行。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from api.clients.pg import PgDatabase, normalize_dsn
from api.ingest import jobs as job_store
from api.ingest.fingerprint import file_hash, version_id_for
from api.parse.chunking import Acl, Chunk
from api.tests.conftest import BASE_ENV

DOC_KEY = "pdf_reports/demo"
EXPECTED_ACL = (False, ("fin",), ("staff",))


def _make_sample(root: Path) -> Path:
    """Run _make_sample."""
    path = root / "pdf_reports" / "demo.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n% ingest worker test fixture\n")
    return path


def _enqueue(
    conn: psycopg.Connection[object],
    path: Path,
    version_id: str,
    *,
    doc_key: str = DOC_KEY,
) -> int:
    """Run _enqueue."""
    conn.execute(
        "INSERT INTO documents (doc_key, filename, status) "
        "VALUES (%s, %s, 'queued')",
        (doc_key, path.name),
    )
    conn.execute(
        "INSERT INTO doc_versions (version_id, doc_key, file_hash, status) "
        "VALUES (%s, %s, %s, 'pending')",
        (version_id, doc_key, file_hash(path)),
    )
    job_id = job_store.enqueue_job(conn, doc_key=doc_key, version_id=version_id)
    conn.commit()
    return job_id


def _cleanup(
    conn: psycopg.Connection[object], version_id: str, *, doc_key: str = DOC_KEY
) -> None:
    """Run _cleanup."""
    conn.execute("DELETE FROM chunks WHERE version_id = %s", (version_id,))
    conn.execute("DELETE FROM ingest_jobs WHERE doc_key = %s", (doc_key,))
    conn.execute(
        "DELETE FROM doc_versions WHERE version_id = %s", (version_id,)
    )
    conn.execute("DELETE FROM documents WHERE doc_key = %s", (doc_key,))
    conn.commit()


def _fake_parser(
    seen: dict[str, Any] | None = None, *, raises: Exception | None = None
):
    """返回一份带齐六项随行信息的假解析结果。.

    ACL 原样带入 chunk 便于落库断言。
    """

    def parser(path, *, doc_key, version_id, acl, **kwargs):
        """Run parser."""
        if seen is not None:
            seen["acl"] = (acl.is_public, acl.allowed_depts, acl.allowed_roles)
            seen["path"] = path
        if raises is not None:
            raise raises
        chunk = Chunk(
            chunk_id="ch_itest_ingest_tracer",
            doc_key=doc_key,
            version_id=version_id,
            chunk_type="text",
            page_num=1,
            bbox=[[0.1, 0.1, 0.9, 0.2]],
            content_text="中芯国际：晶圆制造龙头企业。",
            is_public=acl.is_public,
            allowed_depts=acl.allowed_depts,
            allowed_roles=acl.allowed_roles,
            date_confidence="inferred",
        )
        return SimpleNamespace(
            doc=SimpleNamespace(doc_key=doc_key, page_count=1),
            chunks=[chunk],
            warnings=[],
            rejected_figures=[],
        )

    return parser


@pytest.mark.integration
def test_claim_parse_persist_succeed_writes_chunks_and_succeeds_job(
    pg_dsn: str, tmp_path: Path
) -> None:
    """一条 queued job 跑完后 chunks 真在库里，job 变 succeeded。.

    权限是显式传进去的那份。这条是 tracer bullet：整个 worker 的第一个
    端到端切片。它红了说明链路根本没接上，后面所有“幂等 / 失败重试”的
    用例都只是在给不存在的功能加细节。
    """
    from worker.ingest_worker import process_one  # noqa: PLC0415

    dsn = normalize_dsn(pg_dsn)
    samples_root = tmp_path / "samples"
    path = _make_sample(samples_root)
    version_id = version_id_for(DOC_KEY, file_hash(path))

    conn = psycopg.connect(dsn)
    try:
        job_id = _enqueue(conn, path, version_id)
        seen: dict[str, Any] = {}

        pg = PgDatabase(dsn)
        pg.open()
        try:
            result = process_one(
                pg,
                samples_root=samples_root,
                doc_key=DOC_KEY,
                parser=_fake_parser(seen),  # type: ignore[arg-type]
            )
        finally:
            pg.close()

        assert result is not None
        assert (result.job_id, result.status, result.chunks) == (
            job_id,
            "succeeded",
            1,
        )
        assert seen["path"] == path
        assert (
            seen["acl"] == EXPECTED_ACL
        ), "ACL 必须从样本路径规则来，不能是 Acl() 空权限"

        row = conn.execute(
            "SELECT status, error FROM ingest_jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
        assert (row[0], row[1]) == ("succeeded", None)

        stored = conn.execute(
            "SELECT version_id, status, is_public, allowed_depts, "
            "allowed_roles FROM chunks WHERE version_id = %s",
            (version_id,),
        ).fetchall()
        assert len(stored) == 1
        assert stored[0][0] == version_id
        assert stored[0][2:] == (False, ["fin"], ["staff"])

        # worker 只产出 pending 切片，翻牌（active + Qdrant 点）是 A3 promote 的
        # 事。
        version_row = conn.execute(
            "SELECT status FROM doc_versions WHERE version_id = %s",
            (version_id,),
        ).fetchone()
        assert version_row[0] == "pending"
    finally:
        _cleanup(conn, version_id)
        conn.close()


@pytest.mark.integration
def test_rerun_same_version_does_not_duplicate_chunks(
    pg_dsn: str, tmp_path: Path
) -> None:
    """同一 version_id 再跑一轮，chunks 不能翻倍。.

    模拟重跑的方式是把 succeeded 就地退回 queued（002 的唯一索引只挡 open job，
    不挡人工/脚本重放）。没有按 version_id 先删老行时，第二次 INSERT 会撞主键
    或留下双份切片——两种症状这条用例都能抓到。
    """
    from worker.ingest_worker import process_one  # noqa: PLC0415

    dsn = normalize_dsn(pg_dsn)
    samples_root = tmp_path / "samples"
    path = _make_sample(samples_root)
    version_id = version_id_for(DOC_KEY, file_hash(path))

    conn = psycopg.connect(dsn)
    try:
        job_id = _enqueue(conn, path, version_id)
        pg = PgDatabase(dsn)
        pg.open()
        try:
            first = process_one(
                pg,
                samples_root=samples_root,
                doc_key=DOC_KEY,
                parser=_fake_parser(),  # type: ignore[arg-type]
            )
            conn.execute(
                "UPDATE ingest_jobs SET status = 'queued', started_at = NULL, "
                "finished_at = NULL WHERE job_id = %s",
                (job_id,),
            )
            conn.commit()
            second = process_one(
                pg,
                samples_root=samples_root,
                doc_key=DOC_KEY,
                parser=_fake_parser(),  # type: ignore[arg-type]
            )
        finally:
            pg.close()

        assert first is not None and second is not None
        assert (first.status, second.status) == ("succeeded", "succeeded")
        count = conn.execute(
            "SELECT count(*) FROM chunks WHERE version_id = %s", (version_id,)
        ).fetchone()[0]
        assert count == 1, f"重跑后 chunks 变成 {count} 条（应为 1）"
        job_status = conn.execute(
            "SELECT status FROM ingest_jobs WHERE job_id = %s", (job_id,)
        ).fetchone()[0]
        assert job_status == "succeeded"
    finally:
        _cleanup(conn, version_id)
        conn.close()


@pytest.mark.integration
def test_parse_failure_retries_three_times_then_terminal(
    pg_dsn: str, tmp_path: Path
) -> None:
    """解析异常走 fail_job：前三次回 queued 并涨 retry_count。.

    第四次进 failed 终态。
    """
    from worker.ingest_worker import process_one  # noqa: PLC0415

    dsn = normalize_dsn(pg_dsn)
    samples_root = tmp_path / "samples"
    path = _make_sample(samples_root)
    version_id = version_id_for(DOC_KEY, file_hash(path))
    boom = RuntimeError("boom in parser")

    conn = psycopg.connect(dsn)
    try:
        job_id = _enqueue(conn, path, version_id)
        pg = PgDatabase(dsn)
        pg.open()
        try:
            statuses = [
                result.status
                for result in (
                    process_one(
                        pg,
                        samples_root=samples_root,
                        doc_key=DOC_KEY,
                        parser=_fake_parser(raises=boom),  # type: ignore[arg-type]
                        max_retries=3,
                    )
                    for _ in range(4)
                )
            ]
        finally:
            pg.close()

        assert statuses == ["queued", "queued", "queued", "failed"]
        row = conn.execute(
            "SELECT status, retry_count, error FROM ingest_jobs "
            "WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row[0:2] == ("failed", 3)
        assert "parse:" in row[2] and "boom in parser" in row[2]
    finally:
        _cleanup(conn, version_id)
        conn.close()


@pytest.mark.integration
def test_missing_sample_terminal_fails_without_consuming_retry(
    pg_dsn: str, tmp_path: Path
) -> None:
    """样本文件不在目录里属于确定性失败。.

    直接 failed、retry_count 不涨、不再入队。
    """
    from worker.ingest_worker import process_one  # noqa: PLC0415

    dsn = normalize_dsn(pg_dsn)
    samples_root = tmp_path / "samples"  # 故意不放任何文件
    # 文件本身存在（扫描时读得到），但被挪出了 samples_root——worker 应该判确定性
    # 失败。
    source = tmp_path / "demo.pdf"
    source.write_bytes(b"%PDF-1.4\n% moved out of samples\n")
    version_id = version_id_for(DOC_KEY, file_hash(source))

    conn = psycopg.connect(dsn)
    try:
        job_id = _enqueue(conn, source, version_id)
        pg = PgDatabase(dsn)
        pg.open()
        try:
            first = process_one(
                pg,
                samples_root=samples_root,
                doc_key=DOC_KEY,
                parser=_fake_parser(),  # type: ignore[arg-type]
            )
            second = process_one(
                pg,
                samples_root=samples_root,
                doc_key=DOC_KEY,
                parser=_fake_parser(),  # type: ignore[arg-type]
            )
        finally:
            pg.close()

        assert first is not None and first.status == "failed"
        assert second is None, "确定性失败不该回到 queued 再被领一次"
        row = conn.execute(
            "SELECT status, retry_count, error FROM ingest_jobs "
            "WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        assert row[0:2] == ("failed", 0)
        assert "SampleNotFound" in row[2]
    finally:
        _cleanup(conn, version_id)
        conn.close()


def test_acl_rules_are_explicit_and_never_public() -> None:
    """每条路径规则都必须带非空部门与角色，且不是全放行。."""
    from worker.ingest_worker import acl_for_sample  # noqa: PLC0415

    for rel in (
        "pdf_reports/dong-fang-zheng-quan.pdf",
        "policy/travel-cn.pdf",
        "hr/leave-2025.pdf",
        "layout/prose-demo.pdf",
    ):
        acl = acl_for_sample(rel)
        assert not acl.is_public, rel
        assert acl.allowed_depts, rel
        assert acl.allowed_roles, rel
        assert not acl.is_empty(), rel


def test_acl_rule_refuses_unlisted_directory() -> None:
    """没配规则的目录必须报错——'猜一个权限'比拒绝入库危险。."""
    from worker.ingest_worker import (  # noqa: PLC0415
        UnsupportedSamplePath,
        acl_for_sample,
    )

    with pytest.raises(UnsupportedSamplePath):
        acl_for_sample("mystery/未分类.pdf")


def test_acl_for_pdf_reports_matches_eval_identity() -> None:
    """研报的部门口径与黄金集检索身份一致（depts=fin / roles=staff）。."""
    from worker.ingest_worker import acl_for_sample  # noqa: PLC0415

    acl = acl_for_sample("pdf_reports/zhong-xin-guo-ji.pdf")
    assert (acl.is_public, acl.allowed_depts, acl.allowed_roles) == EXPECTED_ACL
    assert acl == Acl(
        is_public=False, allowed_depts=("fin",), allowed_roles=("staff",)
    )


@pytest.mark.integration
def test_real_parser_persists_a_real_pdf(pg_dsn: str, tmp_path: Path) -> None:
    """假 parser 验编排，这条验真链路：真 PDF -> 真解析 -> 真落库。.

    用 A2 的 stdlib 版面夹具（`layout/prose-demo.pdf`，ASCII doc_key），不占用
    真实研报的 queued job，也不动 samples/ 里的任何文件。
    """
    from worker.ingest_worker import process_one  # noqa: PLC0415

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import make_layout_samples  # noqa: PLC0415

    dsn = normalize_dsn(pg_dsn)
    samples_root = tmp_path / "samples"
    path = samples_root / "layout" / "itest-prose-demo.pdf"
    path.parent.mkdir(parents=True)
    path.write_bytes(make_layout_samples.build("layout/prose-demo.pdf"))

    doc_key = "layout/itest-prose-demo"  # 测试命名空间，不撞 layout/prose-demo
    version_id = version_id_for(doc_key, file_hash(path))
    conn = psycopg.connect(dsn)
    try:
        _enqueue(conn, path, version_id, doc_key=doc_key)
        pg = PgDatabase(dsn)
        pg.open()
        try:
            result = process_one(pg, samples_root=samples_root, doc_key=doc_key)
        finally:
            pg.close()

        assert result is not None
        assert result.status == "succeeded"
        assert result.chunks > 0, "真 PDF 解析出 0 片：worker 不该记 succeeded"
        rows = conn.execute(
            "SELECT count(*) AS n, "
            "count(*) FILTER (WHERE allowed_depts = ARRAY['fin','hr']) "
            "AS with_acl FROM chunks WHERE version_id = %s",
            (version_id,),
        ).fetchone()
        assert rows[0] == result.chunks
        assert rows[1] == result.chunks, "ACL 必须逐片落库，不能只在内存里"
    finally:
        conn.rollback()  # 失败路径可能把事务留在 INERROR，先回滚再删测试数据
        _cleanup(conn, version_id, doc_key=doc_key)
        conn.close()


@pytest.mark.integration
def test_main_once_reports_json_and_exits_on_empty_queue(pg_dsn: str) -> None:
    """`--once --json` 是 `make ingest-once` 的底层入口。.

    空队列也必须正常退出。
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update({key: str(value) for key, value in BASE_ENV.items()})
    env["DATABASE_URL"] = pg_dsn
    env["PYTHONPATH"] = str(repo_root)

    proc = subprocess.run(  # noqa: S603 - 固定入口脚本
        [
            sys.executable,
            str(repo_root / "worker" / "ingest_worker.py"),
            "--once",
            "--json",
            "--doc-key",
            "pdf_reports/demo",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1000:]
    assert proc.stdout.strip(), f"stdout empty; stderr={proc.stderr[-2000:]}"
    payload = json.loads(proc.stdout)
    assert (payload["processed"], payload["succeeded"], payload["failed"]) == (
        0,
        0,
        0,
    )
    assert payload["ok"] is True
