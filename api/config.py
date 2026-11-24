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

    # --- B3：检索的其余业务阈值（§7.2 / §7.3 / §7.5 / §8.1 / §8.2）---
    # 每一条都是设计文档点名的数字，所以按本模块的第一条规则：来自环境变量，
    # 代码里不给默认值。给了默认值就等于"文档改了阈值、代码还在用旧的"。
    #
    # 精排预算（§7.3"精排超时（> 800 毫秒）"）。超了就降级为按名次取前 N，
    # 并在审计里记 degrade=rerank_timeout。
    rerank_budget_ms: int = Field(gt=0)
    # 精排候选上限（§7.5"上限 50，超了按名次截"）。
    rerank_candidates_max: int = Field(gt=0)
    # 同一父片最多保留几条（§7.2"同一父片最多保留两条子片"）。
    parent_child_keep: int = Field(gt=0)
    # 门控的辅助判定带（§8.1"最高分在 0.38–0.42 之间按辅助条件决定"）。
    gate_borderline_low: float = Field(gt=0, le=1)
    gate_borderline_high: float = Field(gt=0, le=1)
    # 澄清触发：两份以上文档且分数差小于这个值（§8.2"差小于 0.05"）。
    clarify_score_delta: float = Field(gt=0, lt=1)
    # 澄清选项上限（§8.2"最多四个"）。
    clarify_options_max: int = Field(gt=0)
    # 拒答时给出的疑似文档上限（§8.1"有权限的疑似文档清单（≤3 份）"）。
    suspected_docs_max: int = Field(gt=0)
    # 复杂问题的有效证据条数下限（§8.1"复杂问题要求 ≥2 条分属不同段落"）。
    evidence_min_paras: int = Field(gt=0)

    # --- 限额（§11.4）---
    qa_budget_s: int = Field(default=30, gt=0)
    max_question_chars: int = Field(default=500, gt=0)
    qa_max_concurrency: int = Field(default=8, gt=0)

    # --- B4：生成与校验（§9 / §10 / 附录 C.2）---
    # 逐字增量的字符数。不是业务阈值而是传输粒度，所以给默认值：
    # 2 字一帧在中文下的观感接近打字机，一帧约 50 字节开销，一次 300 字的答案
    # 约 150 帧，浏览器与反代都不会有问题。调大只是"蹦字"更明显。
    sse_token_chunk_chars: int = Field(default=2, gt=0, le=64)

    # --- 审计（§11.2：保留一年，按合规要求可调）---
    audit_retention_days: int = Field(default=365, gt=0)

    # --- 健康检查 ---
    health_probe_timeout_ms: int = Field(default=1500, gt=0)

    # --- 摄取（A1：样本扫描 / ingest_jobs 状态机）---
    # 样本目录：开发期摄取不是"上传接口"，是扫目录（§3.1）。给默认值是因为它属于
    # 基础设施参数（换个部署位置不该改代码的语义），但目录不存在时扫描必须报错而不是
    # 静悄悄返回 0 条——见 api/ingest/fingerprint.py 与 /ingest/scan 的 500/空结果区分。
    ingest_samples_dir: str = "samples"
    # 重试上限：口径是"1 次尝试 + N 次重试"（§4.4「失败重试三次转人工清单」）。
    ingest_max_retries: int = Field(default=3, ge=0, le=10)
    # 常驻 worker 的空队列轮询间隔（秒）。和 vector_sync_interval_s 同属运维参数：
    # 改它不改变"哪些材料可检索"，所以给默认值。
    ingest_poll_interval_s: float = Field(default=10.0, gt=0)

    # --- 向量写入 / 同步 / 回收（A3：§3.6、§4.1-§4.3）---
    # 同步失败重试三次（§4.2）。和 ingest_max_retries 同口径：三次**重试**，
    # 即最多 4 次尝试。给了默认值是因为它属于运维参数，不改变检索语义。
    vector_sync_max_attempts: int = Field(default=4, ge=1, le=10)
    # 重试退避基数（秒）：第 n 次失败后等 base * 2^(n-1)。翻牌后一分钟内必须同步完
    # （§4.2），所以基数不能大：1s/2s/4s 三次退避仍在窗口内。
    vector_sync_backoff_s: float = Field(default=1.0, ge=0.0)
    # 旧版本保留窗口（§4.3「保留 24 小时，留给回滚的门」）。
    vector_retention_hours: int = Field(default=24, ge=0)
    # 切片图目录（§3.4 裁图落在 figures/{chunk_id}.webp）；回收时一起清。
    figures_dir: str = "figures"
    # worker 的同步轮询间隔（秒）。§4.2 要求"翻牌后一分钟内必须同步完"，
    # 所以默认 10s：一次抖动最多吃掉两三轮重试，仍在一分钟预算内。
    vector_sync_interval_s: float = Field(default=10.0, gt=0)
    # 每 N 轮做一次全量对账（0 = 不做）。对账要滚动整个集合，比 drain 贵一个量级，
    # 所以不在每一轮做；但"漂移没人数才可怕"（§4.2），所以也不能不做。
    vector_sync_reconcile_every: int = Field(default=10, ge=0)

    # --- 身份（§5.1 / B2 卡实现解析，这里只做配置承载）---
    # AUTH_MODE 决定凭证怎么来：fixed_jwt 验签；mock_header 只给本地开发用。
    # 两种模式的必填项由下面的跨字段校验分别盯着，避免"某模式下少配一项也能起"。
    auth_mode: Literal["fixed_jwt", "mock_header"] = "fixed_jwt"
    auth_jwt_secret: SecretStr = SecretStr("")
    auth_jwt_alg: Literal["HS256", "HS384", "HS512"] = "HS256"
    auth_jwt_user_id_claim: str = "sub"
    auth_jwt_depts_claim: str = "user_depts"
    auth_jwt_roles_claim: str = "user_roles"
    auth_jwt_admin_claim: str = "admin"
    # 开发夹具身份（AUTH_MODE=mock_header 时使用）。给空默认值而不是造一个假用户：
    # 默认身份一旦写进代码，生产也会有一个"谁都能当"的账号。
    auth_mock_user_id: str = ""
    auth_mock_depts: str = ""
    auth_mock_roles: str = ""
    auth_mock_admin: bool = False

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
        # 精排窗口不能超过候选上限：超了等于一半候选白召回，而精排预算却按更大
        # 的窗口在算——两边不一致时，先超时的是精排，先失效的却是召回。
        if self.rerank_top_n_max > self.rerank_candidates_max:
            raise ValueError("RERANK_TOP_N_MAX must be <= RERANK_CANDIDATES_MAX")
        # 澄清带必须落在两档门控之间（§8.1 的"0.38–0.42 之间"是一个相对位置，
        # 不是绝对数值）。这里只校验"上下界递增"，不硬性要求它落在 [reject_none,
        # reject_low] 之内——因为门控阈值本身**可调**（换模型后要重标定，见
        # default 卡的模型定档说明）。硬性限制会让"把阈值调到 0.10/0.55 重标定"
        # 直接启动失败，那是把标定这件事堵死。
        #
        # 越界的处置交给 `GateConfig`：它把澄清带裁到 [reject_none, reject_low] 内
        # （见下），保证"带宽为 0"时退化成纯阈值判定而不是永远不生效。
        if self.gate_borderline_low >= self.gate_borderline_high:
            raise ValueError("GATE_BORDERLINE_LOW must be < GATE_BORDERLINE_HIGH")
        if self.db_pool_min > self.db_pool_max:
            raise ValueError("DB_POOL_MIN must be <= DB_POOL_MAX")
        # 配了网关却没配模型名，等于让代码去猜一个模型。
        for gw, model in (("LLM", self.llm_model), ("VLM", self.vlm_model)):
            base = self.llm_base_url if gw == "LLM" else self.vlm_base_url
            if base.strip() and not model.strip():
                raise ValueError(f"{gw}_MODEL is required when {gw}_BASE_URL is set")
        # 身份模式的必填项分别盯着：缺了就在启动时点名，而不是等第一个请求 401。
        # 用空 secret 验签等于不验签——这是最容易被忽略的一种"配好了但没生效"。
        if (
            self.auth_mode == "fixed_jwt"
            and not self.auth_jwt_secret.get_secret_value()
        ):
            raise ValueError("AUTH_JWT_SECRET is required when AUTH_MODE=fixed_jwt")
        # mock_header 不校验 AUTH_MOCK_* 是否有值：身份来自请求头，
        # 环境变量只是给 `scripts/` 与 curl 用的便利夹具。要求它必填，
        # 会让"只想用请求头试一下"的场景卡在启动阶段。
        return self

    @property
    def auth_signs_tokens(self) -> bool:
        """是否真的在验签。`mock_header` 只该出现在开发环境。"""
        return self.auth_mode == "fixed_jwt"

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
                "candidates_max": self.rerank_candidates_max,
                "rerank_budget_ms": self.rerank_budget_ms,
                "parent_child_keep": self.parent_child_keep,
                "max_hops": self.max_hops,
                "evidence_max_chars": self.evidence_max_chars,
                "evidence_min_paras": self.evidence_min_paras,
            },
            "clarify": {
                "score_delta": self.clarify_score_delta,
                "options_max": self.clarify_options_max,
                "suspected_docs_max": self.suspected_docs_max,
                "borderline": [self.gate_borderline_low, self.gate_borderline_high],
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
            "generation": {
                # 生成与判别的模型名是"配置指纹"的一部分：换模型必须能在 /healthz
                # 一眼看出来，否则排查质量回退时第一个问题（跑的是哪个模型）答不上。
                "llm_configured": bool(self.llm_base_url.strip()),
                # Temperature 固定 0（§9.2），不做成配置项：它是硬约束不是旋钮。
                "temperature": 0.0,
                "sse_token_chunk_chars": self.sse_token_chunk_chars,
            },
            "stores": {
                "qdrant_collection": self.qdrant_collection,
                "audit_retention_days": self.audit_retention_days,
            },
        }

    def public_ingest_config(self) -> dict[str, Any]:
        """摄取侧的配置指纹（非生产返回）。

        `scan_lock_key` 也在这里：advisory lock 的数值属于实现细节，但验证脚本需要
        用它来制造"另一个进程持锁"的场景。让服务自己报，脚本就不必抄一份数值
        （抄了之后改 key 不会红，脚本只会静静失去测并发的能力）。
        """
        from api.ingest.lockkeys import SCAN_LOCK_KEY  # 局部导入：config 不该依赖 ingest 包

        return {
            "samples_dir": self.ingest_samples_dir,
            "max_retries": self.ingest_max_retries,
            "poll_interval_s": self.ingest_poll_interval_s,
            "scan_lock_key": SCAN_LOCK_KEY,
            "vector_sync": {
                "max_attempts": self.vector_sync_max_attempts,
                "backoff_s": self.vector_sync_backoff_s,
                "retention_hours": self.vector_retention_hours,
                "figures_dir": self.figures_dir,
                "interval_s": self.vector_sync_interval_s,
                "reconcile_every": self.vector_sync_reconcile_every,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。读不到必填项时 pydantic 会点名缺哪个变量后抛错。"""
    return Settings()


def reset_settings_cache() -> None:
    """测试用：环境变量变了以后重新加载。"""
    get_settings.cache_clear()
