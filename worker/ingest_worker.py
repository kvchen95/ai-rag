#!/usr/bin/env python3
"""ai-rag 摄取 worker：把 `ingest_jobs` 从 queued 跑到 succeeded（详细设计.md §3.1）。

链路是 claim -> 定位样本 -> parse_document -> 批量 INSERT chunks -> succeed_job。

为什么没有 index 这一步
----------------------

这里只产出 **pending** 切片，不写 Qdrant。写点、翻牌、同步是 A3 `promote()` 的事：
Qdrant 没有事务，把写点和 PG 提交绑在同一个 worker 事务里，网络一抖就会出现
"PG 说成功、点没写进去"的隐性漂移（见 `api/ingest/pipeline.py` 的注释）。
worker 的角色是把材料变成可检索**候选**， promote 再一次性让它可见。

三条硬规矩
----------

1. **一条 job 一个短事务**：claim 自己提交（长解析不能占着事务），parse 在库外，
   persist + succeed 同一个事务（切片和"succeed"要么一起成，要么一起回滚）。
2. **幂等**：同一 version_id 重跑先删旧切片。chunk_id 是确定性短码，删了再插
   保证条数不翻倍；已 succeeded 的 job 不会被 `claim_next_job` 再领出来。
3. **失败分类**：样本找不到 / 版本对不上 / 权限规则没配 = 确定性失败，直接
   terminal_fail（重试没有意义）；解析或落库异常 = fail_job（还有重试额度）。

ACL 从样本路径规则来（`acl_for_sample`），绝不传 `Acl()`：空权限会被 A2 的
六项校验挡下，而 `is_public=True` 等于把投研报告全公司放行——两者都不是
"开发期图省事"能接受的默认值。
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.clients.pg import PgDatabase  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.ingest import jobs as job_store  # noqa: E402
from api.ingest.fingerprint import (  # noqa: E402
    DocKeyNeedsPinyin,
    doc_key_for,
    file_hash,
    iter_sample_paths,
    version_id_for,
)
from api.observability import configure_logging, get_logger, log_event, log_exception  # noqa: E402
from api.parse.chunking import Acl  # noqa: E402
from api.parse.pipeline import MissingAcl, ParseOutcome, parse_document  # noqa: E402

logger = get_logger("worker.ingest")

# 与 scripts/verify_parse_pipeline.py:91 / api/tests/test_parse_persist_integration.py
# 同一份落库形状（含 004 迁移的 table_cells / notes）。
INSERT_SQL = """
INSERT INTO chunks (
    chunk_id, doc_key, version_id, chunk_type, parent_id, page_num, bbox,
    image_path, content_text, table_html, table_md, table_cells, notes,
    effective_date, effective_year,
    date_confidence, is_public, allowed_depts, allowed_roles, token_count, status
) VALUES (
    %(chunk_id)s, %(doc_key)s, %(version_id)s, %(chunk_type)s, %(parent_id)s,
    %(page_num)s, %(bbox)s::jsonb, %(image_path)s, %(content_text)s,
    %(table_html)s, %(table_md)s, %(table_cells)s::jsonb, %(notes)s,
    %(effective_date)s, %(effective_year)s,
    %(date_confidence)s, %(is_public)s, %(allowed_depts)s, %(allowed_roles)s,
    %(token_count)s, 'pending'
)
"""

# 幂等的落点：重跑同一个 version_id 时先删旧切片，再插新的。
DELETE_VERSION_SQL = "DELETE FROM chunks WHERE version_id = %s"

# 开发期 ACL 口径：`eval/golden_50.jsonl` 全部是 `pdf_reports/*` 研报，检索身份
# 是 depts=('fin',) / roles=('staff',)（eval/run_eval.py 的 --dept/--role 默认值）。
# 所以研报给 fin+staff；原有 policy/hr/layout 样本保持 fin+hr+staff，避免把已有
# 的可检索材料变成"谁都看不到"。未列出的目录必须报错，不给隐式放行。
ACL_RULES: tuple[tuple[str, Acl], ...] = (
    ("pdf_reports/", Acl(is_public=False, allowed_depts=("fin",), allowed_roles=("staff",))),
    ("policy/", Acl(is_public=False, allowed_depts=("fin", "hr"), allowed_roles=("staff",))),
    ("hr/", Acl(is_public=False, allowed_depts=("fin", "hr"), allowed_roles=("staff",))),
    ("layout/", Acl(is_public=False, allowed_depts=("fin", "hr"), allowed_roles=("staff",))),
)


class SampleNotFound(LookupError):
    """样本目录里没有这条 job 对应的文件。"""


class VersionMismatch(RuntimeError):
    """文件还在，但字节变了（旧 version 的 job 不该拿新文件内容解析）。"""


class UnsupportedSamplePath(ValueError):
    """路径没有对应的 ACL 规则。宁可失败，也不猜一个权限。"""


class EmptyParse(RuntimeError):
    """解析成功但一片都没切出来：翻牌会据此拒绝，worker 也不该记 succeeded。"""


Parser = Callable[..., ParseOutcome]


@dataclass(slots=True)
class IngestOutcome:
    job_id: int
    doc_key: str
    version_id: str
    status: str  # succeeded / queued（还能重试）/ failed（终态）
    chunks: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "doc_key": self.doc_key,
            "version_id": self.version_id,
            "status": self.status,
            "chunks": self.chunks,
            "error": self.error,
        }


def acl_for_sample(rel_path: str) -> Acl:
    """按 `samples/` 下的相对路径选 ACL。显式、可枚举、不可隐式放行。"""
    normalized = rel_path.replace("\\", "/").lstrip("/")
    for prefix, acl in ACL_RULES:
        if normalized.startswith(prefix):
            return acl
    raise UnsupportedSamplePath(f"no ACL rule for sample path: {rel_path}")


def find_sample(samples_root: Path, doc_key: str, version_id: str | None) -> Path:
    """job 的 (doc_key, version_id) -> 样本文件。

    没有直接用 `list_samples()`：它会为**每个**样本算 doc_key，其中中文文件名
    需要 pypinyin；少装一个依赖就让所有 ASCII job 一起卡住。这里逐文件算，缺
    pypinyin 的文件只跳过自己，等 R1b 重建镜像后再扫到。
    """
    root = samples_root.resolve()
    if not version_id:
        raise VersionMismatch(f"{doc_key}: job has no version_id")
    for path in iter_sample_paths(root):
        rel = path.relative_to(root).as_posix()
        try:
            candidate = doc_key_for(rel)
        except DocKeyNeedsPinyin:
            continue
        if candidate != doc_key:
            continue
        expected = version_id_for(doc_key, file_hash(path))
        if expected != version_id:
            raise VersionMismatch(
                f"{doc_key}: job version {version_id} does not match on-disk {expected}"
            )
        return path
    raise SampleNotFound(f"{doc_key}: no sample under {root} matches this job")


def _insert_params(chunk: Any) -> dict[str, Any]:
    """`Chunk.as_row()` -> psycopg 能直接吃的 dict（jsonb 列显式序列化）。"""
    row = chunk.as_row()
    return {
        **row,
        "bbox": json.dumps(row["bbox"], ensure_ascii=False),
        "table_cells": (
            json.dumps(row["table_cells"], ensure_ascii=False)
            if row["table_cells"] is not None
            else None
        ),
    }


def _error_text(exc: BaseException, *, prefix: str = "") -> str:
    detail = str(exc).strip().replace("\n", " ")
    text = f"{prefix}{type(exc).__name__}: {detail}" if detail else f"{prefix}{type(exc).__name__}"
    return text[:500]


def _terminal_fail(pg: PgDatabase, job_id: int, error: str) -> None:
    with pg.connection() as conn:
        job_store.terminal_fail_job(conn, job_id, error)


def _fail(pg: PgDatabase, job_id: int, error: str, max_retries: int) -> str:
    """返回 job 的新状态（queued 还能试，failed 是终态）。"""
    with pg.connection() as conn:
        status, retry_count = job_store.fail_job(conn, job_id, error, max_retries=max_retries)
    return status.value


def process_one(
    pg: PgDatabase,
    *,
    samples_root: Path,
    doc_key: str | None = None,
    parser: Parser = parse_document,
    acl_for: Callable[[str], Acl] = acl_for_sample,
    figures_dir: Path | None = None,
    max_retries: int = 3,
) -> IngestOutcome | None:
    """领一条 queued job 并跑完整条链路。队列空返回 None。

    `doc_key` 可选：定向处理某份文档（测试隔离、单文档重跑），不传就是全局 FIFO。
    `parser` / `acl_for` 是依赖注入点：测试用假 parser 验编排，生产用真解析器。
    """
    with pg.connection() as conn:
        job = job_store.claim_next_job(conn, doc_key=doc_key)
    if job is None:
        return None

    job_id = int(job["job_id"])
    doc_key = str(job["doc_key"])
    version_id = str(job["version_id"]) if job["version_id"] is not None else ""

    try:
        path = find_sample(samples_root, doc_key, version_id)
        acl = acl_for(path.relative_to(samples_root.resolve()).as_posix())
        outcome = parser(
            path, doc_key=doc_key, version_id=version_id, acl=acl, figures_dir=figures_dir
        )
        chunks = list(outcome.chunks)
        if not chunks:
            raise EmptyParse(f"{doc_key}: parser produced no chunks")
    except (SampleNotFound, VersionMismatch, UnsupportedSamplePath, MissingAcl) as exc:
        error = _error_text(exc)
        _terminal_fail(pg, job_id, error)
        log_event(logger, "ingest_terminal_failed", job_id=job_id, doc_key=doc_key, error=error)
        return IngestOutcome(job_id, doc_key, version_id, "failed", 0, error)
    except Exception as exc:  # noqa: BLE001 - 解析异常要记进 job，不能让 worker 退出
        error = _error_text(exc, prefix="parse: ")
        status = _fail(pg, job_id, error, max_retries)
        log_exception(logger, "ingest_parse_failed", job_id=job_id, doc_key=doc_key)
        return IngestOutcome(job_id, doc_key, version_id, status, 0, error)

    try:
        with pg.connection() as conn:
            conn.execute(DELETE_VERSION_SQL, (version_id,))
            params = [_insert_params(chunk) for chunk in chunks]
            with conn.cursor() as cur:
                cur.executemany(INSERT_SQL, params)
            job_store.succeed_job(conn, job_id)
    except Exception as exc:  # noqa: BLE001 - 落库异常同样要有明确失败原因
        error = _error_text(exc, prefix="persist: ")
        status = _fail(pg, job_id, error, max_retries)
        log_exception(logger, "ingest_persist_failed", job_id=job_id, doc_key=doc_key)
        return IngestOutcome(job_id, doc_key, version_id, status, 0, error)

    log_event(
        logger, "ingest_job_succeeded", job_id=job_id, doc_key=doc_key,
        version_id=version_id, chunks=len(chunks),
    )
    return IngestOutcome(job_id, doc_key, version_id, "succeeded", len(chunks))


@dataclass(slots=True)
class RoundReport:
    """一轮消费的计数。failed 是终态失败，retried 是还能重试的失败。"""

    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "retried": self.retried,
            "ok": self.ok,
            "results": self.results,
        }


def run_once(
    pg: PgDatabase,
    *,
    samples_root: Path,
    doc_key: str | None = None,
    max_jobs: int = 500,
    parser: Parser = parse_document,
    acl_for: Callable[[str], Acl] = acl_for_sample,
    figures_dir: Path | None = None,
    max_retries: int = 3,
) -> RoundReport:
    """消费到队列空或达到 max_jobs。上限防止"失败后立刻回 queued"把单轮跑成死循环。"""
    report = RoundReport()
    for _ in range(max_jobs):
        outcome = process_one(
            pg,
            samples_root=samples_root,
            doc_key=doc_key,
            parser=parser,
            acl_for=acl_for,
            figures_dir=figures_dir,
            max_retries=max_retries,
        )
        if outcome is None:
            break
        report.processed += 1
        if outcome.status == "succeeded":
            report.succeeded += 1
        elif outcome.status == "failed":
            report.failed += 1
        else:
            report.retried += 1
        report.results.append(outcome.as_dict())
    return report


STOP = False


def _handle_stop(signum: int, _frame: object) -> None:
    global STOP
    logger.info("received signal %s, shutting down after the current round", signum)
    STOP = True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="consume ingest_jobs: parse -> persist -> succeed")
    ap.add_argument("--once", action="store_true", help="drain the queue once, then exit")
    ap.add_argument("--interval", type=float, default=None, help="poll interval in loop mode")
    ap.add_argument("--max-jobs", type=int, default=500, help="cap for one round")
    ap.add_argument("--doc-key", help="only consume jobs for this doc_key")
    ap.add_argument("--samples-dir", help="override INGEST_SAMPLES_DIR")
    ap.add_argument("--figures-dir", help="override FIGURES_DIR")
    ap.add_argument("--json", action="store_true", help="print the round report as JSON")
    args = ap.parse_args(argv)

    settings = get_settings()
    configure_logging(
        level=settings.log_level, service=f"{settings.service_name}-ingest-worker",
        env=settings.app_env,
    )
    if args.json:
        # 机器可读输出独占 stdout：结构化日志挪到 stderr，避免 JSON 里混进日志行。
        for handler in logging.getLogger().handlers:
            if getattr(handler, "_ai_rag_json", False) and isinstance(
                handler, logging.StreamHandler
            ):
                handler.setStream(sys.stderr)
    interval = args.interval if args.interval is not None else settings.ingest_poll_interval_s
    samples_root = Path(args.samples_dir or settings.ingest_samples_dir)
    figures_dir = Path(args.figures_dir or settings.figures_dir)
    max_retries = settings.ingest_max_retries

    pg = PgDatabase(
        settings.database_url,
        min_size=1,
        max_size=4,
        connect_timeout_s=settings.db_connect_timeout_s,
        application_name="ai-rag-ingest-worker",
    )
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    ready = Path("/tmp/ingest-worker.ready")
    if not args.once:
        try:
            ready.write_text("ready\n", encoding="utf-8")
        except OSError:
            log_exception(logger, "ingest_worker_ready_file_failed")
    pg.open()
    log_event(
        logger, "ingest_worker_started", once=args.once, interval_s=interval,
        samples_dir=str(samples_root),
    )
    try:
        while True:
            try:
                report = run_once(
                    pg,
                    samples_root=samples_root,
                    doc_key=args.doc_key,
                    max_jobs=args.max_jobs,
                    figures_dir=figures_dir,
                    max_retries=max_retries,
                )
            except Exception:  # noqa: BLE001 - 单轮失败不能把常驻 worker 弄死
                log_exception(logger, "ingest_round_failed")
                if args.once:
                    return 2
                report = RoundReport()

            if args.once:
                if args.json:
                    print(json.dumps(report.as_dict(), ensure_ascii=False))
                else:
                    print(
                        f"ingest-once: processed={report.processed} succeeded={report.succeeded} "
                        f"failed={report.failed} retried={report.retried}"
                    )
                return 0 if report.ok else 1

            if report.processed:
                log_event(logger, "ingest_round", **{
                    key: getattr(report, key)
                    for key in ("processed", "succeeded", "failed", "retried")
                })
            slept = 0.0
            while slept < interval and not STOP:
                time.sleep(min(1.0, interval - slept))
                slept += 1.0
            if STOP:
                break
    finally:
        pg.close()
        if not args.once:
            try:
                ready.unlink(missing_ok=True)
            except OSError:
                pass
        log_event(logger, "ingest_worker_stopped")
    return 0


__all__ = [
    "IngestOutcome",
    "RoundReport",
    "SampleNotFound",
    "UnsupportedSamplePath",
    "VersionMismatch",
    "acl_for_sample",
    "find_sample",
    "main",
    "process_one",
    "run_once",
]


if __name__ == "__main__":
    sys.exit(main())
