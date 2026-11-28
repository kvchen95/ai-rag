"""审计写入（详细设计.md §11.2）。.

一次问答一条记录，落到 PG 的 `qa_log`。表由 `db/migrations/001_init.sql`
（DA1 卡）建，列口径见附录 A.4 + §11.2 的身份快照。这里只负责写，不重复建表：
迁移的入口只有一个（`scripts/db_migrate.py`），两套 runner 抢同一张
`schema_migrations` 迟早会出现"我记录过、你没记录"的分裂。

三条口径：

1. **只存切片编号与分数摘要，不存完整原文**——原文在库里本来就有，
   重复存会让审计表变成第二份正文，两边还会不一致。
2. **身份快照是必填**：没有 user_id 的审计回答不了"谁在什么时候看了什么"，
   第三道闸等于没做。表里 user_id 目前可空（DB 层拦不住），应用层在这里拦。
3. **审计写失败不能把一次成功的回答变成失败**：调用方拿到
   `AuditResult(ok=False)`，自己决定是否告警；这里只记日志、绝不抛。
   但失败一定是 ERROR 级——静默吞掉就等于"审计缺失且无人知道"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.clients.pg import PgDatabase
from api.errors import ErrorCode
from api.observability import get_logger, log_event
from api.resources import messages as msg

logger = get_logger("api.audit")

Gate1Verdict = Literal["pass", "reject_none", "reject_low", "clarify"]
Gate2Verdict = Literal["supported", "not_supported", "skipped"]

REJECT_VERDICTS = ("reject_none", "reject_low")

INSERT_SQL = """
INSERT INTO qa_log (
    user_id, user_depts, user_roles, is_admin,
    question, retrieved_ids, score_snapshot,
    gate1_verdict, gate2_verdict, hop_count,
    answer, citations, reject_reason, latency_ms, model
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s, %s, %s
)
RETURNING id
"""

PURGE_SQL = (
    "DELETE FROM qa_log WHERE created_at < now() - make_interval(days => %s)"
)


class QaLogEntry(BaseModel):
    """一条问答审计。字段名与附录 A.4 对齐，另加 §11.2 的身份快照。."""

    model_config = ConfigDict(extra="forbid")

    # --- 身份快照（§11.2 / §5.1）：管理员跨部门访问必须在审计里可辨 ---
    user_id: str = Field(min_length=1, description=msg.DESC_AUDIT_USER_ID)
    user_depts: list[str] = Field(default_factory=list)
    user_roles: list[str] = Field(default_factory=list)
    is_admin: bool = False

    # --- 问答本身（附录 A.4）---
    question: str = Field(min_length=1, description=msg.DESC_AUDIT_QUESTION)
    retrieved_ids: list[str] = Field(
        default_factory=list, description=msg.DESC_AUDIT_RETRIEVED
    )
    score_snapshot: dict[str, Any] = Field(
        default_factory=dict, description=msg.DESC_AUDIT_SCORES
    )
    gate1_verdict: Gate1Verdict
    gate2_verdict: Gate2Verdict | None = None
    hop_count: int = Field(default=0, ge=0)
    answer: str | None = None
    citations: list[dict[str, Any]] | None = None
    reject_reason: ErrorCode | None = None
    latency_ms: int = Field(ge=0)
    model: str | None = None

    @model_validator(mode="after")
    def _check_paired_rules(self) -> QaLogEntry:
        """成对口径。DB 层只有单列 CHECK，这三条只能在应用层兜。.

        每条都配了"反方向"的断言，否则只约束一边的规则会被
        "改语义不改数量"的写法绕过（例如把 low_confidence 的判定改成 pass，
        行照样插得进去，统计口径却变了）。
        """
        if self.gate1_verdict in REJECT_VERDICTS and self.reject_reason is None:
            raise ValueError(
                msg.AUDIT_REJECT_REASON_REQUIRED.format(
                    verdict=self.gate1_verdict
                )
            )
        if self.reject_reason in (
            ErrorCode.NO_EVIDENCE,
            ErrorCode.LOW_CONFIDENCE,
        ) and (self.gate1_verdict not in REJECT_VERDICTS):
            raise ValueError(
                msg.AUDIT_REJECT_MISMATCH.format(
                    reason=self.reject_reason, verdict=self.gate1_verdict
                )
            )
        # 第五道失败时判别器仍可能为 supported，两者不矛盾：门控放行且证据有据，
        # 但同属性多值的内部一致性不过，仍然只能拒答并附原文。
        return self


@dataclass(frozen=True, slots=True)
class AuditResult:
    """AuditResult."""
    ok: bool
    audit_id: int | None = None
    error: str | None = None


class AuditWriter:
    """AuditWriter."""
    def __init__(self, pg: PgDatabase, *, retention_days: int = 365) -> None:
        """Initialize the instance."""
        self._pg = pg
        self.retention_days = retention_days

    def record(self, entry: QaLogEntry) -> AuditResult:
        """Run record."""
        params = (
            entry.user_id,
            entry.user_depts,
            entry.user_roles,
            entry.is_admin,
            entry.question,
            entry.retrieved_ids,
            Jsonb(entry.score_snapshot),
            entry.gate1_verdict,
            entry.gate2_verdict,
            entry.hop_count,
            entry.answer,
            None if entry.citations is None else Jsonb(entry.citations),
            None if entry.reject_reason is None else entry.reject_reason.value,
            entry.latency_ms,
            entry.model,
        )
        try:
            row = self._pg.fetch_one(INSERT_SQL, params)
        except Exception as exc:  # noqa: BLE001 - 审计失败不改写调用结果，但必须留痕
            logger.error(
                "audit_write_failed",
                extra={
                    "fields": {
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:300],
                    }
                },
            )
            return AuditResult(
                ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}"
            )
        audit_id = int(row["id"]) if row else None
        # 日志里带 audit_id 与 request_id，两者合起来就是审计行与链路的连接点。
        log_event(
            logger,
            "audit_written",
            audit_id=audit_id,
            gate1=entry.gate1_verdict,
            hops=entry.hop_count,
            is_admin=entry.is_admin,
        )
        return AuditResult(ok=True, audit_id=audit_id)

    def purge_expired(self) -> int:
        """按保留期清理（§11.2：默认一年，按合规要求可调）。由定时任务调用。."""
        deleted = self._pg.execute(PURGE_SQL, (self.retention_days,))
        log_event(
            logger,
            "audit_purged",
            deleted=deleted,
            retention_days=self.retention_days,
        )
        return deleted
