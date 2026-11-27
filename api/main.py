"""应用装配。.

`create_app()` 是唯一装配入口，不在模块顶层实例化：
测试要能在没有环境变量、没有 PG/Qdrant 的情况下 import 它，
uvicorn 用 `--factory` 调用它。模块顶层 `app = create_app()` 会让
"import 一个模块"副作用式地连库，测试与命令行工具全被拖下水。
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from api import __version__
from api.clients.base import AsyncHealthProbe, HealthProbe
from api.clients.pg import PgDatabase
from api.clients.qdrant import QdrantGateway
from api.config import Settings, get_settings
from api.errors import install_error_handlers
from api.middleware import RequestContextMiddleware
from api.observability import configure_logging, get_logger, log_event
from api.routers import health, ingest, qa, samples

logger = get_logger("api.main")


def create_app(
    settings: Settings | None = None,
    *,
    pg: HealthProbe | None = None,
    qdrant: AsyncHealthProbe | None = None,
) -> FastAPI:
    """`pg` / `qdrant` 只在测试里注入替身；生产走真实客户端。."""
    settings = settings or get_settings()
    configure_logging(
        level=settings.log_level,
        service=settings.service_name,
        env=settings.app_env,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Run lifespan."""
        app.state.settings = settings
        app.state.started_at = time.time()
        app.state.pg = pg if pg is not None else _build_pg(settings)
        app.state.qdrant = (
            qdrant if qdrant is not None else _build_qdrant(settings)
        )
        _open(app.state.pg)
        await _start(app.state.qdrant)
        log_event(
            logger,
            "api_started",
            version=__version__,
            env=settings.app_env,
            # 启动时把"跑的是哪套口径"打进日志，排障不用进容器看环境变量。
            config=settings.public_config(),
        )
        try:
            yield
        finally:
            await _stop(app.state.qdrant)
            _close(app.state.pg)
            log_event(logger, "api_stopped")

    app = FastAPI(
        title="ai-rag API",
        version=__version__,
        description="金融条款问答：在线链路 API（详细设计.md 附录 C）",
        lifespan=lifespan,
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(qa.router)
    # 引用溯源要的两个只读接口（附录 C.1 的样本直出 + 切片图）：
    # F2 的角标点回依赖它们。
    app.include_router(samples.router)
    return app


def _build_pg(settings: Settings) -> PgDatabase:
    """Run _build_pg."""
    return PgDatabase(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        connect_timeout_s=settings.db_connect_timeout_s,
        application_name=settings.service_name,
    )


def _build_qdrant(settings: Settings) -> QdrantGateway:
    """Run _build_qdrant."""
    return QdrantGateway(
        settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout_s=settings.qdrant_timeout_s,
    )


def _open(dep: Any) -> None:
    """Run _open."""
    opener = getattr(dep, "open", None)
    if callable(opener):
        opener()


def _close(dep: Any) -> None:
    """Run _close."""
    closer = getattr(dep, "close", None)
    if callable(closer):
        closer()


async def _start(dep: Any) -> None:
    """Run _start."""
    starter = getattr(dep, "start", None)
    if callable(starter):
        result = starter()
        if inspect.isawaitable(result):
            await result


async def _stop(dep: Any) -> None:
    """Run _stop."""
    stopper = getattr(dep, "stop", None)
    if callable(stopper):
        result = stopper()
        if inspect.isawaitable(result):
            await result
