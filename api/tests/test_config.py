"""配置：必填项不许有代码默认值，跨字段约束必须真的拦得住。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from api.config import Settings, get_settings, reset_settings_cache
from api.tests.conftest import BASE_ENV, build_settings, make_settings

REPO_ROOT = Path(__file__).resolve().parents[2]

# 这些是"业务口径"：换模型、调阈值必须只改环境变量。
# 放在这里当一份清单，是为了让"以后有人顺手加个默认值"立刻红。
MUST_BE_REQUIRED = (
    "database_url",
    "qdrant_url",
    "qdrant_collection",
    "embedding_base_url",
    "embedding_api_key",
    "embedding_model",
    "embedding_dim",
    "rerank_base_url",
    "rerank_api_key",
    "rerank_model",
    "gate_reject_none",
    "gate_reject_low",
    "retrieval_top_k",
    "rerank_top_n_min",
    "rerank_top_n_max",
    "max_hops",
    "evidence_max_chars",
)


@pytest.mark.parametrize("field", MUST_BE_REQUIRED)
def test_business_values_have_no_code_default(field: str) -> None:
    info = Settings.model_fields[field]
    assert info.is_required(), f"{field} 在代码里给了默认值，等于硬编码"


def test_missing_required_field_names_the_variable() -> None:
    values = {k: v for k, v in BASE_ENV.items() if k != "GATE_REJECT_LOW"}
    with pytest.raises(ValidationError) as excinfo:
        build_settings(**values)
    assert "gate_reject_low" in str(excinfo.value).lower()


def test_thresholds_and_models_come_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GATE_REJECT_LOW", "0.55")
    monkeypatch.setenv("EMBEDDING_MODEL", "BAAI/other-model")
    monkeypatch.setenv("EMBEDDING_DIM", "1024")
    reset_settings_cache()
    try:
        loaded = get_settings()
    finally:
        reset_settings_cache()
    assert loaded.gate_reject_low == pytest.approx(0.55)
    assert loaded.embedding_model == "BAAI/other-model"
    assert loaded.embedding_dim == 1024


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"GATE_REJECT_NONE": 0.40}, "GATE_REJECT_NONE must be < GATE_REJECT_LOW"),
        ({"GATE_REJECT_LOW": 0.10}, "GATE_REJECT_NONE must be < GATE_REJECT_LOW"),
        ({"GATE_REJECT_LOW": 1.5}, "less than or equal to 1"),
        ({"GATE_REJECT_NONE": 0.0}, "greater than 0"),
        ({"RERANK_TOP_N_MIN": 9}, "RERANK_TOP_N_MIN must be <= RERANK_TOP_N_MAX"),
        ({"RERANK_TOP_N_MAX": 31}, "RERANK_TOP_N_MAX must be <= RETRIEVAL_TOP_K"),
        ({"MAX_HOPS": 9}, "less than or equal to 5"),
        ({"EMBEDDING_DIM": 0}, "greater than 0"),
        ({"QDRANT_URL": "qdrant:6333"}, "must start with http:// or https://"),
    ],
)
def test_cross_field_and_range_rules(overrides: dict[str, object], needle: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_settings(**overrides)
    assert needle in str(excinfo.value)


def test_gateway_without_model_is_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        make_settings(LLM_BASE_URL="https://gw.example.com/v1")
    assert "LLM_MODEL is required" in str(excinfo.value)


def test_gateway_with_model_is_accepted() -> None:
    loaded = make_settings(
        LLM_BASE_URL="https://gw.example.com/v1", LLM_MODEL="gw-qwen-max"
    )
    assert loaded.llm_model == "gw-qwen-max"


def test_public_config_has_no_secrets() -> None:
    loaded = make_settings(EMBEDDING_API_KEY="super-secret-embedding-key")
    dumped = repr(loaded.public_config())
    assert "super-secret-embedding-key" not in dumped
    assert loaded.public_config()["gates"] == {"reject_none": 0.2, "reject_low": 0.4}
    assert loaded.public_config()["retrieval"]["top_k"] == 30


def test_env_example_covers_every_required_variable() -> None:
    """代码要求某变量、.env.example 却没写，人照着 example 起服务必然失败。"""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    example_keys = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }
    missing = [
        name.upper() for name in MUST_BE_REQUIRED if name.upper() not in example_keys
    ]
    assert not missing, f".env.example 缺少配置项: {missing}"
