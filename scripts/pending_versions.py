"""列出待转正的 pending 版本（version_id / doc_key / 切片数）。.

作为脚本而不是 Makefile heredoc 存在，因为 heredoc 的结束标记在 Makefile 里
被 tab 缩进后 bash 认不出来（`make pending` 报 "here-document delimited by
end-of-file"）。脚本文件没有这个问题，而且能单独跑、能加测试。
"""

from __future__ import annotations

if __package__:
    from .resources.pending_versions_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/pending_versions.py
    from resources.pending_versions_msgs import MESSAGES as M

from api.clients.pg import PgDatabase
from api.config import get_settings


def main() -> int:
    """Run main."""
    db = PgDatabase(get_settings().database_url)
    # PgDatabase 的池由 lifespan 打开；独立脚本没有 lifespan，必须自己开/关，
    # 否则 connection() 直接抛 PoolClosed（实测 make pending 因此 rc=2）。
    db.open()
    try:
        with db.connection() as conn:
            rows = conn.execute(

                    "\n"
                    "                SELECT v.version_id, v.doc_key,\n"
                    "                       (SELECT count(*) FROM chunks c "
                    "WHERE c.version_id = v.version_id)\n"
                    "                FROM doc_versions v\n"
                    "                WHERE v.status = 'pending'\n"
                    "                ORDER BY v.doc_key\n"
                    "                "

            ).fetchall()
    finally:
        db.close()
    if not rows:
        print(M["PRINT_01"])
        return 0
    print(f"{M['PRINT_02']}{len(rows)}{M['PRINT_03']}")
    for version_id, doc_key, n in rows:
        print(f"  {doc_key}")
        print(f"    version_id = {version_id}{M['PRINT_04']}{n}{M['PRINT_05']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
