"""本地链路追踪：一行一条 JSON 事件，写到本地文件（不进数据库）。

设计口径：

- **一次请求一个 `request_id`，多个事件行**：按 id 过滤就能还原整条链路；事件即时
  落盘，不需要等请求结束再一次性 flush（异常路径也不会丢）。
- **默认落 `logs/qa_trace.jsonl`**。compose 把仓库根挂到容器的 `/app`，所以宿主侧
  `<repo>/logs/qa_trace.jsonl` 直接可见；`make down` 不会清掉它。
- **本地调试设施，不是审计**：正文/问题文本可以进这里，但绝不写进 `qa_log`
  （§11.2 只存编号与分数摘要）。`AI_RAG_TRACE_ENABLED=0` 可整体关掉。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from api.observability import request_id_var

ENV_FILE = "AI_RAG_TRACE_FILE"
ENV_ENABLED = "AI_RAG_TRACE_ENABLED"
DEFAULT_FILE = "logs/qa_trace.jsonl"
MAX_FIELD_CHARS = 2000
MAX_ITEMS = 200

_lock = threading.Lock()
_configured: tuple[Path, bool] | None = None


def reset_trace() -> None:
    """重新读取环境变量。仅供测试与显式改配置的调用方使用。"""
    global _configured
    with _lock:
        _configured = None


def _target() -> Path | None:
    global _configured
    path = Path(os.environ.get(ENV_FILE, DEFAULT_FILE))
    raw = os.environ.get(ENV_ENABLED, "1").strip().lower()
    enabled = raw not in ("0", "false", "no", "off")
    with _lock:
        _configured = (path, enabled)
    return path if enabled else None


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD_CHARS else value[:MAX_FIELD_CHARS] + "…"
    if isinstance(value, (list, tuple)):
        return [_clip(item) for item in list(value)[:MAX_ITEMS]]
    if isinstance(value, dict):
        return {key: _clip(item) for key, item in list(value.items())[:MAX_ITEMS]}
    return value


def trace(event: str, /, **fields: Any) -> None:
    """写一条链路事件。关掉或写失败都不影响正在跑的问答。"""
    path = _target()
    if path is None:
        return
    payload = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "request_id": request_id_var.get(),
        "event": event,
        **_clip(fields),
    }
    line = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        # 追踪是调试设施：磁盘满了、目录不可写都不能把一次问答变成失败。
        return


__all__ = ["reset_trace", "trace"]
