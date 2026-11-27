"""重建脚本跑通 + "sparse 可从 PG 重建"（详细设计.md §3.6 / §4.2 / 附录 B）。

验收原句有两条要在这里落地：

- **Qdrant point 同时含 dense+bm25** → `test_vectors.py` 已逐点断言；
- **重建脚本跑通** → 这里跑真脚本（子进程），把集合清空后从 PG 全量重建，再断言
  两边逐条一致。

为什么必须真跑脚本而不是在测试里调它的函数：重建脚本是**独立入口**（运维直接调），
它有自己的 DSN/URL 解析、参数与退出码。只测函数会漏掉"脚本启动就崩"这类问题——
而那正是运维最先遇到的。

两条硬要求：
1. **幂等**：跑两次不产生重复点（point id = uuid5(chunk_id)）；
2. **可全量重建**：集合整个删掉后能从 PG 重建回来，且 payload 与向量质量
   逐条一致。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from api.ingest.flip import flip
from api.ingest.vectors import index_version
from api.tests.a3.conftest import (  # 只 import 辅助函数；fixtures 自动发现
    DENSE_DIM,
    seed_document,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
REBUILD = REPO_ROOT / "scripts" / "qdrant_rebuild.py"


@pytest.fixture
def rebuild(qdrant_reachable, embedder, collection):
    """跑一次真重建脚本，返回 (解析后的 JSON 摘要, CompletedProcess)。

    URL/模型/集合**全部显式传入**，不读环境变量：`api/tests/conftest.py`
    的 autouse `_clean_env` 会清掉 Settings 的字段名，脚本里 `os.getenv`
    兜底会静默落到默认值（`http://127.0.0.1:6333` + `kb_chunks`）——那会让
    测试去碰真实集合。
    """

    def _run(*extra: str):
        args = [
            sys.executable,
            str(REBUILD),
            "--dsn",
            _plain_dsn(),
            "--qdrant-url",
            qdrant_reachable,
            "--collection",
            collection,
            "--embedding-url",
            embedder.base_url,
            "--embedding-model",
            embedder.model,
            "--dim",
            str(DENSE_DIM),
            "--json",
            *extra,
        ]
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
        }
        env["PYTHONPATH"] = str(REPO_ROOT)
        proc = subprocess.run(  # noqa: S603 - 固定脚本路径
            args,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            check=False,
            timeout=240,
        )
        summary = None
        if proc.returncode == 0:
            last = proc.stdout.strip().splitlines()[-1]
            summary = json.loads(last)
        return summary, proc

    return _run


def _points_by_chunk(store) -> dict[str, dict]:
    return {p["payload"]["chunk_id"]: p["payload"] for p in _scroll_all(store)}


def _scroll_all(store) -> list[dict]:
    out: list[dict] = []
    offset = None
    while True:
        body: dict = {"limit": 256, "with_payload": True, "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        resp = store._call(
            "POST", f"/collections/{store.collection}/points/scroll", body
        )
        result = resp["result"]
        out.extend(result["points"])
        offset = result.get("next_page_offset")
        if not offset or not result["points"]:
            return out


def _vectors_by_chunk(store) -> dict[str, dict]:
    from scripts.qdrant_common import DENSE_VECTOR, SPARSE_VECTOR

    out: dict[str, dict] = {}
    for point in _scroll_all(store):
        resp = store._call(
            "POST",
            f"/collections/{store.collection}/points",
            {"ids": [point["id"]], "with_vector": True, "with_payload": True},
        )
        for full in resp["result"]:
            vectors = full["vector"]
            out[full["payload"]["chunk_id"]] = {
                "dense": vectors[DENSE_VECTOR],
                "bm25_tokens": len(vectors[SPARSE_VECTOR]["indices"]),
            }
    return out


@pytest.fixture
def active_version(conn, store, embedder, pool) -> str:
    """一个已上线、已同步完的版本。

    重建按 `v_active_chunks` 取数，只认双 active。翻牌后要 drain 一次：点的
    payload 状态在翻牌那一刻还是 pending（那是切片写进来时的状态），drain
    之后才变成 active。不 drain 就重建，会得到“payload 状态跟原来不一样”的
    假漂移——那验证的是时序而不是重建。
    """
    from api.ingest.sync import drain

    version_id = seed_document(conn, key="rebuild", version_tag="v1")
    conn.commit()
    index_version(conn, store, embedder, version_id)
    flip(conn, version_id)
    conn.commit()
    drain(pool, store, max_attempts=4, backoff_s=0.0)
    return version_id


def test_rebuild_dry_run_reports_the_plan(
    conn, store, embedder, active_version, rebuild
) -> None:
    summary, proc = rebuild("--dry-run")
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert summary["dry_run"] is True
    assert summary["total"] == 2
    assert summary["rows"] == {"text": 2}
    # dry-run 不写：集合里还是前面 index_version 写进去的 2 个点
    assert store.collection_points() == 2


def test_rebuild_restores_everything_after_the_collection_is_wiped(
    conn, store, embedder, active_version, rebuild, qdrant_reachable, collection
) -> None:
    """核心验收：把集合清空，从 PG 全量重建，两边逐条一致。

    重建前先记下每个点的 payload 与"bm25 是否真的有 token"，重建后逐条比对——
    只比条数会把"点回来了但向量空了"这种半成品当成成功。
    """
    from scripts import qdrant_common as qc

    before_payloads = _points_by_chunk(store)
    assert len(before_payloads) == 2
    before_vectors = _vectors_by_chunk(store)
    assert all(v["dense"] for v in before_vectors.values())
    assert all(v["bm25_tokens"] > 0 for v in before_vectors.values())

    # 物理清空集合（等价于"向量库整个丢了"）
    qc.delete_collection(qdrant_reachable, collection)
    assert qc.collection_info(qdrant_reachable, collection) is None

    summary, proc = rebuild("--ensure-collection")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert summary["upserted"] == 2
    assert summary["collection_points"] == 2

    after_payloads = _points_by_chunk(store)
    assert set(after_payloads) == set(before_payloads)
    for chunk_id, before in before_payloads.items():
        assert (
            after_payloads[chunk_id] == before
        ), f"payload drifted for {chunk_id}"

    after_vectors = _vectors_by_chunk(store)
    for chunk_id, before in before_vectors.items():
        after = after_vectors[chunk_id]
        assert after["bm25_tokens"] == before["bm25_tokens"], (
            f"bm25 tokens drifted for {chunk_id}: "
            f"{before['bm25_tokens']} -> {after['bm25_tokens']}"
        )
        assert len(after["dense"]) == DENSE_DIM
        # 同一段正文编码两次：TEI 是确定性的，数值应完全一致
        assert after["dense"] == pytest.approx(before["dense"], abs=1e-6)


def test_rebuild_is_idempotent(
    conn, store, embedder, active_version, rebuild
) -> None:
    """跑两次：点数不变（point id 确定性，重放覆盖不重复）。"""
    first, proc1 = rebuild()
    assert proc1.returncode == 0, proc1.stderr[-1500:]
    second, proc2 = rebuild()
    assert proc2.returncode == 0, proc2.stderr[-1500:]
    assert first["upserted"] == second["upserted"] == 2
    assert store.collection_points() == 2


def test_rebuild_only_sees_the_active_version(
    conn, store, embedder, rebuild, qdrant_reachable, collection
) -> None:
    """重建按 `v_active_chunks` 取数：只有双 active 的切片会被重建。

    旧版（deprecated）的切片不在取数范围里——它们的点留在库里（回收窗口内不可检索）
    ，
    但重建不会把它们写回来。这是
    §4.2「以库为准」的正确含义：重建恢复的是**可检索
    集**，不是历史点。
    """
    old = seed_document(
        conn, key="rebuildonly", version_tag="old", version_status="active"
    )
    conn.execute(
        "UPDATE chunks SET status = 'active' WHERE version_id = %s", (old,)
    )
    conn.commit()
    index_version(conn, store, embedder, old)
    new = seed_document(conn, key="rebuildonly", version_tag="new")
    conn.commit()
    index_version(conn, store, embedder, new)
    flip(conn, new)
    conn.commit()

    from scripts import qdrant_common as qc

    qc.delete_collection(qdrant_reachable, collection)
    summary, proc = rebuild("--ensure-collection")
    assert proc.returncode == 0, proc.stderr[-2000:]

    # 新版 2 条 active；旧版 2 条已 deprecated → 不在重建范围
    assert summary["upserted"] == 2
    payloads = _points_by_chunk(store)
    assert all(p["version_id"] == new for p in payloads.values())
    assert all(p["status"] == "active" for p in payloads.values())


def test_rebuild_all_status_includes_pending(
    conn, store, embedder, rebuild, qdrant_reachable, collection
) -> None:
    """`--all-status` 能重建待转正的点（给"先索引再翻牌"的流程用）。"""
    version_id = seed_document(conn, key="allstatus", version_tag="v1")
    conn.commit()
    from scripts import qdrant_common as qc

    qc.delete_collection(qdrant_reachable, collection)
    summary, proc = rebuild("--ensure-collection", "--all-status")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert summary["upserted"] == 2
    payloads = _points_by_chunk(store)
    assert all(p["status"] == "pending" for p in payloads.values())
    assert {p["version_id"] for p in payloads.values()} == {version_id}


def _plain_dsn() -> str:
    """脚本吃纯 psycopg DSN（`postgresql://`），不吃 SQLAlchemy 前缀。"""
    raw = (
        os.getenv("AI_RAG_TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""
    )
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw
