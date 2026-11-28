"""一次性转正所有 pending 版本（开发/评测用）。.

作为脚本存在而不是 Makefile heredoc，理由见 scripts/pending_versions.py 的说明。

为什么"跑完所有再汇总失败"而不是"任一失败即停"
------------------------------------------------
11 份样本里可能只有 1 份解析失败。一失败就停会让后面 8 份都不转正，评测时
表现为"某类题全拒答"，排查的人很难第一时间想到是 promote 半途停了。
所以这里跑完所有，最后统一打印失败清单——成功的不被失败的那份连累。
"""

from __future__ import annotations

import subprocess
import sys

if __package__:
    from .resources.promote_all_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/promote_all.py
    from resources.promote_all_msgs import MESSAGES as M

from api.clients.pg import PgDatabase
from api.config import get_settings


def main() -> int:
    """Run main."""
    db = PgDatabase(get_settings().database_url)
    # 独立脚本没有 FastAPI lifespan，连接池必须自己开/关；否则 PoolClosed。
    db.open()
    try:
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT version_id, doc_key
                FROM doc_versions
                WHERE status = 'pending'
                ORDER BY doc_key
                """
            ).fetchall()
    finally:
        db.close()
    if not rows:
        print(M["PRINT_01"])
        return 0

    print(f"{M['PRINT_02']}{len(rows)}{M['PRINT_03']}")
    failed: list[tuple[str, str]] = []
    for version_id, doc_key in rows:
        result = subprocess.run(
            [sys.executable, "scripts/a3_ingest.py", "promote", version_id],
            capture_output=True,
            text=True,
        )
        ok = result.returncode == 0
        print(f"  {'OK  ' if ok else 'FAIL'} {doc_key}")
        if not ok:
            failed.append((doc_key, (result.stderr or result.stdout).strip()))

    if failed:
        print(M["PRINT_04"])
        for doc_key, detail in failed:
            print(f"\n--- {doc_key} ---")
            print(detail)
        return 1
    print(M["PRINT_05"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
