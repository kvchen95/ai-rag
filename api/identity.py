"""身份快照（详细设计.md §5.1）。.

在线链路的第一个动作不是理解问题，是认人：`能不能看` 决定 `能不能搜`。
这个模块只做一件事——把登录凭证换成一份四字段快照：

    {user_id, depts, roles, admin}

四条口径写死在这里，不在调用点各写一遍：

1. **凭证无效 → 401**（`auth_invalid`），不进入检索。签名错、过期、alg 不符、
   缺 `sub`，全部按无效处理。alg 只认配置里那一个：接受 `none` 或"按 token 头部
   自选 alg"是经典的算法混淆漏洞。
2. **valid 但没有部门 → 空集**，只能看公开文档（§5.2 边界表）。这在新入职还没
   分配部门的人身上天天发生，不能当成"没有权限"直接拒——库里还有公开制度。
3. **管理员可以跨部门，但审计里必须可辨**。快照自己带 `audit_flags()`，
   调用方不用记得加这个标记。
4. **部门与角色是数组且去重排序**：用单值会在调岗时出问题；顺序不定则同一份
   条件每次生成不同的过滤表达式，日志和缓存都对不上。

不在这里做的事：越权判断（403 属于闸二/闸三，见 §11.1）与"人有没有这个部门"
（身份不在本系统维护，§5.1 第一句）。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Literal

import jwt
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.config import Settings
from api.errors import AppError, ErrorCode
from api.observability import get_logger, log_event
from api.resources import messages as msg

logger = get_logger("api.identity")

# 开发夹具身份用的请求头（AUTH_MODE=mock_header 时才读）。
HEADER_USER_ID = "X-User-Id"
HEADER_DEPTS = "X-User-Depts"
HEADER_ROLES = "X-User-Roles"
HEADER_ADMIN = "X-User-Admin"

ADMIN_AUDIT_FLAG = "admin_access"


def _normalise_claim_list(value: Any, *, claim: str) -> tuple[str, ...]:
    """把部门/角色 claim 归一成去重排序的元组。.

    只接受三种形状：数组、逗号分隔的字符串、空/缺省。其余一律按凭证无效处理——
    静默把 dict 或数字当成"没有部门"会让人误以为只是权限小，而不是凭证坏了。
    """
    if value is None:
        return ()
    if isinstance(value, str):
        items = [
            item for item in (part.strip() for part in value.split(",")) if item
        ]
    elif isinstance(value, list | tuple | set | frozenset):
        items = []
        for item in value:
            if not isinstance(item, str):
                raise _invalid(
                    msg.CLAIM_ITEM_TYPE.format(
                        claim=claim, type_name=type(item).__name__
                    )
                )
            item = item.strip()
            if item:
                items.append(item)
    else:
        raise _invalid(
            msg.CLAIM_SHAPE.format(claim=claim, type_name=type(value).__name__)
        )
    return tuple(sorted(set(items)))


def _invalid(detail: str) -> AppError:
    """Run _invalid."""
    return AppError(ErrorCode.AUTH_INVALID, detail=detail)


class IdentitySnapshot(BaseModel):
    """一份身份快照。四个字段之外不再扩展（§5.1：每多一个字段要改三处）。."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1, description=msg.DESC_USER_ID)
    depts: tuple[str, ...] = Field(
        default_factory=tuple, description=msg.DESC_DEPTS
    )
    roles: tuple[str, ...] = Field(
        default_factory=tuple, description=msg.DESC_ROLES
    )
    admin: bool = Field(default=False, description=msg.DESC_ADMIN)

    @field_validator("user_id")
    @classmethod
    def _user_id_not_blank(cls, value: str) -> str:
        """Run _user_id_not_blank."""
        value = value.strip()
        if not value:
            raise ValueError(msg.USER_ID_BLANK)
        return value

    @field_validator("depts", "roles", mode="before")
    @classmethod
    def _clean_list(cls, value: Any) -> tuple[str, ...]:
        """Run _clean_list."""
        if value is None:
            return ()
        if isinstance(value, str):
            return _normalise_claim_list(value, claim="depts/roles")
        return _normalise_claim_list(value, claim="depts/roles")

    @property
    def is_public_only(self) -> bool:
        """没有任何部门与角色的人：只能看公开文档（§5.2 边界表）。.

        单独给一个可命名的判断，是为了让日志与测试都能指着它说话，
        而不是在各处写 `not x.depts and not x.roles`。
        """
        return not self.depts and not self.roles

    def audit_flags(self) -> tuple[str, ...]:
        """需要额外写进审计的标记（§5.1：管理员跨部门访问必须可辨）。."""
        return (ADMIN_AUDIT_FLAG,) if self.admin else ()

    def to_audit(self) -> dict[str, Any]:
        """qa_log 的四列身份快照。字段名与 db/migrations/001_init.sql 对齐。."""
        return {
            "user_id": self.user_id,
            "user_depts": list(self.depts),
            "user_roles": list(self.roles),
            "is_admin": self.admin,
        }


class IdentityResolver:
    """凭证 → 快照。两种模式：`fixed_jwt`（默认）与 `mock_header`（仅开发）。.

    两种模式互斥且都在配置层校验过必填项（见 `Settings._check_cross_field`），
    所以这里不需要在运行时猜"没配 secret 是不是想用 mock"。
    """

    def __init__(self, settings: Settings) -> None:
        """Initialize the instance."""
        self._settings = settings
        self._secret = settings.auth_jwt_secret.get_secret_value()

    @property
    def mode(self) -> Literal["fixed_jwt", "mock_header"]:
        """Run mode."""
        return self._settings.auth_mode

    def resolve(
        self,
        *,
        authorization: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> IdentitySnapshot:
        """Run resolve."""
        if self.mode == "mock_header":
            return self._from_headers(headers or {})
        return self._from_authorization(authorization)

    # --- fixed_jwt ---

    def _from_authorization(
        self, authorization: str | None
    ) -> IdentitySnapshot:
        """Run _from_authorization."""
        if not authorization or not authorization.strip():
            raise _invalid(msg.MISSING_AUTHORIZATION)
        scheme, _, token = authorization.strip().partition(" ")
        if scheme.lower() != "bearer":
            raise _invalid(
                msg.SCHEME_NOT_BEARER.format(scheme=scheme or msg.EMPTY_SCHEME)
            )
        token = token.strip()
        if not token:
            raise _invalid(msg.MISSING_BEARER_TOKEN)
        claims = self._decode(token)
        return self._snapshot_from_claims(claims)

    def _decode(self, token: str) -> dict[str, Any]:
        """Run _decode."""
        s = self._settings
        try:
            claims = jwt.decode(
                token,
                key=self._secret,
                # 只接受配置里那一个 alg：把 token 头部的 alg 当权威
                # 是算法混淆漏洞。
                algorithms=[s.auth_jwt_alg],
                # 没有 exp 的 token 永不过期，等于发了一张长期通行证。
                options={"require": ["exp"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise _invalid(msg.TOKEN_EXPIRED) from exc
        except jwt.InvalidSignatureError as exc:
            raise _invalid(msg.TOKEN_BAD_SIGNATURE) from exc
        except jwt.InvalidAlgorithmError as exc:
            raise _invalid(
                msg.TOKEN_BAD_ALG.format(alg=s.auth_jwt_alg)
            ) from exc
        except jwt.InvalidTokenError as exc:
            # 解码失败的原因（格式/claims/必须项缺失）只进 detail，
            # 不回显给用户。
            raise _invalid(
                msg.TOKEN_UNPARSEABLE.format(error_type=type(exc).__name__)
            ) from exc
        if not isinstance(claims, dict):
            raise _invalid(msg.TOKEN_PAYLOAD_NOT_OBJECT)
        return claims

    def _snapshot_from_claims(
        self, claims: Mapping[str, Any]
    ) -> IdentitySnapshot:
        """Run _snapshot_from_claims."""
        s = self._settings
        raw_user = claims.get(s.auth_jwt_user_id_claim)
        if raw_user is None:
            raise _invalid(
                msg.TOKEN_MISSING_CLAIM.format(claim=s.auth_jwt_user_id_claim)
            )
        if isinstance(raw_user, int) and not isinstance(raw_user, bool):
            raw_user = str(raw_user)
        if not isinstance(raw_user, str) or not raw_user.strip():
            raise _invalid(
                msg.CLAIM_NOT_NONEMPTY.format(claim=s.auth_jwt_user_id_claim)
            )

        depts = _normalise_claim_list(
            claims.get(s.auth_jwt_depts_claim), claim=s.auth_jwt_depts_claim
        )
        roles = _normalise_claim_list(
            claims.get(s.auth_jwt_roles_claim), claim=s.auth_jwt_roles_claim
        )
        admin = self._admin_from_claim(claims.get(s.auth_jwt_admin_claim))
        snapshot = IdentitySnapshot(
            user_id=raw_user, depts=depts, roles=roles, admin=admin
        )
        self._log_identity(snapshot)
        return snapshot

    @staticmethod
    def _admin_from_claim(value: Any) -> bool:
        """只认布尔与明确的字符串写法，其余按非管理员处理。.

        刻意**不**用 `bool(value)`：`bool("false")` 是 True，一个把 admin 写成
        字符串 "false" 的上游会把所有人变成管理员。
        """
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes"):
                return True
            if lowered in ("false", "0", "no", ""):
                return False
        return False

    # --- mock_header（仅开发）---

    def _from_headers(self, headers: Mapping[str, str]) -> IdentitySnapshot:
        """Run _from_headers."""
        lowered = {key.lower(): value for key, value in headers.items()}
        user_id = (
            lowered.get(HEADER_USER_ID.lower())
            if lowered.get(HEADER_USER_ID.lower())
            else ""
        ).strip()
        if not user_id:
            raise _invalid(
                msg.MISSING_USER_HEADER.format(header=HEADER_USER_ID)
            )
        snapshot = IdentitySnapshot(
            user_id=user_id,
            depts=_normalise_claim_list(
                lowered.get(HEADER_DEPTS.lower()), claim="depts"
            ),
            roles=_normalise_claim_list(
                lowered.get(HEADER_ROLES.lower()), claim="roles"
            ),
            admin=self._admin_from_claim(lowered.get(HEADER_ADMIN.lower())),
        )
        self._log_identity(snapshot)
        return snapshot

    # --- 日志 ---

    def _log_identity(self, snapshot: IdentitySnapshot) -> None:
        """管理员访问单独打一条 ERROR 级不可忽略的事件（§5.1 审计可辨）。.

        user_id 进日志是刻意的：它和 qa_log 的审计行是同一个标识，两者合起来
        才能回答"谁在什么时候看了什么"。日志本身不含问题与原文。
        """
        log_event(
            logger,
            "identity_resolved",
            user_id=snapshot.user_id,
            depts=list(snapshot.depts),
            roles=list(snapshot.roles),
            admin=snapshot.admin,
            auth_mode=self.mode,
        )
        if snapshot.admin:
            log_event(
                logger,
                ADMIN_AUDIT_FLAG,
                level=40,
                user_id=snapshot.user_id,
                depts=list(snapshot.depts),
            )


def mint_dev_token(
    settings: Settings,
    *,
    user_id: str,
    depts: tuple[str, ...] = (),
    roles: tuple[str, ...] = (),
    admin: bool = False,
    ttl_s: int = 3600,
) -> str:
    """签发一枚开发用 token。.

    测试、curl、前端联调都靠它，避免各自手搓 claim 名。

    生产环境拒绝调用——把签发能力留在生产进程里，等于给了一个"谁都能当"的入口。
    """
    if settings.is_prod:
        raise RuntimeError(msg.MINT_TOKEN_PROD_FORBIDDEN)
    if settings.auth_mode != "fixed_jwt":
        raise RuntimeError(
            msg.MINT_TOKEN_WRONG_MODE.format(mode=settings.auth_mode)
        )
    now = int(time.time())
    claims = {
        settings.auth_jwt_user_id_claim: user_id,
        settings.auth_jwt_depts_claim: list(depts),
        settings.auth_jwt_roles_claim: list(roles),
        settings.auth_jwt_admin_claim: admin,
        "iat": now,
        "exp": now + ttl_s,
    }
    return jwt.encode(
        claims,
        settings.auth_jwt_secret.get_secret_value(),
        algorithm=settings.auth_jwt_alg,
    )
