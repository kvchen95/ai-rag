"""受限生成的模型调用与输出解析（详细设计.md §9.2 / 附录 D.1 / D.4）。.

这一层只做三件事：发请求、宽松解析一次、把"解析不了"变成明确的失败类型。
它**不判断内容对不对**——那是 §10 五道校验的事。边界划在这里的理由是：
把"模型说了但没有引用"当成解析错误处理，会让一个内容问题伪装成一个格式问题，
而两者的处置完全不同（前者要拒答，后者可以重试一次解析）。

三条落地口径：

1. **`Temperature = 0`**（§9.2、附录 D 末段）。不做"低温度"，就是 0：这个系统里
   任何随机性都只可能让答案变得更不可复核。请求体里显式写 `temperature`，不依赖
   网关默认值——默认值可能在网关侧被改成 0.7，而那样只有对比两次输出才看得出来。
2. **宽松解析一次**（§9.2 表格）：模型常把 JSON 包进 ```json 代码块。拨掉围栏再
   解析一次。仍失败就降级，**不重试三次以上**（附录 D 末段"失败路径统一"）。
   代码块里再套一层解释文字（"以下是 JSON：{...}"）也走同一条宽松路径：
   只取第一个 `{` 到最后一个 `}`。
3. **缺字段就算失败，不补默认值**（§9.2 表格："缺 `status` 或
   `citations` → 降级，不重试"）。补一个 `citations=[]` 会让校验"没有
   引用"那一句失去对象，而模型其实就是没给引用——那是必须被拒答的。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from api.observability import get_logger, log_event
from api.prompts.generate import GENERATION_SYSTEM_PROMPT, JUDGE_SYSTEM_PROMPT

logger = get_logger("api.generate.llm")

Status = Literal["answered", "unknown"]

_CODE_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


class GenerationUnavailableError(RuntimeError):
    """生成服务不可用（连接失败 / 5xx / 超时）。.

    处置是 fallback `model_timeout`。
    """


class GenerationRejectedError(GenerationUnavailableError):
    """模型返回了 4xx。请求形状有问题，重试无意义，也不是"模型超时"。."""


class DraftFormatError(RuntimeError):
    """模型输出解析不出约定结构（非 JSON / 缺字段 / answered 但答案为空）。.

    这一类必须与 `GenerationUnavailableError` 分开：前者是"模型说了但不算
    数"（§9.3 的整条拒答），后者是"系统异常"（§10.3 第三档）。
    混在一起会让一条本可以降到"只给原文"的回答变成"稍后重试"。
    """

    def __init__(self, reason: str, raw: str = "") -> None:
        """Initialize the instance."""
        self.reason = reason
        self.raw = raw[:800]
        super().__init__(f"{reason}: {self.raw[:200]}")


@dataclass(frozen=True, slots=True)
class Fact:
    """唯一事实单元：事实文本 + 证据 chunk_id 列表。."""

    text: str
    evidence_ids: tuple[str, ...] = ()
    kind: str = "fact"
    name: str = ""
    expression: str = ""
    result: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Run as_dict."""
        return {
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "kind": self.kind,
            "name": self.name,
            "expression": self.expression,
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class Draft:
    """一份草稿。.

    facts 是唯一事实源；answer/citations 由渲染层生成，不是持久字段。
    """

    status: Status
    facts: tuple[Fact, ...] = ()
    raw: str = ""
    model: str = ""
    temperature: float = 0.0
    # 解析时是否拨过一层代码块围栏。留痕是因为"模型总在包围栏"是一个可观测的
    # 提示词退化信号，直接吞掉就看不出来了。
    unwrapped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def answer(self) -> str:
        """Run answer."""
        return "\n".join(fact.text for fact in self.facts)

    @property
    def answered(self) -> bool:
        """Run answered."""
        return self.status == "answered"

    def as_audit(self) -> dict[str, Any]:
        """审计口径：草稿 facts 进审计，answer/citations 由渲染层生成。."""
        return {
            "status": self.status,
            "facts": [fact.as_dict() for fact in self.facts],
            "model": self.model,
            "temperature": self.temperature,
            "unwrapped": self.unwrapped,
        }


def strip_code_fence(text: str) -> tuple[str, bool]:
    r"""拨掉 ```json 围栏，并报告"是否动过这段文本"。.

    三个方向都要能处理：
    - 整段被围栏包住：` ```json\\n{...}\\n``` `；
    - 围栏外面还有一句解释：取第一个 `{` 到最后一个 `}`；
    - 没围栏但前后有解释（"结果如下：{...}"）：同样取花括号区间。

    第三种与第二种共用一条规则，因为在**解析之前**我们无法区分"正文里恰好有花括
    号"与"模型把 JSON 埋在句子里"。取花括号是安全的方向：真的是散文时，
    取出来的片段一样解析失败（`DraftFormatError`），与不取的结果相同；
    而模型埋了 JSON 时，取出来能救回一次本来会失败的生成。

    `changed=True` 只是"动过"，不代表围栏一定存在：这个标记进审计用来观察
    "模型是不是总在敷衍格式"，不要拿它当围栏检测器。
    """
    original = text or ""
    stripped = _CODE_FENCE.sub("", original).strip()
    unwrapped = stripped != original.strip()
    if not stripped:
        return "", unwrapped
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped, unwrapped
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        return stripped[start : end + 1], True
    return stripped, unwrapped


def _int_list(value: Any, *, field_name: str) -> tuple[int, ...]:
    """把引用编号数组归一成 tuple[int, ...]。非整数元素直接判格式错误。.

    `True` 是 int 的子类，`[true]` 会被当成 `[1]`——所以显式排除 bool。
    """
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise DraftFormatError(
            f"{field_name} 必须是数组，实际是 {type(value).__name__}"
        )
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            if isinstance(item, str) and item.strip().isdigit():
                out.append(int(item.strip()))
                continue
            raise DraftFormatError(f"{field_name} 里有非整数元素: {item!r}")
        out.append(int(item))
    return tuple(dict.fromkeys(out))


def parse_draft(payload: str, *, model: str = "") -> Draft:
    """把模型输出解析成 `Draft`。.

    任何不符合 §9.2 契约的形状都抛 `DraftFormatError`。

    §9.2 的表格逐行落地：
    - 非 JSON 或带代码块标记 → 宽松解析一次；仍失败则降级（抛）
    - 缺 `status` 或 `citations` → 降级，不重试（抛）
    - `status=answered` 但答案为空 → 视为失败（抛）
    - 引用编号超出证据范围 → **这里不管**，交给 §10.1 第一道（它需要证据编号范
      围）
    """
    body, unwrapped = strip_code_fence(payload)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DraftFormatError("模型输出不是 JSON", raw=body) from exc
    if not isinstance(data, dict):
        raise DraftFormatError(
            f"模型输出不是对象: {type(data).__name__}", raw=body
        )

    status = data.get("status")
    if status not in ("answered", "unknown"):
        raise DraftFormatError(f"status 缺失或非法: {status!r}", raw=body)
    if "facts" not in data:
        raise DraftFormatError("缺少 facts 字段", raw=body)

    facts: list[Fact] = []
    raw_facts = data.get("facts")
    if raw_facts is None:
        raw_facts = []
    if not isinstance(raw_facts, list | tuple):
        raise DraftFormatError(
            f"facts 必须是数组，实际 {type(raw_facts).__name__}", raw=body
        )
    for item in raw_facts:
        if not isinstance(item, dict):
            raise DraftFormatError(f"fact 不是对象: {item!r}", raw=body)
        kind = str(item.get("kind") or "fact").strip().lower()
        if kind not in ("fact", "analysis", "calculation"):
            raise DraftFormatError(f"fact.kind 非法: {kind!r}", raw=body)
        text = item.get("text")
        text = text.strip() if isinstance(text, str) else ""
        name = str(item.get("name") or "").strip()
        expression = str(item.get("expression") or "").strip()
        result = str(item.get("result") or "").strip()
        if kind == "calculation":
            if not name or not expression or not result:
                raise DraftFormatError(
                    "calculation 缺少 name/expression/result", raw=body
                )
            if not text:
                text = f"{name}：{expression} = {result}"
        if status == "answered" and not text:
            raise DraftFormatError(
                "status=answered 但 fact.text 为空", raw=body
            )
        evidence_ids = item.get("evidence_ids")
        if evidence_ids is None:
            evidence_ids = item.get("citations")
        if evidence_ids is None:
            evidence_ids = []
        evidence_ids = _str_list(evidence_ids, field_name="fact.evidence_ids")
        if status == "answered" and not evidence_ids:
            raise DraftFormatError(
                "status=answered 但 fact 缺少 evidence_ids", raw=body
            )
        facts.append(
            Fact(
                text=text,
                evidence_ids=evidence_ids,
                kind=kind,
                name=name,
                expression=expression,
                result=result,
            )
        )

    if status == "answered" and not facts:
        raise DraftFormatError("status=answered 但 facts 为空", raw=body)

    data.get("raw", body)
    known = {"status", "facts", "citations", "claims", "answer"}
    return Draft(
        status=status,  # type: ignore[arg-type]
        facts=tuple(facts),
        raw=body[:4000],
        model=model,
        temperature=0.0,
        unwrapped=unwrapped,
        extra={key: value for key, value in data.items() if key not in known},
    )


def build_generation_messages(
    question: str, evidence_block: str
) -> list[dict[str, str]]:
    """拼三段：角色与约束（固定）、证据包、输出要求。.

    §9.2："三段都固定，不随问题变"。所以问题只在最后一段出现，且不加任何
    "请仔细阅读"之类的祈使句——那些话会随人改动而漂移，而漂移的提示词
    必须重跑评测集（附录 D 末段）。
    """
    return [
        {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"【证据包】\n{evidence_block}\n\n"
                f"【问题】\n{question.strip()}\n\n"
                "只输出 JSON。"
            ),
        },
    ]


# --- 共享 keep-alive 连接池 ---------------------------------------------------
# 根因（2026-11-24 实测）：原先每次 complete() 都 `with httpx.Client(...)`，
# 等于每次问答都做一次全新的 TCP + TLS 握手。第三方 SLB 在握手阶段随机回
# `[SSL: UNEXPECTED_EOF_WHILE_READING]`，30 次冷连接探测失败 5~7 次（~20%）；
# 同一网关换成共享 keep-alive 客户端后降到 1/30，中位延迟 3.1s→2.3s。
# 所以这里持有长连接客户端并被生成/判别两条链路共用（同 endpoint+key+timeout）。
_POOL_LOCK = threading.Lock()
_SHARED_CLIENTS: dict[tuple[str, str, float], httpx.Client] = {}


def _shared_client(
    base_url: str, api_key: str, timeout_s: float
) -> httpx.Client:
    """按 (endpoint, key, timeout) 复用一个线程安全的 keep-alive 客户端。.

    生成与判别是同一个 llm_client(settings) 工厂，走同一把池；不同凭证绝不共用，
    避免把 A 租户的 token 发到 B 的 endpoint。
    """
    key = (base_url.rstrip("/"), api_key, timeout_s)
    with _POOL_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(
                base_url=key[0],
                timeout=timeout_s,
                headers={"Authorization": f"Bearer {api_key}"}
                if api_key
                else {},
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=8,
                    max_keepalive_connections=8,
                    keepalive_expiry=30.0,
                ),
            )
            _SHARED_CLIENTS[key] = client
        return client


def reset_shared_clients() -> None:
    """关闭并清空连接池（测试 / 配置变更后调用）。."""
    with _POOL_LOCK:
        for client in _SHARED_CLIENTS.values():
            client.close()
        _SHARED_CLIENTS.clear()


@dataclass(slots=True)
class LlmClient:
    """OpenAI 兼容网关的 `/chat/completions` 薄客户端。.

    刻意不接 SDK：这里只有一次非流式的严格 JSON 调用，SDK 会带进一整套流式与
    重试逻辑，而重试策略必须是我们的（附录 D："不重试三次以上"）。

    max_attempts 只覆盖**传输层瞬时故障**（握手 SSL EOF / 超时 / 5xx /
    空响应）——这些调用都没有产出内容，重试是幂等的。GenerationRejectedError（4xx）
    和 DraftFormatError（模型输出格式错）不重试，语义见各自的异常类注释。
    """

    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 30.0
    max_attempts: int = 6
    retry_backoff_s: float = 0.3
    retry_budget_s: float = 45.0

    def complete(
        self, messages: Sequence[dict[str, str]], *, temperature: float = 0.0
    ) -> str:
        """Run complete."""
        body = {
            "model": self.model,
            "messages": list(messages),
            # 显式写 0：不依赖网关默认值（默认值可能被改成 0.7，而那时只有对比
            # 两次输出才看得出来）。
            "temperature": temperature,
            "stream": False,
        }
        # 总预算与 LLM_TIMEOUT_S 对齐：单次真超时（如 30s）用满预算后不再重试，
        # 不会把一次问答放大成 N×30s。瞬时 SSL EOF 通常在 1~7s 失败，
        # 预算内可重试。
        deadline = time.monotonic() + self.retry_budget_s
        last: GenerationUnavailableError | None = None
        for attempt in range(1, max(1, self.max_attempts) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                client = _shared_client(
                    self.base_url, self.api_key, self.timeout_s
                )
                resp = client.post(
                    "/chat/completions", json=body, timeout=remaining
                )
                if resp.status_code >= 500:
                    raise GenerationUnavailableError(
                        f"llm -> HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                if resp.status_code >= 400:
                    # 4xx 是请求形状错误，重试无意义——立即抛出，不进重试分支。
                    raise GenerationRejectedError(
                        f"llm rejected: HTTP {resp.status_code} "
                        f"{resp.text[:300]}"
                    )
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise GenerationUnavailableError(
                        f"llm returned non-JSON: {exc}"
                    ) from exc
                choices = (data or {}).get("choices") or []
                if not choices:
                    raise GenerationUnavailableError("llm returned no choices")
                message = choices[0].get("message") or {}
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise GenerationUnavailableError(
                        "llm returned empty content"
                    )
                return content
            except httpx.TimeoutException as exc:
                last = GenerationUnavailableError(
                    f"llm timed out after {self.timeout_s}s"
                )
                last.__cause__ = exc
            except httpx.HTTPError as exc:
                # SSL EOF 走这里（httpx.ConnectError ⊂ httpx.HTTPError）。
                last = GenerationUnavailableError(f"llm unreachable: {exc}")
                last.__cause__ = exc
            except GenerationRejectedError:
                raise
            except GenerationUnavailableError as exc:
                last = exc

            if attempt >= max(1, self.max_attempts):
                raise last  # type: ignore[misc]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last  # type: ignore[misc]
            # 指数退避 + 抖动：避免同一批请求在 SLB 恢复瞬间同时重试造成二次抖
            # 动。
            backoff = min(
                self.retry_backoff_s * (2 ** (attempt - 1)), remaining
            )
            if backoff > 0:
                time.sleep(backoff * (0.5 + random.random() * 0.5))
            log_event(
                logger,
                "llm_retry",
                level=30,
                attempt=attempt,
                model=self.model,
                reason=str(last)[:160],
            )
        raise last  # type: ignore[misc]


def generate_draft(
    client: LlmClient,
    question: str,
    evidence_block: str,
    *,
    model: str | None = None,
) -> Draft:
    """一次生成：发请求 → 解析。解析失败**不重试**（§9.2 表格）。.

    "不重试"是有代价的取舍：重试一次能把"模型偶发多说一句解释"救回来，但也会把
    一次真正的提示词退化变成偶发失败而被忽略。§9.2 明确要求不重试，这里照做。
    """
    content = client.complete(
        build_generation_messages(question, evidence_block), temperature=0.0
    )
    draft = parse_draft(content, model=model or client.model)
    if draft.unwrapped:
        log_event(
            logger,
            "draft_unwrapped_code_fence",
            level=30,
            model=draft.model,
        )
    return draft


def _str_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    """Run _str_list."""
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise DraftFormatError(
            f"{field_name} 必须是数组，实际是 {type(value).__name__}"
        )
    out: list[str] = []
    for item in value:
        if isinstance(item, bool):
            raise DraftFormatError(f"{field_name} 里有非字符串元素: {item!r}")
        if isinstance(item, str):
            out.append(item)
            continue
        if isinstance(item, int):
            out.append(str(item))
            continue
        raise DraftFormatError(f"{field_name} 里有非字符串元素: {item!r}")
    return tuple(dict.fromkeys(out))


__all__ = [
    "Fact",
    "Draft",
    "DraftFormatError",
    "GENERATION_SYSTEM_PROMPT",
    "GenerationRejectedError",
    "GenerationUnavailableError",
    "JUDGE_SYSTEM_PROMPT",
    "LlmClient",
    "build_generation_messages",
    "generate_draft",
    "parse_draft",
    "strip_code_fence",
]

# Backwards-compatible alias (N818: exception names use Error suffix).
GenerationUnavailable = GenerationUnavailableError


# Backwards-compatible alias (N818).
GenerationRejected = GenerationRejectedError
