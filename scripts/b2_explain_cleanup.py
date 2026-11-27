"""清掉 b2 执行计划实验的种子数据与临时索引。.

独立成文件而不是塞进 shell 的 heredoc：多行 python 写在 sh -c 里既难读，
也容易在被复制粘贴时把引号弄坏。
"""

from __future__ import annotations

import os

import psycopg

from scripts.b2_explain_seed import DOC_KEY

DSN = (os.getenv("DATABASE_URL") or "").replace(
    "postgresql+psycopg://", "postgresql://"
)
TEMP_INDEXES = ("ix_tmp_pub", "ix_tmp_depts", "ix_tmp_roles")


def main() -> int:
    if not DSN:
        print("ERROR: DATABASE_URL 未设置")
        return 2
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name in TEMP_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        conn.execute("DELETE FROM chunks WHERE doc_key = %s", (DOC_KEY,))
        conn.execute("DELETE FROM doc_versions WHERE doc_key = %s", (DOC_KEY,))
        conn.execute("DELETE FROM documents WHERE doc_key = %s", (DOC_KEY,))
        left = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    print(f"cleaned: 剩余 chunks {left} 行，临时索引已删")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
