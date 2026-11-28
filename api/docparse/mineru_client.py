"""MinerU 官方云 API 客户端：本地文件走签名上传（单文件也走 batch 接口）。.

官方 `/api/v4/extract/task` 只收公网 URL；本地 PDF 必须先申请上传
链接、PUT 字节，再用 `batch_id` 轮询结果。接口字段按
https://mineru.net/apiManage/docs 落地。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from api.resources import messages as msg


class MineruError(RuntimeError):
    """MinerU 请求失败或返回无法解释的状态。."""


def result_origin_url(url: str) -> str:
    """MinerU CDN TLS 失败时，回退到 OSS 源站（实测源站可下载）。.

    只改 host，不改 path/query；用户可通过 `MINERU_RESULT_ORIGIN_HOST` 覆盖。
    """
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(url)
    host = os.environ.get(
        "MINERU_RESULT_ORIGIN_HOST", "mineru.oss-cn-shanghai.aliyuncs.com"
    )
    if parsed.hostname != "cdn-mineru.openxlab.org.cn":
        return url
    return urlunsplit(
        (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
    )


class MineruClient:
    """MineruClient."""
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 3.0,
        poll_timeout_s: float = 900.0,
    ) -> None:
        """Initialize the instance."""
        if not token:
            raise ValueError(msg.MINERU_KEY_EMPTY)
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.poll_timeout_s = poll_timeout_s

    def close(self) -> None:
        """Release resources; requests are one-shot so nothing is held."""

    def __enter__(self) -> MineruClient:
        """Enter the context manager."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Exit the context manager."""
        self.close()

    def _api(
        self, path: str, *, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run _api."""
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_s
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:  # pragma: no cover - 真服务端错误
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise MineruError(f"MinerU HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - 网络不可达
            raise MineruError(
                msg.MINERU_NETWORK_ERROR.format(reason=exc.reason)
            ) from exc
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:  # pragma: no cover
            raise MineruError(
                msg.MINERU_NOT_JSON.format(error=exc)
            ) from exc
        if parsed.get("code") != 0:
            raise MineruError(
                f"MinerU code={parsed.get('code')}: {parsed.get('msg')} "
                f"(trace_id={parsed.get('trace_id')})"
            )
        return parsed

    def submit_local(self, path: Path, data_id: str) -> str:
        """Run submit_local."""
        response = self._api(
            "/file-urls/batch",
            payload={
                "files": [{"name": path.name, "data_id": data_id}],
                "model_version": "vlm",
                "enable_table": True,
                "language": "ch",
            },
        )
        data = response.get("data")
        if data is None:
            data = {}
        batch_id = data.get("batch_id")
        urls = data.get("file_urls")
        if urls is None:
            urls = []
        if not batch_id or len(urls) != 1:
            raise MineruError(msg.MINERU_UPLOAD_SHAPE)
        self._put_binary(urls[0], path.read_bytes())
        return str(batch_id)

    def _put_binary(self, url: str, data: bytes) -> None:
        """签名上传：手写 PUT，避免标准库自动补 Content-Type。."""
        parsed = urlsplit(url)
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        if parsed.scheme == "https":
            conn: HTTPSConnection | HTTPConnection = HTTPSConnection(
                parsed.hostname, parsed.port or 443, timeout=self.timeout_s
            )
        else:
            conn = HTTPConnection(
                parsed.hostname, parsed.port or 80, timeout=self.timeout_s
            )
        try:
            conn.putrequest("PUT", target, skip_accept_encoding=True)
            conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            response = conn.getresponse()
            response.read()
            if response.status not in (200, 201):
                raise MineruError(
                    msg.MINERU_UPLOAD_FAILED.format(
                        status=response.status
                    )
                )
        finally:
            conn.close()

    def wait_result(self, batch_id: str) -> str:
        """Run wait_result."""
        deadline = time.monotonic() + self.poll_timeout_s
        while True:
            response = self._api(f"/extract-results/batch/{batch_id}")
            body = response.get("data")
            if body is None:
                body = {}
            results = body.get("extract_result")
            if results is None:
                results = []
            if not results:
                raise MineruError(msg.MINERU_NO_EXTRACT_RESULT)
            item = results[0]
            state = item.get("state")
            if state == "done":
                url = item.get("full_zip_url")
                if not url:
                    raise MineruError(msg.MINERU_DONE_NO_ZIP)
                return str(url)
            if state == "failed":
                raise MineruError(
                    msg.MINERU_PARSE_FAILED.format(
                        reason=item.get("err_msg") or msg.MINERU_UNKNOWN_REASON
                    )
                )
            if time.monotonic() >= deadline:
                raise MineruError(
                    msg.MINERU_POLL_TIMEOUT.format(
                        timeout=self.poll_timeout_s, state=state
                    )
                )
            time.sleep(self.poll_interval_s)

    def _get_bytes(self, url: str) -> bytes:
        """Run _get_bytes."""
        try:
            with urllib.request.urlopen(
                url, timeout=self.timeout_s
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:  # pragma: no cover
            raise MineruError(
                msg.MINERU_DOWNLOAD_HTTP.format(code=exc.code)
            ) from exc
        except urllib.error.URLError as exc:  # pragma: no cover
            raise MineruError(
                msg.MINERU_DOWNLOAD_ERROR.format(reason=exc.reason)
            ) from exc

    def download(self, url: str) -> bytes:
        """Run download."""
        try:
            return self._get_bytes(url)
        except MineruError:
            fallback = result_origin_url(url)
            if fallback == url:
                raise
            return self._get_bytes(fallback)

    def parse_local(self, path: Path, data_id: str) -> dict[str, Any]:
        """Run parse_local."""
        batch_id = self.submit_local(path, data_id)
        zip_url = self.wait_result(batch_id)
        return {"batch_id": batch_id, "zip_bytes": self.download(zip_url)}


__all__ = ["MineruClient", "MineruError"]
