"""FastAPI 依赖：把凭证换成身份快照与过滤条件。

为什么放一层依赖而不是在路由里 `resolver.resolve(...)`：
路由函数只需要声明 `identity: IdentitySnapshot = Depends(require_identity)`，
401 与 403 的映射、审计标记、日志就都在同一处发生。B3 的 `/qa/stream` 只要声明
这一个参数，就不会出现"忘了看 Authorization 头"这种缺口。

`app.state` 而不是模块级单例：`create_app()` 可以被调用多次（测试里每次一个），
模块级缓存会把上一个 app 的配置带过来。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from api.identity import (
    HEADER_DEPTS,
    HEADER_ROLES,
    HEADER_USER_ID,
    IdentityResolver,
    IdentitySnapshot,
)


def get_identity_resolver(request: Request) -> IdentityResolver:
    resolver = getattr(request.app.state, "identity_resolver", None)
    if resolver is None:
        # 懒建而不是在 lifespan 里建：没有身份的路径（healthz/livez）不该因为
        # 身份配置有问题就起不来。
        resolver = IdentityResolver(request.app.state.settings)
        request.app.state.identity_resolver = resolver
    return resolver


def require_identity(
    request: Request,
    resolver: Annotated[IdentityResolver, Depends(get_identity_resolver)],
) -> IdentitySnapshot:
    """凭证无效时抛 `auth_invalid`（401），由 errors.py 的表映射成响应。"""
    headers = {
        HEADER_USER_ID: request.headers.get(HEADER_USER_ID, ""),
        HEADER_DEPTS: request.headers.get(HEADER_DEPTS, ""),
        HEADER_ROLES: request.headers.get(HEADER_ROLES, ""),
        "X-User-Admin": request.headers.get("X-User-Admin", ""),
    }
    return resolver.resolve(
        authorization=request.headers.get("authorization"), headers=headers
    )


IdentityDep = Annotated[IdentitySnapshot, Depends(require_identity)]
