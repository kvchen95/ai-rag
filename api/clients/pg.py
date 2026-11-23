"""PostgreSQL 访问层。

PG 是唯一真相（详细设计.md 附录 B），所以这里的职责只有三件：
连接池、事务边界、探针。业务 SQL 写在各自的仓储模块里，不堆在这。

事务边界固定：`with pg.connection() as conn:` 正常退出提交、异常回滚。
调用方不要自己 commit，避免出现"一半提交一半没提交"。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from api.clients.base import Probe, short_error
from api.observability import get_logger, log_event

logger = get_logger("api.pg")


def normalize_dsn(dsn: str) -> str:
    """compose 里用的是 SQLAlchemy 风格 DSN（postgresql+psycopg://），
    psycopg 自己不认这个前缀，统一在这里剥掉，别让每个调用方各剥一次。"""
    dsn = dsn.strip()
    for prefix in ("postgresql+psycopg://", "postgres+psycopg://"):
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix) :]
    return dsn


def mask_dsn(dsn: str) -> str:
    """日志里只留 host/db，密码段打成 ***。"""
    head, _, tail = normalize_dsn(dsn).partition("://")
    if "@" in tail:
        creds, _, hostpart = tail.rpartition("@")
        user = creds.split(":", 1)[0]
        return f"{head}://{user}:***@{hostpart}"
    return f"{head}://{tail}"


class PgDatabase:
    """连接池 + 探针。open/close 由 lifespan 管，不在构造里连库。"""

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 8,
        connect_timeout_s: float = 3.0,
        pool_timeout_s: float = 5.0,
        application_name: str = "ai-rag-api",
    ) -> None:
        self.dsn = normalize_dsn(dsn)
        self._connect_timeout_s = connect_timeout_s
        self._pool = ConnectionPool(
            conninfo=self.dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=pool_timeout_s,
            kwargs={
                "connect_timeout": max(1, int(round(connect_timeout_s))),
                "application_name": application_name,
            },
            name="ai-rag-pg",
            open=False,
        )

    # --- 生命周期 ---

    def open(self) -> None:
        """wait=False：库还没起来时进程也要能启动，
        让 /healthz 去区分"我起来了但依赖没起来"。"""
        self._pool.open(wait=False)
        log_event(logger, "pg_pool_opened", dsn=mask_dsn(self.dsn))

    def close(self) -> None:
        self._pool.close()
        log_event(logger, "pg_pool_closed")

    # --- 事务 ---

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """一个事务。正常退出提交，异常回滚——不提供 autocommit 入口，
        免得有人在里面做两步写然后只提交了一半。"""
        with self._pool.connection() as conn:
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    @contextmanager
    def session(self) -> Iterator[psycopg.Connection[Any]]:
        """一条连接，**事务由调用方自己管**。

        只给"一个逻辑任务跨多个事务"的场景用（例如样本扫描：一个文件一个事务，
        整轮共用一个会话级 advisory lock）。`connection()` 的"退出即提交"在这种
        场景下会把半轮扫描提前提交掉，所以这里不替调用方 commit：
        调用方每处理完一个单元自己 commit/rollback。

        退出时如果还有未提交的事务，回滚掉——不静默提交别人的半成品。
        连接归还池前会 `reset`（psycopg_pool 默认行为），会话级锁在还池前必须
        已经显式释放，这一点由调用方保证（见 api/ingest/scan.py）。
        """
        with self._pool.connection() as conn:
            try:
                yield conn
            finally:
                if not conn.closed and conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    conn.rollback()

    # --- 查询 ---

    def fetch_one(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> dict[str, Any] | None:
        with self.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def fetch_all(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        with self.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        with self.connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    # --- 探针 ---

    def ping(self, *, timeout_s: float | None = None) -> Probe:
        timeout_s = timeout_s or self._connect_timeout_s
        started = time.perf_counter()
        try:
            with self._pool.connection(timeout=timeout_s) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    cur.execute("SHOW server_version")
                    version = cur.fetchone()
            latency = (time.perf_counter() - started) * 1000
            return Probe(
                ok=True,
                latency_ms=latency,
                extra={
                    "server_version": version[0] if version else None,
                    "pool": {
                        "size": self._pool.get_stats().get("pool_size"),
                        "idle": self._pool.get_stats().get("pool_available"),
                    },
                },
            )
        except Exception as exc:  # noqa: BLE001 - 探针只报错不抛
            latency = (time.perf_counter() - started) * 1000
            return Probe(ok=False, latency_ms=latency, error=short_error(exc))
