r"""`POST /api/v1/qa/stream` 的 HTTP 契约（附录 C.1 / C.2、§10.4）。

三条只有走真 HTTP 才验得到的东西：

1. **401 / 422 在开流之前**：状态码必须是 401/422，body 是 JSON 错误体。
   如果身份或入参错误被拖进流里，前端会先收到 200 再收到一条 fallback——
   它据此显示的文案是"稍后重试"，而用户其实该去重新登录或改请求。
2. **响应头**：`text/event-stream` + `no-cache` + `X-Accel-Buffering: no`。
   少了最后一个，nginx 会把整条流攒完再发，SSE 退化成一次性响应。
3. **帧的真实字节形状**：`event: <name>\\ndata: {...}\\n\\n`，一条 `done` 结尾。

检索链路用替身（真 PG/Qdrant 的端到端在集成用例里）：这里要验的是"HTTP 与 SSE
这一层"，不是"检索对不对"。替身接在 `build_service` 上——那是路由唯一的装配点。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.qa import events as ev
from api.qa.orchestrator import QaOutcome
from api.tests.conftest import make_settings

FIN_HEADERS = {
    # AUTH_MODE=mock_header：身份来自请求头，401 只验"缺头"这一种
    "X-User-Id": "u_fin",
    "X-User-Depts": "fin",
    "X-User-Roles": "staff",
}


class _StubPipeline:
    def close(self) -> None:
        return None


def _settings(**overrides: Any):
    return make_settings(AUTH_MODE="mock_header", **overrides)


@contextmanager
def _client(
    monkeypatch: pytest.MonkeyPatch, service_factory
) -> Iterator[TestClient]:
    """起一个真 app（lifespan 也跑），只把 `build_service` 换成替身。"""
    import api.routers.qa as qa_router

    monkeypatch.setattr(
        qa_router, "build_service", lambda request, settings: service_factory()
    )
    app = create_app(_settings())
    with TestClient(app) as client:
        yield client


def _service(builder):
    """把一个"只发事件"的函数包成 `QaService` 的替身。"""

    class _Stub:
        def __init__(self) -> None:
            self.pipeline = _StubPipeline()

        def run(self, question: str, identity: Any, **kwargs: Any) -> QaOutcome:
            return builder(question, identity, kwargs.get("emit"))

    return _Stub


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把整段 SSE 文本拆成 [(事件名, data)]。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        assert name and data, f"SSE 帧缺 event 或 data: {block!r}"
        out.append((name, json.loads(data)))
    return out


def _drain(
    client: TestClient,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> str:
    with client.stream(
        "POST",
        "/api/v1/qa/stream",
        json=payload,
        headers=headers or FIN_HEADERS,
    ) as resp:
        assert resp.status_code == 200, resp.read()
        return "".join(resp.iter_text())


# --- 开流之前的错误：401 / 422 -----------------------------------------------


def test_missing_identity_is_401_with_a_json_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺身份必须是 401，不能是一条 fallback 事件流。"""
    with _client(monkeypatch, _service(lambda *a, **k: None)) as client:
        resp = client.post(
            "/api/v1/qa/stream", json={"question": "住宿标准是多少"}
        )
    assert resp.status_code == 401
    payload = resp.json()
    assert payload["error"]["code"] == "auth_invalid"
    assert payload["error"]["request_id"]


def test_blank_question_is_422_and_never_opens_a_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch, _service(lambda *a, **k: None)) as client:
        resp = client.post(
            "/api/v1/qa/stream", json={"question": "   "}, headers=FIN_HEADERS
        )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


def test_unknown_body_field_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    """`extra="forbid"`：前端多传一个字段要立刻红，而不是被静默忽略。"""
    with _client(monkeypatch, _service(lambda *a, **k: None)) as client:
        resp = client.post(
            "/api/v1/qa/stream",
            json={"question": "住宿标准", "ndept": "fin"},
            headers=FIN_HEADERS,
        )
    assert resp.status_code == 422


def test_time_range_with_reversed_bounds_is_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _client(monkeypatch, _service(lambda *a, **k: None)) as client:
        resp = client.post(
            "/api/v1/qa/stream",
            json={
                "question": "住宿标准",
                "time_range": {"from": 20251231, "to": 20250101},
            },
            headers=FIN_HEADERS,
        )
    assert resp.status_code == 422


# --- 正常流：头 + 帧 ----------------------------------------------------------


def test_sse_headers_disable_proxy_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def builder(question, identity, emit):
        emit(ev.step("retrieval"))
        emit(ev.done(latency_ms=1, hop_count=0, verdict="pass"))
        return QaOutcome(question=question, verdict="pass", identity=identity)

    with (
        _client(monkeypatch, _service(builder)) as client,
        client.stream(
            "POST",
            "/api/v1/qa/stream",
            json={"question": "住宿标准"},
            headers=FIN_HEADERS,
        ) as resp,
    ):
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        body = "".join(resp.iter_text())
    assert [name for name, _ in _parse_sse(body)] == ["step", "done"]


def test_frames_are_framed_with_event_and_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def builder(question, identity, emit):
        emit(ev.step("retrieval", hop=0, elapsed_ms=1.5))
        emit(ev.token("500 元"))
        emit(
            ev.citations(
                [
                    {
                        "n": 1,
                        "doc_key": "policy/travel",
                        "doc_name": "差旅管理办法-2025版.pdf",
                        "page_num": 47,
                        "bbox": [[0.1, 0.2, 0.9, 0.3]],
                        "snippet": "住宿费标准：每人每天 500 元。",
                        "chunk_id": "ch_a",
                    }
                ]
            )
        )
        emit(ev.done(latency_ms=1200, hop_count=0, verdict="pass"))
        return QaOutcome(question=question, verdict="pass", identity=identity)

    with _client(monkeypatch, _service(builder)) as client:
        body = _drain(client, {"question": "住宿标准"})

    frames = _parse_sse(body)
    assert [name for name, _ in frames] == [
        "step",
        "token",
        "citations",
        "done",
    ]
    assert frames[1][1] == {"type": "token", "delta": "500 元"}
    citation = frames[2][1]["items"][0]
    assert citation["chunk_id"] == "ch_a"
    assert citation["page_num"] == 47
    assert frames[-1][1]["verdict"] == "pass"


def test_question_longer_than_the_limit_is_truncated_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§11.4：单问题长度 500 字，"截断并提示"。"""
    seen: dict[str, str] = {}

    def builder(question, identity, emit):
        seen["question"] = question
        emit(ev.done(latency_ms=1, hop_count=0, verdict="reject_none"))
        return QaOutcome(
            question=question, verdict="reject_none", identity=identity
        )

    with _client(monkeypatch, _service(builder)) as client:
        _drain(client, {"question": "住" * 800})
    assert len(seen["question"]) == 500


def test_scope_and_time_range_reach_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def builder(question, identity, emit):
        emit(ev.done(latency_ms=1, hop_count=0, verdict="pass"))
        return QaOutcome(question=question, verdict="pass", identity=identity)

    class _Capturing:
        def __init__(self) -> None:
            self.pipeline = _StubPipeline()

        def run(self, question, identity, **kwargs):
            captured.update(kwargs)
            return builder(question, identity, kwargs.get("emit"))

    with _client(monkeypatch, _Capturing) as client:
        _drain(
            client,
            {
                "question": "2025年住宿标准",
                "scope": {"doc_keys": ["policy/travel"]},
                "time_range": {"from": 20250101, "to": 20251231},
            },
        )
    assert captured["doc_keys"] == ["policy/travel"]
    assert captured["time_range"].date_from == 20250101
    assert captured["time_range"].date_to == 20251231
    assert (
        captured["time_range"].basis == "explicit"
    ), "前端给的时间窗不是推断出来的"


def test_worker_raising_closes_the_stream_without_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker 抛异常时 HTTP 状态已经是 200 了：流必须结束，前端不能一直转圈。

    替身自己不发 `done`（那由 `QaService.run` 的 finally 保证，见
    `test_qa_sse.py::test_unhandled_exception_still_emits_done`）。这里验的是
    **路由层不会挂住**：`close` 哨兵在 worker 的 finally 里，异常路径也会到。
    """

    def builder(question, identity, emit):
        raise RuntimeError("boom")

    with _client(monkeypatch, _service(builder)) as client:
        body = _drain(client, {"question": "住宿标准"})
    assert body == ""


# --- 装配与配置 ---------------------------------------------------------------


def test_stream_endpoint_is_registered() -> None:
    app = create_app(_settings())
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/qa/stream" in paths


def test_no_llm_configured_still_serves_the_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配生成模型时服务必须能起来：检索与审计仍然有价值（评测与门控标定要用）。"""
    settings = _settings()
    assert settings.llm_model == ""
    import api.routers.qa as qa_router

    assert qa_router.llm_client(settings) is None
    assert qa_router.judge_client(settings) is None

    def builder(question, identity, emit):
        emit(ev.fallback("model_timeout", "稍后重试", []))
        emit(
            ev.done(
                latency_ms=1,
                hop_count=0,
                verdict="model_timeout",
                code="model_timeout",
            )
        )
        return QaOutcome(
            question=question, verdict="model_timeout", identity=identity
        )

    with _client(monkeypatch, _service(builder)) as client:
        body = _drain(client, {"question": "住宿标准"})
    assert [name for name, _ in _parse_sse(body)] == ["fallback", "done"]


def test_public_config_exposes_the_generation_fingerprint() -> None:
    """换模型必须能在 /healthz 一眼看出来（config 指纹）。"""
    settings = make_settings(SSE_TOKEN_CHUNK_CHARS=4)
    generation = settings.public_config()["generation"]
    assert generation["temperature"] == 0.0
    assert generation["sse_token_chunk_chars"] == 4
    assert generation["llm_configured"] is False
