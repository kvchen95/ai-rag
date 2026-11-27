"""healthz：依赖故障必须能被区分——"挂了"和"哪个挂了"是两件事。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.clients.base import Probe
from api.main import create_app
from api.tests.conftest import make_settings


class FakePg:
    def __init__(self, probe: Probe) -> None:
        self.probe = probe
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def ping(self, *, timeout_s: float | None = None) -> Probe:
        return self.probe


class FakeQdrant:
    def __init__(self, probe: Probe | BaseException) -> None:
        self.probe = probe
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def ping(self, *, timeout_s: float | None = None) -> Probe:
        if isinstance(self.probe, BaseException):
            raise self.probe
        return self.probe


def _client(pg: FakePg, qdrant: FakeQdrant, **overrides: object) -> TestClient:
    return TestClient(
        create_app(make_settings(**overrides), pg=pg, qdrant=qdrant)
    )


def test_healthy_returns_200_with_both_checks() -> None:
    pg = FakePg(
        Probe(ok=True, latency_ms=1.2, extra={"server_version": "16.4"})
    )
    qd = FakeQdrant(
        Probe(ok=True, latency_ms=2.3, extra={"collection_present": True})
    )
    with _client(pg, qd) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["checks"]["postgres"]["ok"] is True
        assert body["checks"]["postgres"]["server_version"] == "16.4"
        assert body["checks"]["qdrant"]["ok"] is True
    assert pg.opened and pg.closed
    assert qd.started and qd.stopped


def test_postgres_down_is_503_and_points_at_postgres() -> None:
    pg = FakePg(
        Probe(
            ok=False,
            latency_ms=2000.0,
            error="OperationalError: connection refused",
        )
    )
    qd = FakeQdrant(Probe(ok=True, latency_ms=2.0))
    with _client(pg, qd) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["ok"] is False
    assert "connection refused" in body["checks"]["postgres"]["error"]
    assert body["checks"]["qdrant"]["ok"] is True


def test_qdrant_down_is_503_and_points_at_qdrant() -> None:
    pg = FakePg(Probe(ok=True, latency_ms=1.0))
    qd = FakeQdrant(
        Probe(ok=False, latency_ms=2000.0, error="ConnectError: [Errno 111]")
    )
    with _client(pg, qd) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["postgres"]["ok"] is True
    assert body["checks"]["qdrant"]["ok"] is False


def test_probe_crash_does_not_become_500() -> None:
    """探针自己抛异常时 healthz 仍须是可解析的 503。

    不能退化成 handler 兜底的 500。
    """
    pg = FakePg(Probe(ok=True, latency_ms=1.0))
    qd = FakeQdrant(RuntimeError("event loop closed"))
    with _client(pg, qd) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["checks"]["qdrant"]["ok"] is False
    assert "event loop closed" in resp.json()["checks"]["qdrant"]["error"]


def test_collection_missing_is_not_a_dependency_failure() -> None:
    """库是空的但服务是好的：首次入库前 api 不能被判 unhealthy 反复重启。"""
    pg = FakePg(Probe(ok=True, latency_ms=1.0))
    qd = FakeQdrant(
        Probe(
            ok=True,
            latency_ms=1.0,
            extra={"collection": "kb_chunks", "collection_present": False},
        )
    )
    with _client(pg, qd) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["checks"]["qdrant"]["collection_present"] is False


def test_livez_ignores_dependencies() -> None:
    pg = FakePg(Probe(ok=False, latency_ms=1.0, error="down"))
    qd = FakeQdrant(Probe(ok=False, latency_ms=1.0, error="down"))
    with _client(pg, qd) as client:
        resp = client.get("/livez")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_config_fingerprint_exposed_in_dev_only() -> None:
    with _client(
        FakePg(Probe(ok=True, latency_ms=1.0)),
        FakeQdrant(Probe(ok=True, latency_ms=1.0)),
    ) as client:
        body = client.get("/healthz").json()
    assert body["config"]["gates"] == {"reject_none": 0.2, "reject_low": 0.4}
    assert body["config"]["models"]["embedding"] == {
        "model": "BAAI/bge-small-zh-v1.5",
        "dim": 512,
    }

    with _client(
        FakePg(Probe(ok=True, latency_ms=1.0)),
        FakeQdrant(Probe(ok=True, latency_ms=1.0)),
        APP_ENV="prod",
    ) as client:
        prod_body = client.get("/healthz").json()
        assert "config" not in prod_body
        assert client.get("/docs").status_code == 404


@pytest.mark.parametrize("app_env", ["dev", "test", "prod"])
def test_healthz_never_leaks_secrets(app_env: str) -> None:
    with _client(
        FakePg(Probe(ok=True, latency_ms=1.0)),
        FakeQdrant(Probe(ok=True, latency_ms=1.0)),
        APP_ENV=app_env,
        EMBEDDING_API_KEY="should-not-appear",
        LLM_API_KEY="should-not-appear-either",
    ) as client:
        text = client.get("/healthz").text
    assert "should-not-appear" not in text
