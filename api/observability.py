"""日志与请求上下文。

输出结构化作一行的 JSON：机器能 grep，人也能读。
请求上下文只放 request_id，业务字段由调用方显式给，不靠拆字符串。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from typing import Any

REQUEST_ID_HEADER = "X-Request-Id"
_INCOMING_HEADERS = (REQUEST_ID_HEADER.lower(), "x-trace-id", "traceparent")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """一行一条 JSON，字段固定，附 request_id 与 exc 文本。"""

    def __init__(self, *, service: str, env: str) -> None:
        super().__init__()
        self.service = service
        self.env = env

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
            "service": self.service,
            "env": self.env,
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    *, level: str = "INFO", service: str = "ai-rag-api", env: str = "dev"
) -> None:
    """幂等：重复调用只换级别，不叠加 handler（否则一条日志打三遍）。"""
    root = logging.getLogger()
    handler = next(
        (h for h in root.handlers if getattr(h, "_ai_rag_json", False)), None
    )
    if handler is None:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler._ai_rag_json = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter(service=service, env=env))
        root.handlers = [handler]
    else:
        handler.setFormatter(JsonFormatter(service=service, env=env))
    root.setLevel(level.upper())
    # uvicorn 自带的 access log 是文本行，与我们的 JSON 访问日志重复，关掉避免双写。
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("uvicorn.error").setLevel(level.upper())
    # 出站 HTTP 的每请求日志由我们自己的访问日志覆盖；healthz 每 15 秒探一次
    # Qdrant，httpx 的 INFO 会把日志刷满，反而淹没真正的告警。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(
            max(logging.WARNING, logging.getLogger().level)
        )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger, event: str, /, *, level: int = logging.INFO, **fields: Any
) -> None:
    """结构化打点：event 是稳定字符串，字段随事件走。"""
    logger.log(level, event, extra={"fields": fields})


def log_exception(
    logger: logging.Logger, event: str, /, *, level: int = logging.ERROR, **fields: Any
) -> None:
    """结构化打点 + 当前异常栈。

    `logger.exception` 不接受 `extra={"fields": ...}`，它的字段会被吞掉——
    需要"错误栈 + 结构化字段"时用这个，别退化成 f-string 拼进消息。
    """
    logger.log(level, event, exc_info=True, extra={"fields": fields})


def extract_request_id(headers: list[tuple[bytes, bytes]]) -> str | None:
    """从入站请求头取 trace id；支持 X-Request-Id / X-Trace-Id / W3C traceparent。"""
    for raw_key, raw_value in headers:
        key = raw_key.decode("latin-1").lower()
        if key not in _INCOMING_HEADERS:
            continue
        value = raw_value.decode("latin-1").strip()
        if not value:
            continue
        if key == "traceparent":
            # 00-<32hex trace-id>-<16hex span-id>-<flags>
            parts = value.split("-")
            if len(parts) >= 3 and len(parts[1]) == 32:
                return parts[1]
            continue
        return value[:128]
    return None
