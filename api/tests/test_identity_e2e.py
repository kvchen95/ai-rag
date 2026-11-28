"""身份 → 过滤 → 审计的端到端串联（§5 → §11.2）。.

单测已经覆盖了每一块；这里验的是"拼起来还成立"的三件事：

1. 凭证无效走 HTTP 401，且响应体不泄漏内部细节（§11.3 的 auth_invalid）。
2. 同一份身份同时决定过滤条件与审计快照——两处如果不一致，
   就出现"按 A 的权限搜、记 B 的账"，事后无法追查。
3. 管理员访问在审计里可辨（§5.1 最后一句）。

不需要真库：身份与过滤是纯函数，审计行的形状由 QaLogEntry 校验。
真库侧的写入由 test_audit_integration.py 覆盖（B1 卡）。
"""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.audit import QaLogEntry
from api.deps import IdentityDep
from api.errors import install_error_handlers
from api.filters import build_filter
from api.identity import mint_dev_token
from api.tests.conftest import make_settings

SECRET = "e2e-secret"


def _app(**overrides: object) -> FastAPI:
    """最小 app：只装身份依赖、错误映射与一个示例路由。."""
    values: dict[str, object] = {"AUTH_JWT_SECRET": SECRET}
    values.update(overrides)
    settings = make_settings(**values)

    app = FastAPI()
    app.state.settings = settings
    install_error_handlers(app)

    @app.get("/whoami")
    def whoami(identity: IdentityDep) -> dict[str, object]:
        """Run whoami."""
        flt = build_filter(identity, doc_keys=["policy/travel-cn"])
        return {
            "identity": identity.model_dump(),
            "filter": flt.describe(),
            "audit": identity.to_audit(),
        }

    return app


def _token(settings, **claims: object) -> str:
    """Run _token."""
    payload: dict[str, object] = {
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    payload.update(claims)
    return jwt.encode(
        payload,
        settings.auth_jwt_secret.get_secret_value(),
        algorithm=settings.auth_jwt_alg,
    )


def test_missing_credential_is_401_with_auth_invalid_code() -> None:
    """Run test_missing_credential_is_401_with_auth_invalid_code."""
    client = TestClient(_app())
    resp = client.get("/whoami")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "auth_invalid"
    assert body["error"]["message"] == "请重新登录"
    # 内部细节只进日志，不进响应体。
    assert "Authorization" not in body["error"]["message"]
    assert body["error"]["request_id"]


def test_valid_credential_flows_identity_into_filter_and_audit() -> None:
    """Run test_valid_credential_flows_identity_into_filter_and_audit."""
    settings = make_settings(AUTH_JWT_SECRET=SECRET)
    token = mint_dev_token(
        settings, user_id="u_fin", depts=("fin", "corp"), roles=("staff",)
    )
    client = TestClient(_app())
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()

    identity = body["identity"]
    assert identity["user_id"] == "u_fin"
    assert identity["depts"] == ["corp", "fin"]

    # 过滤条件用的是同一份身份（不是从别处再取一次）。
    assert body["filter"]["acl_branches"] == ["public", "depts", "roles"]
    assert body["filter"]["user_id"] == "u_fin"

    # 审计快照也是同一份：三处一致才能在事后还原"按谁的权限搜的"。
    assert body["audit"] == {
        "user_id": "u_fin",
        "user_depts": ["corp", "fin"],
        "user_roles": ["staff"],
        "is_admin": False,
    }


def test_audit_snapshot_satisfies_qa_log_contract() -> None:
    """身份快照要能直接塞进 qa_log 的行模型，字段名不能各写一套。."""
    settings = make_settings(AUTH_JWT_SECRET=SECRET)
    token = mint_dev_token(settings, user_id="u_x", depts=("fin",), admin=True)
    client = TestClient(_app())
    body = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert body["identity"]["admin"] is True
    entry = QaLogEntry(
        **body["audit"],
        question="差旅住宿标准",
        gate1_verdict="pass",
        latency_ms=12,
    )
    assert entry.user_depts == ["fin"]
    assert entry.is_admin is True


def test_admin_access_is_visible_in_both_filter_and_audit() -> None:
    """§5.1：管理员跨部门访问必须在审计里可辨——审计列 + 过滤描述的可辨标记。."""
    settings = make_settings(AUTH_JWT_SECRET=SECRET)
    token = mint_dev_token(settings, user_id="u_admin", depts=(), admin=True)
    client = TestClient(_app())
    body = client.get(
        "/whoami", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert body["audit"]["is_admin"] is True
    assert body["filter"]["audit_flags"] == ["admin_access"]
    # 管理员不带 ACL 分支，但 status=active 仍在（版本兜底另算）。
    assert body["filter"]["acl_branches"] == []


def test_no_depts_user_is_not_rejected() -> None:
    """新入职没分配部门：能登录、能看公开，而不是 403。.

    这条最容易被写成"拒绝"。
    """
    settings = make_settings(AUTH_JWT_SECRET=SECRET)
    token = mint_dev_token(settings, user_id="u_new")
    client = TestClient(_app())
    resp = client.get("/whoami", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["filter"]["public_only"] is True
    assert body["filter"]["acl_branches"] == ["public"]


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer not-a-jwt",
        "Basic dXNlcjpwYXNz",
        "Bearer " + "a" * 40,
    ],
)
def test_every_bad_authorization_shape_is_401(header: str) -> None:
    """Run test_every_bad_authorization_shape_is_401."""
    client = TestClient(_app())
    resp = client.get(
        "/whoami", headers={"Authorization": header} if header else {}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "auth_invalid"


def test_mock_header_mode_end_to_end() -> None:
    """Run test_mock_header_mode_end_to_end."""
    client = TestClient(
        _app(AUTH_MODE="mock_header", AUTH_MOCK_USER_ID="u_mock")
    )
    resp = client.get(
        "/whoami",
        headers={
            "X-User-Id": "u_mock",
            "X-User-Depts": "hr",
            "X-User-Roles": "staff",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["identity"]["depts"] == ["hr"]
    assert body["filter"]["acl_branches"] == ["public", "depts", "roles"]
