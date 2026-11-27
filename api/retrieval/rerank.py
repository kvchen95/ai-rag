"""精排（详细设计.md §7.3）：逐对打分、取前 5–8 条、超预算降级并留审计。.

三条实测事实决定了这个模块的形状（真 TEI CPU + bge-reranker-base）：

1. **单次请求最多 32 条**：塞 33 条进来服务端直接
   `413 {"error":"batch size 33 > maximum allowed batch size 32"}`，不是截断
   也不是排队。而 §7.5 允许 50 个候选 → 必须分批。
2. **耗时随条数线性**：8 条 184ms、20 条 355ms、30 条 484ms（best-of-2）。
   50 条会落在 800ms 预算边缘，分批后串行更慢 → 所以既要分批，也要**跨批
   共享一个 deadline**，不能每批各给 800ms。
3. **`raw_scores=false` 给的是 sigmoid 后的 0..1 分**（实测 raw
   `[7.98, -0.909, -1.60, -2.31, -5.99, -10.20]` 对应
   `[0.99966, 0.28718, 0.16804, 0.09063, 0.00251, 0.00004]`）。§8.1 的
   0.20/0.40 是**分位数值**，所以必须用归一化后的分；用 raw logits 比阈值
   等于拿两套刻度对比。

降级（§7.3 边界表）不是"出错就算了"，它必须留下三条审计信息：
`degraded=True`、`reason`（`rerank_timeout` / `rerank_unavailable` /
`rerank_partial`）、以及**降到什么**（按名次取前 N）。少任何一条，事后都无法
判断"这次的 0.38 是真的弱相关，还是精排压根没跑"。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from api.retrieval.hybrid import Candidate
from api.trace import trace as trace_event

# TEI 的 --max-client-batch-size 默认 32：超过直接 413（实测）。
TEI_MAX_BATCH = 32

DegradeReason = Literal[
    "rerank_timeout", "rerank_unavailable", "rerank_partial"
]


class RerankUnavailableError(RuntimeError):
    """精排服务整段不可用（连接失败 / 5xx）。调用方据此降级。."""


class RerankTimeoutError(RerankUnavailableError):
    """**超时**（客户端 deadline 到了），区别于"服务不可用"。.

    实测撞出来的：`httpx.ReadTimeout` 早先被统一包成
    `RerankUnavailableError`，于是审计里把"预算 1ms 超时"记成
    `rerank_unavailable`。两者的运维含义完全不同——

    - `rerank_timeout`：预算不够（候选太多 / 机器慢）→ 调
      `RERANK_BUDGET_MS` 或减候选
    - `rerank_unavailable`：服务挂了 → 看容器、看模型加载

    混成一个码，事后看到"降级"根本不知道该动哪里。所以超时必须有自己的类型。
    """


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    """精排结果 + 降级审计。`ordered` 已按分数降序（降级时按融合名次）。."""

    ordered: list[Candidate]
    degraded: bool = False
    reason: DegradeReason | None = None
    # 实际打进精排的条数。降级时它可能小于候选数（没跑完的那批）。
    scored: int = 0
    elapsed_ms: float = 0.0
    # 降级时用了什么口径取前 N，审计要能读出"这次是按名次取的"。
    fallback: str | None = None

    def as_audit(self) -> dict[str, Any]:
        """Run as_audit."""
        return {
            "degraded": self.degraded,
            "degrade": self.reason,
            "scored": self.scored,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "fallback": self.fallback,
            "top1": None
            if not self.ordered or self.ordered[0].rerank_score is None
            else round(self.ordered[0].rerank_score, 6),
        }


class Reranker:
    """TEI `/rerank` 的薄客户端。.

    为什么不用 Qdrant 侧的 rerank：本项目的精排模型是 BAAI/bge-reranker-base，
    走 TEI 暴露的 `/rerank`（它内部就是 cross-encoder），与编码服务同一个镜像、
    同一套模型缓存。引入第二种服务只会多一个要监控的东西。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        timeout_s: float = 10.0,
        max_batch: int = TEI_MAX_BATCH,
    ) -> None:
        """Initialize the instance."""
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_batch = max(1, min(max_batch, TEI_MAX_BATCH))
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        # trust_env=False：容器内访问 http://rerank:8000 绝不能走宿主代理。
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout_s,
            headers=headers,
            trust_env=False,
        )

    def close(self) -> None:
        """Run close."""
        self._client.close()

    def __enter__(self) -> Reranker:
        """Enter the context manager."""
        return self

    def __exit__(self, *_exc: object) -> None:
        """Exit the context manager."""
        self.close()

    def score(
        self, query: str, texts: Sequence[str], *, timeout_s: float
    ) -> list[float]:
        """单批打分。返回与 `texts` 同序的 0..1 分。.

        `timeout_s` 是**这一批剩下的预算**，不是固定值：整个精排共享一个
        deadline，否则 2 批各 800ms 就变成 1600ms。
        """
        if not texts:
            return []
        if len(texts) > self.max_batch:
            raise ValueError(
                f"batch of {len(texts)} exceeds TEI max batch {self.max_batch}"
            )
        body = {
            "query": query,
            "texts": list(texts),
            "raw_scores": False,  # 0..1：§8.1 的阈值是按这个刻度定的（实测）
        }
        try:
            resp = self._client.post("/rerank", json=body, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            # 超时单独一类：它不是"服务挂了"，是"这次没在预算内返回"。
            raise RerankTimeoutError(
                f"rerank exceeded its budget ({timeout_s*1000:.0f}ms)"
            ) from exc
        except httpx.HTTPError as exc:
            raise RerankUnavailableError(
                f"rerank service unreachable: {exc}"
            ) from exc
        if resp.status_code >= 500:
            raise RerankUnavailableError(
                f"rerank -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            # 4xx 是请求形状问题（超批 / 空 texts），重试无意义，也不能降级成
            # "按名次取"——那会把一个代码 bug 伪装成一次正常的保守降级。
            raise RerankUnavailableError(
                f"rerank rejected the request: HTTP {resp.status_code} "
                f"{resp.text[:300]}"
            )
        try:
            rows = resp.json()
        except ValueError as exc:
            raise RerankUnavailableError(
                f"rerank returned non-JSON: {exc}"
            ) from exc
        scores = [0.0] * len(texts)
        seen = 0
        for row in rows or []:
            index = int(row.get("index", -1))
            if 0 <= index < len(scores):
                scores[index] = float(row.get("score") or 0.0)
                seen += 1
        if seen != len(texts):
            # 条数对不上说明服务端漏了条目；把漏的当 0 分处理并让调用方知道
            # （0 分会落进拒答段——保守方向）。
            raise RerankUnavailableError(
                f"rerank returned {seen} scores for {len(texts)} texts"
            )
        trace_event(
            "rerank.batch",
            n_texts=len(texts),
            scores=[round(score, 6) for score in scores],
            text_heads=[text[:120] for text in texts],
        )
        return scores


def clip_for_rerank(text: str, max_chars: int | None) -> str:
    """精排专用截短：只裁进模型的文本，绝不改证据包里的原文。.

    `max_chars=None`（未配置）时原样返回，保留纯逻辑用例与非 pipeline 调用方
    的旧行为；配置值来自 `RERANK_TEXT_MAX_CHARS`，不在代码里写业务数字。
    """
    # 精排只吃相关性信号：PDF 表格文本里大量换行/空格会把"归属于上市公司股东的净
    # 利润"拆成"净 利润"，交叉编码器对这种碎片化文本打分偏低。证据包仍用原文，
    # 这里只把空白规整成单空格，不让排版碎片影响相关性。
    text = "".join((text or "").split())
    if max_chars is None or max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars]


def rerank_candidates(
    reranker: Reranker,
    query: str,
    candidates: Sequence[Candidate],
    texts: dict[str, str],
    *,
    budget_ms: int,
    top_n_min: int,
    top_n_max: int,
    candidates_max: int,
    text_max_chars: int | None = None,
) -> RerankOutcome:
    """精排 + 截断 + 降级。.

    步骤顺序是有讲究的：

    1. **按融合名次先截到 `candidates_max`**（§7.5"上限 50，超了按名次截"）。
       先截再分批，预算才是可估的。
    2. **分批串行打分，共享一个 deadline**。第一批就超时 → 整体降级；
       中途某批失败 → 已打分的照用、未打分的按融合名次垫后，记
       `rerank_partial`。
    3. **取前 top_n_max / 至少 top_n_min**：候选不足下限时有多少给多少（§7.3）。
    4. 降级时**不编造分数**：`rerank_score` 保持 None，按融合名次取前 N。
       给降级的候选填一个 0.0 会让门控判成"无相关"，而真相是"我们没打分"。
    5. **正文长度决定精排耗时**：8 条真实切片（长表格可达万字）在 TEI CPU 上
       3.6–9.4s，同一批正文截到 400 字后稳定在 2.1s 左右（实测 best-of-3）。
       800ms 预算对原始正文无论如何都不够，所以除了把预算调到
       `RERANK_BUDGET_MS`，进精排的正文还要按 `RERANK_TEXT_MAX_CHARS` 截短。
       证据包仍用原文（§9.1），截短只影响相关性打分，不影响引用回查。
    """
    ordered = list(candidates)[:candidates_max]
    if not ordered:
        return RerankOutcome(ordered=[], scored=0, fallback=None)

    deadline = time.monotonic() + max(0.001, budget_ms / 1000.0)
    started = time.perf_counter()
    scored: dict[str, float] = {}
    degraded: bool = False
    reason: DegradeReason | None = None

    for start in range(0, len(ordered), reranker.max_batch):
        batch = ordered[start : start + reranker.max_batch]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            degraded = True
            reason = "rerank_timeout"
            break
        batch_texts = [
            clip_for_rerank(texts.get(item.chunk_id, ""), text_max_chars)
            for item in batch
        ]
        try:
            scores = reranker.score(query, batch_texts, timeout_s=remaining)
        except RerankTimeoutError:
            # 超时：预算用完了。无论之前打没打过分，原因都记 `rerank_timeout`
            # （而不是 partial）——"预算不够"是一个可操作的信号，而 partial 会让
            # 人以为"某批数据有问题"。
            degraded = True
            reason = "rerank_timeout"
            break
        except RerankUnavailableError:
            degraded = True
            # 一批都没打完 = 服务不可用；打了一半 = 部分降级。两者的运维含义不同
            # （前者要告警"精排挂了"，后者是"这批候选太慢"）。
            reason = "rerank_unavailable" if not scored else "rerank_partial"
            break
        for item, score in zip(batch, scores, strict=False):
            scored[item.chunk_id] = score

    elapsed_ms = (time.perf_counter() - started) * 1000

    if scored:
        # 打过分的一律按分数降序；没打分的（降级时）按融合名次垫后，
        # 而不是混进排序——它们的分数是未知，不是 0。
        with_score = [
            item.with_rerank(scored[item.chunk_id])
            for item in ordered
            if item.chunk_id in scored
        ]
        without = [item for item in ordered if item.chunk_id not in scored]
        with_score.sort(
            key=lambda item: (-(item.rerank_score or 0.0), item.fused_rank)
        )
        merged = with_score + without
    else:
        # 一条都没打上分：整体按融合名次（§7.3"退回按名次取前 8"）。
        merged = list(ordered)
        degraded = True
        reason = reason or "rerank_unavailable"

    window = merged[: max(top_n_min, min(top_n_max, len(merged)))]
    fallback = "fused_rank" if degraded else None
    outcome = RerankOutcome(
        ordered=window,
        degraded=degraded,
        reason=reason,
        scored=len(scored),
        elapsed_ms=elapsed_ms,
        fallback=fallback,
    )
    trace_event(
        "rerank.selected",
        query=query,
        ordered=[item.as_dict() for item in window],
        **outcome.as_audit(),
    )
    return outcome


__all__ = [
    "TEI_MAX_BATCH",
    "DegradeReason",
    "RerankOutcome",
    "RerankUnavailableError",
    "Reranker",
    "clip_for_rerank",
    "rerank_candidates",
]

# Backwards-compatible alias (N818: exception names use Error suffix).
RerankUnavailable = RerankUnavailableError


# Backwards-compatible alias (N818).
RerankTimeout = RerankTimeoutError
