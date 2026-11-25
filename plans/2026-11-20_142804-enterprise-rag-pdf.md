# 企业级 RAG 搭建计划（PDF 知识库，私有化部署）

> 生成时间：2026-11-20 14:28 CST · 状态：待执行 · 执行者无需本仓库先验上下文

## Goal

搭一个私有化部署的企业级 RAG 系统：上传 PDF → 自动解析/OCR → 切分 → 向量化入库 → 带引用来源的中文问答 API，试点规模（<1 万份 PDF，<100 万 chunk）单机可跑。

## 已拍板的决策（不要再问）

| 决策点 | 结论 |
|---|---|
| 部署 | 私有化/自托管，Docker Compose 单机 |
| 规模 | 试点：≤1 万份 PDF、≤100 万 chunk、低并发（<10 QPS） |
| 语言 | 中文为主，少量英文 |
| 算力 | 无 GPU，纯 CPU（模型选型必须 CPU 友好） |
| ACL | 一期不需要（单租户统一可见），schema 预留扩展 |
| LLM | 企业已有 OpenAI 兼容网关（base_url + api_key 由用户在 `.env` 填） |
| PDF 形态 | 混合：自动判断文本型/扫描型，扫描件走 OCR 兜底 |

## 技术栈（定版）

| 层 | 选型 | 理由 |
|---|---|---|
| 语言/包管理 | Python 3.11 + **uv** | 用户硬性要求 uv |
| API | FastAPI + uvicorn | 标准选择 |
| 数据库+向量 | **PostgreSQL 16 + pgvector**（HNSW） | 单机规模一个库搞定元数据+向量，不引入独立向量库（Milvus/Qdrant 属过度设计，YAGNI） |
| PDF 解析 | PyMuPDF（文本型）+ **RapidOCR**（onnxruntime 版 PaddleOCR，扫描件兜底） | RapidOCR 比 paddlepaddle 原生安装轻得多，CPU 友好，中文识别质量够 |
| 切分 | 自研段落优先切分器（500 字，重叠 100） | 可控、可测试，不引 LangChain |
| Embedding | `BAAI/bge-m3`（1024 维，dense），CPU 批量编码 | 中文/中英混合质量最好的开源 embedding 之一；太大可切 `BAAI/bge-small-zh-v1.5`（512 维，见 Risks） |
| Rerank | `BAAI/bge-reranker-v2-m3`，用 sentence-transformers 的 `CrossEncoder` 加载 | 免装 FlagEmbedding；可配置开关（CPU 上 top-20 重排约 1~2s） |
| LLM 接入 | `openai` 官方 client 指向企业网关 | OpenAI 兼容 |
| 任务队列 | 一期不引 Redis/Celery：PostgreSQL job 表 + `FOR UPDATE SKIP LOCKED` 轮询 worker | 单机试点够用；预留升级路径 |

不引入：LangChain / LlamaIndex（解析链路自研更可控）、Redis、Milvus/Qdrant、Langfuse（一期日志落 Postgres 即可）。

## 架构

```
PDF ──上传──▶ FastAPI ──▶ documents(queued) ──▶ ingest_jobs
                                                │
                                    worker 轮询(FOR UPDATE SKIP LOCKED)
                                                │
              PyMuPDF 提取 ──字符不足──▶ RapidOCR ──▶ 清洗 ──▶ 切分
                                                │
                                    bge-m3 批量编码(CPU, 归一化)
                                                ▼
                                    chunks(embedding vector(1024)) + HNSW 索引

提问 ──▶ /api/chat ──▶ bge-m3 编码 ──▶ pgvector top-50 ──▶ CrossEncoder 重排 top-8
      ──▶ 拼 Prompt(强制引用) ──▶ 企业 LLM 网关 ──▶ 答案 + sources[doc_id,page,snippet]
```

## 目录结构（新建项目，假设落在 `/workspace/enterprise-rag/`）

```
enterprise-rag/
├── pyproject.toml
├── .env.example
├── docker-compose.yml
├── Dockerfile
├── scripts/init_db.sql
├── eval/golden.jsonl
├── src/rag/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py
│   ├── chunking.py
│   ├── parse_pdf.py
│   ├── embed.py
│   ├── retrieve.py
│   ├── llm.py
│   ├── ingest_worker.py
│   └── api_main.py
├── tests/
│   ├── test_chunking.py
│   ├── test_parse_pdf.py
│   ├── test_api.py
│   └── conftest.py
└── src/rag/eval_run.py
```

## 数据契约（英文键 + 中文字段字典）

**documents** — 文档主表
| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigserial PK | 文档 ID |
| filename | text | 原始文件名 |
| sha256 | text unique | 内容哈希，防重复入库 |
| page_count | int | 页数 |
| status | text | queued/parsed/indexed/failed |
| error | text | 失败原因，成功为 NULL |
| created_at / updated_at | timestamptz | 时间戳 |

**chunks** — 切片表
| 字段 | 类型 | 说明 |
|---|---|---|
| id | bigserial PK | 切片 ID |
| document_id | bigint FK→documents | 所属文档 |
| seq | int | 文档内顺序号 |
| page | int | 起始页码（引用展示用） |
| content | text | 切片正文 |
| embedding | vector(1024) | 归一化后向量 |
| token_count | int | 字符数（中文近似 token） |

**ingest_jobs** — 摄取任务表（简易队列）
| 字段 | 说明 |
|---|---|
| id / document_id / status(queued/running/done/failed) / error / created_at / started_at / finished_at | 任务状态机 |

**qa_log** — 问答留痕（运维排查 + 后续做评测语料）
| 字段 | 说明 |
|---|---|
| id / question / answer / source_chunk_ids(bigint[]) / latency_ms / created_at | 一次问答一条 |

## Step-by-step 任务

> 通用约定：所有命令在 `/workspace/enterprise-rag/` 下执行；每完成一个任务 `git add -A && git commit -m "..."`。TDD 任务按「先写测试→跑红→最小实现→跑绿→提交」。

### T0 项目初始化（~5 min）

```bash
mkdir -p /workspace/enterprise-rag && cd /workspace/enterprise-rag
git init
uv init --name rag --python 3.11 .
```

把 `pyproject.toml` 内容替换为：

```toml
[project]
name = "rag"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "psycopg[binary,pool]>=3.2",
    "pgvector>=0.3.6",
    "pymupdf>=1.24",
    "rapidocr-onnxruntime>=1.3",
    "sentence-transformers>=3.2",
    "openai>=1.50",
    "pydantic-settings>=2.5",
]

[dependency-groups]
dev = ["pytest>=8.3", "httpx>=0.27"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/rag"]
```

```bash
uv sync            # 预期：创建 .venv 并安装依赖，末尾显示 N packages
uv run python -c "import fitz, pgvector, fastapi, openai; print('imports ok')"
# 预期输出：imports ok
```

`.env.example`：

```env
DATABASE_URL=postgresql://rag:rag@localhost:5432/rag
EMBEDDING_MODEL=BAAI/bge-m3
RERANK_MODEL=BAAI/bge-reranker-v2-m3
RERANK_ENABLED=true
LLM_BASE_URL=https://llm-gateway.internal/v1
LLM_API_KEY=change-me
LLM_MODEL=qwen2.5-72b-instruct
CHUNK_SIZE=500
CHUNK_OVERLAP=100
RETRIEVE_TOP_K=50
RERANK_TOP_N=8
```

### T1 数据库起服务 + 建表（~5 min）

`docker-compose.yml`：

```yaml
services:
  db:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: rag
      POSTGRES_PASSWORD: rag
      POSTGRES_DB: rag
    ports: ["5432:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./scripts/init_db.sql:/docker-entrypoint-initdb.d/init.sql
volumes:
  pgdata:
```

`scripts/init_db.sql`：

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id          bigserial PRIMARY KEY,
    filename    text NOT NULL,
    sha256      text NOT NULL UNIQUE,
    page_count  int,
    status      text NOT NULL DEFAULT 'queued',
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chunks (
    id           bigserial PRIMARY KEY,
    document_id  bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq          int NOT NULL,
    page         int NOT NULL,
    content      text NOT NULL,
    embedding    vector(1024) NOT NULL,
    token_count  int NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id);
-- 表约 500 行数据后建 HNSW 索引更快；试点规模直接建也无妨
CREATE INDEX IF NOT EXISTS idx_chunks_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

CREATE TABLE IF NOT EXISTS ingest_jobs (
    id          bigserial PRIMARY KEY,
    document_id bigint NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status      text NOT NULL DEFAULT 'queued',
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS qa_log (
    id               bigserial PRIMARY KEY,
    question         text NOT NULL,
    answer           text NOT NULL,
    source_chunk_ids bigint[] NOT NULL DEFAULT '{}',
    latency_ms       int,
    created_at       timestamptz NOT NULL DEFAULT now()
);
```

```bash
docker compose up -d
docker compose exec db psql -U rag -c "\dt"
# 预期：documents / chunks / ingest_jobs / qa_log 四张表
```

### T2 config + db 连接模块（~4 min，含测试）

`src/rag/config.py`：

```python
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://rag:rag@localhost:5432/rag"
    embedding_model: str = "BAAI/bge-m3"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_enabled: bool = True
    llm_base_url: str = "http://localhost:9997/v1"
    llm_api_key: str = "dummy"
    llm_model: str = "qwen2.5-72b-instruct"
    chunk_size: int = 500
    chunk_overlap: int = 100
    retrieve_top_k: int = 50
    rerank_top_n: int = 8


settings = Settings()
```

`src/rag/db.py`：

```python
import psycopg
from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

from rag.config import settings

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(settings.database_url, max_size=8, open=True)
        register_vector_on_all(_pool)
    return _pool


def register_vector_on_all(pool: ConnectionPool) -> None:
    def configure(conn: psycopg.Connection) -> None:
        register_vector(conn)

    pool.configure(configure=configure)
```

`src/rag/__init__.py`：空文件。

验证：

```bash
uv run python -c "from rag.db import get_pool; p=get_pool(); print(p.connectioninfo.dsn.split()[0])"
# 预期：postgres=<...> 形式输出且不报错（需要 T1 的库已起）
```

### T3 切分器 chunking.py（TDD，~8 min）

先写 `tests/test_chunking.py`：

```python
from rag.chunking import chunk_text


def test_short_text_single_chunk():
    assert chunk_text("短文本一句话。", size=500, overlap=100) == ["短文本一句话。"]


def test_paragraphs_packed_to_limit():
    paras = ["第一段落。" * 50] * 3  # 每段 250 字
    chunks = chunk_text("\n".join(paras), size=500, overlap=100)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) >= 2


def test_long_paragraph_hard_split_with_overlap():
    text = "字" * 1200
    chunks = chunk_text(text, size=500, overlap=100)
    assert all(len(c) <= 500 for c in chunks)
    assert chunks[0][-100:] == chunks[1][:100]  # 重叠区一致
    assert "".join(chunks) == text  # 无内容丢失


def test_no_empty_chunks():
    chunks = chunk_text("段落一。\n\n\n段落二。", size=500, overlap=100)
    assert all(c.strip() for c in chunks)
```

```bash
uv run pytest tests/test_chunking.py -x
# 预期：ModuleNotFoundError/ImportError —— 红
```

实现 `src/rag/chunking.py`：

```python
import re

_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def _split_long(text: str, size: int, overlap: int) -> list[str]:
    """超长段落按句子边界硬切，带 overlap，保证无丢失。"""
    sentences = [s for s in _SENT_SPLIT.split(text) if s]
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        while len(s) > size:  # 单句仍超长，按字符硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(s[:size])
            s = s[size - overlap:]
        if len(buf) + len(s) > size and buf:
            chunks.append(buf)
            buf = buf[-overlap:] if overlap else ""
        buf += s
    if buf:
        chunks.append(buf)
    return chunks


def chunk_text(text: str, size: int = 500, overlap: int = 100) -> list[str]:
    """段落优先打包切分。返回非空 chunk 列表。"""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_split_long(para, size, overlap))
            continue
        if len(buf) + len(para) + 1 > size and buf:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks
```

```bash
uv run pytest tests/test_chunking.py
# 预期：4 passed —— 绿；git commit -m "feat: chunking with paragraph packing and overlap"
```

### T4 PDF 解析 parse_pdf.py（TDD，~10 min）

先写 `tests/test_parse_pdf.py`，用 PyMuPDF 现场造一个文本型 PDF 当夹具：

```python
import fitz
import pytest

from rag.parse_pdf import extract_pages, is_scanned


@pytest.fixture
def text_pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 100), f"这是第{i + 1}页的测试文本内容，用于验证解析流程。" * 3)
    doc.save(path)
    return path


def test_extract_text_pdf(text_pdf):
    pages = extract_pages(text_pdf)
    assert len(pages) == 2
    assert all("测试文本内容" in p.text for p in pages)


def test_is_scanned_false_for_text_pdf(text_pdf):
    assert is_scanned(text_pdf) is False
```

```bash
uv run pytest tests/test_parse_pdf.py -x
# 预期：红（模块不存在）
```

实现 `src/rag/parse_pdf.py`：

```python
import logging
from dataclasses import dataclass
from pathlib import Path

import fitz

logger = logging.getLogger(__name__)

MIN_CHARS_PER_PAGE = 30  # 低于此字符数视为疑似扫描页
SCANNED_RATIO = 0.5     # 超过 50% 疑似扫描页 → 整本走 OCR


@dataclass
class Page:
    page_no: int  # 1-based
    text: str


def _ocr_pages(path: Path, page_nos: list[int]) -> dict[int, str]:
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    results: dict[int, str] = {}
    doc = fitz.open(path)
    for no in page_nos:
        pix = doc[no - 1].get_pixmap(dpi=200)
        lines, _ = ocr(pix.tobytes("png"))
        results[no] = "\n".join(line[1] for line in lines) if lines else ""
    return results


def _page_texts(path: Path) -> list[str]:
    with fitz.open(path) as doc:
        return [page.get_text("text") for page in doc]


def is_scanned(path: Path) -> bool:
    texts = _page_texts(path)
    thin = sum(1 for t in texts if len(t.strip()) < MIN_CHARS_PER_PAGE)
    return len(texts) > 0 and thin / len(texts) > SCANNED_RATIO


def extract_pages(path: Path) -> list[Page]:
    texts = _page_texts(path)
    if is_scanned(path):
        logger.info("scanned pdf detected, running OCR: %s", path)
        texts = list(_ocr_pages(path, range(1, len(texts) + 1)).values())
    return [Page(no=i + 1, text=t) for i, t in enumerate(texts)]
```

```bash
uv run pytest tests/test_parse_pdf.py
# 预期：2 passed —— 绿；commit
```

> 注：扫描件的端到端 OCR 质量验证放 T10 冒烟阶段，用一份真实扫描 PDF 人工抽查，不在单元测试覆盖（造真扫描件成本高，YAGNI）。

### T5 Embedding 模块 embed.py（~5 min）

`src/rag/embed.py`：

```python
from functools import lru_cache

from sentence_transformers import SentenceTransformer

from rag.config import settings


@lru_cache(maxsize=1)
def get_embedder() -> SentenceTransformer:
    return SentenceTransformer(settings.embedding_model, device="cpu")


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """返回 L2 归一化向量，配合 cosine 距离使用。"""
    if not texts:
        return []
    model = get_embedder()
    emb = model.encode(
        texts,
        batch_size=batch_size,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return emb.tolist()
```

验证（首次会下载约 2GB 模型，网络慢时提前说）：

```bash
uv run python -c "
from rag.embed import embed_texts
v = embed_texts(['这是一段中文测试'])
print(len(v), len(v[0]))
assert abs(sum(x*x for x in v[0]) - 1.0) < 1e-3  # 已归一化
print('embed ok')
"
# 预期：1 1024 / embed ok
```

### T6 入库 worker ingest_worker.py（TDD with fake embedder，~10 min）

先写 `tests/conftest.py`（连接测试库，清表隔离）：

```python
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://rag:rag@localhost:5432/rag")


@pytest.fixture(autouse=True)
def clean_db():
    from rag.db import get_pool

    with get_pool().connection() as conn:
        conn.execute("TRUNCATE qa_log, ingest_jobs, chunks, documents CASCADE")
    yield
```

先写 `tests/test_ingest.py`（用 monkeypatch 假 embedding，避免测试下载模型）：

```python
from pathlib import Path

import fitz
import pytest

from rag.ingest_worker import ingest_document


@pytest.fixture
def fake_embed(monkeypatch):
    dim = 8
    calls = []

    def fake_embed_texts(texts, batch_size=32):
        calls.extend(texts)
        return [[0.1] * dim for _ in texts]

    monkeypatch.setattr("rag.ingest_worker.embed_texts", fake_embed_texts)
    monkeypatch.setattr("rag.ingest_worker.EMBED_DIM", dim)
    return calls


def make_pdf(tmp_path, text="这是入库测试文档内容。" * 30) -> Path:
    p = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), text)
    doc.save(p)
    return p


def test_ingest_happy_path(tmp_path, fake_embed):
    pdf = make_pdf(tmp_path)
    doc_id = ingest_document(pdf, filename="doc.pdf")
    from rag.db import get_pool

    with get_pool().connection() as conn:
        row = conn.execute("SELECT status, page_count FROM documents WHERE id=%s", (doc_id,)).fetchone()
        n = conn.execute("SELECT count(*) FROM chunks WHERE document_id=%s", (doc_id,)).fetchone()[0]
    assert row[0] == "indexed"
    assert row[1] == 1
    assert n > 0
    assert all("入库测试文档内容" in t for t in fake_embed)


def test_ingest_idempotent_on_same_sha(tmp_path, fake_embed):
    pdf = make_pdf(tmp_path)
    id1 = ingest_document(pdf, filename="a.pdf")
    with pytest.raises(ValueError, match="duplicate"):
        ingest_document(pdf, filename="b.pdf")
    assert id1 > 0
```

```bash
uv run pytest tests/test_ingest.py -x
# 预期：红
```

实现 `src/rag/ingest_worker.py`：

```python
import hashlib
import logging
import time
from pathlib import Path

from rag.chunking import chunk_text
from rag.config import settings
from rag.db import get_pool
from rag.embed import embed_texts
from rag.parse_pdf import extract_pages

logger = logging.getLogger(__name__)
EMBED_DIM = 1024  # 与 bge-m3 一致；fake embedder 测试时 monkeypatch 为 8


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ingest_document(path: Path, filename: str) -> int:
    digest = sha256_of(path)
    with get_pool().connection() as conn:
        dup = conn.execute("SELECT id FROM documents WHERE sha256=%s", (digest,)).fetchone()
        if dup:
            raise ValueError(f"duplicate document sha256={digest}")
        row = conn.execute(
            "INSERT INTO documents (filename, sha256, status) VALUES (%s,%s,'parsed') RETURNING id",
            (filename, digest),
        ).fetchone()
        doc_id = row[0]

    pages = extract_pages(path)
    full_text = "\n".join(f"[page {p.page_no}]\n{p.text}" for p in pages)
    pieces = chunk_text(full_text, size=settings.chunk_size, overlap=settings.chunk_overlap)

    # 每个 chunk 反查所属页码（用其中的最后一个 page 标记）
    chunks: list[tuple[int, str]] = []
    for seq, piece in enumerate(pieces):
        page = max((int(tok) for tok in _page_marks(piece)), default=1)
        chunks.append((page, piece))

    vectors = embed_texts([c[1] for c in chunks])

    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE documents SET page_count=%s, updated_at=now() WHERE id=%s",
            (len(pages), doc_id),
        )
        for (page, content), vec in zip(chunks, vectors):
            conn.execute(
                "INSERT INTO chunks (document_id, seq, page, content, embedding, token_count)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (doc_id, page, page, content, vec, len(content)),
            )
    return doc_id


def _page_marks(text: str) -> list[str]:
    import re

    return re.findall(r"(?<=\[page )\d+(?=\])", text)


def run_worker_loop(poll_seconds: float = 2.0) -> None:  # pragma: no cover
    """常驻 worker：轮询 ingest_jobs，FOR UPDATE SKIP LOCKED 防多 worker 重复消费。"""
    from rag.db import get_pool

    while True:
        with get_pool().connection() as conn:
            job = conn.execute(
                """
                WITH next_job AS (
                    SELECT j.id FROM ingest_jobs j
                    WHERE j.status='queued'
                    ORDER BY j.id
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE ingest_jobs SET status='running', started_at=now()
                WHERE id IN (SELECT id FROM next_job)
                RETURNING id, document_id
                """
            ).fetchone()
        if job is None:
            time.sleep(poll_seconds)
            continue
        job_id, doc_id = job
        try:
            with get_pool().connection() as conn:
                filename, sha = conn.execute(
                    "SELECT filename, sha256 FROM documents WHERE id=%s", (doc_id,)
                ).fetchone()
            # 原件统一放在 data/raw/<sha256>.pdf，见 T8 上传接口
            ingest_document(Path("data/raw") / f"{sha}.pdf", filename)
            with get_pool().connection() as conn:
                conn.execute(
                    "UPDATE ingest_jobs SET status='done', finished_at=now() WHERE id=%s", (job_id,)
                )
                conn.execute("UPDATE documents SET status='indexed', updated_at=now() WHERE id=%s", (doc_id,))
        except Exception as exc:
            logger.exception("ingest job %s failed", job_id)
            with get_pool().connection() as conn:
                conn.execute(
                    "UPDATE ingest_jobs SET status='failed', error=%s, finished_at=now() WHERE id=%s",
                    (str(exc), job_id),
                )
                conn.execute(
                    "UPDATE documents SET status='failed', error=%s, updated_at=now() WHERE id=%s",
                    (str(exc), doc_id),
                )


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    run_worker_loop()
```

```bash
uv run pytest tests/test_ingest.py
# 预期：2 passed；commit -m "feat: ingest pipeline with pg-backed job queue"
```

### T7 检索 retrieve.py + LLM llm.py（~8 min）

`src/rag/retrieve.py`：

```python
from dataclasses import dataclass

from rag.config import settings
from rag.db import get_pool
from rag.embed import embed_texts


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    page: int
    content: str
    score: float  # cosine 相似度（越大越好）


def vector_search(question: str, top_k: int | None = None) -> list[Hit]:
    top_k = top_k or settings.retrieve_top_k
    qvec = embed_texts([question])[0]
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT c.id, c.document_id, c.page, c.content,
                   1 - (c.embedding <=> %s::vector) AS score
            FROM chunks c
            ORDER BY c.embedding <=> %s::vector
            LIMIT %s
            """,
            (qvec, qvec, top_k),
        ).fetchall()
    return [Hit(*row) for row in rows]


def rerank(question: str, hits: list[Hit]) -> list[Hit]:
    if not settings.rerank_enabled or not hits:
        return hits
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(settings.rerank_model, device="cpu", max_length=512)
    scores = model.compute_score([[question, h.content] for h in hits])
    ranked = sorted(zip(hits, scores), key=lambda x: x[1], reverse=True)
    return [h for h, _ in ranked[: settings.rerank_top_n]]


def retrieve(question: str) -> list[Hit]:
    return rerank(question, vector_search(question))
```

`src/rag/llm.py`：

```python
import time

from openai import OpenAI

from rag.config import settings

SYSTEM_PROMPT = (
    "你是企业内部知识库助手。仅依据下面提供的【参考资料】回答问题；"
    "如果资料不足以回答，明确回答“资料中未找到相关信息”。"
    "回答中引用资料处用 [编号] 标注，编号对应资料前的 [N] 标记。使用中文回答。"
)


def build_context(hits) -> str:
    parts = [f"[{i}] (文档{h.document_id} 第{h.page}页)\n{h.content}" for i, h in enumerate(hits, 1)]
    return "\n\n".join(parts)


def answer_question(question: str, hits) -> tuple[str, float]:
    client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    t0 = time.monotonic()
    resp = client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"【参考资料】\n{build_context(hits)}\n\n【问题】{question}"},
        ],
        temperature=0.1,
    )
    latency_ms = int((time.monotonic() - t0) * 1000)
    return resp.choices[0].message.content, latency_ms
```

> 注意：部分企业网关不支持 `temperature` 等参数，若 T11 冒烟报 400，把 `temperature=0.1` 删掉重试（记入 Risks）。

验证检索（需 T5 模型已下载、库里有数据）：

```bash
uv run python -c "
from rag.retrieve import vector_search
hits = vector_search('入库测试文档内容')
print(len(hits), hits[0].score if hits else None)
"
# 预期：>0 个命中，score 接近 1
```

### T8 API 层 api_main.py（TDD，~10 min）

先写 `tests/test_api.py`：

```python
from fastapi.testclient import TestClient

import fitz

from rag.api_main import app

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_upload_document(tmp_path, monkeypatch):
    p = tmp_path / "doc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "接口测试文档内容。" * 20)
    doc.save(p)

    monkeypatch.setattr("rag.api_main.ingest_document", lambda path, filename: 42)
    r = client.post("/api/documents", files={"file": ("doc.pdf", p.read_bytes(), "application/pdf")})
    assert r.status_code == 201
    assert r.json()["document_id"] == 42
```

```bash
uv run pytest tests/test_api.py -x
# 预期：红
```

实现 `src/rag/api_main.py`：

```python
import hashlib
import logging
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from rag.config import settings
from rag.db import get_pool
from rag.ingest_worker import ingest_document
from rag.llm import answer_question
from rag.retrieve import retrieve

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="enterprise-rag")
RAW_DIR = Path("data/raw")


class ChatRequest(BaseModel):
    question: str


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/documents", status_code=201)
def upload_document(file: UploadFile):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(415, "only pdf supported")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RAW_DIR / f"tmp-{file.filename}"
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    final = RAW_DIR / f"{digest}.pdf"
    if final.exists():
        tmp.unlink()
        raise HTTPException(409, "duplicate document")
    tmp.replace(final)
    with get_pool().connection() as conn:
        doc_id = conn.execute(
            "INSERT INTO documents (filename, sha256, status) VALUES (%s,%s,'queued') RETURNING id",
            (file.filename, digest),
        ).fetchone()[0]
        conn.execute("INSERT INTO ingest_jobs (document_id) VALUES (%s)", (doc_id,))
    return {"document_id": doc_id, "status": "queued"}


@app.get("/api/documents/{doc_id}")
def document_status(doc_id: int):
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, error, page_count, (SELECT count(*) FROM chunks WHERE document_id=%s)"
            " FROM documents WHERE id=%s",
            (doc_id, doc_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "document not found")
    return {"document_id": doc_id, "status": row[0], "error": row[1],
            "page_count": row[2], "chunk_count": row[3]}


@app.post("/api/chat")
def chat(req: ChatRequest):
    hits = retrieve(req.question)
    if not hits:
        return {"answer": "资料中未找到相关信息。", "sources": []}
    answer, latency_ms = answer_question(req.question, hits)
    sources = [{"document_id": h.document_id, "page": h.page,
                "snippet": h.content[:120]} for h in hits]
    with get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO qa_log (question, answer, source_chunk_ids, latency_ms)"
            " VALUES (%s,%s,%s,%s)",
            (req.question, answer, [h.chunk_id for h in hits], latency_ms),
        )
    return JSONResponse({"answer": answer, "sources": sources})
```

> 注：上传接口是「同步建 documents 记录 + 排队 job」，真正的解析由 T6 的 worker 异步做 —— 上面 `ingest_document` 的 import 仅用于测试 monkeypatch 复用；若测试通过后发现在生产路径未被调用，把该 import 移到测试文件（保持 DRY，不留死代码）。

```bash
uv run pytest tests/test_api.py
# 预期：2 passed；commit
```

### T9 评测脚本 eval_run.py + 黄金集（~8 min）

`eval/golden.jsonl`（先用 10~20 条，格式如下，问题写你知道答案在语料里的）：

```jsonl
{"question": "示例问题1", "expected_document": "doc1.pdf", "expected_page": 3}
{"question": "示例问题2", "expected_document": "doc2.pdf", "expected_page": 12}
```

`src/rag/eval_run.py`：

```python
"""检索质量评测：hit@5 / MRR。仅测检索，不调 LLM，CPU 上秒级跑完。"""
import json
from pathlib import Path

from rag.retrieve import vector_search


def run(golden_path: Path = Path("eval/golden.jsonl")) -> dict:
    rows = [json.loads(line) for line in golden_path.read_text().splitlines() if line.strip()]
    hits_at_5, ranks = [], []
    for row in rows:
        hits = vector_search(row["question"], top_k=5)
        rank = next(
            (i + 1 for i, h in enumerate(hits)
             if h.document_id and _doc_name(h.document_id) == row["expected_document"]),
            None,
        )
        hits_at_5.append(rank is not None)
        ranks.append(1.0 / rank if rank else 0.0)
    n = len(rows)
    return {"n": n, "hit@5": sum(hits_at_5) / n, "mrr": sum(ranks) / n}


def _doc_name(document_id: int) -> str:
    from rag.db import get_pool

    with get_pool().connection() as conn:
        return conn.execute("SELECT filename FROM documents WHERE id=%s", (document_id,)).fetchone()[0]


if __name__ == "__main__":
    print(run())
```

```bash
uv run python -m rag.eval_run
# 预期：{'n': 10, 'hit@5': 0.9, 'mrr': 0.8} 之类。验收线：hit@5 >= 0.7，低于则调 chunk_size/换 bge-small-zh-v1.5 对照
```

### T10 打包 Dockerfile + 全栈 compose（~5 min）

`Dockerfile`：

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install uv && uv sync --frozen --no-dev
COPY src ./src
COPY scripts ./scripts
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "rag.api_main:app", "--host", "0.0.0.0", "--port", "8000"]
```

在 `docker-compose.yml` 追加（worker 与 api 共用镜像）：

```yaml
  worker:
    build: .
    command: python -m rag.ingest_worker
    env_file: .env
    environment:
      DATABASE_URL: postgresql://rag:rag@db:5432/rag
    volumes:
      - ./data:/app/data
    depends_on:
      - db

  api:
    build: .
    env_file: .env
    environment:
      DATABASE_URL: postgresql://rag:rag@db:5432/rag
    ports: ["8000:8000"]
    volumes:
      - ./data:/app/data
    depends_on:
      - db
```

### T11 端到端冒烟（~10 min）

```bash
cp .env.example .env   # 填入真实 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
docker compose up -d --build
docker compose logs worker --tail=5   # 预期：worker 启动无报错

# 1. 上传一份真实 PDF
curl -s -F "file=@/path/to/sample.pdf" http://localhost:8000/api/documents
# 预期：{"document_id":1,"status":"queued"}

# 2. 轮询状态直到 indexed（CPU 上 bge-m3 编码较慢，小文件 1~2 分钟）
curl -s http://localhost:8000/api/documents/1
# 预期：{"document_id":1,"status":"indexed","chunk_count":N,...}

# 3. 问答
curl -s -X POST http://localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"question":"<语料中确定存在答案的问题>"}'
# 预期：{"answer":"...[1]...","sources":[{"document_id":1,"page":N,"snippet":"..."}]}

# 4. OCR 链路：上传一份真实扫描件，确认 status 变 indexed 且 chunk_count>0
```

四步全绿即 MVP 完成，`git commit -m "feat: end-to-end smoke passes"`。

## Tests / validation 汇总

| 层 | 测试 | 命令 | 通过标准 |
|---|---|---|---|
| 切分 | 单元 | `uv run pytest tests/test_chunking.py` | 4 passed，无内容丢失 |
| 解析 | 单元（自造 PDF） | `uv run pytest tests/test_parse_pdf.py` | 2 passed |
| 入库 | 集成（fake embedder） | `uv run pytest tests/test_ingest.py` | 2 passed，status=indexed |
| API | 集成 | `uv run pytest tests/test_api.py` | 2 passed |
| 检索质量 | 评测 | `uv run python -m rag.eval_run` | hit@5 ≥ 0.7 |
| 端到端 | 冒烟 | T11 四个 curl | 全部返回预期结构 |

## Risks / 权衡 / 待确认问题

1. **CPU 上 bge-m3 编码吞吐低**（约 20~50 chunk/s）：1 万份 PDF 首次入库可能要跑数小时。缓解：worker 本就异步、可并行跑 2~4 个实例（job 表已用 SKIP LOCKED）；若不可接受，切换 `BAAI/bge-small-zh-v1.5`（512 维，需同步改 `EMBED_DIM` 与 `init_db.sql` 的 `vector(1024)`，并用 eval 脚本对比 hit@5）。
2. **rerank 在 CPU 上每次问答多 1~2s**：已做 `RERANK_ENABLED` 开关；试点并发低可接受。
3. **扫描件 OCR 质量**：RapidOCR 对盖章/表格/竖排文本较弱。表格密集的文档一期刊不解析表格结构（chunk 里是线性化文字），二期可上 MinerU/表格识别。
4. **企业 LLM 网关兼容性**：个别网关不支持 `temperature` 或流式参数；T11 冒烟如遇 400 按注释降级。
5. **chunk 反查页码用正则启发式**（`[page N]` 标记）：跨页 chunk 页码取最后出现标记，引用定位误差 ≤1 页，一期可接受。
6. **待确认**：企业网关的具体 model 名称与上下文窗口长度（影响 CHUNK_SIZE 与 context 拼装上限）；PDF 是否有加密件（一期直接 failed 报错，不破解）。
7. **二期预留（一期不做，YAGNI）**：文档级 ACL（chunks/documents 加 tenant/permission 列 + 检索 SQL 过滤）、Web UI、MinerU 表格解析、增量更新/删除重索引。
