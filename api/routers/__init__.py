"""路由集合。在线链路的分册路由由后续卡片挂进来，这里只当归集点。"""

from __future__ import annotations

from api.routers import health, ingest

__all__ = ["health", "ingest"]
