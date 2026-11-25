"""OpenAI 兼容模型输出里的 JSON 对象提取。"""

from __future__ import annotations

import json
from typing import Any


def parse_json_object(content: str) -> dict[str, Any] | None:
    """从模型文本中提取第一个 JSON 对象；失败返回 None。

    模型可能包代码围栏或前后说明，这里只做宽松定位，不猜测字段。
    """
    body = (content or "").strip()
    start, end = body.find("{"), body.rfind("}")
    if 0 <= start < end:
        body = body[start : end + 1]
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


__all__ = ["parse_json_object"]
