"""集中配置。

两条规则，写在这里就不用在每个模块里重复：

1. **业务口径一律来自环境变量，代码里不给默认值**——门控阈值、topK/topN、
   证据预算、跳数、模型名与维度。默认值就是"硬编码"，换一个模型要改代码，
   迟早出现"配置里写 A、代码里跑 B"。缺了就启动失败，报错里点名缺哪个变量。
2. **基础设施参数给安全默认值**——连接池大小、连接超时、探测超时。
   这些值不改变业务语义，给了默认值也不会造成"跑的不是你以为的那套"。

参考：详细设计.md §7.5（参数与预算）、§8.1（门控）、§11.4（限额）、附录 B。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """进程启动时读一次，不可变（frozen）。"""

    model_config = SettingsConfigDict(extra="forbid", case_sensitive=False, frozen=True)

    # --- 运行环境 ---
    app_env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "ai-rag-api"
    log_level: str = "INFO"

    # --- PostgreSQL：唯一真相 ---
    database_url: str
    db_pool_min: int = Field(default=1, ge=0)
    db_pool_max: int = Field(default=8, ge=1)
    db_connect_timeout_s: float = Field(default=3.0, gt=0)

    # --- Qdrant：只存切片编号、向量与过滤字段 ---
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: SecretStr = SecretStr("")
    qdrant_timeout_s: float = Field(default=5.0, gt=0)

    # --- 模型网关（全部走 OpenAI 兼容 / TEI 原生接口，名字与维度都不写死）---
    embedding_base_url: str
    embedding_api_key: SecretStr
    embedding_model: str
    embedding_dim: int = Field(gt=0)
    embedding_timeout_s: float = Field(default=10.0, gt=0)

    rerank_base_url: str
    rerank_api_key: SecretStr
    rerank_model: str
    rerank_timeout_s: float = Field(default=10.0, gt=0)

    # LLM/VLM 走外部网关；未配置时相关能力不可用，但服务本身要能起来。
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = ""
    llm_timeout_s: float = Field(default=30.0, gt=0)

    vlm_base_url: str = ""
    vlm_api_key: SecretStr = SecretStr("")
    vlm_model: str = ""
    vlm_timeout_s: float = Field(default=60.0, gt=0)

    # --- 检索与门控（§7.5 / §8.1）---
    gate_reject_none: float = Field(gt=0, le=1)
    gate_reject_low: float = Field(gt=0, le=1)
    retrieval_top_k: int = Field(gt=0)
    rerank_top_n_min: int = Field(gt=0)
    rerank_top_n_max: int = Field(gt=0)
    max_hops: int = Field(ge=0, le=5)
    evidence_max_chars: int = Field(gt=0)

    # --- 限额（§11.4）---
    qa_budget_s: int = Field(default=30, gt=0)
    max_question_chars: int = Field(default=500, gt=0)
    qa_max_concurrency: int = Field(default=8, gt=0)

    # --- 审计（§11.2：保留一年，按合规要求可调）---
    audit_retention_days: int = Field(default=365, gt=0)

    # --- 健康检查 ---
    health_probe_timeout_ms: int = Field(default=1500, gt=0)

    # --- 身份（§5.1 / B2 卡实现解析，这里只做配置承载）---
    auth_mode: str = "fixed_jwt"
    auth_jwt_secret: SecretStr = SecretStr("")
    auth_jwt_alg: str = "HS256"
    auth_jwt_user_id_claim: str = "sub"
    auth_jwt_depts_claim: str = "user_depts"
    auth_jwt_roles_claim: str = "user_roles"
    auth_jwt_admin_claim: str = "admin"

    @field_validator("qdrant_url", "embedding_base_url", "rerank_base_url")
    @classmethod
    def _require_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            raise ValueError("must be a non-empty URL")
        if not value.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return value

    @field_validator(
        "database_url", "qdrant_collection", "embedding_model", "rerank_model"
    )
    @classmethod
    def _require_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @model_validator(mode="after")
    def _check_cross_field(self) -> "Settings":
        # 门控两档必须严格递增，否则 reject_none 区间接不到 reject_low。
        if self.gate_reject_none >= self.gate_reject_low:
            raise ValueError("GATE_REJECT_NONE must be < GATE_REJECT_LOW")
        if self.rerank_top_n_min > self.rerank_top_n_max:
            raise ValueError("RERANK_TOP_N_MIN must be <= RERANK_TOP_N_MAX")
        # 精排取不了比召回到的更多：top_n_max 必须落在 top_k 之内。
        if self.rerank_top_n_max > self.retrieval_top_k:
            raise ValueError("RERANK_TOP_N_MAX must be <= RETRIEVAL_TOP_K")
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("DB_POOL_MIN must be <= DB_POOL_MAX")
        # 配了网关却没配模型名，等于让代码去猜一个模型。
        for gw, model in (("LLM", self.llm_model), ("VLM", self.vlm_model)):
            base = self.llm_base_url if gw == "LLM" else self.vlm_base_url
            if base.strip() and not model.strip():
                raise ValueError(f"{gw}_MODEL is required when {gw}_BASE_URL is set")
        return self

    # --- 派生量 ---

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def health_probe_timeout_s(self) -> float:
        return self.health_probe_timeout_ms / 1000.0

    def public_config(self) -> dict[str, Any]:
        """给 /healthz 用的配置指纹：只有口径，没有密钥。

        用途是"配置化"这件事能被直接看到——换阈值、换模型，curl 一眼就知道
        跑的是哪套值，不用去翻容器环境变量。
        """
        return {
            "gates": {
                "reject_none": self.gate_reject_none,
                "reject_low": self.gate_reject_low,
            },
            "retrieval": {
                "top_k": self.retrieval_top_k,
                "top_n_min": self.rerank_top_n_min,
                "top_n_max": self.rerank_top_n_max,
                "max_hops": self.max_hops,
                "evidence_max_chars": self.evidence_max_chars,
            },
            "limits": {
                "qa_budget_s": self.qa_budget_s,
                "max_question_chars": self.max_question_chars,
                "qa_max_concurrency": self.qa_max_concurrency,
            },
            "models": {
                "embedding": {"model": self.embedding_model, "dim": self.embedding_dim},
                "rerank": {"model": self.rerank_model},
                "llm": {"model": self.llm_model or None},
            },
            "stores": {
                "qdrant_collection": self.qdrant_collection,
                "audit_retention_days": self.audit_retention_days,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。读不到必填项时 pydantic 会点名缺哪个变量后抛错。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试用：环境变量变了以后重新加载。"""
    get_settings.cache_clear()
