"""`POST /api/v1/ingest/scan` 与 `GET /api/v1/documents/{doc_key}` 的契约。

单元层只验**接口自己的责任**：状态码、响应体形状、错误码映射、`doc_key:path`
能不能带斜杠。扫描逻辑本身由集成用例覆盖——这里用替身把扫描替掉，正是为了
把"接口错了"和"扫描错了"分开，红的时候不用猜是哪一层。

真容器里的 200/404/409 由 `scripts/verify_scan_api.py` 实测（本文件末尾有对应
的断言清单，两边口径必须一致）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from api.clients.base import Probe
from api.ingest.decide import Outcome, SkipReason
from api.ingest.scan import ScanInProgress, ScanItem, ScanReport, SamplesRootMissing
from api.main import create_app
from api.tests.conftest import make_settings


class _Pg:
    """只需要 open/close/ping/session：扫描与查询被替身接管后，路由不会真的读库。"""

    def open(self) -> None: ...
    def close(self) -> None: ...
    def ping(self, *, timeout_s: float | None = None) -> Probe:
        return Probe(ok=True, latency_ms=1.0)

    @contextmanager
    def session(self) -> Iterator[Any]:
        yield object()


class _Qdrant:
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def ping(self, *, timeout_s: float | None = None) -> Probe:
        return Probe(ok=True, latency_ms=1.0)


def _client(**overrides: object) -> TestClient:
    return TestClient(
        create_app(make_settings(**overrides), pg=_Pg(), qdrant=_Qdrant())
    )


def _patch_scan(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> None:
    from api.routers import ingest as ingest_router

    monkeypatch.setattr(ingest_router, "scan_samples", behaviour)


def _patch_doc(monkeypatch: pytest.MonkeyPatch, behaviour: Any) -> None:
    from api.routers import ingest as ingest_router

    monkeypatch.setattr(ingest_router, "get_document", behaviour)


def test_scan_returns_counts_and_items(monkeypatch: pytest.MonkeyPatch) -> None:
    report = ScanReport(
        items=[
            ScanItem(
                doc_key="policy/travel-cn",
                filename="travel-cn.pdf",
                outcome=Outcome.ADD,
                version_id="policy/travel-cn#a91f2b3c4d5e",
                job_id=7,
            ),
            ScanItem(
                doc_key="policy/expense-cn",
                filename="expense-cn.pdf",
                outcome=Outcome.SKIP,
                reason=SkipReason.UNCHANGED,
                version_id="policy/expense-cn#0011223344ff",
            ),
        ]
    )
    _patch_scan(monkeypatch, lambda *a, **kw: report)

    with _client() as client:
        resp = client.post("/api/v1/ingest/scan")

    assert resp.status_code == 200
    body = resp.json()
    # 附录 C.1 的四个计数恒定出现（哪怕某个是 0）。
    assert body["added"] == 1
    assert body["changed"] == 0
    assert body["skipped"] == 1
    assert body["failed"] == 0
    assert body["reasons"] == {"unchanged": 1}
    assert {item["outcome"] for item in body["items"]} == {"add", "skip"}


def test_scan_conflict_is_409_with_contract_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验收第一条的接口侧：并发扫描返回 409，且错误体与其它错误同源。"""

    def boom(*_a: object, **_kw: object) -> None:
        raise ScanInProgress()

    _patch_scan(monkeypatch, boom)
    with _client() as client:
        resp = client.post("/api/v1/ingest/scan")

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "scan_in_progress"
    assert error["message"] == "扫描正在进行中"
    assert error["request_id"] == resp.headers["X-Request-Id"]
    # detail（内部路径）不能外泄。
    assert "samples" not in resp.text


def test_scan_missing_samples_dir_is_500_and_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "目录不存在"不能伪装成"扫到 0 个文件"：那会让运维以为一切正常。"""

    def boom(*_a: object, **_kw: object) -> None:
        raise SamplesRootMissing(__import__("pathlib").Path("/nowhere"))

    _patch_scan(monkeypatch, boom)
    with _client() as client:
        resp = client.post("/api/v1/ingest/scan")

    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "samples_missing"


def test_document_accepts_doc_key_with_slashes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`policy/travel-cn` 是路径参数里带斜杠的唯一写法：路由必须用 `:path`。"""
    from api.routers import ingest as ingest_router

    seen: list[str] = []

    def fake_get_document(_conn: Any, doc_key: str) -> dict[str, Any]:
        seen.append(doc_key)
        return {
            "doc_key": doc_key,
            "filename": "travel-cn.pdf",
            "page_count": 2,
            "status": "active",
            "created_at": None,
            "version_count": 2,
            "pending_count": 1,
            "chunk_count": 4,
            "active_version_id": "policy/travel-cn#aaaa",
            "active_file_hash": "a" * 64,
            "active_activated_at": None,
        }

    _patch_doc(monkeypatch, fake_get_document)
    monkeypatch.setattr(ingest_router, "latest_job_for_doc", lambda _conn, _key: None)
    with _client() as client:
        resp = client.get("/api/v1/documents/policy/travel-cn")

    assert resp.status_code == 200
    assert seen == ["policy/travel-cn"]
    body = resp.json()
    assert body["doc_key"] == "policy/travel-cn"
    assert body["active_version"]["version_id"] == "policy/travel-cn#aaaa"
    assert body["pending_versions"] == 1
    assert body["version_count"] == 2
    # latest_job 缺失时也必须有这个键：消费者不该为"没有任务"写 null 分支。
    assert body["latest_job"] is None
    assert body["last_error"] is None


def test_missing_document_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_doc(monkeypatch, lambda _conn, _key: None)
    with _client() as client:
        resp = client.get("/api/v1/documents/policy/ghost")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert resp.json()["error"]["message"] == "未找到该文档"


def test_document_payload_includes_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`retry_budget_left` 是"还要等几轮"的唯一来源，接口必须透出它。"""

    class _FakeConn:
        def __enter__(self) -> "_FakeConn":
            return self

        def __exit__(self, *_: object) -> None:
            return None

    class _FakePg:
        def open(self) -> None: ...
        def close(self) -> None: ...
        def ping(self, *, timeout_s: float | None = None) -> Probe:
            return Probe(ok=True, latency_ms=1.0)

        def session(self) -> _FakeConn:
            return _FakeConn()

    _patch_doc(
        monkeypatch,
        lambda _conn, key: {
            "doc_key": key,
            "filename": "x.pdf",
            "page_count": None,
            "status": "queued",
            "created_at": None,
            "version_count": 1,
            "pending_count": 1,
            "chunk_count": 0,
            "active_version_id": None,
            "active_file_hash": None,
            "active_activated_at": None,
        },
    )
    from api.routers import ingest as ingest_router
    from datetime import datetime, timezone

    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(
        ingest_router,
        "latest_job_for_doc",
        lambda _conn, _key: {
            "job_id": 11,
            "doc_key": "policy/x",
            "version_id": "policy/x#abc",
            "status": "queued",
            "retry_count": 2,
            "error": "boom",
            "created_at": now,
            "started_at": None,
            "finished_at": None,
        },
    )

    app = create_app(
        make_settings(INGEST_MAX_RETRIES=3), pg=_FakePg(), qdrant=_Qdrant()
    )
    with TestClient(app) as client:
        body = client.get("/api/v1/documents/policy/x").json()

    job = body["latest_job"]
    assert job["retry_count"] == 2
    assert job["retry_budget_left"] == 1  # 3 - 2
    # 还在 queued：上次的 error 不是"最近错误"，别让人以为现在还在失败。
    assert body["last_error"] is None


def test_last_error_only_from_failed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    from api.routers import ingest as ingest_router

    _patch_doc(
        monkeypatch,
        lambda _conn, key: {
            "doc_key": key,
            "filename": "x.pdf",
            "page_count": None,
            "status": "failed",
            "created_at": None,
            "version_count": 1,
            "pending_count": 0,
            "chunk_count": 0,
            "active_version_id": None,
            "active_file_hash": None,
            "active_activated_at": None,
        },
    )
    now = datetime(2026, 11, 24, tzinfo=timezone.utc)
    monkeypatch.setattr(
        ingest_router,
        "latest_job_for_doc",
        lambda _conn, _key: {
            "job_id": 12,
            "doc_key": "policy/x",
            "version_id": None,
            "status": "failed",
            "retry_count": 3,
            "error": "unreadable: PermissionError",
            "created_at": now,
            "started_at": now,
            "finished_at": now,
        },
    )

    with _client() as client:
        body = client.get("/api/v1/documents/policy/x").json()

    assert body["last_error"] == "unreadable: PermissionError"
    assert body["latest_job"]["retry_budget_left"] == 0  # 终态不再重试


def test_scan_route_is_registered_under_api_v1() -> None:
    """路径前缀写死在附录 C 里，改前缀等于改契约。"""
    with _client() as client:
        paths = {route.path for route in client.app.routes}  # type: ignore[attr-defined]
    assert "/api/v1/ingest/scan" in paths
    assert "/api/v1/documents/{doc_key:path}" in paths
