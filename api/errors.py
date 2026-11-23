"""错误分类（详细设计.md §11.3）。

一张表管三件事：对外文案、HTTP 状态、要不要告警。三件事必须同源——
如果文案在路由里写、状态码在 handler 里写、告警规则在监控里写，
迟早出现"用户看到稍后重试，运维没收到告警"。

约定：
- `detail` 只进日志与审计，永远不进响应体（不泄漏内部结构）。
- `sse_fallback=True` 的码在流已开始后以 `fallback` 事件下发（附录 C.2），
  `http_status` 只用于"还没开流"的时候。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.observability import get_logger, log_event, request_id_var

logger = get_logger("api.errors")


class ErrorCode(StrEnum):
    AUTH_INVALID = "auth_invalid"
    FORBIDDEN = "forbidden"
    NO_EVIDENCE = "no_evidence"
    LOW_CONFIDENCE = "low_confidence"
    UNVERIFIABLE = "unverifiable"
    MODEL_TIMEOUT = "model_timeout"
    INDEX_STALE = "index_stale"
    INTERNAL = "internal"
    # 契约层扩展：§11.3 的表只管业务错误，入参不合法需要一个自己的码，
    # 不能塞进 internal（会误触发告警），也不该借 auth_invalid 的文案。
    INVALID_REQUEST = "invalid_request"


class AlertPolicy(StrEnum):
    NONE = "none"  # 用户侧问题，不告警
    BURST = "burst"  # 连续出现才告警（forbidden：偶发是正常越权拦截）
    ALWAYS = "always"  # 出现即告警


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    http_status: int
    user_message: str
    alert: AlertPolicy
    sse_fallback: bool = False


# §11.3 原表逐行落地；顺序与文档一致，便于对照。
ERROR_SPECS: Mapping[ErrorCode, ErrorSpec] = {
    ErrorCode.AUTH_INVALID: ErrorSpec(401, "请重新登录", AlertPolicy.NONE),
    ErrorCode.FORBIDDEN: ErrorSpec(403, "无权查看", AlertPolicy.BURST),
    ErrorCode.NO_EVIDENCE: ErrorSpec(
        200, "未找到可验证依据", AlertPolicy.NONE, sse_fallback=True
    ),
    ErrorCode.LOW_CONFIDENCE: ErrorSpec(
        200, "现有资料不足以下结论", AlertPolicy.NONE, sse_fallback=True
    ),
    ErrorCode.UNVERIFIABLE: ErrorSpec(
        200, "本次不作结论，附原文", AlertPolicy.ALWAYS, sse_fallback=True
    ),
    ErrorCode.MODEL_TIMEOUT: ErrorSpec(
        504, "稍后重试", AlertPolicy.ALWAYS, sse_fallback=True
    ),
    ErrorCode.INDEX_STALE: ErrorSpec(
        503, "稍后重试", AlertPolicy.ALWAYS, sse_fallback=True
    ),
    ErrorCode.INTERNAL: ErrorSpec(
        500, "稍后重试", AlertPolicy.ALWAYS, sse_fallback=True
    ),
    ErrorCode.INVALID_REQUEST: ErrorSpec(422, "请求格式不正确", AlertPolicy.NONE),
}


def spec_for(code: ErrorCode) -> ErrorSpec:
    return ERROR_SPECS[code]


class AppError(Exception):
    """业务错误。抛出点只关心码与内部细节，对外文案/状态码由表决定。"""

    def __init__(
        self,
        code: ErrorCode,
        *,
        detail: str | None = None,
        user_message: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.context = dict(context or {})
        self.spec = spec_for(code)
        self.user_message = user_message or self.spec.user_message
        super().__init__(f"{code.value}: {detail or self.user_message}")

    def body(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.user_message,
                "request_id": request_id,
            }
        }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        rid = request_id_var.get()
        log_event(
            logger,
            "app_error",
            level=_log_level(exc.spec.alert),
            code=exc.code.value,
            alert=exc.spec.alert.value,
            detail=exc.detail,
            **exc.context,
        )
        return JSONResponse(status_code=exc.spec.http_status, content=exc.body(rid))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = request_id_var.get()
        # 入参错误只回字段位置，不回用户传来的原值（可能带敏感内容）。
        fields = [
            ".".join(str(p) for p in err.get("loc", ()) if p != "body")
            for err in exc.errors()
        ]
        log_event(logger, "invalid_request", level=30, fields=fields)
        err = AppError(ErrorCode.INVALID_REQUEST, detail=", ".join(fields) or None)
        return JSONResponse(status_code=err.spec.http_status, content=err.body(rid))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        rid = request_id_var.get()
        logger.exception(
            "unhandled_exception", extra={"fields": {"error_type": type(exc).__name__}}
        )
        err = AppError(ErrorCode.INTERNAL, detail=type(exc).__name__)
        return JSONResponse(status_code=err.spec.http_status, content=err.body(rid))


def _log_level(alert: AlertPolicy) -> int:
    # 业务上可预期的拒绝（无证据/低置信）用 WARNING，其余错误用 ERROR；
    # 真正的分级告警由 alert 字段在日志侧做，不靠日志级别硬猜。
    return 30 if alert is AlertPolicy.NONE else 40
