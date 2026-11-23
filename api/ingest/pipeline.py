"""A3 的一键流水线：把"新版本（pending）"变成"唯一生效版本 + 向量库同步完成"。

四步，每步都能单独跑（脚本里也是分开的子命令）：

    1. index   把该版本的切片写进 Qdrant（dense + bm25 + payload）
    2. flip    同事务翻牌（旧版 deprecated、新版 active、切片状态同步、写期望状态）
    3. drain   消费期望状态（改 payload.status / 删点，失败退避重试）
    4. verify  两边分布对账（以 PG 为准），返回是否一致

为什么 index 在 flip **之前**
-----------------------------

§4.1 的时序图是"翻牌 -> 按 doc_key 批量改 payload 状态"，隐含了"点已经在库里"。
反过来（先翻牌、再写点）会在两步之间留下一个窗口：版本已 active，但向量库里一条点
都没有——检索侧 `status=active` 过滤拿不到任何东西，线上表现为"刚上线的版本问不出来"。
先写点时它们的 payload.status 是 `pending`（切片本来就是 pending），检索侧看不到，
所以那个窗口是安全的：**点先齐、再一次性变可见**。

`flip` 的空版本保护（`EmptyVersion`）与这里的顺序配合：没有切片的版本翻不过去，
于是"翻牌把服务翻空"这件事在两道门上都过不去。

一个失败不吞掉后一步
--------------------

index 成功、flip 失败（比如版本已被别人翻过）时，点已经写进去了但状态是 pending
——不可检索、也不算错，下次 flip 成功就用上了。所以这里把三步的**结果都返回**，
由调用方决定要不要告警，而不是"第一步失败就整个 raise"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg_pool import ConnectionPool

from api.ingest.flip import FlipError, FlipResult, flip
from api.ingest.reconcile import ReconcileReport, reconcile
from api.ingest.sync import DrainReport, drain
from api.ingest.vectors import Embedder, IndexReport, QdrantVectorStore, index_version
from api.observability import get_logger, log_event, log_exception

logger = get_logger("api.ingest.pipeline")


@dataclass(slots=True)
class PromoteResult:
    version_id: str
    index: IndexReport | None = None
    flip: FlipResult | None = None
    drain: DrainReport | None = None
    reconcile: ReconcileReport | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.error is not None:
            return False
        if self.flip is None:
            return False
        if self.drain is not None and not self.drain.ok:
            return False
        if self.reconcile is not None and self.reconcile.remaining:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "ok": self.ok,
            "error": self.error,
            "index": self.index.as_dict() if self.index else None,
            "flip": self.flip.as_dict() if self.flip else None,
            "drain": self.drain.as_dict() if self.drain else None,
            "reconcile": self.reconcile.as_dict() if self.reconcile else None,
        }


def promote(
    pool: ConnectionPool[Any],
    store: QdrantVectorStore,
    embedder: Embedder,
    version_id: str,
    *,
    sync_max_attempts: int = 4,
    sync_backoff_s: float = 1.0,
    batch_size: int | None = None,
    reconcile_after: bool = True,
) -> PromoteResult:
    """index -> flip -> drain -> reconcile。

    **前置条件不满足时抛 FlipError 的子类**（VersionNotFound / EmptyVersion /
    NotPending），不吞成 `error` 字段：这三种情况的 HTTP 语义完全不同（404 / 422 /
    409），只有让调用方拿到异常类型才映射得对。实测踩过：全吞成 `error` + 207，
    接口层就分不出"没有这个版本"和"已翻过"。

    其余失败（编码服务挂了、Qdrant 拒收、同步没完成）不抛：它们发生时点不同，
    但共同点是"系统性问题、重试或对账能救"，所以全部留在返回值里，由调用方决定
    告警口径。
    """
    result = PromoteResult(version_id=version_id)

    # 1) 写点。在**一个事务**里：切片行 → 点 → 回填 embedding_dense 一起提交。
    try:
        with pool.connection() as conn:
            result.index = index_version(
                conn, store, embedder, version_id, batch_size=batch_size
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log_exception(logger, "promote_index_failed", version_id=version_id)
        result.error = f"index: {type(exc).__name__}: {exc}"
        return result

    # 2) 翻牌（自己的事务；前置条件不满足直接抛给调用方，见 docstring）
    try:
        with pool.connection() as conn:
            result.flip = flip(conn, version_id)
            conn.commit()
    except FlipError:
        raise
    except Exception as exc:  # noqa: BLE001
        log_exception(logger, "promote_flip_failed", version_id=version_id)
        result.error = f"flip: {type(exc).__name__}: {exc}"
        return result

    # 3) 同步（把期望状态推给 Qdrant）
    result.drain = drain(
        pool,
        store,
        max_attempts=sync_max_attempts,
        backoff_s=sync_backoff_s,
    )

    # 4) 对账（默认做：它是"确实一致"的唯一证据，比信任 drain 的返回值可靠）
    if reconcile_after:
        try:
            with pool.connection() as conn:
                result.reconcile = reconcile(conn, store, doc_key=result.flip.doc_key)
                conn.commit()
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "promote_reconcile_failed", version_id=version_id)
            result.error = f"reconcile: {type(exc).__name__}: {exc}"

    log_event(
        logger,
        "promote_done",
        version_id=version_id,
        ok=result.ok,
        drift=result.reconcile.by_kind() if result.reconcile else None,
    )
    return result


__all__ = ["PromoteResult", "promote"]
