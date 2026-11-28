"""样本与切片图两条只读接口的契约。.

覆盖 `/api/v1/samples/{doc_key}.pdf` 与
`/api/v1/chunks/{chunk_id}/figure`。

这两条是"引用角标点得回去"的物质基础：一条给原文，一条给切片图。它们最容易被
写成"能返回文件就行"，所以这里盯三件走真 HTTP 才看得见的事：

1. **404 与 403 分开**：文件不在是运维事实（404），文件在但看不见是权限
   事实（403）；
2. **401 在开文件之前**：凭证无效必须是 401，不能让一个匿名请求触发磁盘读取；
3. **响应体不带细节**：§11.3 的 `detail` 只进日志——错误 JSON 里不许出现路径、
   真名、SQL、异常类名。

PG 用替身：这里验的是路由这一层（状态码、可见性判断的调用顺序、路径校验），
真库上的可见性语义由 B2 的 filters 用例负责，不重复。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.tests.conftest import make_settings

FIN_HEADERS = {
    "X-User-Id": "u_fin",
    "X-User-Depts": "fin",
    "X-User-Roles": "staff",
}


class FakePg:
    """只回答两个问题：这份文档可见吗、这个切片可见吗。."""

    def __init__(
        self,
        *,
        visible: bool = True,
        image_path: str | None = "figures/ch_1.webp",
    ) -> None:
        """Initialize the instance."""
        self.visible = visible
        self.image_path = image_path
        self.queries: list[str] = []

    def fetch_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        """Run fetch_one."""
        self.queries.append(sql)
        if "FROM chunks" in sql:
            return {"image_path": self.image_path} if self.image_path else None
        return {"?column?": 1} if self.visible else None

    def fetch_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        """Run fetch_all."""
        self.queries.append(sql)
        return []

    def execute(
        self, sql: str, params: Any = None
    ) -> int:  # pragma: no cover - 只读路由
        """Run execute."""
        raise AssertionError("这些接口不许写库")

    def ping(self, timeout_s: float | None = None) -> dict[str, Any]:
        """/healthz 会调用它；这些用例不测健康检查，给个固定形状即可。."""
        return {"ok": True, "latency_ms": 0.0, "error": None}


def _client(tmp_path: Path, pg: FakePg) -> TestClient:
    """Run _client."""
    settings = make_settings(
        AUTH_MODE="mock_header",
        INGEST_SAMPLES_DIR=str(tmp_path / "samples"),
        FIGURES_DIR=str(tmp_path / "figures"),
    )
    return TestClient(create_app(settings, pg=pg))


@pytest.fixture()
def samples_root(tmp_path: Path) -> Path:
    """Run samples_root."""
    root = tmp_path / "samples"
    (root / "policy").mkdir(parents=True)
    (root / "policy" / "travel-cn.pdf").write_bytes(b"%PDF-1.4\n% sample\n")
    return root


def test_serves_the_pdf_when_visible(
    samples_root: Path, tmp_path: Path
) -> None:
    """Run test_serves_the_pdf_when_visible."""
    with _client(tmp_path, FakePg(visible=True)) as client:
        response = client.get(
            "/api/v1/samples/policy/travel-cn.pdf", headers=FIN_HEADERS
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-1.4")


def test_404_when_the_sample_file_is_missing(
    samples_root: Path, tmp_path: Path
) -> None:
    """Run test_404_when_the_sample_file_is_missing."""
    with _client(tmp_path, FakePg(visible=True)) as client:
        response = client.get(
            "/api/v1/samples/policy/expense-cn.pdf", headers=FIN_HEADERS
        )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    # 不泄漏路径：detail 只进日志。
    assert "samples" not in body["error"]["message"]
    assert "/tmp" not in response.text


def test_403_when_the_identity_cannot_see_the_document(
    samples_root: Path, tmp_path: Path
) -> None:
    """Run test_403_when_the_identity_cannot_see_the_document."""
    with _client(tmp_path, FakePg(visible=False)) as client:
        response = client.get(
            "/api/v1/samples/policy/travel-cn.pdf", headers=FIN_HEADERS
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert "travel" not in response.json()["error"]["message"]


def test_401_before_touching_the_disk(
    samples_root: Path, tmp_path: Path
) -> None:
    """Run test_401_before_touching_the_disk."""
    pg = FakePg(visible=True)
    with _client(tmp_path, pg) as client:
        response = client.get("/api/v1/samples/policy/travel-cn.pdf")
    assert response.status_code == 401
    assert pg.queries == [], "身份无效时不该查库，更不该读文件"


@pytest.mark.parametrize(
    "doc_key",
    [
        "..%2F..%2Fetc%2Fpasswd",
        "policy%2F..%2F..%2Fsecret",
        ".env",
    ],
)
def test_path_traversal_is_a_404(
    doc_key: str, samples_root: Path, tmp_path: Path
) -> None:
    """Run test_path_traversal_is_a_404."""
    with _client(tmp_path, FakePg(visible=True)) as client:
        response = client.get(
            f"/api/v1/samples/{doc_key}.pdf", headers=FIN_HEADERS
        )
    assert response.status_code == 404


def test_figure_served_when_the_webp_exists(
    samples_root: Path, tmp_path: Path
) -> None:
    """Run test_figure_served_when_the_webp_exists."""
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "ch_1.webp").write_bytes(b"RIFFxxxxWEBPVP8 ")
    with _client(
        tmp_path, FakePg(visible=True, image_path="figures/ch_1.webp")
    ) as client:
        response = client.get("/api/v1/chunks/ch_1/figure", headers=FIN_HEADERS)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content.startswith(b"RIFF")


def test_figure_404_when_the_chunk_has_no_image(
    samples_root: Path, tmp_path: Path
) -> None:
    """Run test_figure_404_when_the_chunk_has_no_image."""
    with _client(tmp_path, FakePg(visible=True, image_path=None)) as client:
        response = client.get("/api/v1/chunks/ch_2/figure", headers=FIN_HEADERS)
    assert response.status_code == 404


def test_figure_404_when_the_file_was_never_written(
    samples_root: Path, tmp_path: Path
) -> None:
    # 库里有 image_path，盘上没有文件（回收脚本删了图但没删行）。
    """Run test_figure_404_when_the_file_was_never_written."""
    with _client(
        tmp_path, FakePg(visible=True, image_path="figures/ch_9.webp")
    ) as client:
        response = client.get("/api/v1/chunks/ch_9/figure", headers=FIN_HEADERS)
    assert response.status_code == 404
    assert "figures" not in response.json()["error"]["message"]


def test_figure_403_when_the_chunk_is_not_visible(
    samples_root: Path, tmp_path: Path
) -> None:
    """Run test_figure_403_when_the_chunk_is_not_visible."""
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "ch_1.webp").write_bytes(b"RIFFxxxx")
    with _client(
        tmp_path, FakePg(visible=False, image_path="figures/ch_1.webp")
    ) as client:
        response = client.get("/api/v1/chunks/ch_1/figure", headers=FIN_HEADERS)
    assert response.status_code == 403
