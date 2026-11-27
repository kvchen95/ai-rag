"""摄取接口（详细设计.md 附录 C.1 + §4 的转正/同步/对账/回收）。.

六个接口：

- `POST /api/v1/ingest/scan`——扫 samples/；失败 409（任务进行中）。
- `GET /api/v1/documents/{doc_key}`——查入库状态；失败 404。
- `POST /api/v1/ingest/versions/{version_id}/promote`——索引+翻牌+
  同步+对账；失败 404/409/422。
- `POST /api/v1/ingest/versions/{version_id}/rollback`——24h 内回滚；
  失败 404/409。
- `POST /api/v1/ingest/sync`——消费期望状态。
- `GET /api/v1/ingest/reconcile`——两边对账（`?dry_run=true`）。

后四个是 A3 的运维面：翻牌与同步**必须**能通过接口触发，否则运维只能
进容器敲脚本（那是"能跑"和"能运营"的区别）。它们都是同步阻塞的重活
（编码、HTTP 往返），一律 `asyncio.to_thread` 丢线程池，理由同扫描：
卡住事件循环会让 /healthz 探不到。

五条实现约束：

1. **扫描/翻牌都是同步阻塞的重活**，用 `asyncio.to_thread` 丢到线程池。
   否则一轮扫描会把事件循环卡住，连带 `/healthz` 都探不到——容器会被判
   unhealthy。psycopg 连接池是线程安全的，工作线程借连接不受影响。
2. **409 的判定点是"拿到锁没有"，不是"有没有 queued 任务"**。锁在库层，
   跨进程有效（两个 api 副本、或 api + 运维手跑脚本），内存里那把锁做不到
   这一点。
3. **错误体走 `AppError`**，不在路由里手搓 409/404。§11.3 的表没有这几档，
   所以 `errors.py` 补了 `not_found` / `scan_in_progress` / `samples_missing` /
   `version_conflict` 四个码——文案、状态码、告警口径仍然只有一个来源。
4. **`doc_key` 是路径参数且带 `/`**（`policy/travel-cn`）。路由必须写
   `/documents/{doc_key:path}`，否则 FastAPI 只会匹配到 `policy` 那一段。
   同理 `version_id` 也带 `#`（`policy/travel-cn#a91f`），
   路由要用 `{version_id:path}`。
5. **promote 的 422 与 409 分开**：版本找不到是 404，不是 pending（已翻过）
   是 409，空版本是 422（"解析没产出东西"是内容问题，不是并发问题）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.clients.pg import PgDatabase
from api.errors import AppError, ErrorCode
from api.ingest.flip import (
    EmptyVersion,
    FlipError,
    NotPending,
    VersionNotFound,
    rollback_to,
)
from api.ingest.jobs import latest_job_for_doc, retry_budget_left
from api.ingest.reconcile import reconcile
from api.ingest.scan import (
    SamplesRootMissing,
    ScanInProgress,
    get_document,
    scan_samples,
)
from api.ingest.sync import drain
from api.observability import get_logger

router = APIRouter(prefix="/api/v1", tags=["ingest"])
logger = get_logger("api.ingest.api")


def _pg(request: Request) -> PgDatabase:
    """Run _pg."""
    return request.app.state.pg


def _samples_root(request: Request) -> Path:
    """Run _samples_root."""
    return Path(request.app.state.settings.ingest_samples_dir)


@router.post("/ingest/scan")
async def scan(request: Request) -> JSONResponse:
    """扫 `samples/`：新文档 / 变更 / 跳过 / 失败。.

    响应体（附录 C.1 的四个计数 + 明细）：

    ```json
    {"added":1,"changed":0,"skipped":2,"failed":0,
     "reasons":{"unchanged":2},
     "items":[{"doc_key":"policy/travel-cn","filename":"travel-cn.pdf",
               "outcome":"add","version_id":"policy/travel-cn#a91f","job_id":7}]}
    ```

    并发扫第二次返回 409：第二个扫描只会看到同一批文件、算出同样的指纹，
    纯浪费 IO，还要和第一个抢同一批行的行锁。
    """
    settings = request.app.state.settings
    try:
        report = await asyncio.to_thread(
            scan_samples,
            _pg(request),
            _samples_root(request),
            max_retries=settings.ingest_max_retries,
        )
    except ScanInProgress as exc:
        # 异常链里带上根因，AppError 的 detail 只进日志。
        raise AppError(
            ErrorCode.SCAN_IN_PROGRESS,
            detail=f"{exc} (root={settings.ingest_samples_dir})",
        ) from exc
    except SamplesRootMissing as exc:
        raise AppError(ErrorCode.SAMPLES_MISSING, detail=str(exc)) from exc
    return JSONResponse(status_code=200, content=report.as_dict())


@router.get("/documents/{doc_key:path}")
async def read_document(doc_key: str, request: Request) -> JSONResponse:
    """查入库状态：状态、页数、当前生效版本、最近错误。.

    404 只表示"库里没有这个文档"。这个接口是运维视角的：能查到一个文档存在与否
    本身就是信息，对外的可见性由 B2 的权限层决定，不由这里决定。
    """
    pg = _pg(request)
    max_retries = request.app.state.settings.ingest_max_retries

    def _load() -> dict[str, Any] | None:
        """Run _load."""
        with pg.session() as conn:
            row = get_document(conn, doc_key)
            if row is None:
                return None
            latest = latest_job_for_doc(conn, doc_key)
        payload = _document_payload(row)
        if latest is not None:
            payload["latest_job"] = _job_payload(
                latest, max_retries=max_retries
            )
            # "最近错误"只来自最近一次 failed：成功了还挂着上次的错误，
            # 排障的人会以为现在还在失败。
            payload["last_error"] = (
                latest["error"] if latest["status"] == "failed" else None
            )
        else:
            payload["latest_job"] = None
            payload["last_error"] = None
        return payload

    payload = await asyncio.to_thread(_load)
    if payload is None:
        raise AppError(ErrorCode.NOT_FOUND, detail=f"doc_key={doc_key}")
    return JSONResponse(status_code=200, content=payload)


# --- A3：转正 / 回滚 / 同步 / 对账 --------------------------------------------
#
# 这四个动作都通过**同一个 pool + 同一个 store** 走，不另建连接：
# `app.state.pg` 已经是进程单例的连接池，`_store(request)` 每请求新建
# （httpx.Client 便宜，且它持有 keep-alive 连接、跨线程共享会有隐患——
# 这些端点跑在 to_thread 里）。


def _store(request: Request):
    """每请求一个 QdrantVectorStore（httpx.Client 不做跨线程共享）。.

    参数全部来自 Settings，不读 os.environ：配置的唯一来源是 `api/config.py`，
    脚本里读环境变量是脚本的事（那里没有 Settings），服务里读就是两套口径。
    """
    from api.ingest.vectors import QdrantVectorStore

    settings = request.app.state.settings
    return QdrantVectorStore(
        base_url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout_s=settings.qdrant_timeout_s,
    )


def _embedder(request: Request):
    """Run _embedder."""
    from api.ingest.vectors import Embedder

    settings = request.app.state.settings
    return Embedder(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout_s=settings.embedding_timeout_s,
    )


@router.post("/ingest/versions/{version_id:path}/promote")
async def promote_version(version_id: str, request: Request) -> JSONResponse:
    """把一个 pending 版本转正：索引 → 翻牌 → 同步 → 对账（§4.1、§4.2）。.

    顺序不能反（见 `api/ingest/pipeline.py` 的说明）：先写点（payload=pending，
    检索看不到）再翻牌，就没有"版本已生效、向量库还空着"的窗口。
    """
    settings = request.app.state.settings

    def _run() -> dict[str, Any]:
        """Run _run."""
        from api.ingest.pipeline import promote

        store = _store(request)
        embedder = _embedder(request)
        try:
            result = promote(
                request.app.state.pg.pool,
                store,
                embedder,
                version_id,
                sync_max_attempts=settings.vector_sync_max_attempts,
                sync_backoff_s=settings.vector_sync_backoff_s,
            )
            return result.as_dict()
        finally:
            embedder.close()
            store.close()

    try:
        payload = await asyncio.to_thread(_run)
    except VersionNotFound as exc:
        raise AppError(ErrorCode.NOT_FOUND, detail=str(exc)) from exc
    except EmptyVersion as exc:
        raise AppError(ErrorCode.INVALID_REQUEST, detail=str(exc)) from exc
    except NotPending as exc:
        raise AppError(ErrorCode.VERSION_CONFLICT, detail=str(exc)) from exc
    # 状态码：成功 200；失败但已落库（同步没完）用 207 表达"部分完成",
    # 让调用方知道需要看 body 里的 drain/reconcile 而不是只看 HTTP 码。
    return JSONResponse(
        status_code=200 if payload.get("ok") else 207, content=payload
    )


@router.post("/ingest/versions/{version_id:path}/rollback")
async def rollback_version(version_id: str, request: Request) -> JSONResponse:
    """24 小时内把旧版翻回生效（§4.4）。."""
    settings = request.app.state.settings

    def _run() -> dict[str, Any]:
        """Run _run."""
        pg: PgDatabase = request.app.state.pg
        store = _store(request)
        try:
            with pg.connection() as conn:
                result = rollback_to(conn, version_id)
            # 回滚同样是"库里改完再由同步器推 Qdrant"
            report = drain(
                pg.pool,
                store,
                max_attempts=settings.vector_sync_max_attempts,
                backoff_s=settings.vector_sync_backoff_s,
            )
            return {"rollback": result.as_dict(), "drain": report.as_dict()}
        finally:
            store.close()

    try:
        payload = await asyncio.to_thread(_run)
    except VersionNotFound as exc:
        raise AppError(ErrorCode.NOT_FOUND, detail=str(exc)) from exc
    except NotPending as exc:
        raise AppError(ErrorCode.VERSION_CONFLICT, detail=str(exc)) from exc
    except FlipError as exc:
        raise AppError(ErrorCode.INVALID_REQUEST, detail=str(exc)) from exc
    ok = payload["drain"]["failed"] == 0
    return JSONResponse(status_code=200 if ok else 207, content=payload)


@router.post("/ingest/sync")
async def sync_vectors(request: Request) -> JSONResponse:
    """消费到期的期望状态（worker 定时调它，运维也可手动触发）。."""
    settings = request.app.state.settings

    def _run() -> dict[str, Any]:
        """Run _run."""
        store = _store(request)
        try:
            report = drain(
                request.app.state.pg.pool,
                store,
                max_attempts=settings.vector_sync_max_attempts,
                backoff_s=settings.vector_sync_backoff_s,
            )
            return report.as_dict()
        finally:
            store.close()

    payload = await asyncio.to_thread(_run)
    return JSONResponse(
        status_code=200 if payload["failed"] == 0 else 207, content=payload
    )


@router.get("/ingest/reconcile")
async def reconcile_vectors(
    request: Request, doc_key: str | None = None, dry_run: bool = False
) -> JSONResponse:
    """两边对账：默认按 PG 修好；`dry_run=true` 只报告（验收"漂移看得见"）。.

    **有残留差异就返回 409**（不是 200）：这个接口的主要用途是健康检查与验收，
    返回 200 而 body 里写着 `remaining: 3` 会让调用方以为一切正常。
    """

    def _run() -> dict[str, Any]:
        """Run _run."""
        pg: PgDatabase = request.app.state.pg
        store = _store(request)
        try:
            with pg.connection() as conn:
                report = reconcile(
                    conn, store, doc_key=doc_key, dry_run=dry_run
                )
            return report.as_dict()
        finally:
            store.close()

    payload = await asyncio.to_thread(_run)
    return JSONResponse(
        status_code=409 if payload["remaining"] else 200, content=payload
    )


def _job_payload(job: dict[str, Any], *, max_retries: int) -> dict[str, Any]:
    """Run _job_payload."""
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "retry_count": job["retry_count"],
        # 还剩几次重试：终态一律 0，避免"再等等它自己会重试"的错觉。
        "retry_budget_left": retry_budget_left(
            job["status"], job["retry_count"], max_retries=max_retries
        ),
        "version_id": job.get("version_id"),
        "error": job["error"],
        "created_at": _iso(job["created_at"]),
        "started_at": _iso(job["started_at"]),
        "finished_at": _iso(job["finished_at"]),
    }


def _document_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Run _document_payload."""
    return {
        "doc_key": row["doc_key"],
        "filename": row["filename"],
        "status": row["status"],
        "page_count": row["page_count"],
        "created_at": _iso(row["created_at"]),
        "version_count": int(row["version_count"]),
        "pending_versions": int(row["pending_count"]),
        "chunk_count": int(row["chunk_count"]),
        # 当前生效版本单列：§5.2 的版本条件只认它，前端也不该自己去猜
        # 哪个版本算数。
        "active_version": (
            {
                "version_id": row["active_version_id"],
                "file_hash": row["active_file_hash"],
                "activated_at": _iso(row["active_activated_at"]),
            }
            if row["active_version_id"]
            else None
        ),
    }


def _iso(value: Any) -> str | None:
    """Run _iso."""
    return value.isoformat() if value is not None else None


__all__ = ["router"]
