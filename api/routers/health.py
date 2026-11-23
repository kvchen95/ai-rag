"""健康检查。

`/healthz` 回答"依赖通不通"，且必须能区分是哪一个坏了：
- 全部通 → 200 status=ok
- 任一不通 → 503 status=degraded，且 checks.<dep>.ok 精确指出是谁
- Qdrant 集合缺失不算依赖故障（库是空的，服务是好的），单独用
  collection_present 报出来

`/livez` 只看进程本身，不碰外部依赖——容器被依赖拖成 unhealthy 再被
反复重启，会把"下游慢"放大成"上游全挂"。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api import __version__
from api.clients.base import Probe
from api.observability import get_logger, log_event

router = APIRouter(tags=["ops"])
logger = get_logger("api.health")


def _failed(probe: BaseException) -> Probe:
    """gather(return_exceptions=True) 兜底：探针自己崩了也要变成一条 check，
    否则 /healthz 会 500，而 500 恰恰是最不区分故障的那种回答。"""
    return Probe(
        ok=False,
        latency_ms=0.0,
        error=f"probe crashed: {type(probe).__name__}: {probe}",
    )


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    state = request.app.state
    settings = state.settings
    timeout_s = settings.health_probe_timeout_s

    pg_probe, qdrant_probe = await asyncio.gather(
        asyncio.to_thread(state.pg.ping, timeout_s=timeout_s),
        state.qdrant.ping(timeout_s=timeout_s),
        return_exceptions=True,
    )
    checks: dict[str, Any] = {
        "postgres": (
            _failed(pg_probe) if isinstance(pg_probe, BaseException) else pg_probe
        ).as_dict(),
        "qdrant": (
            _failed(qdrant_probe)
            if isinstance(qdrant_probe, BaseException)
            else qdrant_probe
        ).as_dict(),
    }
    ok = all(check["ok"] for check in checks.values())
    body: dict[str, Any] = {
        "status": "ok" if ok else "degraded",
        "service": settings.service_name,
        "version": __version__,
        "env": settings.app_env,
        "uptime_s": round(time.time() - state.started_at, 3),
        "checks": checks,
    }
    # 配置指纹只在非生产环境暴露：这是排障用的，不是对外接口。
    if not settings.is_prod:
        body["config"] = settings.public_config()
    if not ok:
        log_event(
            logger,
            "healthz_degraded",
            level=40,
            failed=[name for name, check in checks.items() if not check["ok"]],
        )
    return JSONResponse(status_code=200 if ok else 503, content=body)


@router.get("/livez")
async def livez(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": request.app.state.settings.service_name,
        "version": __version__,
        "uptime_s": round(time.time() - request.app.state.started_at, 3),
    }
