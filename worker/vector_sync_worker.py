#!/usr/bin/env python3
"""ai-rag worker：把"期望状态"持续推给 Qdrant（详细设计.md §4.2）。

为什么必须有这个进程
--------------------

翻牌事务只写"向量库应该是什么样"（`vector_sync_tasks`），不发 HTTP——这是为了让
PG 的事务边界不被网络抖动绑住。代价是：**必须有东西去消费那些期望状态**，否则
翻牌在库里生效了、向量库永远不知道，检索侧会一直用旧状态。

也可以由运维手工 `make vector-sync`，但 §4.2 要求"翻牌后一分钟内必须同步完"。
手工不满足"一定发生"，所以这里起一个常驻循环。

为什么不用 cron / 不挂在 api 里
------------------------------

- 挂 api：每个 api 副本都会跑一轮，多副本时重复调用（虽然 drain 是幂等的，
  但会放大 Qdrant 的写压力）。worker 是单实例，天生合适。
- 用 cron：cron 的最小间隔是分钟级，且独立部署后拿不到同一份 Settings。
  worker 已经在 compose 里、已经有全部依赖，加一个循环比再拉一个进程省事。

循环的两件事
------------

1. **drain**：消费到期的期望状态（改 payload.status / 删点），失败按 §4.2 重试三次；
2. **对账**（默认每 10 轮做一次）：修复漏网的漂移。对账比 drain 贵（要滚动整个
   集合），所以不是每轮都做；但"漂移没人数才可怕"（§4.2），所以也不能不做。

两者都失败时**不退出**：Qdrant 抖一下就把 worker 弄死，容器重启后仍然抖，就成了
重启风暴。正确做法是记录 + 退避 + 继续。
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.clients.pg import PgDatabase  # noqa: E402
from api.config import get_settings  # noqa: E402
from api.ingest.reconcile import reconcile  # noqa: E402
from api.ingest.sync import drain  # noqa: E402
from api.ingest.vectors import QdrantVectorStore  # noqa: E402
from api.observability import configure_logging, get_logger, log_event, log_exception  # noqa: E402

logger = get_logger("worker.vector_sync")

STOP = False


def _handle_stop(signum: int, _frame: object) -> None:
    global STOP
    logger.info("received signal %s, shutting down after the current round", signum)
    STOP = True


def _tick(
    pg: PgDatabase,
    store: QdrantVectorStore,
    *,
    sync_max_attempts: int,
    backoff_s: float,
    reconcile_every: int,
    round_no: int,
) -> None:
    report = drain(pg.pool, store, max_attempts=sync_max_attempts, backoff_s=backoff_s)
    if report.processed:
        log_event(
            logger,
            "vector_sync_round",
            round=round_no,
            processed=report.processed,
            done=report.done,
            retried=report.retried,
            failed=report.failed,
        )
        if report.failed:
            # failed 已经进了漂移账（sync.record_drift），这里只是把水位打出来。
            log_event(logger, "vector_sync_exhausted", round=round_no, failed=report.failed)

    if reconcile_every and round_no % reconcile_every == 0:
        # 对账要一条连接做 PG 侧读取（Qdrant 调用也在里面），用短事务。
        with pg.connection() as conn:
            rec = reconcile(conn, store)
        if rec.drifts:
            log_event(
                logger,
                "vector_reconcile_round",
                round=round_no,
                drift_counts=rec.by_kind(),
                healed=len(rec.healed),
                remaining=len(rec.remaining),
            )
        # 未修完的差异是"需要人看"的信号，用 ERROR 级别打出来（对账已写进漂移账）。
        if rec.remaining:
            log_event(
                logger,
                "vector_reconcile_remaining",
                level=40,
                round=round_no,
                remaining=len(rec.remaining),
                kinds=sorted({d.kind for d in rec.remaining}),
            )


def main() -> int:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, service=f"{settings.service_name}-worker", env=settings.app_env
    )

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    # 口径全部来自 Settings（环境变量），不在 worker 里另写一份默认值。
    interval_s = float(settings.vector_sync_interval_s)
    reconcile_every = int(settings.vector_sync_reconcile_every)

    pg = PgDatabase(
        settings.database_url,
        min_size=1,
        max_size=4,
        connect_timeout_s=settings.db_connect_timeout_s,
        application_name="ai-rag-worker",
    )
    store = QdrantVectorStore(
        base_url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout_s=settings.qdrant_timeout_s,
    )

    # ready 文件：compose 的健康检查就等这个（沿用 D1 的约定）。
    ready = Path("/tmp/worker.ready")
    try:
        ready.write_text("ready\n", encoding="utf-8")
    except OSError:
        log_exception(logger, "worker_ready_file_failed")

    pg.open()
    log_event(
        logger,
        "vector_sync_worker_started",
        interval_s=interval_s,
        reconcile_every=reconcile_every,
        collection=settings.qdrant_collection,
    )

    round_no = 0
    try:
        while not STOP:
            round_no += 1
            try:
                _tick(
                    pg,
                    store,
                    sync_max_attempts=settings.vector_sync_max_attempts,
                    backoff_s=settings.vector_sync_backoff_s,
                    reconcile_every=reconcile_every,
                    round_no=round_no,
                )
            except Exception:  # noqa: BLE001 - 单轮失败不该把 worker 弄死
                # Qdrant 抖一下 -> 容器重启 -> 仍然抖 -> 重启风暴。所以记录后继续。
                log_exception(logger, "vector_sync_round_failed", round=round_no)

            # 分段 sleep：收到 SIGTERM 时最多等 1 秒就退出，而不是等满整个间隔。
            slept = 0.0
            while slept < interval_s and not STOP:
                time.sleep(min(1.0, interval_s - slept))
                slept += 1.0
    finally:
        store.close()
        pg.close()
        try:
            ready.unlink(missing_ok=True)
        except OSError:
            pass
        log_event(logger, "vector_sync_worker_stopped", rounds=round_no)
    return 0


if __name__ == "__main__":
    sys.exit(main())
