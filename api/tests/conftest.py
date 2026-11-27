"""测试夹具。

两件事：
1. 清掉环境变量，保证测试读的是用例显式给的值——否则本机 .env 或
   compose 环境会把"代码默认值"的断言悄悄变成"环境里碰巧是这么写的"。
2. 给一份完整的必填配置，缺哪个字段就在这一处补，别在每个用例里抄。
"""

from __future__ import annotations

import os

import pytest

from api.config import Settings

# 与 .env.example 对齐的必填项。test_config 里有一条断言盯着这两处不许脱节。
BASE_ENV: dict[str, object] = {
    "DATABASE_URL": "postgresql+psycopg://ai_rag:ai_rag_dev_password@127.0.0.1:5432/ai_rag",
    "QDRANT_URL": "http://127.0.0.1:6333",
    "QDRANT_COLLECTION": "kb_chunks",
    "EMBEDDING_BASE_URL": "http://embedding:8000/v1",
    "EMBEDDING_API_KEY": "local",
    "EMBEDDING_MODEL": "BAAI/bge-small-zh-v1.5",
    "EMBEDDING_DIM": 512,
    "RERANK_BASE_URL": "http://rerank:8000",
    "RERANK_API_KEY": "local",
    "RERANK_MODEL": "BAAI/bge-reranker-base",
    "GATE_REJECT_NONE": 0.20,
    "GATE_REJECT_LOW": 0.40,
    "GATE_BORDERLINE_LOW": 0.38,
    "GATE_BORDERLINE_HIGH": 0.42,
    "RETRIEVAL_TOP_K": 30,
    "RERANK_TOP_N_MIN": 5,
    "RERANK_TOP_N_MAX": 8,
    "RERANK_CANDIDATES_MAX": 50,
    "RERANK_BUDGET_MS": 800,
    "RERANK_TEXT_MAX_CHARS": 400,
    "PARENT_CHILD_KEEP": 2,
    "CLARIFY_SCORE_DELTA": 0.05,
    "CLARIFY_OPTIONS_MAX": 4,
    "SUSPECTED_DOCS_MAX": 3,
    "EVIDENCE_MIN_PARAS": 2,
    "MAX_HOPS": 3,
    "EVIDENCE_MAX_CHARS": 6000,
    # 身份：fixed_jwt 模式下没有 secret 等于不验签，所以它也是必填项（§5.1）。
    "AUTH_JWT_SECRET": "dev-only-test-secret",
}


def build_settings(**values: object) -> Settings:
    """构造 Settings：键名统一转小写。

    pydantic 的 `case_sensitive=False` 只作用于**环境变量**查找，
    直接传 init 参数时字段名必须精确匹配。测试里写 EVN 风格的大写键更贴近
    compose，于是统一在这里做一次归一，避免每个用例各写一遍小写。
    """
    return Settings(**{key.lower(): value for key, value in values.items()})


def make_settings(**overrides: object) -> Settings:
    return build_settings(**{**BASE_ENV, **overrides})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(Settings.model_fields):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    """集成测试用的真实 PG。

    优先取 AI_RAG_TEST_DATABASE_URL；没有就退回 DATABASE_URL（容器内跑测试时
    这个变量本来就有，指向 compose 里的 postgres）。两个都没有就跳过——
    不假装跑过。
    """
    dsn = os.getenv("AI_RAG_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        pytest.skip(
            "neither AI_RAG_TEST_DATABASE_URL nor DATABASE_URL is set "
            "(integration needs a real PostgreSQL)"
        )
    assert dsn
    return dsn
