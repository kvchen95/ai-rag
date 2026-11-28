"""忠实性判别器（详细设计.md §10.1 第四道 / 附录 D.4）。.

判别器是这套系统里唯一"用模型判模型"的地方，所以它的约束比生成还紧：

1. **只吃三样东西**：问题、被引证据原文、草稿答案。不吃整库上下文——附录 D 原话
   "判别器只吃'问题+被引证据+答案'，不吃整库上下文，避免它自己脑补"。这里由
   `judge()` 的签名保证：它没有地方拿到别的证据。
2. **超时/格式错一律按"不支持"处理**（§10.1 表格第四道"超时/格式错按不支持"）。
   这是 Fail-Closed：判别器挂了等于没法证明答案有据，那就不能给结论。
   注意这与"系统异常"（§10.3 第三档）不同——判别器不可用**不是**"稍后重试"，
   而是"本次不作结论，附原文"。区别在于：这一档还能给用户原文，比一句
   "稍后重试"有用得多。
3. **`Temperature = 0`**，同生成。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal, cast

from api.generate.llm import (
    GenerationUnavailable,
    LlmClient,
    strip_code_fence,
)
from api.observability import get_logger, log_event
from api.prompts.generate import JUDGE_SYSTEM_PROMPT
from api.resources import messages as msg

logger = get_logger("api.generate.judge")

Verdict = Literal["SUPPORTED", "NOT_SUPPORTED", "skipped"]

_VERDICT_WORDS = re.compile(
    r"SUPPORTED|NOT_SUPPORTED|NOT SUPPORTED", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class JudgeResult:
    """一次判别。.

    `skipped=True` 表示没跑（没配模型），与"判了不支持"必须分开。
    """

    verdict: Verdict
    reason: str = ""
    skipped: bool = False
    elapsed_ms: float = 0.0

    @property
    def supported(self) -> bool:
        """Run supported."""
        return self.verdict == "SUPPORTED"

    def as_audit(self) -> dict[str, object]:
        # 审计只记判定与原因，不记录被引证据原文（§11.2 不存原文）。
        """Run as_audit."""
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "skipped": self.skipped,
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def build_judge_messages(
    question: str, evidence_block: str, answer: str
) -> list[dict[str, str]]:
    """问题 + 被引证据 + 答案，三段固定顺序。."""
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": msg.JUDGE_USER_PROMPT.format(
                question=question.strip(),
                evidence=evidence_block,
                answer=answer.strip(),
            ),
        },
    ]


def parse_verdict(payload: str) -> tuple[str, str]:
    """解析判别器输出。返回 (verdict, reason)；判不出来时抛 ValueError。.

    两种可救的形状：约定 JSON，以及"整句只说了 SUPPORTED / NOT_SUPPORTED"。
    后者是真实会出现的（判别器是通用模型，不一定守 JSON 约束），而它的语义
    与 JSON 完全一致，所以按字面认。除此之外一律判格式错——**不猜**。
    注意 `NOT_SUPPORTED` 里有 `SUPPORTED` 子串，所以必须先检查前者。
    """
    body, _ = strip_code_fence(payload)
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict):
        verdict = (
            str(data.get("verdict") if data.get("verdict") else "")
            .strip()
            .upper()
            .replace(" ", "_")
        )
        if verdict in ("SUPPORTED", "NOT_SUPPORTED"):
            return verdict, str(
                data.get("reason") if data.get("reason") else ""
            )[:500]
        raise ValueError(
            msg.JUDGE_VERDICT_INVALID.format(verdict=data.get("verdict"))
        )
    words = _VERDICT_WORDS.findall(body)
    if own := [w.upper().replace(" ", "_") for w in words]:
        # 出现了两种标记时判格式错：那是"我不确定"的写法，不能当成支持。
        if len(set(own)) == 1:
            return own[0], body[:500]
        raise ValueError(
            msg.JUDGE_TWO_VERDICTS.format(verdicts=sorted(set(own)))
        )
    raise ValueError(msg.JUDGE_UNPARSABLE.format(body=body[:200]))


def judge(
    client: LlmClient | None,
    question: str,
    evidence_block: str,
    answer: str,
) -> JudgeResult:
    """跑第四道校验。.

    `client=None` 表示没配生成/判别模型 → `skipped`（不是不支持）。

    `skipped` 与 `NOT_SUPPORTED` 的处置相同（都走降级），但审计里必须能分辨：
    前者是部署缺配置，后者是答案真的没被判据支持。混成一个码会让"模型没配"
    看起来像"模型说答案错了"。
    """
    if client is None:
        return JudgeResult(
            verdict="skipped", reason=msg.JUDGE_NOT_CONFIGURED, skipped=True
        )

    import time

    started = time.perf_counter()
    try:
        content = client.complete(
            build_judge_messages(question, evidence_block, answer),
            temperature=0.0,
        )
        verdict, reason = parse_verdict(content)
    except GenerationUnavailable as exc:
        # §10.1 表格：超时按不支持。这是 Fail-Closed，不是"稍后重试"：
        # 判别器不可用时我们仍能给出原文，比一句"稍后重试"有用。
        elapsed = (time.perf_counter() - started) * 1000
        log_event(
            logger,
            "judge_unavailable",
            level=40,
            error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return JudgeResult(
            verdict="NOT_SUPPORTED",
            reason=msg.JUDGE_UNAVAILABLE.format(error_type=type(exc).__name__),
            elapsed_ms=elapsed,
        )
    except ValueError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        log_event(logger, "judge_bad_output", level=40, error=str(exc)[:200])
        return JudgeResult(
            verdict="NOT_SUPPORTED",
            reason=msg.JUDGE_BAD_OUTPUT.format(error=exc)[:500],
            elapsed_ms=elapsed,
        )
    elapsed = (time.perf_counter() - started) * 1000
    return JudgeResult(
        verdict=cast(Verdict, verdict), reason=reason, elapsed_ms=elapsed
    )


__all__ = [
    "JudgeResult",
    "Verdict",
    "build_judge_messages",
    "judge",
    "parse_verdict",
]
