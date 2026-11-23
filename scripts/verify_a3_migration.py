"""003 迁移在**全新 database** 上的可复现验证（含约束逐条拒坏行）。

用真·新库（CREATE DATABASE ai_rag_a3verify）而不是新 schema：pgvector 的 `vector`
类型装在 public schema，改 search_path 会让 001 的 `vector(512)` 找不到类型（实测）。
新库的 public 就是干净的，等价于"从零装机"。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parent.parent
ADMIN = "postgresql://ai_rag:ai_rag_dev_password@127.0.0.1:5432/ai_rag"
DBNAME = "ai_rag_a3verify"
DSN = f"postgresql://ai_rag:ai_rag_dev_password@127.0.0.1:5432/{DBNAME}"

FAILS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(label)


def rejects(conn, label: str, sql: str, params: tuple = ()) -> None:
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        check(f"{label} 被拒", False, "没有报错（应当被约束拦住）")
    except Exception as exc:  # noqa: BLE001
        detail = str(exc).splitlines()[0][:110]
        check(f"{label} 被拒", True, f"{type(exc).__name__}: {detail}")
    finally:
        conn.rollback()


def migrate(dsn: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(REPO / "scripts" / "db_migrate.py"), "--dsn", dsn],
        capture_output=True, text=True, cwd=REPO, check=False,
    )


def main() -> int:
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DBNAME}")
        conn.execute(f"CREATE DATABASE {DBNAME}")
    print(f"=== 新建 database {DBNAME}（等价于从零装机）")

    proc = migrate(DSN)
    print(proc.stdout[-1500:])
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
    check("001+002+003 全新库 apply 成功", proc.returncode == 0, f"rc={proc.returncode}")
    if proc.returncode != 0:
        _drop(restore=False)
        return 1

    proc2 = migrate(DSN)
    check(
        "重跑 no-op",
        proc2.returncode == 0 and "nothing to do" in proc2.stdout,
        proc2.stdout.strip()[-100:],
    )

    try:
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('public.vector_sync_tasks')::text,"
                    " to_regclass('public.vector_drift_log')::text,"
                    " to_regclass('public.chunks')::text,"
                    " to_regclass('public.v_active_chunks')::text"
                )
                tables = cur.fetchone()
            check("vector_sync_tasks / vector_drift_log / chunks / v_active_chunks 都在",
                  all(t is not None for t in tables), str(tables))

            with conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'doc_versions' "
                    "  AND column_name = 'deprecated_at'"
                )
                check("doc_versions.deprecated_at 存在", cur.fetchone() is not None)

                cur.execute(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                    "AND indexname IN ('ix_doc_versions_deprecated_at',"
                    "                  'ix_vector_sync_due', 'uq_vector_drift_open_dedupe')"
                )
                names = {r[0] for r in cur.fetchall()}
            check("三个新索引都在", len(names) == 3, str(sorted(names)))

            # 造一行版本，供后面的约束测试用
            with conn.cursor() as cur:
                cur.execute("INSERT INTO documents (doc_key, filename) VALUES ('v/doc','x.pdf')")
                cur.execute(
                    "INSERT INTO doc_versions (version_id, doc_key, file_hash, status)"
                    " VALUES ('v/doc#a','v/doc',%s,'pending')", ("a" * 64,)
                )
            conn.commit()

            rejects(conn, "desired_status 非法",
                    "INSERT INTO vector_sync_tasks (version_id, doc_key, desired_status)"
                    " VALUES ('v/doc#a','v/doc','bogus')")
            rejects(conn, "sync status 非法",
                    "INSERT INTO vector_sync_tasks (version_id, doc_key, desired_status, status)"
                    " VALUES ('v/doc#a','v/doc','active','weird')")
            rejects(conn, "failed 无 error",
                    "INSERT INTO vector_sync_tasks (version_id, doc_key, desired_status, status)"
                    " VALUES ('v/doc#a','v/doc','active','failed')")
            rejects(conn, "done 无 synced_at",
                    "INSERT INTO vector_sync_tasks (version_id, doc_key, desired_status, status)"
                    " VALUES ('v/doc#a','v/doc','active','done')")
            rejects(conn, "attempts 为负",
                    "INSERT INTO vector_sync_tasks (version_id, doc_key, desired_status, attempts)"
                    " VALUES ('v/doc#a','v/doc','active',-1)")
            rejects(conn, "drift kind 非法",
                    "INSERT INTO vector_drift_log (doc_key, kind, dedupe_key)"
                    " VALUES ('v/doc','whatever','k1')")

            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO vector_sync_tasks (version_id, doc_key, desired_status)"
                    " VALUES ('v/doc#a','v/doc','active')"
                )
                cur.execute(
                    "INSERT INTO vector_drift_log (doc_key, kind, dedupe_key)"
                    " VALUES ('v/doc','status_mismatch','k2')"
                )
            conn.commit()
            check("合法行能插入", True)

            rejects(conn, "未修复漂移重复",
                    "INSERT INTO vector_drift_log (doc_key, kind, dedupe_key)"
                    " VALUES ('v/doc','status_mismatch','k2')")

            with conn.cursor() as cur:
                cur.execute("UPDATE vector_drift_log SET fixed = true WHERE dedupe_key = 'k2'")
                cur.execute(
                    "INSERT INTO vector_drift_log (doc_key, kind, dedupe_key)"
                    " VALUES ('v/doc','status_mismatch','k2')"
                )
            conn.commit()
            check("修好之后可以再记一条", True)

            # 003 的关键行为：删版本级联删同步任务（一次都别留）
            with conn.cursor() as cur:
                cur.execute("DELETE FROM doc_versions WHERE version_id = 'v/doc#a'")
                cur.execute("SELECT count(*) FROM vector_sync_tasks WHERE version_id = 'v/doc#a'")
                left = cur.fetchone()[0]
            conn.commit()
            check("删版本级联删同步任务", left == 0, f"left={left}")

            # 001 的既有约束在这套迁移里仍然有效（回归：003 不该把它们弄坏）
            with conn.cursor() as cur:
                cur.execute("INSERT INTO documents (doc_key, filename) VALUES ('v/doc2','y.pdf')")
                cur.execute(
                    "INSERT INTO doc_versions (version_id, doc_key, file_hash, status)"
                    " VALUES ('v/doc2#a','v/doc2',%s,'pending')", ("b" * 64,)
                )
                cur.execute(
                    "UPDATE doc_versions SET status='active', activated_at=now() "
                    "WHERE version_id='v/doc2#a'"
                )
            conn.commit()
            rejects(conn, "第二个 active 版本（ux_active_version）",
                    "INSERT INTO doc_versions (version_id, doc_key, file_hash, status,"
                    " activated_at) VALUES ('v/doc2#b','v/doc2',%s,'active', now())",
                    ("c" * 64,))
            rejects(conn, "active 版本无 activated_at",
                    "INSERT INTO doc_versions (version_id, doc_key, file_hash, status)"
                    " VALUES ('v/doc2#c','v/doc2',%s,'active')", ("d" * 64,))
    finally:
        _drop(restore=True)

    print("\n" + "=" * 70)
    if FAILS:
        print(f"FAILED: {len(FAILS)} -> {FAILS}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _drop(*, restore: bool) -> None:
    with psycopg.connect(ADMIN, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (DBNAME,)
        )
        conn.execute(f"DROP DATABASE IF EXISTS {DBNAME}")
    if restore:
        print(f"\n=== 清理 database {DBNAME}")


if __name__ == "__main__":
    sys.exit(main())
