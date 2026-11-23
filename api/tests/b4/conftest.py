"""B4 集成夹具：真 PG + 真 Qdrant + 真 TEI 检索，生成/判别用可注入的替身。

为什么生成用替身而不是真模型：这台机器上**没有配置 LLM 网关**（`LLM_BASE_URL` /
`LLM_MODEL` 都是空），所以"真模型生成"这一环在这里无法实测。诚实的做法是把它
明确标出来（见 `test_b4_integration.py` 末尾的说明），而不是用假模型假装跑通。

替身只替换**两处外部调用**：

- `LlmClient.complete`（生成与判别）—— 返回预设的 JSON；
- 其余全部真跑：真 PG 取正文、真 Qdrant 两路召回、真 TEI 编码与精排、真门控、
  真证据包组装、真五道校验、真 SSE 帧。

这样"citations 含 doc/page/bbox/snippet/chunk_id"这条验收验的是**库里的真行**：
bbox 与页码来自 `chunks` 表，而不是测试里编的字面量。
"""

from __future__ import annotations

from typing import Any

import pytest

from api.generate.llm import LlmClient
from api.identity import IdentitySnapshot
from api.qa.orchestrator import QaService
from api.retrieval.pipeline import build_pipeline
from api.tests.a3.conftest import (  # noqa: F401 - 传递依赖：b3 的 fixture 会向它要
    a3_dsn,
    chunk_rows,
    migrated,
    seed_document,
)
from api.tests.b3.conftest import (  # noqa: F401 - fixtures 靠 conftest 发现
    b3_pg,
    collection,
    conn,
    doc_key,
    embedder,
    insert_chunk,
    make_chunk,
    pool,
    publish,
    qdrant_reachable,
    query_embedder,
    rerank_reachable,
    reranker,
    retriever,
    store,
    unique,
    version_of,
)

# B4 复用 B3 的 `b3test/` 命名空间：造数走 `publish()`，它把 doc_key 写成
# `b3test/<name>`，而 b3 的清理 fixture 按那个前缀精确删。自己再起一个 `b4test/`
# 前缀只会让两套清理互不相识，残留行会被下一次全库对拍的用例当成陌生数据。
NS = "b3test"

FIN = IdentitySnapshot(user_id="u_fin", depts=("fin",), roles=("staff",))


class ScriptedLlm(LlmClient):
    """按脚本返回的 `LlmClient` 替身。

    两个方法分开配（生成 / 判别）：判别器的返回要与生成的对上才能验"第五道通过"，
    所以脚本必须能分别指定。它记录每次调用的 `temperature`——§9.2 的
    `Temperature = 0` 是硬约束，只有记下来才能断言。
    """

    def __init__(self, *, generation: str, verdict: str = "SUPPORTED") -> None:  # noqa: D107
        self.generation = generation
        self.verdict = verdict
        self.temperatures: list[float] = []
        self.prompts: list[str] = []

    def complete(self, messages, *, temperature: float = 0.0) -> str:  # type: ignore[override]
        self.temperatures.append(temperature)
        body = "\n".join(message["content"] for message in messages)
        self.prompts.append(body)
        if "SUPPORTED" in body:
            return f'{{"verdict":"{self.verdict}","reason":"scripted"}}'
        return self.generation


@pytest.fixture(scope="session")
def b4_pg(b3_pg):  # noqa: F811 - 复用 B3 的真 PG（同一份 schema，同一个迁移入口）
    return b3_pg


def build_qa_service(
    *,
    b3_pg,
    settings,
    embedder,  # noqa: ARG001 - 只为拿它做 skip 判定
    retriever: Any,
    reranker: Any,
    llm: LlmClient | None,
) -> QaService:
    """按 B3 的真链路 + 给定的生成替身装一条问答链路。

    走 `build_pipeline(settings, pg)` 而不是自己 new 五个对象：装配只有一处，
    少传一个参数就会表现成"召回少了几条"而没有报错（见 `pipeline.build_pipeline`
    的说明）。检索侧的 `retriever` / `reranker` 用 fixture 里的（它们指向真服务）。
    """
    from api.retrieval.pipeline import QueryEmbedder

    pipeline = build_pipeline(settings, b3_pg)
    # 用 conftest 里已经建好的客户端替换掉工厂新建的那两个，避免重复建连接池。
    pipeline.retriever.close()
    pipeline.reranker.close()
    pipeline.retriever = retriever
    pipeline.reranker = reranker
    pipeline.embedder = QueryEmbedder(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout_s=30,
    )
    return QaService(
        pipeline=pipeline,
        llm=llm,
        judge_client=llm,
        evidence_max_chars=settings.evidence_max_chars,
        token_chunk_chars=settings.sse_token_chunk_chars,
        model_name="scripted",
    )


__all__ = [
    "FIN",
    "NS",
    "ScriptedLlm",
    "b3_pg",
    "b4_pg",
    "build_qa_service",
    "collection",
    "conn",
    "doc_key",
    "embedder",
    "make_chunk",
    "pool",
    "publish",
    "qdrant_reachable",
    "rerank_reachable",
    "reranker",
    "store",
    "unique",
    "version_of",
]
