"""摄取接口（详细设计.md 附录 C.1）。

两个接口，形状照抄附录 C：

| 方法 | 路径 | 作用 | 失败 |
|---|---|---|---|
| POST | `/api/v1/ingest/scan` | 扫 samples/ | 409 任务进行中 |
| GET  | `/api/v1/documents/{doc_key}` | 查入库状态 | 404 |

四条实现约束：

1. **扫描是同步阻塞的重活**（读文件算指纹、写库），用 `asyncio.to_thread` 丢到线程池。
   否则一轮扫描会把事件循环卡住，连带 `/healthz` 都探不到——容器会被判 unhealthy。
   psycopg 连接池是线程安全的，扫描线程借连接不受影响。
2. **409 的判定点是"拿到锁没有"，不是"有没有 queued 任务"**。锁在库层，跨进程有效
   （两个 api 副本、或 api + 运维手跑脚本），内存里那把锁做不到这一点。
3. **错误体走 `AppError`**，不在路由里手搓 409/404。§11.3 的表没有这两档，
   所以 `errors.py` 补了 `not_found` / `scan_in_progress` 两个码——文案、状态码、
   告警口径仍然只有一个来源。
4. **`doc_key` 是路径参数且带 `/`**（`policy/travel-cn`）。路由必须写
   `/documents/{doc_key:path}`，否则 FastAPI 只会匹配到 `policy` 那一段。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.clients.pg import PgDatabase
from api.errors import AppError, ErrorCode
from api.ingest.jobs import latest_job_for_doc, retry_budget_left
from api.ingest.scan import (
    SamplesRootMissing,
    ScanInProgress,
    get_document,
    scan_samples,
)
from api.observability import get_logger

router = APIRouter(prefix="/api/v1", tags=["ingest"])
logger = get_logger("api.ingest.api")


def _pg(request: Request) -> PgDatabase:
    return request.app.state.pg


def _samples_root(request: Request) -> Path:
    return Path(request.app.state.settings.ingest_samples_dir)


@router.post("/ingest/scan")
async def scan(request: Request) -> JSONResponse:
    """扫 `samples/`：新文档 / 变更 / 跳过 / 失败。

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
    """查入库状态：状态、页数、当前生效版本、最近错误。

    404 只表示"库里没有这个文档"。这个接口是运维视角的：能查到一个文档存在与否
    本身就是信息，对外的可见性由 B2 的权限层决定，不由这里决定。
    """
    pg = _pg(request)
    max_retries = request.app.state.settings.ingest_max_retries

    def _load() -> dict[str, Any] | None:
        with pg.session() as conn:
            row = get_document(conn, doc_key)
            if row is None:
                return None
            latest = latest_job_for_doc(conn, doc_key)
        payload = _document_payload(row)
        if latest is not None:
            payload["latest_job"] = _job_payload(latest, max_retries=max_retries)
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


def _job_payload(job: dict[str, Any], *, max_retries: int) -> dict[str, Any]:
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
    return {
        "doc_key": row["doc_key"],
        "filename": row["filename"],
        "status": row["status"],
        "page_count": row["page_count"],
        "created_at": _iso(row["created_at"]),
        "version_count": int(row["version_count"]),
        "pending_versions": int(row["pending_count"]),
        "chunk_count": int(row["chunk_count"]),
        # 当前生效版本单列：§5.2 的版本条件只认它，前端也不该自己去猜哪个版本算数。
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
    return value.isoformat() if value is not None else None


__all__ = ["router"]
