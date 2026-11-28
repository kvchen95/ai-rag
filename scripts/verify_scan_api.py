#!/usr/bin/env python3
"""实测扫描接口（真 HTTP、真 PG）。.

为什么不能只靠 TestClient：

- **409 的并发是跨进程的**。TestClient + 替身只能证明"锁拿不到时抛异常"，
证明不了"另一个进程持锁时我这边真的拿不到"。这里起真实 HTTP 请求，并在
**另一个 OS 进程**里持 advisory lock 来模拟"另一个扫描进程"。
- **`doc_key` 带斜杠**要过 uvicorn 的路由层，TestClient 也证明不了这一点。
- **响应的 JSON 形状**要经过真实序列化（datetime、Decimal 之类会在这一步炸）。

用法（宿主侧，psycopg 可选；没有就退回 `docker exec psql` 持锁）：

python3 scripts/verify_scan_api.py                       # http:
//127.0.0.1:8000 python3 scripts/verify_scan_api.py --base-url http:
//127.0.0.1:5173/api python3 scripts/verify_scan_api.py --dsn postgresql:
//...

退出码 0 = 全部符合预期；非 0 说明契约破了，输出里会写出期望与实际。
锁 key 从 `/healthz` 的 `ingest.scan_lock_key` 读（只在非生产返回），不抄数值
进脚本——抄了之后改 key 不会红，脚本只会静静失去测并发的能力。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from .resources.verify_scan_api_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/verify_scan_api.py
    from resources.verify_scan_api_msgs import MESSAGES as M

# 仓库自带的三个样本（scripts/make_samples.py 生成）归一化后的 doc_key
EXPECTED_SAMPLE_DOCS = {"policy/travel-cn", "policy/expense-cn", "hr/leave"}


def call(base_url: str, path: str, method: str = "GET") -> tuple[int, dict]:
    """Run call."""
    req = urllib.request.Request(base_url.rstrip("/") + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"_raw": body}


def read_lock_key(base_url: str) -> tuple[int | None, str]:
    """Run read_lock_key."""
    status, body = call(base_url, "/healthz")
    if status != 200:
        return None, f"healthz status={status}"
    key = (body.get("ingest") or {}).get("scan_lock_key")
    if key is None:
        return (
            None,
            "healthz has no ingest.scan_lock_key (APP_ENV=prod? or older"
            " build)",
        )
    return int(key), "ok"


def hold_lock(dsn: str | None, key: int) -> tuple[subprocess.Popen[str], str]:
    """在独立进程里持锁 60 秒，返回 (进程, 用的方式)。."""
    if dsn:
        code = (
            "import psycopg,time\n"
            f"c = psycopg.connect({dsn!r}, autocommit=True)\n"
            f"c.execute('SELECT pg_advisory_lock({key})')\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return proc, "host python + psycopg"
    proc = subprocess.Popen(  # noqa: S603
        [
            "docker",
            "exec",
            "ai-rag-postgres",
            "psql",
            "-U",
            "ai_rag",
            "-d",
            "ai_rag",
            "-c",
            f"SELECT pg_advisory_lock({key});",
            "-c",
            "SELECT pg_sleep(60);",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc, "docker exec psql"


def main() -> int:
    """Run main."""
    ap = argparse.ArgumentParser(
        description="verify /api/v1/ingest/scan contract"
    )
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument(
        "--dsn", default=None, help="PG DSN for holding the scan lock"
    )
    ap.add_argument(
        "--skip-conflict", action="store_true", help=M["HELP_01"]
    )
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    failures: list[str] = []
    unverified: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        """Run check."""
        print(
            f"{'PASS' if ok else 'FAIL'}  {name}"
            + (f"   <- {detail}" if detail and not ok else "")
        )
        if not ok:
            failures.append(name)

    print(f"target: {base}\n")

    # --- 1. 扫描：四个计数、失败为 0、doc_key 归一化
    # ---------------------------
    status, body = call(base, "/api/v1/ingest/scan", "POST")
    check(
        "POST /api/v1/ingest/scan -> 200",
        status == 200,
        f"status={status} body={str(body)[:400]}",
    )
    if status == 200:
        required = {"added", "changed", "skipped", "failed", "reasons", "items"}
        check(
            "body carries the four counts + reasons + items",
            required <= set(body),
            f"missing={sorted(required - set(body))}",
        )
        failures = [
            i for i in body.get("items", []) if i.get("outcome") == "fail"
        ]
        check(
            "no file failed",
            body.get("failed") == 0,
            f"failed={body.get('failed')} {failures}",
        )
        docs = {item["doc_key"] for item in body.get("items", [])}
        check(
            "doc_key normalisation on real files (leave-2025 -> leave)",
            docs == EXPECTED_SAMPLE_DOCS,
            f"got={sorted(docs)}",
        )

    # --- 2. 重复扫描：全部跳过（验收第一条）-----------------------------------
    status2, body2 = call(base, "/api/v1/ingest/scan", "POST")
    check("second scan -> 200", status2 == 200, f"status={status2}")
    if status2 == 200:
        counts = {
            k: body2.get(k) for k in ("added", "changed", "skipped", "failed")
        }
        check(
            "rescan adds/changes/fails nothing",
            counts == {**counts, "added": 0, "changed": 0, "failed": 0},
            f"counts={counts}",
        )
        check(
            "rescan reports reason=unchanged",
            (body2.get("reasons") or {}).get("unchanged", 0) >= 1,
            f"reasons={body2.get('reasons')}",
        )

    # --- 3. GET document：200 + 形状 ------------------------------------------
    status3, doc = call(base, "/api/v1/documents/policy/travel-cn")
    check(
        "GET /api/v1/documents/policy/travel-cn -> 200 (slash in path)",
        status3 == 200,
        f"status={status3} body={str(doc)[:300]}",
    )
    if status3 == 200:
        for field in (
            "doc_key",
            "filename",
            "status",
            "version_count",
            "pending_versions",
            "chunk_count",
            "active_version",
            "latest_job",
            "last_error",
        ):
            check(
                f"document payload has {field}",
                field in doc,
                f"keys={sorted(doc)}",
            )
        check(
            "doc_key echoed verbatim",
            doc.get("doc_key") == "policy/travel-cn",
            str(doc.get("doc_key")),
        )
        job = doc.get("latest_job") or {}
        check(
            "latest_job present with retry accounting",
            {"job_id", "status", "retry_count", "retry_budget_left"}
            <= set(job),
            f"job={job}",
        )

    # --- 4. 404：契约错误体
    # ----------------------------------------------------
    status4, err = call(base, "/api/v1/documents/policy/does-not-exist")
    check("unknown doc_key -> 404", status4 == 404, f"status={status4}")
    error = err.get("error") or {}
    check(
        "404 uses the shared error contract (code + message + request_id)",
        error.get("code") == "not_found" and bool(error.get("request_id")),
        f"body={err}",
    )

    # --- 5. 409：另一个进程持锁
    # ------------------------------------------------
    if args.skip_conflict:
        unverified.append("concurrent scan -> 409 (--skip-conflict)")
    else:
        key, why = read_lock_key(base)
        check("read scan lock key from /healthz", key is not None, why)
        if key is not None:
            holder, how = hold_lock(args.dsn, key)
            print(f"      (lock held by {how}, key={key})")
            try:
                time.sleep(2.0)
                status5, err5 = call(base, "/api/v1/ingest/scan", "POST")
                check(
                    "concurrent scan -> 409",
                    status5 == 409,
                    f"status={status5} body={str(err5)[:300]}",
                )
                check(
                    "409 body uses scan_in_progress",
                    (err5.get("error") or {}).get("code") == "scan_in_progress",
                    f"body={err5}",
                )
            finally:
                holder.terminate()
                try:
                    holder.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    holder.kill()

            status6, _ = call(base, "/api/v1/ingest/scan", "POST")
            check(
                "scan works again after the lock is released",
                status6 == 200,
                f"status={status6}",
            )

    print()
    if unverified:
        for item in unverified:
            print(f"UNVERIFIED: {item}")
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print(
        "ALL CHECKS PASSED"
        + (" (with unverified items above)" if unverified else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
