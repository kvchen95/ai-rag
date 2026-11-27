"""Qdrant 访问层。.

只做"连得上 / 集合在不在"和后续卡片要用的 SDK 句柄。
向量检索的具体查询形状属于检索卡，不在这里提前固化。

健康探针用两个请求，而不是只看 /healthz：
`/healthz` 是静态字符串，Qdrant 事件循环卡住时它也可能立刻返回；
`/collections` 走真正的 API 与存储，才说明服务可用。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from api.clients.base import Probe, short_error
from api.observability import get_logger, log_event

logger = get_logger("api.qdrant")


class QdrantGateway:
    """QdrantGateway."""
    def __init__(
        self,
        base_url: str,
        *,
        collection: str,
        api_key: str = "",
        timeout_s: float = 5.0,
    ) -> None:
        """Initialize the instance."""
        self.base_url = base_url.rstrip("/")
        self.collection = collection
        self._timeout_s = timeout_s
        headers = {"api-key": api_key} if api_key else {}
        # trust_env=False：容器内访问 http://qdrant:6333
        # 绝不能走宿主 HTTP(S)_PROXY。
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            headers=headers,
            trust_env=False,
        )
        self._sdk: Any | None = None

    async def start(self) -> None:
        """Run start."""
        log_event(
            logger,
            "qdrant_client_started",
            base_url=self.base_url,
            collection=self.collection,
        )

    async def stop(self) -> None:
        """Run stop."""
        await self._client.aclose()

    @property
    def sdk(self) -> Any:
        """同步 SDK 句柄，给向量读写用；延迟创建，避免只为探针就依赖 SDK。."""
        if self._sdk is None:
            from qdrant_client import QdrantClient

            self._sdk = QdrantClient(
                url=self.base_url,
                timeout=self._timeout_s,
                check_compatibility=False,
            )
        return self._sdk

    async def ping(self, *, timeout_s: float | None = None) -> Probe:
        """Run ping."""
        started = time.perf_counter()
        timeout = httpx.Timeout(timeout_s or self._timeout_s)
        try:
            health = await self._client.get("/healthz", timeout=timeout)
            if health.status_code >= 400:
                return Probe(
                    ok=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=f"healthz status={health.status_code}",
                )
            listing = await self._client.get("/collections", timeout=timeout)
            if listing.status_code >= 400:
                return Probe(
                    ok=False,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=f"collections status={listing.status_code}",
                )
            names = [
                c.get("name")
                for c in (listing.json().get("result") or {}).get(
                    "collections", []
                )
            ]
            latency = (time.perf_counter() - started) * 1000
            # 集合缺失不是"依赖挂了"：库是空的但服务是好的。单独报出来，
            # 免得 /healthz 在首次入库前把 api 判成 unhealthy。
            return Probe(
                ok=True,
                latency_ms=latency,
                extra={
                    "collection": self.collection,
                    "collection_present": self.collection in names,
                    "collections": len(names),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 探针只报错不抛
            return Probe(
                ok=False,
                latency_ms=(time.perf_counter() - started) * 1000,
                error=short_error(exc),
            )

    async def collection_info(self) -> dict[str, Any] | None:
        """集合存在则返回它的配置摘要，不存在返回 None（不抛 404）。."""
        resp = await self._client.get(f"/collections/{self.collection}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json().get("result")
