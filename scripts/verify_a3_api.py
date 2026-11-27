"""真 HTTP 验 A3 的四个运维端点（宿主 uvicorn + 真 PG/Qdrant/TEI）。.

验什么（都是"只有真发 HTTP 才看得见"的东西）：

1. 路由形状：`/ingest/versions/{version_id}/promote` 里的 version_id 带 `#`，
   写成普通路径参数会被当成 fragment 截断；
2. 状态码语义：404（没有这个版本）/ 409（已翻过）/ 422（空版本）；
3. `reconcile` 有残留差异时返回 409 而不是 200；
4. promote 的返回体结构（index / flip / drain / reconcile 四段）。

用法：python3 verify_a3_api.py [--base http://127.0.0.1:18081]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

for _var in [
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
]:
    os.environ.pop(_var, None)

import psycopg  # noqa: E402

NS = "a3api"
DOC = f"{NS}/travel"
V_EMPTY = f"{DOC}#empty000"
V_OK = f"{DOC}#ok11111"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(
        f"  [{'PASS' if ok else 'FAIL'}] {label}"
        + (f"  ({detail})" if detail else "")
    )
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def call(base: str, method: str, path: str, expect: int) -> tuple[int, dict]:
    req = urllib.request.Request(base.rstrip("/") + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            code, body = resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        code, body = exc.code, exc.read().decode()
    payload = json.loads(body) if body else {}
    status_ok = code == expect
    print(f"\n$ {method} {path} -> {code} (expect {expect})")
    if not status_ok:
        print(f"  body: {body[:600]}")
    return code, payload if status_ok else {
        "_unexpected": {"code": code, "body": body[:600]}
    }


def dsn() -> str:
    raw = (
        os.getenv("DATABASE_URL")
        or "postgresql://ai_rag:ai_rag_dev_password@127.0.0.1:5432/ai_rag"
    )
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if raw.startswith(prefix):
            return "postgresql://" + raw[len(prefix) :]
    return raw


def sql(query: str, params: tuple = ()) -> None:
    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        conn.commit()


def cleanup() -> None:
    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        for table in (
            "vector_drift_log",
            "vector_sync_tasks",
            "chunks",
            "doc_versions",
            "ingest_jobs",
            "documents",
        ):
            cur.execute(
                f"DELETE FROM {table} WHERE doc_key LIKE %s", (f"{NS}/%",)
            )
        conn.commit()
    import httpx

    with httpx.Client(
        base_url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
        timeout=10,
        trust_env=False,
    ) as client:
        client.post(
            "/collections/kb_chunks/points/delete?wait=true",
            json={
                "filter": {
                    "must": [{"key": "doc_key", "match": {"value": DOC}}]
                }
            },
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base", default=os.getenv("API_BASE", "http://127.0.0.1:18081")
    )
    args = ap.parse_args()
    base = args.base

    print("=" * 78)
    print(f"A3 API 契约实测（真 HTTP @ {base}）")
    print("=" * 78)

    try:
        cleanup()

        # 造一个空版本（翻它必须是 422，而不是把服务翻空）
        sql(
            "INSERT INTO documents (doc_key, filename, status) VALUES (%s,"
            " 'travel.pdf', 'queued')",
            (DOC,),
        )
        sql(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status) "
            "VALUES (%s, %s, %s, 'pending')",
            (V_EMPTY, DOC, "e" * 64),
        )

        print("\n[1] 404：库里没有这个版本")
        code, _ = call(
            base, "POST", "/api/v1/ingest/versions/a3api/nope%23v1/promote", 404
        )
        check("不存在的版本 -> 404", code == 404)

        print("\n[2] 422：空版本拒绝转正")
        code, body = call(
            base,
            "POST",
            f"/api/v1/ingest/versions/{V_EMPTY.replace('#', '%23')}/promote",
            422,
        )
        check("空版本 -> 422", code == 422)
        active = _scalar(
            "SELECT count(*) FROM doc_versions WHERE doc_key = %s AND status ="
            " 'active'",
            (DOC,),
        )
        check("空版本没有被翻成 active", active == 0, f"active={active}")

        print("\n[3] 造一个有切片的版本，走 promote")
        sql(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status) "
            "VALUES (%s, %s, %s, 'pending')",
            (V_OK, DOC, "f" * 64),
        )
        for i, text in enumerate(
            [
                "住宿费标准：总监级每人每天 800 元，经理级 600 元。",
                "市内交通费凭票报销，单次不超过 100 元。",
            ],
            start=1,
        ):
            sql(
                "INSERT INTO chunks (chunk_id, doc_key, version_id,"
                " chunk_type, page_num, bbox,"
                " content_text, effective_date, effective_year,"
                " date_confidence, is_public, status)"
                " VALUES (%s, %s, %s, 'text', 1, '[[0.1,0.1,0.9,0.2]]'::jsonb,"
                " %s,"
                " 20260301, 2026, 'explicit', true, 'pending')",
                (f"ch_a3api_{i}", DOC, V_OK, text),
            )
        code, body = call(
            base,
            "POST",
            f"/api/v1/ingest/versions/{V_OK.replace('#', '%23')}/promote",
            200,
        )
        check(
            "promote -> 200",
            code == 200,
            str(body)[:200] if code != 200 else "",
        )
        check(
            "返回体含 index/flip/drain/reconcile 四段",
            all(k in body for k in ("index", "flip", "drain", "reconcile")),
            str(list(body)),
        )
        if code == 200:
            check(
                "promote ok=true",
                body.get("ok") is True,
                str(body.get("error")),
            )
            check(
                "索引了 2 个点",
                body["index"]["points"] == 2,
                str(body["index"]),
            )
            check(
                "对账无残留",
                body["reconcile"]["remaining"] == 0,
                str(body["reconcile"]["drift_counts"]),
            )

        print("\n[4] 409：重复 promote 同一个版本")
        code, _ = call(
            base,
            "POST",
            f"/api/v1/ingest/versions/{V_OK.replace('#', '%23')}/promote",
            409,
        )
        check("已 active 的版本再 promote -> 409", code == 409)

        print("\n[5] reconcile：干净时 200，有漂移时 409")
        code, body = call(
            base, "GET", f"/api/v1/ingest/reconcile?doc_key={DOC}", 200
        )
        check("干净 -> 200", code == 200)
        check(
            "remaining=0",
            body.get("remaining") == 0,
            str(body.get("drift_counts")),
        )

        # 手工弄坏一条 payload
        import httpx

        from scripts.qdrant_common import point_id_for

        with httpx.Client(
            base_url=os.getenv("QDRANT_URL", "http://127.0.0.1:6333"),
            timeout=10,
            trust_env=False,
        ) as client:
            client.post(
                "/collections/kb_chunks/points/payload?wait=true",
                json={
                    "payload": {"status": "deprecated"},
                    "points": [point_id_for("ch_a3api_1")],
                },
            )

        code, body = call(
            base,
            "GET",
            f"/api/v1/ingest/reconcile?doc_key={DOC}&dry_run=true",
            409,
        )
        check("有漂移且 dry-run -> 409（不是 200）", code == 409)
        check(
            "dry-run 看见了 1 处 status_mismatch",
            body.get("drift_counts") == {"status_mismatch": 1},
            str(body.get("drift_counts")),
        )

        code, body = call(
            base, "GET", f"/api/v1/ingest/reconcile?doc_key={DOC}", 200
        )
        check("真修好后 -> 200", code == 200)
        code, body = call(
            base, "GET", f"/api/v1/ingest/reconcile?doc_key={DOC}", 200
        )
        check("再查一次仍干净", body.get("remaining") == 0)

        print("\n[6] sync 端点：没有待同步任务时 processed=0")
        code, body = call(base, "POST", "/api/v1/ingest/sync", 200)
        check("sync -> 200", code == 200)
        check("没有可疑的失败", body.get("failed") == 0, str(body))

        print("\n[7] rollback：把已 deprecated 的版本翻回来")
        # 先造一个 deprecated 版本（曾经上线过）
        sql(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status,"
            " activated_at,"
            " deprecated_at) VALUES (%s, %s, %s, 'deprecated', now(), now())",
            (f"{DOC}#prev2222", DOC, "a" * 64),
        )
        code, body = call(
            base,
            "POST",
            "/api/v1/ingest/versions/"
            f"{DOC.replace('/', '%2F')}%23prev2222/rollback",
            200,
        )
        check(
            "rollback -> 200",
            code == 200,
            str(body)[:200] if code != 200 else "",
        )
        if code == 200:
            check(
                "回滚到了那个版本",
                body["rollback"]["restored"] == f"{DOC}#prev2222",
                str(body["rollback"]),
            )

        code, body = call(
            base, "GET", "/api/v1/ingest/reconcile?doc_key=" + DOC, 200
        )
        check(
            "回滚后对账干净",
            body.get("remaining") == 0,
            str(body.get("drift_counts")),
        )
    finally:
        print("\n[cleanup]")
        cleanup()

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _scalar(query: str, params: tuple = ()) -> int:
    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return int(cur.fetchone()[0])


if __name__ == "__main__":
    sys.exit(main())
