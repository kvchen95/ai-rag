#!/usr/bin/env python3
"""A3 端到端演练：用真 CLI + 真 PG + 真 Qdrant + 真 TEI 跑完一条完整链路。.

不 mock 任何东西，每一步都调**运维会真正敲的命令**（scripts/a3_ingest.py 的子命
令），
并把输出原样贴出来。这条脚本的存在意义是：验收"重建脚本跑通"和"同步失败可对账修
复"
时，不必靠"我看过测试"这种口头保证。

演练的 8 步（对应 §4 的每个动作）：

1. 造数据：一个文档两个版本（v1 待上线、v2 之后改版）+ 每版 3 条切片
2. promote v1      -> index + flip + drain + reconcile
3. status          -> 检查 version/chunk/sync/drift 全绿
4. 造新的 pending  v2（模拟"文件改了字节"）
5. promote v2      -> v1 应立刻 deprecated、v2 active
6. 破坏同步（手工把一条 payload 改错） -> reconcile --dry-run 必须看见，
reconcile 修好
7. 把 v1 的下线时间推到 25h 前 -> reclaim 删切片/点/图，再 status
8. rebuild 全量重建（集合清空后从 PG 重建）-> 逐条比对

用法（宿主或容器内都行；宿主需 psycopg/httpx）：

DATABASE_URL=... QDRANT_URL=... EMBEDDING_BASE_URL=...     python
scripts/verify_a3_pipeline.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .resources.verify_a3_pipeline_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/verify_a3_pipeline.py
    from resources.verify_a3_pipeline_msgs import MESSAGES as M

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from api.clients.pg import normalize_dsn  # noqa: E402

PROXY_VARS = [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
]
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
    """Run check."""
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def cli(*args: str, expect_rc: int = 0) -> dict:
    """调真 CLI（子进程），返回解析后的 JSON。."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    for var in PROXY_VARS:
        env.pop(var, None)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "a3_ingest.py"),
        *args,
        "--dsn",
        DSN,
        "--qdrant-url",
        QDRANT,
        "--embedding-url",
        EMBEDDING,
        "--embedding-model",
        EMBEDDING_MODEL,
        "--dim",
        DIM,
        "--json",
    ]
    proc = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=300,
        check=False,
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
    """Run sql."""
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
    return rows


def seed(
    key_suffix: str, version_id: str, status: str, chunks: list[tuple[str, str]]
) -> None:
    """Run seed."""
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents (doc_key, filename, status) VALUES (%s, %s,"
            " 'queued') "
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
                (
                    "\n"
                    "                INSERT INTO chunks (chunk_id, doc_key, "
                    "version_id, chunk_type, page_num,\n"
                    "                                    bbox, content_text, "
                    "effective_date, effective_year,\n"
                    "                                    date_confidence, "
                    "is_public, status)\n"
                    "                VALUES (%s, %s, %s, 'text', 1, "
                    "'[[0.1,0.1,0.9,0.2]]'::jsonb, %s,\n"
                    "                        20260301, 2026, 'explicit', true, "
                    "'pending')\n"
                    "                "
                ),
                (chunk_id, DOC, version_id, text),
            )
        conn.commit()


def cleanup() -> None:
    """Run cleanup."""
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        for table in (
            "vector_drift_log",
            "vector_sync_tasks",
            "chunks",
            "doc_versions",
            "ingest_jobs",
            "documents",
        ):
            column = "doc_key" if table != "qa_log" else "user_id"
            cur.execute(
                f"DELETE FROM {table} WHERE {column} LIKE %s", (f"{NS}/%",)
            )
        conn.commit()
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id LIKE %s",
            (f"{CHUNK_PREFIX}%",),
        )
    # 集合里的点：按 doc_key 删（清理残留）

    import httpx

    with httpx.Client(base_url=QDRANT, timeout=10, trust_env=False) as client:
        client.post(
            "/collections/kb_chunks/points/delete?wait=true",
            json={
                "filter": {
                    "must": [{"key": "doc_key", "match": {"value": DOC}}]
                }
            },
        )


def qdrant_points() -> dict[str, dict]:
    """Run qdrant_points."""
    import httpx

    out: dict[str, dict] = {}
    offset = None
    with httpx.Client(base_url=QDRANT, timeout=15, trust_env=False) as client:
        while True:
            body = {
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
                "filter": {
                    "must": [{"key": "doc_key", "match": {"value": DOC}}]
                },
            }
            if offset:
                body["offset"] = offset
            resp = client.post(
                "/collections/kb_chunks/points/scroll", json=body
            )
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
    """手工把一条 payload 改错（模拟同步漏掉一条）。."""
    import httpx

    from scripts.qdrant_common import point_id_for

    with httpx.Client(base_url=QDRANT, timeout=10, trust_env=False) as client:
        resp = client.post(
            "/collections/kb_chunks/points/payload?wait=true",
            json={
                "payload": {"status": status},
                "points": [point_id_for(chunk_id)],
            },
        )
        resp.raise_for_status()


def main() -> int:
    """Run main."""
    figures = Path(tempfile.mkdtemp(prefix="a3figs-"))
    print("=" * 78)
    print(M["PRINT_01"])
    print(f"doc_key={DOC}  figures={figures}")
    print("=" * 78)

    try:
        cleanup()

        # --- 1. 造数据 --------------------------------------------------------
        print(M["PRINT_02"])
        seed(
            "1",
            V1,
            "pending",
            [
                (
                    f"{CHUNK_PREFIX}_1",
                    M["SEED_01"],
                ),
                (
                    f"{CHUNK_PREFIX}_2",
                    M["SEED_02"],
                ),
                (
                    f"{CHUNK_PREFIX}_3",
                    M["SEED_03"],
                ),
            ],
        )
        (figures / f"{CHUNK_PREFIX}_1.webp").write_bytes(b"RIFF....WEBP")
        rows = sql(
            "SELECT count(*) AS n FROM chunks WHERE doc_key = %s", (DOC,)
        )
        check(M["CHECK_01"], rows[0]["n"] == 3, f"chunks={rows[0]['n']}")

        # --- 2. promote v1 ----------------------------------------------------
        print(M["PRINT_03"])
        out = cli("promote", V1)
        check(M["CHECK_02"], out.get("ok") is True, str(out.get("error")))
        check(M["CHECK_03"], out["index"]["points"] == 3, str(out["index"]))
        check(
            M["CHECK_04"],
            out["flip"]["activated_chunks"] == 3,
            str(out["flip"]),
        )
        check(
            M["CHECK_05"],
            out["drain"]["done"] >= 1 and out["drain"]["failed"] == 0,
            str(out["drain"]),
        )
        check(
            M["CHECK_06"],
            out["reconcile"]["remaining"] == 0,
            str(out["reconcile"]["drift_counts"]),
        )

        # --- 3. status --------------------------------------------------------
        print(M["PRINT_04"])
        st = cli("status", "--doc-key", DOC)
        check(
            M["CHECK_07"],
            st["versions"] == {"active": 1},
            str(st["versions"]),
        )
        check(
            M["CHECK_08"], st["chunks"] == {"active": 3}, str(st["chunks"])
        )
        check(
            M["CHECK_09"], st["double_active"] == [], str(st["double_active"])
        )
        check(
            M["CHECK_10"],
            st["vector_sync_pending"] == 0,
            str(st["vector_sync_pending"]),
        )
        check(M["CHECK_11"], st["open_drift"] == {}, str(st["open_drift"]))
        check(
            M["CHECK_12"],
            st["qdrant_points"] >= 3,
            f"points={st['qdrant_points']}",
        )

        # --- 4. 新版 v2 -------------------------------------------------------
        print(M["PRINT_05"])
        seed(
            "2",
            V2,
            "pending",
            [
                (
                    f"{CHUNK_PREFIX}_4",
                    M["SEED_04"],
                ),
                (
                    f"{CHUNK_PREFIX}_5",
                    M["SEED_05"],
                ),
                (
                    f"{CHUNK_PREFIX}_6",
                    M["SEED_06"],
                ),
            ],
        )
        before = qdrant_points()
        check(
            M["CHECK_13"],
            len([p for p in before.values() if p["version_id"] == V1]) == 3,
        )
        check(
            M["CHECK_14"],
            not [p for p in before.values() if p["version_id"] == V2],
        )

        # --- 5. promote v2 ----------------------------------------------------
        print(M["PRINT_06"])
        out2 = cli("promote", V2)
        check(M["CHECK_15"], out2.get("ok") is True, str(out2.get("error")))
        check(
            M["CHECK_16"],
            out2["flip"]["previous_active"] == V1,
            str(out2["flip"]),
        )
        after = qdrant_points()
        v1_states = {
            p["status"] for p in after.values() if p["version_id"] == V1
        }
        v2_states = {
            p["status"] for p in after.values() if p["version_id"] == V2
        }
        check(
            M["CHECK_17"],
            v1_states == {"deprecated"},
            str(v1_states),
        )
        check(M["CHECK_18"], v2_states == {"active"}, str(v2_states))

        st = cli("status", "--doc-key", DOC)
        check(
            M["CHECK_19"],
            st["versions"] == {"active": 1, "deprecated": 1},
            str(st["versions"]),
        )
        check(
            M["CHECK_20"],
            st["chunks"] == {"active": 3, "deprecated": 3},
            str(st["chunks"]),
        )
        check(
            M["CHECK_21"],
            st["double_active"] == [],
            str(st["double_active"]),
        )

        # --- 6. 破坏同步 -> 对账修复
        # -------------------------------------------
        print(M["PRINT_07"])
        set_payload_wrong(f"{CHUNK_PREFIX}_4", "deprecated")
        dr = cli("reconcile", "--doc-key", DOC, "--dry-run", expect_rc=1)
        check(
            M["CHECK_22"],
            dr["drift_counts"] == {"status_mismatch": 1},
            str(dr["drift_counts"]),
        )
        check(
            M["CHECK_23"],
            dr["remaining"] == 1,
            str(dr["remaining"]),
        )

        fixed = cli("reconcile", "--doc-key", DOC)
        check(
            M["CHECK_24"],
            fixed["remaining"] == 0,
            str(fixed["drift_counts"]),
        )
        check(
            M["CHECK_25"],
            cli("reconcile", "--doc-key", DOC)["remaining"] == 0,
        )

        # --- 7. 回收 ----------------------------------------------------------
        print(M["PRINT_08"])
        sql(
            "UPDATE doc_versions SET deprecated_at = now() - interval '25"
            " hours' "
            "WHERE version_id = %s",
            (V1,),
        )
        rec = cli(
            "reclaim",
            "--doc-key",
            DOC,
            "--retention-hours",
            "24",
            "--figures-dir",
            str(figures),
        )
        check(M["CHECK_26"], rec["purged"] == 1, str(rec["purged"]))
        check(M["CHECK_27"], rec["chunks"] == 3, str(rec["chunks"]))
        check(M["CHECK_28"], rec["figures"] == 1, str(rec["figures"]))
        check(
            M["CHECK_29"],
            not (figures / f"{CHUNK_PREFIX}_1.webp").exists(),
        )
        remaining = qdrant_points()
        check(
            M["CHECK_30"],
            not [p for p in remaining.values() if p["version_id"] == V1],
            str({p["version_id"] for p in remaining.values()}),
        )
        check(
            M["CHECK_31"],
            len([p for p in remaining.values() if p["version_id"] == V2]) == 3,
        )
        st = cli("status", "--doc-key", DOC)
        check(
            M["CHECK_32"],
            st["versions"] == {"active": 1, "purged": 1},
            str(st["versions"]),
        )

        # --- 8. 全量重建 ------------------------------------------------------
        print(M["PRINT_09"])
        import httpx

        before_rebuild = qdrant_points()
        with httpx.Client(
            base_url=QDRANT, timeout=15, trust_env=False
        ) as client:
            client.delete("/collections/kb_chunks")
            client.put(
                "/collections/kb_chunks",
                json={
                    "vectors": {
                        "dense": {"size": int(DIM), "distance": "Cosine"}
                    },
                    "sparse_vectors": {"bm25": {"modifier": "idf"}},
                    "on_disk_payload": True,
                },
            )
        print(M["PRINT_10"])

        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        for var in PROXY_VARS:
            env.pop(var, None)
        proc = subprocess.run(  # noqa: S603
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "qdrant_rebuild.py"),
                "--dsn",
                DSN,
                "--qdrant-url",
                QDRANT,
                "--collection",
                "kb_chunks",
                "--embedding-url",
                EMBEDDING,
                "--embedding-model",
                EMBEDDING_MODEL,
                "--dim",
                DIM,
                "--ensure-collection",
                "--json",
            ],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=300,
            check=False,
        )
        print("\n$ qdrant_rebuild.py --ensure-collection")
        if proc.returncode != 0:
            print(proc.stdout[-2000:])
            print(proc.stderr[-3000:], file=sys.stderr)
        check(M["CHECK_33"], proc.returncode == 0, proc.stderr[-300:])
        summary = (
            json.loads(proc.stdout.strip().splitlines()[-1])
            if proc.returncode == 0
            else {}
        )
        check(
            M["CHECK_34"],
            summary.get("upserted") == 3,
            str(summary),
        )

        after_rebuild = qdrant_points()
        check(
            M["CHECK_35"],
            len(after_rebuild)
            == len(
                [p for p in before_rebuild.values() if p["version_id"] == V2]
            ),
            f"{len(after_rebuild)} vs 3",
        )

        # 逐条比对 payload（status / version_id）
        mismatches = []
        for chunk_id, point in after_rebuild.items():
            expected = before_rebuild.get(chunk_id)
            if expected is None:
                continue
            if (point["status"], point["version_id"]) != (
                expected["status"],
                expected["version_id"],
            ):
                mismatches.append((chunk_id, expected, point))
        check(M["CHECK_36"], not mismatches, str(mismatches[:3]))

        # 关键：重建后的点同时带 dense 与 bm25，且 bm25 有 token
        with httpx.Client(
            base_url=QDRANT, timeout=15, trust_env=False
        ) as client:
            ids = [p["id"] for p in after_rebuild.values()]
            resp = client.post(
                "/collections/kb_chunks/points",
                json={"ids": ids, "with_vector": True, "with_payload": True},
            )
            points_full = resp.json()["result"]
        both = [p for p in points_full if set(p["vector"]) == {"dense", "bm25"}]
        check(
            M["CHECK_37"],
            len(both) == len(points_full),
            f"{len(both)}/{len(points_full)}",
        )
        sparse_ok = [
            p for p in points_full if len(p["vector"]["bm25"]["indices"]) > 3
        ]
        check(
            M["CHECK_38"],
            len(sparse_ok) == len(points_full),
            f"{len(sparse_ok)}/{len(points_full)} "
            "tokens="
            f"{[len(p['vector']['bm25']['indices']) for p in points_full]}",
        )

        final = cli("reconcile", "--doc-key", DOC)
        check(
            M["CHECK_39"],
            final["remaining"] == 0,
            str(final["drift_counts"]),
        )

    finally:
        print(M["PRINT_11"])
        cleanup()
        shutil.rmtree(figures, ignore_errors=True)

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}{M['PRINT_12']}")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
