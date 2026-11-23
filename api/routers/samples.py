"""样本文件与切片图的直出（详细设计.md 附录 C.1 的 `GET /api/v1/samples/{doc_key}.pdf`、
§3.4/§11.1 的取文件闸二）。

为什么这两个接口必须存在：引用角标要"点回去"就得能拿到原文与切片图。附录 C.1 把
`samples/{doc_key}.pdf` 定成 404/403，§11.1 把"取文件前再核一次权限"定成闸二——
前端构造的 doc_key 是用户可控的，只在检索侧过滤拦不住"直接要文件"。

三条实现口径：

1. **闸二与检索侧同源**。可见性判断直接复用 `build_filter` 的 ACL 片段，查
   `v_active_chunks`（双 active 视图）。这里另写一份 `is_public OR depts` 的 SQL，
   迟早会和检索侧分叉：那样会出现"搜得到但点不开"或者更糟的"搜不到但点得开"。
2. **404 与 403 分得开，但都不带细节**。文件不在 → 404（运维/样本问题）；
   文件在、身份看不见 → 403（§11.1"越权统一 403，不提示文档标题"）。
   两者的响应体都只有 `errors.py` 表里的固定文案，`detail` 只进日志。
3. **路径必须逐段校验**。`doc_key` 是路径参数（带 `/`），`..`、绝对路径、盘符
   一律视为 404：这不是数据问题，是有人在探边界。

切片图（`figures/{chunk_id}.webp`）走同一套：查 chunk 行 → 判可见 → 读文件。
读不到文件是 404，不是 500——切片图是解析产物，缺图不该让整条引用链崩掉，
前端会降级成"该切片没有图表"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from api.clients.pg import PgDatabase
from api.deps import IdentityDep
from api.errors import AppError, ErrorCode
from api.filters import build_filter
from api.identity import IdentitySnapshot
from api.observability import get_logger

logger = get_logger("api.samples")

router = APIRouter(prefix="/api/v1", tags=["samples"])

# 只允许"相对路径 + .pdf"这一种形状。`\` 也挡掉：宿主机是 Windows 时它是分隔符。
_FORBIDDEN_PARTS = ("..", "", ".", "\\")


def _safe_doc_key(doc_key: str) -> str | None:
    """校验 doc_key 形状，返回规范化后的相对路径（不含 .pdf）；不合法返回 None。"""
    if not doc_key or len(doc_key) > 512:
        return None
    if doc_key.startswith("/") or ":" in doc_key or "\x00" in doc_key:
        return None
    parts = doc_key.split("/")
    for part in parts:
        if part in _FORBIDDEN_PARTS:
            return None
        # `.` 开头的段（隐藏文件）也不放行：样本目录里不该有，放行只会多一个探测面。
        if part.startswith("."):
            return None
    return "/".join(parts)


def _resolve_under(root: Path, relative: str) -> Path | None:
    """把相对路径解析到 root 之下；解析后跳出 root 的返回 None。"""
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    return candidate


def doc_is_visible(pg: PgDatabase, identity: IdentitySnapshot, doc_key: str) -> bool:
    """这个身身份能不能看见这份文档的**任何**一个生效切片（闸二）。

    判据是"存在可见切片"，不是"有权限标记"：一份文档的切片 ACL 可能逐片不同
    （§3.3 的六项随行信息里 ACL 跟着切片走）。取不到任何行 = 不存在或全不可见，
    调用方按 403 处理——不区分这两种情况是 §11.1 的要求。
    """
    flt = build_filter(identity, doc_keys=[doc_key])
    where, params = flt.pg_where("c", source="v_active_chunks")
    sql = f"SELECT 1 FROM v_active_chunks AS c WHERE {where} LIMIT 1"
    return pg.fetch_one(sql, params) is not None


def chunk_is_visible(pg: PgDatabase, identity: IdentitySnapshot, chunk_id: str) -> bool:
    """切片级可见性：同一套 ACL 片段，只是把条件收窄到这一个 chunk_id。"""
    flt = build_filter(identity)
    where, params = flt.pg_where("c", source="v_active_chunks")
    sql = f"SELECT 1 FROM v_active_chunks AS c WHERE c.chunk_id = %s AND {where} LIMIT 1"
    return pg.fetch_one(sql, [chunk_id, *params]) is not None


def chunk_image_path(pg: PgDatabase, chunk_id: str) -> str | None:
    row = pg.fetch_one(
        "SELECT image_path FROM chunks WHERE chunk_id = %s ORDER BY created_at DESC LIMIT 1",
        [chunk_id],
    )
    if not row:
        return None
    value = row.get("image_path")
    return value if isinstance(value, str) and value else None


@router.get("/samples/{doc_key:path}.pdf")
async def read_sample_pdf(doc_key: str, request: Request, identity: IdentityDep) -> FileResponse:
    """样本直出：`samples/{doc_key}.pdf`。

    顺序是"先文件、后权限"：文件根本不存在时给 404（这是运维事实），
    文件在但看不见时给 403（这是权限事实）。反过来先查权限也能跑，但运维拿不到
    "样本根本没生成"这个信号。
    """
    settings = request.app.state.settings
    safe = _safe_doc_key(doc_key)
    if safe is None:
        raise AppError(ErrorCode.NOT_FOUND, detail=f"unsafe doc_key: {doc_key!r}")

    root = Path(settings.ingest_samples_dir)
    path = _resolve_under(root, f"{safe}.pdf")
    if path is None or not path.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            detail=f"sample file missing: root={root} doc_key={safe}",
        )

    pg: PgDatabase = request.app.state.pg
    visible = await asyncio.to_thread(doc_is_visible, pg, identity, safe)
    if not visible:
        raise AppError(
            ErrorCode.FORBIDDEN,
            detail=f"doc_key={safe} not visible to user={identity.user_id}",
        )

    logger.info("sample_pdf_served", extra={"doc_key": safe})
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{safe.rsplit('/', 1)[-1]}.pdf",
        headers={"Cache-Control": "private, max-age=60"},
    )


@router.get("/chunks/{chunk_id}/figure")
async def read_chunk_figure(chunk_id: str, request: Request, identity: IdentityDep) -> FileResponse:
    """切片图直出：`{FIGURES_DIR}/{chunk_id}.webp`（§3.4 的裁图）。

    四步都可能 404，而且**不该合并成一个**：chunk 不存在、无 image_path、图没落盘、
    文件被清掉——运维要能分清是哪一种，所以 `detail` 分开写（只进日志）。
    """
    settings = request.app.state.settings
    if not chunk_id or len(chunk_id) > 256 or "/" in chunk_id or "\\" in chunk_id:
        raise AppError(ErrorCode.NOT_FOUND, detail=f"unsafe chunk_id: {chunk_id!r}")

    pg: PgDatabase = request.app.state.pg
    image_path = await asyncio.to_thread(chunk_image_path, pg, chunk_id)
    if image_path is None:
        raise AppError(ErrorCode.NOT_FOUND, detail=f"chunk has no image_path: {chunk_id}")

    visible = await asyncio.to_thread(chunk_is_visible, pg, identity, chunk_id)
    if not visible:
        raise AppError(
            ErrorCode.FORBIDDEN,
            detail=f"chunk not visible: {chunk_id} user={identity.user_id}",
        )

    figures_root = Path(settings.figures_dir)
    path = _resolve_under(figures_root, f"{chunk_id}.webp")
    if path is None or not path.is_file():
        raise AppError(
            ErrorCode.NOT_FOUND,
            detail=f"figure file missing: dir={figures_root} chunk={chunk_id} image_path={image_path}",
        )

    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "private, max-age=60"})


__all__: list[str] = [
    "chunk_image_path",
    "chunk_is_visible",
    "doc_is_visible",
    "read_chunk_figure",
    "read_sample_pdf",
    "router",
]
