"""语义切分：目标 500 字、重叠 50；表格整块保留，每片带页码。."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from typing import Any

TARGET_CHARS = 500
OVERLAP_CHARS = 50
_BREAKS = "。！？；.!?;\n"


class TeiEmbeddings:
    """LangChain SemanticChunker 的 TEI 适配器（只用标准库发 HTTP）。."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        api_key: str = "",
        timeout_s: float = 30.0,
    ):
        """Initialize the instance."""
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim
        self.api_key = api_key
        self.timeout_s = timeout_s

    def embed_documents(self, texts):
        """Run embed_documents."""
        body = json.dumps({"input": list(texts), "model": self.model}).encode(
            "utf-8"
        )
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.base_url + "/embeddings",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout_s
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = sorted(
            payload.get("data") or [], key=lambda row: row.get("index", 0)
        )
        vectors = [list(map(float, row["embedding"])) for row in rows]
        if len(vectors) != len(texts) or any(
            len(vector) != self.dim for vector in vectors
        ):
            raise ValueError("TEI embedding 返回数量或维度不匹配")
        return vectors

    def embed_query(self, text):
        """Run embed_query."""
        return self.embed_documents([text])[0]


class LocalEmbeddings:
    """没有 TEI 时用项目同款本地模型，避免额外云费用。."""

    def __init__(self, model: str):
        """Initialize the instance."""
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model)

    def embed_documents(self, texts):
        """Run embed_documents."""
        return self.model.encode(
            list(texts), normalize_embeddings=True
        ).tolist()

    def embed_query(self, text):
        """Run embed_query."""
        return self.embed_documents([text])[0]


def make_semantic_chunker():
    """默认 percentile=95；阈值可用 CHUNK_BREAKPOINT_PERCENTILE 覆盖。."""
    from langchain_experimental.text_splitter import SemanticChunker

    base_url = os.environ.get("EMBEDDING_BASE_URL", "").strip()
    if base_url:
        embeddings = TeiEmbeddings(
            base_url=base_url,
            model=os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"),
            dim=int(os.environ.get("EMBEDDING_DIM", "512")),
            api_key=os.environ.get("EMBEDDING_API_KEY", ""),
            timeout_s=float(os.environ.get("EMBEDDING_TIMEOUT_S", "30")),
        )
    else:
        embeddings = LocalEmbeddings(
            os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
        )
    percentile = float(os.environ.get("CHUNK_BREAKPOINT_PERCENTILE", "95"))
    return SemanticChunker(
        embeddings=embeddings,
        buffer_size=1,
        breakpoint_threshold_type="percentile",
        breakpoint_threshold_amount=percentile,
        min_chunk_size=400,
        sentence_split_regex=r"(?<=[。！？；.!?])\s*",
    )


def semantic_chunks(
    text: str, *, target: int = TARGET_CHARS, overlap: int = OVERLAP_CHARS
) -> list[str]:
    """Run semantic_chunks."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= target:
        return [text]

    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + target, length)
        if end < length:
            window = text[start:end]
            best = -1
            for marker in _BREAKS:
                position = window.rfind(marker)
                if position > best:
                    best = position
            # 只在后半段断句，避免切出过短的前片。
            if best >= target // 2:
                end = start + best + 1
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= length:
            break
        start = max(start + 1, end - overlap)
    return pieces


def chunk_blocks(
    blocks: list[dict[str, Any]],
    version_id: str,
    *,
    target: int = TARGET_CHARS,
    overlap: int = OVERLAP_CHARS,
    splitter: Any | None = None,
) -> list[dict[str, Any]]:
    """Run chunk_blocks."""
    splitter = splitter or make_semantic_chunker()
    chunks: list[dict[str, Any]] = []
    headings: list[tuple[int, str]] = []

    for block in blocks:
        if block.get("type") in {"footer", "page_number", "page_footnote"}:
            continue
        level = block.get("heading_level")
        text = str(block.get("text") or "").strip()
        if len(text) <= 1:
            continue
        if level is not None and text:
            headings = [item for item in headings if item[0] < level] + [
                (level, text)
            ]
        heading_path = " > ".join(item[1] for item in headings)
        page_num = int(block.get("page_idx") or 0) + 1
        block_id = block.get("block_id")
        source_ids = [block_id] if block_id else []

        if block.get("type") == "table":
            if not text:
                continue
            chunks.append(
                _make_chunk(
                    text,
                    page_num,
                    "table",
                    heading_path,
                    source_ids,
                    version_id,
                    len(chunks),
                )
            )
            continue

        for semantic_piece in splitter.split_text(text):
            for piece in semantic_chunks(
                semantic_piece, target=target, overlap=overlap
            ):
                chunks.append(
                    _make_chunk(
                        piece,
                        page_num,
                        str(block.get("type") or "text"),
                        heading_path,
                        source_ids,
                        version_id,
                        len(chunks),
                    )
                )

    return chunks


def _make_chunk(
    text: str,
    page_num: int,
    chunk_type: str,
    heading_path: str,
    source_ids: list[str],
    version_id: str,
    index: int,
) -> dict[str, Any]:
    """Run _make_chunk."""
    digest = hashlib.sha256(
        f"{version_id}|{page_num}|{chunk_type}|{index}|{text[:80]}".encode()
    ).hexdigest()[:12]
    return {
        "chunk_id": f"ch_{digest}",
        "text": text,
        "page_num": page_num,
        "chunk_type": chunk_type,
        "char_count": len(text),
        "source_block_ids": source_ids,
        "heading_path": heading_path,
    }


__all__ = ["TARGET_CHARS", "OVERLAP_CHARS", "semantic_chunks", "chunk_blocks"]
