"""请求上下文中间件：一个 request_id 串起日志、错误响应与审计。.

用纯 ASGI 而不是 BaseHTTPMiddleware：后者会把响应包一层异步生成器，
SSE（/api/v1/qa/stream）的 token 可能被这层缓冲住，表现为"前端等到结束才
一次性收到全部 token"。这种 bug 很难在开发期的短回答里发现。
"""

from __future__ import annotations

import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.observability import (
    REQUEST_ID_HEADER,
    extract_request_id,
    get_logger,
    log_event,
    request_id_var,
)

logger = get_logger("api.access")

# 这些事件不带 request_id 只会刷屏，没有诊断价值。
_SKIP_PATHS = {"/healthz", "/livez"}


class RequestContextMiddleware:
    """RequestContextMiddleware."""
    def __init__(self, app: ASGIApp) -> None:
        """Initialize the instance."""
        self.app = app

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        """Run __call__."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers")
        request_id = (
            extract_request_id(headers if headers else [])
            or uuid.uuid4().hex
        )
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            """Run send_with_request_id."""
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path = scope.get("path", "")
            if path not in _SKIP_PATHS:
                log_event(
                    logger,
                    "http_request",
                    method=scope.get("method"),
                    path=path,
                    status=status_code,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            request_id_var.reset(token)
