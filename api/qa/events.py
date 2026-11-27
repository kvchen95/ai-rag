r"""SSE 事件契约（详细设计.md §10.4 / 附录 C.2）。.

六类事件，顺序固定：

    step（可多次）→ token（可多次）或 clarify/fallback → citations（可选）→ done

三条实现约束：

1. **`done` 永远最后一条**，包括异常路径。没有它前端会一直转圈（它没有别的
   "结束"信号）。所以发事件的循环用 `try/finally` 保证它。
2. **拒答路径没有 `token`**（§10.4"拒答路径只有进度与收尾两类，没有逐字"）。
   `clarify` / `fallback` 与 `token` 互斥——附录 C.2 的箭头写的是"或"。
3. **`token` 是在校验通过之后才发的**。这一条容易被误读成"不做流式"，但它其实
   正是 §10 的要求："答案要等下一章校验通过才算数"。前端的状态机也支持这个顺序
   （检索中 → 逐字中 → 结束），附录 C 的时序图里 `verify` 阶段排在 `token`
   之前。代价是首字延迟 = 生成 + 校验的总时长；收益是屏幕上不会出现一句后来被
   撤掉的话。**生成是严格 JSON 的非流式调用**（§9.2 要 status/citations/claims
   四个字段才能校验），所以逐字只能发生在这之后。

事件名用附录 C.2 的六个：`step` / `clarify` / `token` / `citations` /
`fallback` / `done`。SSE 帧格式按 `event: <name>\\ndata: <json>\\n\\n`——把事件名
也写进 `event:` 行，前端可以用 `addEventListener` 而不是自己解析 data 里的
type。两者都带：`data` 里也有 `type`，因为一条流被重放/落盘时事件名会丢，
`type` 不会。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

SseEventName = Literal[
    "step", "clarify", "token", "citations", "fallback", "done"
]

# 附录 C.2 的 phase 取值。多一个少一个都会让前端状态机停在错误的态。
PHASE_RETRIEVAL = "retrieval"
PHASE_RERANK = "rerank"
PHASE_GENERATE = "generate"
PHASE_VERIFY = "verify"

PHASES: tuple[str, ...] = (
    PHASE_RETRIEVAL,
    PHASE_RERANK,
    PHASE_GENERATE,
    PHASE_VERIFY,
)


@dataclass(frozen=True, slots=True)
class SseEvent:
    """SseEvent."""

    name: SseEventName
    payload: dict[str, Any]

    def frame(self) -> str:
        """一帧 SSE。.

        `data` 里必须带 `type`（见模块头第 3 条说明的对偶关系）。
        """
        body = {"type": self.name, **self.payload}
        return (
            f"event: {self.name}\ndata: "
            f"{json.dumps(body, ensure_ascii=False)}\n\n"
        )


def step(
    phase: str,
    *,
    hop: int = 0,
    elapsed_ms: float = 0.0,
    note: str | None = None,
) -> SseEvent:
    """`step`：进入阶段或跳次。.

    `note` 是扩展字段（可读的一句话，前端可忽略）。
    """
    payload: dict[str, Any] = {
        "hop": int(hop),
        "phase": phase,
        "elapsed_ms": round(float(elapsed_ms), 1),
    }
    if note:
        payload["note"] = note
    return SseEvent("step", payload)


def token(delta: str) -> SseEvent:
    """Run token."""
    return SseEvent("token", {"delta": delta})


def citations(items: Sequence[dict[str, Any]]) -> SseEvent:
    """`citations.items[]` 各字段。.

    n / doc_key / doc_name / page_num / bbox / snippet / chunk_id。
    """
    return SseEvent("citations", {"items": [dict(item) for item in items]})


def clarify(question: str, options: Sequence[dict[str, Any]]) -> SseEvent:
    """`clarify`：options 含 label 与回填参数（附录 C.2）。.

    同时给 `value`：前端 F1 的 `ClarifyOption` 只有 `{label, value}`，
    而 `value` 就是用户点选后要回填的东西。这里让 `value` = `doc_key`，
    并把完整的 `params` 一起发出去——前端只读 label/value 也能工作，
    未来要传时间范围也不必改契约。
    """
    return SseEvent(
        "clarify",
        {
            "question": question,
            "options": [
                {
                    "label": str(option.get("label") or ""),
                    "value": str(
                        option.get("doc_key") or option.get("value") or ""
                    ),
                    **{k: v for k, v in option.items() if k not in ("label",)},
                }
                for option in options
            ],
        },
    )


def fallback(
    code: str, message: str, docs: Sequence[dict[str, Any]] = ()
) -> SseEvent:
    """`fallback`：降级或拒答。.

    `docs` 只含**有权限**文档（由检索前置过滤保证）。
    """
    return SseEvent(
        "fallback",
        {
            "code": code,
            "message": message,
            "docs": [dict(item) for item in docs],
        },
    )


def done(
    *,
    latency_ms: float,
    hop_count: int,
    verdict: str,
    code: str | None = None,
    uncovered: Sequence[str] = (),
) -> SseEvent:
    """`done`：收尾，总是最后一条。.

    `verdict` 用 §8.4 的五态词（pass / clarify / reject_none / reject_low /
    partial），另加一个 `unverifiable`：门控放行了、但 §10 校验不过。
    它不是第六种门控态，而是"用户最后拿到了什么"的第六种可能——判分口径要求把"证
    据不足拒答"与"校验不过拒答"分开算（§12.1 与 §11.3 的告警策略都不同），
    合成一个词就分不开了。

    `code` 是 §11.3 的错误码（拒答与降级时才有），`uncovered` 只在 `partial` 时
    非空（§8.4"已查实事实 + 未覆盖清单"里的后半句）。
    """
    payload: dict[str, Any] = {
        "latency_ms": round(float(latency_ms)),
        "hop_count": int(hop_count),
        "verdict": verdict,
    }
    if code:
        payload["code"] = code
    if uncovered:
        payload["uncovered"] = [str(item) for item in uncovered]
    return SseEvent("done", payload)


def chunk_text(text: str, size: int) -> Iterable[str]:
    """把已校验的答案切成逐字增量。.

    `size` 太小时 SSE 帧数暴涨（一帧 ~50 字节开销），太大时观感上就是"一次蹦出
    来"。默认由配置给（`SSE_TOKEN_CHUNK_CHARS`）。空文本返回空迭代器——拒绝发一个
    `delta: ""`，那会让前端进入"逐字中"却没有可显示的内容。
    """
    if size <= 0:
        raise ValueError(f"chunk size 必须 > 0，实际 {size}")
    if not text:
        return ()
    return tuple(
        text[index : index + size] for index in range(0, len(text), size)
    )


__all__ = [
    "PHASES",
    "PHASE_GENERATE",
    "PHASE_RERANK",
    "PHASE_RETRIEVAL",
    "PHASE_VERIFY",
    "SseEvent",
    "SseEventName",
    "chunk_text",
    "citations",
    "clarify",
    "done",
    "fallback",
    "step",
    "token",
]
