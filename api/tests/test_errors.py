"""错误分类：文案/状态码/告警三件事必须同源，且 detail 不许外泄。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.clients.base import Probe
from api.config import Settings
from api.errors import ERROR_SPECS, AlertPolicy, AppError, ErrorCode, spec_for
from api.main import create_app
from api.tests.conftest import make_settings

# §11.3 的原表：用户文案与告警口径照抄，改这里就等于改产品口径。
EXPECTED = {
    ErrorCode.AUTH_INVALID: (401, "请重新登录", AlertPolicy.NONE),
    ErrorCode.FORBIDDEN: (403, "无权查看", AlertPolicy.BURST),
    ErrorCode.NO_EVIDENCE: (200, "未找到可验证依据", AlertPolicy.NONE),
    ErrorCode.LOW_CONFIDENCE: (200, "现有资料不足以下结论", AlertPolicy.NONE),
    ErrorCode.UNVERIFIABLE: (200, "本次不作结论，附原文", AlertPolicy.ALWAYS),
    ErrorCode.MODEL_TIMEOUT: (504, "稍后重试", AlertPolicy.ALWAYS),
    ErrorCode.INDEX_STALE: (503, "稍后重试", AlertPolicy.ALWAYS),
    ErrorCode.INTERNAL: (500, "稍后重试", AlertPolicy.ALWAYS),
}


class _OkPg:
    def open(self) -> None: ...
    def close(self) -> None: ...
    def ping(self, *, timeout_s: float | None = None) -> Probe:
        return Probe(ok=True, latency_ms=1.0)


class _OkQdrant:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ping(self, *, timeout_s: float | None = None) -> Probe:
        return Probe(ok=True, latency_ms=1.0)


def _client_with_routes(*routes: tuple[str, object]) -> TestClient:
    settings: Settings = make_settings()
    app: FastAPI = create_app(settings, pg=_OkPg(), qdrant=_OkQdrant())
    for path, handler in routes:
        app.get(path)(handler)  # type: ignore[arg-type]
    return TestClient(app)


def test_every_spec_matches_design_table() -> None:
    assert set(ERROR_SPECS) - {ErrorCode.INVALID_REQUEST} == set(EXPECTED)
    for code, (status, message, alert) in EXPECTED.items():
        spec = spec_for(code)
        assert (spec.http_status, spec.user_message, spec.alert) == (
            status,
            message,
            alert,
        ), code


def test_sse_fallback_flag_only_on_streamable_codes() -> None:
    # auth/forbidden 在开流前就该挡掉，不该以 fallback 事件出现在流里。
    assert not spec_for(ErrorCode.AUTH_INVALID).sse_fallback
    assert not spec_for(ErrorCode.FORBIDDEN).sse_fallback
    for code in (
        ErrorCode.NO_EVIDENCE,
        ErrorCode.LOW_CONFIDENCE,
        ErrorCode.UNVERIFIABLE,
        ErrorCode.MODEL_TIMEOUT,
    ):
        assert spec_for(code).sse_fallback


def test_app_error_maps_status_and_hides_detail() -> None:
    def boom() -> None:
        raise AppError(
            ErrorCode.FORBIDDEN, detail="dept fin not allowed for doc policy/travel-cn"
        )

    with _client_with_routes(("/boom", boom)) as client:
        resp = client.get("/boom")

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["message"] == "无权查看"
    assert "policy/travel-cn" not in resp.text  # detail 只进日志
    assert body["error"]["request_id"] == resp.headers["X-Request-Id"]


def test_unhandled_exception_becomes_internal() -> None:
    def boom() -> None:
        raise RuntimeError("db password is hunter2")

    # raise_server_exceptions=False 才是"线上视角"：Starlette 的 500 handler
    # 会把异常再抛给上层（容器/reloader），测试里必须关掉才能看到真实响应。
    settings: Settings = make_settings()
    app: FastAPI = create_app(settings, pg=_OkPg(), qdrant=_OkQdrant())
    app.get("/boom")(boom)  # type: ignore[arg-type]
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/boom")

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal"
    assert resp.json()["error"]["message"] == "稍后重试"
    assert "hunter2" not in resp.text


def test_validation_error_uses_own_code() -> None:
    def need_int(n: int) -> dict[str, int]:
        return {"n": n}

    with _client_with_routes(("/needs-int", need_int)) as client:
        resp = client.get("/needs-int", params={"n": "not-a-number"})

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("code", list(ErrorCode))
def test_error_body_shape_is_stable(code: ErrorCode) -> None:
    err = AppError(code)
    body = err.body("rid-1")
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code.value
    assert body["error"]["message"]
