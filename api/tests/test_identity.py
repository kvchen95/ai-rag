"""身份解析（§5.1）：四种取值 + 三种边界，每种都验正反两个方向。

要点：
- 无部门/多部门/管理员是卡片点名的三条，各自验"能通过"与"行为不同"；
- 401 的每种成因都要有一条，否则"凭证无效 → 401"只是句口号；
- 断言落在快照内容与抛出的错误码上，不落在日志文本上——日志会改，语义不会。
"""

from __future__ import annotations

import time

import jwt
import pytest

from api.config import Settings
from api.errors import AppError, ErrorCode
from api.identity import (
    ADMIN_AUDIT_FLAG,
    HEADER_ADMIN,
    HEADER_DEPTS,
    HEADER_ROLES,
    HEADER_USER_ID,
    IdentityResolver,
    IdentitySnapshot,
    mint_dev_token,
)
from api.tests.conftest import make_settings

SECRET = "unit-test-secret"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"AUTH_JWT_SECRET": SECRET, "AUTH_MODE": "fixed_jwt"}
    values.update(overrides)
    return make_settings(**values)


def _token(settings: Settings, **claims: object) -> str:
    """手搓 token：测试要能造出"缺 exp""admin 是字符串"这类畸形凭证。"""
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


# --- 三种身份形态（卡片点名）------------------------------------------------


def test_no_depts_means_public_only() -> None:
    settings = _settings()
    resolver = IdentityResolver(settings)
    snap = resolver.resolve(
        authorization=f"Bearer {_token(settings, sub='u_new', user_depts=[], user_roles=[])}"
    )
    assert snap.depts == () and snap.roles == ()
    assert snap.is_public_only is True
    # 关键行为：不是"拒绝访问"，而是仍能看公开文档——新员工天天碰到这种情况。
    assert snap.user_id == "u_new"


def test_multiple_depts_and_roles_are_kept_as_sets() -> None:
    settings = _settings()
    resolver = IdentityResolver(settings)
    snap = resolver.resolve(
        authorization=f"Bearer {_token(settings, sub='u_multi', user_depts=['fin', 'corp', 'fin'], user_roles=['staff', 'approver'])}"
    )
    # 去重 + 排序：顺序不定会让同一个人的过滤表达式每次都不同，日志与缓存都对不上。
    assert snap.depts == ("corp", "fin")
    assert snap.roles == ("approver", "staff")
    assert snap.is_public_only is False


def test_depts_as_comma_string_is_accepted() -> None:
    """上游塞逗号串比塞数组常见得多；两种都收，但都要归一成同一形状。"""
    settings = _settings()
    resolver = IdentityResolver(settings)
    snap = resolver.resolve(
        authorization=f"Bearer {_token(settings, sub='u_str', user_depts='fin, corp')}"
    )
    assert snap.depts == ("corp", "fin")


def test_admin_is_marked_for_audit() -> None:
    settings = _settings()
    resolver = IdentityResolver(settings)
    snap = resolver.resolve(
        authorization=f"Bearer {_token(settings, sub='u_admin', user_depts=[], admin=True)}"
    )
    assert snap.admin is True
    # §5.1：管理员跨部门访问必须在审计里可辨——标记由快照自己给出，不靠调用方记得加。
    assert snap.audit_flags() == (ADMIN_AUDIT_FLAG,)
    assert snap.to_audit()["is_admin"] is True


def test_non_admin_has_no_audit_flag() -> None:
    settings = _settings()
    snap = IdentityResolver(settings).resolve(
        authorization=f"Bearer {_token(settings, sub='u_norm', user_depts=['fin'])}"
    )
    assert snap.audit_flags() == ()


@pytest.mark.parametrize("value", ["false", "0", "no", "", None, 0])
def test_admin_flag_is_not_truthy_coerced(value: object) -> None:
    """`bool("false")` 是 True。上游把 admin 写成字符串 "false" 时，
    用真值转换会把所有人变成管理员，所以只认布尔与明确的写法。"""
    settings = _settings()
    snap = IdentityResolver(settings).resolve(
        authorization=f"Bearer {_token(settings, sub='u_x', admin=value)}"
    )
    assert snap.admin is False


@pytest.mark.parametrize("value", [True, "true", "1", "yes", "TRUE"])
def test_admin_flag_accepts_explicit_true(value: object) -> None:
    # 反方向：真值必须被认出来，否则上面那条用例可以靠"永远返回 False"通过。
    settings = _settings()
    snap = IdentityResolver(settings).resolve(
        authorization=f"Bearer {_token(settings, sub='u_x', admin=value)}"
    )
    assert snap.admin is True


# --- 401 的每一种成因 -------------------------------------------------------


def test_missing_authorization_is_401() -> None:
    resolver = IdentityResolver(_settings())
    with pytest.raises(AppError) as excinfo:
        resolver.resolve(authorization=None)
    assert excinfo.value.code is ErrorCode.AUTH_INVALID
    assert excinfo.value.spec.http_status == 401


@pytest.mark.parametrize(
    ("authorization", "needle"),
    [
        ("", "缺少 Authorization"),
        ("Basic dXNlcjpwYXNz", "Bearer"),
        ("Bearer", "没有 token"),
        ("Bearer   ", "没有 token"),
    ],
)
def test_malformed_authorization_scheme_is_401(authorization: str, needle: str) -> None:
    resolver = IdentityResolver(_settings())
    with pytest.raises(AppError) as excinfo:
        resolver.resolve(authorization=authorization)
    assert excinfo.value.code is ErrorCode.AUTH_INVALID
    assert needle in (excinfo.value.detail or "")


def test_expired_token_is_401() -> None:
    settings = _settings()
    token = _token(settings, sub="u_x", exp=int(time.time()) - 10)
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(authorization=f"Bearer {token}")
    assert excinfo.value.code is ErrorCode.AUTH_INVALID
    assert "过期" in (excinfo.value.detail or "")


def test_token_without_exp_is_401() -> None:
    """没有 exp 的 token 永不过期 = 一张长期通行证。必须拒。"""
    settings = _settings()
    token = jwt.encode(
        {"sub": "u_x"}, settings.auth_jwt_secret.get_secret_value(), algorithm="HS256"
    )
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(authorization=f"Bearer {token}")
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


def test_wrong_signature_is_401() -> None:
    settings = _settings()
    forged = jwt.encode(
        {"sub": "u_x", "exp": int(time.time()) + 600},
        "another-secret",
        algorithm="HS256",
    )
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(authorization=f"Bearer {forged}")
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


def test_algorithm_mismatch_is_401() -> None:
    """alg 只认配置里那一个：按 token 头部自选 alg 是算法混淆漏洞。"""
    settings = _settings()
    signed = jwt.encode(
        {"sub": "u_x", "exp": int(time.time()) + 600}, SECRET, algorithm="HS512"
    )
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(authorization=f"Bearer {signed}")
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


def test_unsigned_token_is_401() -> None:
    settings = _settings()
    unsigned = jwt.encode(
        {"sub": "u_x", "exp": int(time.time()) + 600}, None, algorithm="none"
    )
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(authorization=f"Bearer {unsigned}")
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


def test_missing_user_id_claim_is_401() -> None:
    settings = _settings()
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(
            authorization=f"Bearer {_token(settings, user_depts=['fin'])}"
        )
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


@pytest.mark.parametrize("claim_value", [[1, 2], {"fin": True}, 42])
def test_non_string_claim_elements_are_401(claim_value: object) -> None:
    """静默把 dict/数字当成"没有部门"最危险：用户以为只是权限小，其实凭证坏了。"""
    settings = _settings()
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(
            authorization=f"Bearer {_token(settings, sub='u_x', user_depts=claim_value)}"
        )
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


def test_numeric_sub_is_rejected_by_rfc_string_rule() -> None:
    """`sub` 按 RFC 7519 必须是字符串：PyJWT 在解码阶段就拒，比我们更早。
    这条不是缺陷而是标准行为——数字主键的 IdP 要配 `AUTH_JWT_USER_ID_CLAIM=uid` 之类的
    自定义 claim（见下一条），而不是指望我们把 sub 猜成字符串。
    """
    settings = _settings()
    with pytest.raises(AppError) as excinfo:
        IdentityResolver(settings).resolve(
            authorization=f"Bearer {_token(settings, sub=1024)}"
        )
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


def test_numeric_custom_claim_is_coerced() -> None:
    """自定义 claim 不带 RFC 校验，数字主键在这一路归一成字符串。"""
    settings = _settings(AUTH_JWT_USER_ID_CLAIM="uid")
    snap = IdentityResolver(settings).resolve(
        authorization=f"Bearer {_token(settings, uid=1024)}"
    )
    assert snap.user_id == "1024"


# --- 快照自身的不变量 -------------------------------------------------------


def test_snapshot_rejects_blank_user_id() -> None:
    with pytest.raises(ValueError):
        IdentitySnapshot(user_id="   ")


def test_snapshot_is_frozen() -> None:
    snap = IdentitySnapshot(user_id="u_x")
    with pytest.raises(ValueError):
        snap.admin = True  # type: ignore[misc]


def test_snapshot_forbids_extra_fields() -> None:
    """§5.1：四个字段之外不再扩展——多一个字段要在三处同步改。"""
    with pytest.raises(ValueError):
        IdentitySnapshot(user_id="u_x", tenant="t1")  # type: ignore[call-arg]


def test_to_audit_matches_qa_log_columns() -> None:
    snap = IdentitySnapshot(
        user_id="u_x", depts=("fin",), roles=("staff",), admin=False
    )
    assert snap.to_audit() == {
        "user_id": "u_x",
        "user_depts": ["fin"],
        "user_roles": ["staff"],
        "is_admin": False,
    }


# --- mock_header 模式（仅开发）---------------------------------------------


def test_mock_header_mode_reads_identity_from_headers() -> None:
    settings = _settings(
        AUTH_MODE="mock_header", AUTH_MOCK_USER_ID="", AUTH_MOCK_DEPTS=""
    )
    resolver = IdentityResolver(settings)
    snap = resolver.resolve(
        headers={
            HEADER_USER_ID: "u_dev",
            HEADER_DEPTS: "fin,corp",
            HEADER_ROLES: "staff",
            HEADER_ADMIN: "false",
        }
    )
    assert snap.user_id == "u_dev"
    assert snap.depts == ("corp", "fin")
    assert snap.admin is False


def test_mock_header_mode_requires_user_header() -> None:
    resolver = IdentityResolver(_settings(AUTH_MODE="mock_header"))
    with pytest.raises(AppError) as excinfo:
        resolver.resolve(headers={HEADER_DEPTS: "fin"})
    assert excinfo.value.code is ErrorCode.AUTH_INVALID


# --- 签发工具 ---------------------------------------------------------------


def test_mint_dev_token_round_trips() -> None:
    settings = _settings()
    token = mint_dev_token(
        settings, user_id="u_dev", depts=("fin",), roles=("staff",), admin=True
    )
    snap = IdentityResolver(settings).resolve(authorization=f"Bearer {token}")
    assert (snap.user_id, snap.depts, snap.roles, snap.admin) == (
        "u_dev",
        ("fin",),
        ("staff",),
        True,
    )


def test_mint_dev_token_refuses_in_prod() -> None:
    """签发能力留在生产进程里 = 一个谁都能当的入口。"""
    with pytest.raises(RuntimeError, match="生产"):
        mint_dev_token(_settings(APP_ENV="prod"), user_id="u_dev")


def test_mint_dev_token_refuses_in_mock_mode() -> None:
    with pytest.raises(RuntimeError, match="fixed_jwt"):
        mint_dev_token(_settings(AUTH_MODE="mock_header"), user_id="u_dev")


def test_claims_are_configurable() -> None:
    """claim 名走配置：换 IdP 不该改代码。"""
    settings = _settings(
        AUTH_JWT_USER_ID_CLAIM="uid",
        AUTH_JWT_DEPTS_CLAIM="dept_codes",
        AUTH_JWT_ADMIN_CLAIM="is_admin",
    )
    snap = IdentityResolver(settings).resolve(
        authorization=f"Bearer {_token(settings, uid='u_cfg', dept_codes=['hr'], is_admin=True)}"
    )
    assert snap.user_id == "u_cfg"
    assert snap.depts == ("hr",)
    assert snap.admin is True
