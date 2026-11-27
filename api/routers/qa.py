"""问答接口（详细设计.md 附录 C.1 / C.2、§10.4）。

一个接口：`POST /api/v1/qa/stream`。它是 SSE，所以三件事必须做对：

1. **401/422 在开流之前**。身份解析失败必须是 HTTP 401 + JSON 错误体，不能是
   一条 `event: fallback` 的流——前端拿到 401 才知道该去登录，拿到 200 流只会
   显示"稍后重试"。所以 `require_identity` 在生成器之外先跑（FastAPI 的依赖在
   返回 `StreamingResponse` 之前就解析完了），入参校验同理。
2. **头的形状**：`text/event-stream`、`Cache-Control: no-cache`、
   `X-Accel-Buffering: no`（nginx 默认会缓冲整个响应，SSE 会变成"一次性吐出"）、
   `Connection: keep-alive`。
3. **业务异常不冒到 HTTP 层**。开流之后一切错误都变成 `fallback` 事件：那时状态码
   已经发出去了，改不了。`QaService.run` 的 `finally` 保证 `done` 一定发。

两条工程约束：

- 整条链路是**同步阻塞**的（psycopg 连接池、httpx.Client、精排/生成都是同步调用），
  所以放进 `asyncio.to_thread` + 一个队列里跑：直接在事件循环里调会把 `/healthz`
  一起卡住——它每 15 秒探一次，卡 30 秒就会被容器判定 unhealthy 反复重启。
- 线程与事件循环之间只过一个 `asyncio.Queue`：工作线程 `put_nowait`，协程 `await get`。
  不用 `run_in_executor` 包生成器，是因为生成器一旦在线程里 yield 就跨不过边界。

审计写入在流结束之后（`QaService` 已经把 `QaLogEntry` 建好）：写库失败不能把一次
成功的回答变成失败（`AuditWriter` 的口径），所以它在 `finally` 里、且吞异常。
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.audit import AuditWriter
from api.config import Settings
from api.deps import IdentityDep
from api.generate.llm import LlmClient
from api.observability import get_logger, log_event
from api.qa.orchestrator import QaOutcome, QaService
from api.retrieval.pipeline import build_pipeline
from api.temporal.repository import TemporalFactRepository
from api.time_range import TimeRange
from api.trace import trace as trace_event

logger = get_logger("api.qa.api")

router = APIRouter(prefix="/api/v1", tags=["qa"])

# 响应头的形状。nginx 默认 proxy_buffering on，会把整条流攒到结束才发；
# `X-Accel-Buffering: no` 是关掉它的标准做法（nginx 认这个头）。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class ScopeIn(BaseModel):
    """`scope`：限定在哪些文档里查（附录 C.1 的请求示例）。"""

    model_config = ConfigDict(extra="forbid")

    doc_keys: list[str] = Field(default_factory=list)

    @field_validator("doc_keys")
    @classmethod
    def _clean(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


class TimeRangeIn(BaseModel):
    """`time_range`：绝对日期窗口，YYYYMMDD（附录 C.1 的请求示例）。"""

    model_config = ConfigDict(extra="forbid")

    date_from: int = Field(alias="from", ge=19000101, le=29991231)
    date_to: int = Field(alias="to", ge=19000101, le=29991231)

    @field_validator("date_to")
    @classmethod
    def _ordered(cls, value: int, info: Any) -> int:
        start = info.data.get("date_from")
        if start is not None and value < start:
            raise ValueError("to 不能早于 from")
        return value

    def to_range(self) -> TimeRange:
        return TimeRange(
            date_from=self.date_from,
            date_to=self.date_to,
            granularity="day",
            matched="request.time_range",
            basis="explicit",
        )


class QaStreamRequest(BaseModel):
    """`POST /api/v1/qa/stream` 的请求体（附录 C.1）。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    scope: ScopeIn | None = None
    time_range: TimeRangeIn | None = None

    @field_validator("question")
    @classmethod
    def _trim(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question 不能为空白")
        return value


def build_service(request: Request, settings: Settings) -> QaService:
    """按配置装配一条问答链路。

    每次请求新建（而不是放进 app.state）：`HybridRetriever` / `Reranker` /
    `LlmClient` 各自持有 `httpx.Client`，跨线程共享同一个连接池在 `to_thread`
    里跑是有隐患的（连接对象的状态不属于任何单一线程）。httpx.Client 的构造很便宜，
    而每请求一个客户端也让"一次请求慢"不会拖累下一次。

    代价是每请求一次 TCP 握手。对 30 秒预算的问答链路来说，这点开销在噪声里。
    """
    pipeline = build_pipeline(
        settings,
        request.app.state.pg,
        llm=llm_client(settings, timeout_s=min(float(settings.llm_timeout_s), 5.0)),
    )
    return QaService(
        pipeline=pipeline,
        llm=llm_client(settings),
        judge_client=judge_client(settings),
        evidence_max_chars=settings.evidence_max_chars,
        token_chunk_chars=settings.sse_token_chunk_chars,
        qa_budget_s=settings.qa_budget_s,
        model_name=settings.llm_model,
        temporal_repo=TemporalFactRepository(request.app.state.pg),
    )


def llm_client(settings: Settings, *, timeout_s: float | None = None) -> LlmClient | None:
    """没配 `LLM_BASE_URL`/`LLM_MODEL` 时返回 None：那次问答走"系统异常"档，
    而不是启动失败。服务要能起来——检索与审计在没有生成模型时仍然有价值
    （没有它，评测与门控标定根本跑不起来）。

    `timeout_s` 只给时间归一这种短任务用：它失败有规则兜底，不能占用生成预算。
    """
    if not settings.llm_base_url.strip() or not settings.llm_model.strip():
        return None
    resolved_timeout = float(timeout_s if timeout_s is not None else settings.llm_timeout_s)
    retry_budget = min(float(settings.llm_retry_budget_s), max(1.0, resolved_timeout * 2))
    return LlmClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        timeout_s=resolved_timeout,
        max_attempts=settings.llm_max_attempts,
        retry_backoff_s=settings.llm_retry_backoff_s,
        retry_budget_s=retry_budget,
    )


def judge_client(settings: Settings) -> LlmClient | None:
    """判别器（§10.1 第四道）现在复用生成模型的网关与模型名。

    独立成一个函数而不是直接传 `llm`，是因为它迟早要分开：判别与生成用同一个模型时
    "自己判自己"的偏差是系统性的（附录 D.4 的判别器要更保守）。现在两份配置指向同一
    套值，但接口已经允许分开——将来加 `JUDGE_MODEL` 只改这一个函数。

    没配模型时返回 None，`judge()` 给出 `skipped`，第四道按不支持处理（Fail-Closed）。
    """
    return llm_client(settings)


def validate_question(question: str, settings: Settings) -> str:
    """§11.4：单问题长度 500 字，**截断并提示**（不是拒绝）。

    截断而不是 422，理由是这个限制是防护性的（防止一个请求拖垮机器），
    用户的长问题前 500 字仍然有意义。截断的事实进 `detail`，审计里能看到。
    """
    limit = settings.max_question_chars
    if len(question) <= limit:
        return question
    log_event(logger, "question_truncated", level=30, original_chars=len(question), limit=limit)
    return question[:limit]


@router.post("/qa/stream")
async def qa_stream(
    body: QaStreamRequest, request: Request, identity: IdentityDep
) -> StreamingResponse:
    """问答（SSE）。事件顺序见附录 C.2，`done` 永远最后一条。"""
    settings: Settings = request.app.state.settings
    question = validate_question(body.question, settings)
    doc_keys = body.scope.doc_keys if body.scope else None
    time_range = body.time_range.to_range() if body.time_range else None
    trace_event(
        "qa.route_enter",
        question=question,
        doc_keys=doc_keys or [],
        time_range=None if time_range is None else time_range.as_dict(),
        **identity.to_audit(),
    )

    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def emit(event: Any) -> None:
        # 工作线程 → 事件循环。`put_nowait` 而不是 `await put`：工作线程没有事件循环，
        # 用 call_soon_threadsafe 投递是唯一安全的跨线程写法。
        loop.call_soon_threadsafe(queue.put_nowait, ("event", event))

    def worker() -> None:
        service: QaService | None = None
        outcome: QaOutcome | None = None
        try:
            service = build_service(request, settings)
            outcome = service.run(
                question,
                identity,
                doc_keys=doc_keys,
                time_range=time_range,
                emit=emit,
            )
            write_audit(request, settings, outcome)
        except Exception as exc:  # noqa: BLE001 - worker 的异常必须变成流内事件
            log_event(
                logger,
                "qa_worker_failed",
                level=40,
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            trace_event(
                "qa.worker_failed", error=f"{type(exc).__name__}: {str(exc)[:300]}"
            )
        finally:
            # 装配本身失败时 service 还没造出来；不能让清理异常盖掉 close 事件，
            # 否则前端会一直等不到流结束（实测撞过）。
            if service is not None:
                service.pipeline.close()
            loop.call_soon_threadsafe(queue.put_nowait, ("close", None))

    async def stream() -> AsyncIterator[bytes]:
        # 先把工作线程放出去，让第一个 step 尽早到达前端。
        future = loop.run_in_executor(None, worker)
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "close":
                    break
                yield payload.frame().encode("utf-8")
            await future
        except asyncio.CancelledError:
            # 客户端断开：先把工作线程收完再让取消继续传播。不 cancel 它——
            # 它正在用 psycopg 连接，强行中断会留下"半借出"的连接。它写审计仍然
            # 有价值：用户断开了，但这次问答发生过。
            await asyncio.shield(future)
            raise

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def write_audit(request: Request, settings: Settings, outcome: QaOutcome) -> None:
    """写一条审计。失败只记日志，不改写这次回答（§11.2 的第三条口径）。"""
    if outcome.audit is None:  # pragma: no cover - run() 一定建了它
        return
    writer = AuditWriter(request.app.state.pg, retention_days=settings.audit_retention_days)
    writer.record(outcome.audit)


__all__ = [
    "QaStreamRequest",
    "ScopeIn",
    "TimeRangeIn",
    "build_service",
    "judge_client",
    "llm_client",
    "qa_stream",
    "router",
    "validate_question",
    "write_audit",
]
