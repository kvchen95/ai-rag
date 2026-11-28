"""探查：ACL 的 OR 三支能不能被索引覆盖。.

问题是 `is_public OR depts && x OR roles && y` 三支 OR，PG 用 BitmapOr 才能合并
多个索引；但要能走 BitmapOr，三支必须**都是索引条件**。`is_public` 是布尔列，
没有单列索引（只有默认的、选择性很差的），所以整条 OR 退化成 Seq Scan。

这个脚本对同一个查询试几种索引组合，看哪种能让计划器改走 BitmapOr：

  A. 现状（只 doc_key/version_id/year/date/depts/roles 的现有索引）
  B. 加 is_public 的部分索引
  C. 加 (allowed_depts) 与 (allowed_roles) 已有 —— 确认它们被用上
  D. 加一个组合索引 (status, is_public)

跑完把试加的索引全部删掉（DROP INDEX IF EXISTS），不动别人的 schema。
"""

from __future__ import annotations

import os
import sys

if __package__:
    from .resources.b2_explain_acl_probe_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/b2_explain_acl_probe.py
    from resources.b2_explain_acl_probe_msgs import MESSAGES as M

try:
    import psycopg
except ImportError:
    print(M["PRINT_01"], file=sys.stderr)
    sys.exit(3)

DSN = (os.getenv("DATABASE_URL") or "").replace(
    "postgresql+psycopg://", "postgresql://"
)

QUERY = (
            "\n"
            "SELECT chunk_id FROM chunks AS c\n"
            "WHERE status = 'active'\n"
            "  AND (is_public OR allowed_depts && '{fin,corp}'::text[] OR "
            "allowed_roles && '{staff}'::text[])\n"
            "ORDER BY chunk_id\n"
        )

TRIALS = [
    (M["TRIALS_01"], []),
    (
        M["TRIALS_02"],
        [
            "CREATE INDEX ix_tmp_pub ON chunks (is_public) WHERE status ="
            " 'active'"
        ],
    ),
    (
        M["TRIALS_03"],
        [
            "CREATE INDEX ix_tmp_depts ON chunks USING gin (allowed_depts)"
            " WHERE status = 'active'",
            "CREATE INDEX ix_tmp_roles ON chunks USING gin (allowed_roles)"
            " WHERE status = 'active'",
        ],
    ),
    (
        M["TRIALS_04"],
        [
            "CREATE INDEX ix_tmp_pub ON chunks (is_public) WHERE status ="
            " 'active'",
            "CREATE INDEX ix_tmp_depts ON chunks USING gin (allowed_depts)"
            " WHERE status = 'active'",
            "CREATE INDEX ix_tmp_roles ON chunks USING gin (allowed_roles)"
            " WHERE status = 'active'",
        ],
    ),
]

DROPS = [
    "DROP INDEX IF EXISTS ix_tmp_pub",
    "DROP INDEX IF EXISTS ix_tmp_depts",
    "DROP INDEX IF EXISTS ix_tmp_roles",
]


def plan(conn) -> tuple[str, float, str]:
    """Run plan."""
    row = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + QUERY
    ).fetchone()
    doc = row[0][0]
    root = doc["Plan"]

    def walk(node, acc):
        """Run walk."""
        acc.append(node.get("Node Type", ""))
        for child in node.get("Plans", []) or []:
            walk(child, acc)
        return acc

    kinds = walk(root, [])
    return ",".join(kinds), doc.get("Execution Time", 0.0), doc.get("Plan Rows")


def main() -> int:
    """Run main."""
    if not DSN:
        print(M["PRINT_02"], file=sys.stderr)
        return 2
    with psycopg.connect(DSN, autocommit=True) as conn:
        total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        print(f"chunks={total}")
        if total < 5000:
            print(M["PRINT_03"])
            return 1
        for name, creates in TRIALS:
            with conn.cursor() as cur:
                for sql in creates:
                    cur.execute(sql)
                cur.execute("ANALYZE chunks")
                kinds, ms, rows = plan(conn)
                print(
                    f"{name}{M['PRINT_04']}{kinds}"
                    f"{M['PRINT_05']}{ms:.2f}ms  plan_rows: {rows}"
                )
                for sql in DROPS:
                    cur.execute(sql)
        print(M["PRINT_06"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
