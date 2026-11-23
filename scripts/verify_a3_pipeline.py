#!/usr/bin/env python3
"""A3 端到端演练：用真 CLI + 真 PG + 真 Qdrant + 真 TEI 跑完一条完整链路。

不 mock 任何东西，每一步都调**运维会真正敲的命令**（scripts/a3_ingest.py 的子命令），
并把输出原样贴出来。这条脚本的存在意义是：验收"重建脚本跑通"和"同步失败可对账修复"
时，不必靠"我看过测试"这种口头保证。

演练的 8 步（对应 §4 的每个动作）：

  1. 造数据：一个文档两个版本（v1 待上线、v2 之后改版）+ 每版 3 条切片
  2. promote v1      -> index + flip + drain + reconcile
  3. status          -> 检查 version/chunk/sync/drift 全绿
  4. 造新的 pending  v2（模拟"文件改了字节"）
  5. promote v2      -> v1 应立刻 deprecated、v2 active
  6. 破坏同步（手工把一条 payload 改错） -> reconcile --dry-run 必须看见，reconcile 修好
  7. 把 v1 的下线时间推到 25h 前 -> reclaim 删切片/点/图，再 status
  8. rebuild 全量重建（集合清空后从 PG 重建）-> 逐条比对

用法（宿主或容器内都行；宿主需 psycopg/httpx）：

    DATABASE_URL=... QDRANT_URL=... EMBEDDING_BASE_URL=... \
    python scripts/verify_a3_pipeline.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from api.clients.pg import normalize_dsn  # noqa: E402

PROXY_VARS = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
for _var in PROXY_VARS:
    os.environ.pop(_var, None)

NS = "a3e2e"
DOC = f"{NS}/travel"
V1 = f"{DOC}#v1aaaa"
V2 = f"{DOC}#v2bbbb"
CHUNK_PREFIX = "ch_a3e2e"

DSN = normalize_dsn(
    os.getenv("DATABASE_URL")
    or "postgresql://ai_rag:ai_rag_dev_password@127.0.0.1:5432/ai_rag"
)
QDRANT = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
EMBEDDING = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:7997/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
DIM = os.getenv("EMBEDDING_DIM", "512")

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def cli(*args: str, expect_rc: int = 0) -> dict:
    """调真 CLI（子进程），返回解析后的 JSON。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    for var in PROXY_VARS:
        env.pop(var, None)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "a3_ingest.py"),
        *args,
        "--dsn", DSN,
        "--qdrant-url", QDRANT,
        "--embedding-url", EMBEDDING,
        "--embedding-model", EMBEDDING_MODEL,
        "--dim", DIM,
        "--json",
    ]
    proc = subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=300, check=False
    )
    print(f"\n$ a3_ingest.py {' '.join(args)}")
    if proc.stdout.strip():
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    else:
        payload = {}
    if proc.returncode != expect_rc:
        print(proc.stdout[-2000:])
        print(proc.stderr[-3000:], file=sys.stderr)
        raise SystemExit(
            f"CLI {args!r} returned {proc.returncode}, expected {expect_rc}\n"
            f"stderr tail:\n{proc.stderr[-1500:]}"
        )
    return payload


def sql(query: str, params: tuple = ()) -> list[dict]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
    return rows


def seed(key_suffix: str, version_id: str, status: str, chunks: list[tuple[str, str]]) -> None:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (doc_key, filename, status) VALUES (%s, %s, 'queued') "
            "ON CONFLICT (doc_key) DO NOTHING",
            (DOC, "travel.pdf"),
        )
        cur.execute(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status) "
            "VALUES (%s, %s, %s, %s)",
            (version_id, DOC, (key_suffix * 64)[:64], status),
        )
        for chunk_id, text in chunks:
            cur.execute(
                """
                INSERT INTO chunks (chunk_id, doc_key, version_id, chunk_type, page_num,
                                    bbox, content_text, effective_date, effective_year,
                                    date_confidence, is_public, status)
                VALUES (%s, %s, %s, 'text', 1, '[[0.1,0.1,0.9,0.2]]'::jsonb, %s,
                        20260301, 2026, 'explicit', true, 'pending')
                """,
                (chunk_id, DOC, version_id, text),
            )
        conn.commit()


def cleanup() -> None:
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for table in ("vector_drift_log", "vector_sync_tasks", "chunks", "doc_versions",
                      "ingest_jobs", "documents"):
            column = "doc_key" if table != "qa_log" else "user_id"
            cur.execute(f"DELETE FROM {table} WHERE {column} LIKE %s", (f"{NS}/%",))
        conn.commit()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute("SELECT chunk_id FROM chunks WHERE chunk_id LIKE %s", (f"{CHUNK_PREFIX}%",))
    # 集合里的点：按 doc_key 删（清理残留）

    import httpx

    with httpx.Client(base_url=QDRANT, timeout=10, trust_env=False) as client:
        client.post(
            "/collections/kb_chunks/points/delete?wait=true",
            json={"filter": {"must": [{"key": "doc_key", "match": {"value": DOC}}]}},
        )


def qdrant_points() -> dict[str, dict]:
    import httpx

    out: dict[str, dict] = {}
    offset = None
    with httpx.Client(base_url=QDRANT, timeout=15, trust_env=False) as client:
        while True:
            body = {"limit": 256, "with_payload": True, "with_vector": False,
                    "filter": {"must": [{"key": "doc_key", "match": {"value": DOC}}]}}
            if offset:
                body["offset"] = offset
            resp = client.post("/collections/kb_chunks/points/scroll", json=body)
            resp.raise_for_status()
            result = resp.json()["result"]
            for point in result["points"]:
                out[point["payload"]["chunk_id"]] = {
                    "status": point["payload"]["status"],
                    "version_id": point["payload"]["version_id"],
                    "id": point["id"],
                }
            offset = result.get("next_page_offset")
            if not offset or not result["points"]:
                return out


def set_payload_wrong(chunk_id: str, status: str) -> None:
    """手工把一条 payload 改错（模拟同步漏掉一条）。"""
    import httpx

    from scripts.qdrant_common import point_id_for

    with httpx.Client(base_url=QDRANT, timeout=10, trust_env=False) as client:
        resp = client.post(
            "/collections/kb_chunks/points/payload?wait=true",
            json={"payload": {"status": status}, "points": [point_id_for(chunk_id)]},
        )
        resp.raise_for_status()


def main() -> int:
    figures = Path(tempfile.mkdtemp(prefix="a3figs-"))
    print("=" * 78)
    print("A3 端到端演练（真 CLI / 真 PG / 真 Qdrant / 真 TEI）")
    print(f"doc_key={DOC}  figures={figures}")
    print("=" * 78)

    try:
        cleanup()

        # --- 1. 造数据 --------------------------------------------------------
        print("\n[1] 造一个待上线版本 v1（3 条切片）")
        seed("1", V1, "pending", [
            (f"{CHUNK_PREFIX}_1", "住宿费标准：总监级每人每天 800 元，经理级 600 元。"),
            (f"{CHUNK_PREFIX}_2", "市内交通费凭票报销，单次不超过 100 元。"),
            (f"{CHUNK_PREFIX}_3", "出差需提前 3 个工作日在系统内提交申请。"),
        ])
        (figures / f"{CHUNK_PREFIX}_1.webp").write_bytes(b"RIFF....WEBP")
        rows = sql("SELECT count(*) AS n FROM chunks WHERE doc_key = %s", (DOC,))
        check("v1 有 3 条切片", rows[0]["n"] == 3, f"chunks={rows[0]['n']}")

        # --- 2. promote v1 ----------------------------------------------------
        print("\n[2] promote v1：索引 -> 翻牌 -> 同步 -> 对账")
        out = cli("promote", V1)
        check("promote 成功", out.get("ok") is True, str(out.get("error")))
        check("写进 3 个点", out["index"]["points"] == 3, str(out["index"]))
        check("翻牌激活 3 条切片", out["flip"]["activated_chunks"] == 3, str(out["flip"]))
        check("同步全部完成", out["drain"]["done"] >= 1 and out["drain"]["failed"] == 0,
              str(out["drain"]))
        check("对账无残留", out["reconcile"]["remaining"] == 0,
              str(out["reconcile"]["drift_counts"]))

        # --- 3. status --------------------------------------------------------
        print("\n[3] status：看现状")
        st = cli("status", "--doc-key", DOC)
        check("版本 1 个 active", st["versions"] == {"active": 1}, str(st["versions"]))
        check("切片 3 个 active", st["chunks"] == {"active": 3}, str(st["chunks"]))
        check("无双 active", st["double_active"] == [], str(st["double_active"]))
        check("无待同步", st["vector_sync_pending"] == 0, str(st["vector_sync_pending"]))
        check("无未修漂移", st["open_drift"] == {}, str(st["open_drift"]))
        check("集合里 3 个点", st["qdrant_points"] >= 3, f"points={st['qdrant_points']}")

        # --- 4. 新版 v2 -------------------------------------------------------
        print("\n[4] 造 v2（模拟文件字节变了）：v1 仍应继续服务")
        seed("2", V2, "pending", [
            (f"{CHUNK_PREFIX}_4", "住宿费标准：总监级每人每天 900 元，经理级 700 元。"),
            (f"{CHUNK_PREFIX}_5", "市内交通费凭票报销，单次不超过 120 元。"),
            (f"{CHUNK_PREFIX}_6", "出差需提前 2 个工作日在系统内提交申请。"),
        ])
        before = qdrant_points()
        check("v1 的 3 个点仍存在", len([p for p in before.values() if p["version_id"] == V1]) == 3)
        check("v2 还没写点（未 promote）",
              not [p for p in before.values() if p["version_id"] == V2])

        # --- 5. promote v2 ----------------------------------------------------
        print("\n[5] promote v2：v1 应立刻下线，v2 生效")
        out2 = cli("promote", V2)
        check("promote v2 成功", out2.get("ok") is True, str(out2.get("error")))
        check("旧版是 v1", out2["flip"]["previous_active"] == V1, str(out2["flip"]))
        after = qdrant_points()
        v1_states = {p["status"] for p in after.values() if p["version_id"] == V1}
        v2_states = {p["status"] for p in after.values() if p["version_id"] == V2}
        check("v1 的点全部 deprecated", v1_states == {"deprecated"}, str(v1_states))
        check("v2 的点全部 active", v2_states == {"active"}, str(v2_states))

        st = cli("status", "--doc-key", DOC)
        check("版本 1 active + 1 deprecated",
              st["versions"] == {"active": 1, "deprecated": 1}, str(st["versions"]))
        check("切片 3 active + 3 deprecated",
              st["chunks"] == {"active": 3, "deprecated": 3}, str(st["chunks"]))
        check("仍然无双 active", st["double_active"] == [], str(st["double_active"]))

        # --- 6. 破坏同步 -> 对账修复 -------------------------------------------
        print("\n[6] 手工把一条 payload 改错，验证对账能看见并修好")
        set_payload_wrong(f"{CHUNK_PREFIX}_4", "deprecated")
        dr = cli("reconcile", "--doc-key", DOC, "--dry-run", expect_rc=1)
        check("dry-run 看见这处漂移",
              dr["drift_counts"] == {"status_mismatch": 1}, str(dr["drift_counts"]))
        check("dry-run 没修（仍是 1 处）", dr["remaining"] == 1, str(dr["remaining"]))

        fixed = cli("reconcile", "--doc-key", DOC)
        check("对账修好了", fixed["remaining"] == 0, str(fixed["drift_counts"]))
        check("修好后再对账是干净的",
              cli("reconcile", "--doc-key", DOC)["remaining"] == 0)

        # --- 7. 回收 ----------------------------------------------------------
        print("\n[7] 把 v1 的下线时间推到 25 小时前，跑回收")
        sql("UPDATE doc_versions SET deprecated_at = now() - interval '25 hours' "
            "WHERE version_id = %s", (V1,))
        rec = cli("reclaim", "--doc-key", DOC, "--retention-hours", "24",
                  "--figures-dir", str(figures))
        check("回收了 1 个版本", rec["purged"] == 1, str(rec["purged"]))
        check("删了 3 条切片", rec["chunks"] == 3, str(rec["chunks"]))
        check("删了 1 张图", rec["figures"] == 1, str(rec["figures"]))
        check("图文件真的不在了", not (figures / f"{CHUNK_PREFIX}_1.webp").exists())
        remaining = qdrant_points()
        check("v1 的点已从 Qdrant 删除",
              not [p for p in remaining.values() if p["version_id"] == V1],
              str({p["version_id"] for p in remaining.values()}))
        check("v2 的点还在",
              len([p for p in remaining.values() if p["version_id"] == V2]) == 3)
        st = cli("status", "--doc-key", DOC)
        check("版本状态 = active + purged",
              st["versions"] == {"active": 1, "purged": 1}, str(st["versions"]))

        # --- 8. 全量重建 ------------------------------------------------------
        print("\n[8] 集合清空后从 PG 全量重建，逐条比对")
        import httpx

        before_rebuild = qdrant_points()
        with httpx.Client(base_url=QDRANT, timeout=15, trust_env=False) as client:
            client.delete("/collections/kb_chunks")
            client.put(
                "/collections/kb_chunks",
                json={
                    "vectors": {"dense": {"size": int(DIM), "distance": "Cosine"}},
                    "sparse_vectors": {"bm25": {"modifier": "idf"}},
                    "on_disk_payload": True,
                },
            )
        print("  集合已清空，准备重建")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        for var in PROXY_VARS:
            env.pop(var, None)
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable, str(REPO_ROOT / "scripts" / "qdrant_rebuild.py"),
                "--dsn", DSN, "--qdrant-url", QDRANT, "--collection", "kb_chunks",
                "--embedding-url", EMBEDDING, "--embedding-model", EMBEDDING_MODEL,
                "--dim", DIM, "--ensure-collection", "--json",
            ],
            capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=300, check=False,
        )
        print("\n$ qdrant_rebuild.py --ensure-collection")
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            print(proc.stderr[-3000:], file=sys.stderr)
        check("重建脚本 rc=0", proc.returncode == 0, proc.stderr[-300:])
        summary = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.returncode == 0 else {}
        check("重建了 3 个点（v2 的 3 条 active）", summary.get("upserted") == 3, str(summary))

        after_rebuild = qdrant_points()
        check("重建后点数与重建前一致",
              len(after_rebuild) == len([p for p in before_rebuild.values()
                                         if p["version_id"] == V2]),
              f"{len(after_rebuild)} vs 3")

        # 逐条比对 payload（status / version_id）
        mismatches = []
        for chunk_id, point in after_rebuild.items():
            expected = before_rebuild.get(chunk_id)
            if expected is None:
                continue
            if (point["status"], point["version_id"]) != (expected["status"], expected["version_id"]):
                mismatches.append((chunk_id, expected, point))
        check("逐条 payload 一致", not mismatches, str(mismatches[:3]))

        # 关键：重建后的点同时带 dense 与 bm25，且 bm25 有 token
        with httpx.Client(base_url=QDRANT, timeout=15, trust_env=False) as client:
            ids = [p["id"] for p in after_rebuild.values()]
            resp = client.post(
                "/collections/kb_chunks/points",
                json={"ids": ids, "with_vector": True, "with_payload": True},
            )
            points_full = resp.json()["result"]
        both = [p for p in points_full if set(p["vector"]) == {"dense", "bm25"}]
        check("每个点同时有 dense+bm25", len(both) == len(points_full),
              f"{len(both)}/{len(points_full)}")
        sparse_ok = [p for p in points_full
                     if len(p["vector"]["bm25"]["indices"]) > 3]
        check("bm25 真的切出了多个 token（中文 bigram）",
              len(sparse_ok) == len(points_full),
              f"{len(sparse_ok)}/{len(points_full)} "
              f"tokens={[len(p['vector']['bm25']['indices']) for p in points_full]}")

        final = cli("reconcile", "--doc-key", DOC)
        check("重建后对账干净", final["remaining"] == 0, str(final["drift_counts"]))

    finally:
        print("\n[cleanup] 清掉本次演练的行与点")
        cleanup()
        shutil.rmtree(figures, ignore_errors=True)

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 条")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
