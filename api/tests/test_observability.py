"""日志与请求上下文：JSON 可解析、request_id 能串起来、不重复挂 handler。"""

from __future__ import annotations

import json
import logging

from fastapi.testclient import TestClient

from api.clients.base import Probe
from api.main import create_app
from api.observability import (
    JsonFormatter,
    configure_logging,
    extract_request_id,
    log_event,
    request_id_var,
)
from api.tests.conftest import make_settings


class _Pg:
    def open(self) -> None: ...
    def close(self) -> None: ...
    def ping(self, *, timeout_s: float | None = None) -> Probe:
        return Probe(ok=True, latency_ms=1.0)


class _Qdrant:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ping(self, *, timeout_s: float | None = None) -> Probe:
        return Probe(ok=True, latency_ms=1.0)


def _record(event: str, **fields: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "api.test", logging.INFO, __file__, 1, event, None, None
    )
    record.fields = fields
    return record


def test_formatter_outputs_one_json_line_with_request_id() -> None:
    token = request_id_var.set("rid-123")
    try:
        line = JsonFormatter(service="ai-rag-api", env="test").format(
            _record("http_request", status=200)
        )
    finally:
        request_id_var.reset(token)
    payload = json.loads(line)
    assert payload["event"] == "http_request"
    assert payload["status"] == 200
    assert payload["request_id"] == "rid-123"
    assert payload["service"] == "ai-rag-api"
    assert "\n" not in line


def test_formatter_keeps_traceback() -> None:
    try:
        raise ValueError("boom-detail")
    except ValueError:
        import sys

        record = _record("unhandled_exception")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter(service="s", env="test").format(record))
    assert "ValueError" in payload["exc"]
    assert "boom-detail" in payload["exc"]


def test_configure_logging_is_idempotent() -> None:
    configure_logging(level="INFO", service="ai-rag-api", env="test")
    configure_logging(level="DEBUG", service="ai-rag-api", env="test")
    handlers = [
        h
        for h in logging.getLogger().handlers
        if getattr(h, "_ai_rag_json", False)
    ]
    assert len(handlers) == 1, "重复配置会叠加 handler，一条日志打多遍"


def test_request_id_from_headers() -> None:
    assert extract_request_id([(b"x-request-id", b"abc-123")]) == "abc-123"
    assert extract_request_id([(b"x-trace-id", b"trace-9")]) == "trace-9"
    traceparent = b"00-" + b"a" * 32 + b"-" + b"b" * 16 + b"-01"
    assert extract_request_id([(b"traceparent", traceparent)]) == "a" * 32
    assert extract_request_id([]) is None


def test_request_id_is_echoed_and_generated_when_missing() -> None:
    app = create_app(make_settings(), pg=_Pg(), qdrant=_Qdrant())
    with TestClient(app) as client:
        echoed = client.get(
            "/livez", headers={"X-Request-Id": "caller-supplied"}
        )
        generated = client.get("/livez")
    assert echoed.headers["X-Request-Id"] == "caller-supplied"
    assert len(generated.headers["X-Request-Id"]) == 32


def test_log_event_survives_missing_fields() -> None:
    logger = logging.getLogger("api.test.events")
    log_event(logger, "no_fields")
    log_event(logger, "with_fields", answer_id=1)
