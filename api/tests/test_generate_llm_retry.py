"""LLM 网关 resilience 回归：共享 keep-alive 连接池 + 有界重试。

根因（2026-11-24 实测）：LlmClient.complete 每次调用都 `with httpx.Client(...)`，
每次问答都做一次全新 TCP+TLS 握手；第三方 SLB 在握手阶段随机回 SSL EOF，
23% 的握手失败 → model_timeout → 陷阱题被误判为"泄漏"（RL1 13/15）。
"""
from __future__ import annotations

import httpx
import pytest

from api.generate import llm as llm_mod
from api.generate.llm import GenerationRejected, GenerationUnavailable, LlmClient


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)[:200]

    def json(self) -> dict:
        return self._payload


class _ScriptedClient:
    """按脚本依次返回 response 或抛异常；记录调用次数。"""

    def __init__(self, *steps) -> None:
        self.steps = list(steps)
        self.calls = 0
        self.is_closed = False

    def post(self, path, *, json=None, timeout=None):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def close(self) -> None:
        self.is_closed = True


_OK = _FakeResponse(200, {"choices": [{"message": {"content": '{"status":"unknown","answer":"","citations":[]}'}}]})


def _client(**kw) -> LlmClient:
    return LlmClient(base_url="https://gw.example/v1", model="m", api_key="k",
                     retry_backoff_s=0.0, retry_budget_s=10.0, **kw)


def test_retries_transient_ssl_eof_then_succeeds(monkeypatch):
    fake = _ScriptedClient(
        httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
        httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"),
        _OK,
    )
    monkeypatch.setattr(llm_mod, "_shared_client", lambda *a, **k: fake)
    out = _client(max_attempts=4).complete([{"role": "user", "content": "x"}])
    assert "unknown" in out
    assert fake.calls == 3, f"expected 3 attempts, got {fake.calls}"


def test_does_not_retry_4xx(monkeypatch):
    fake = _ScriptedClient(_FakeResponse(400, {"error": "bad request"}))
    monkeypatch.setattr(llm_mod, "_shared_client", lambda *a, **k: fake)
    with pytest.raises(GenerationRejected):
        _client(max_attempts=4).complete([{"role": "user", "content": "x"}])
    assert fake.calls == 1, "4xx is a request-shape error; retry is pointless"


def test_stops_at_max_attempts(monkeypatch):
    err = httpx.ConnectError("SSL EOF")
    fake = _ScriptedClient(err, err, err, err, err)
    monkeypatch.setattr(llm_mod, "_shared_client", lambda *a, **k: fake)
    with pytest.raises(GenerationUnavailable):
        _client(max_attempts=3).complete([{"role": "user", "content": "x"}])
    assert fake.calls == 3


def test_shared_client_reuses_pool_for_same_config():
    llm_mod.reset_shared_clients()
    a = llm_mod._shared_client("https://gw.example/v1", "k", 30.0)
    b = llm_mod._shared_client("https://gw.example/v1/", "k", 30.0)
    c = llm_mod._shared_client("https://gw.example/v1", "other", 30.0)
    assert a is b, "same endpoint+key+timeout must reuse one keep-alive pool"
    assert c is not a, "different credentials must not share a pool"
    llm_mod.reset_shared_clients()
