"""Chinese message catalog for scripts/db_migrate.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # db_migrate.py:39 (LEDGER_SQL)
    "LEDGER_SQL_01": (
        "\n"
        "CREATE TABLE IF NOT EXISTS schema_migrations (\n"
        "    version    text        PRIMARY KEY,\n"
        "    checksum   text        NOT NULL,\n"
        "    applied_at timestamptz NOT NULL DEFAULT now(),\n"
        "    elapsed_ms int         NOT NULL DEFAULT 0\n"
        ");\n"
        "COMMENT ON TABLE schema_migrations IS "
        "'已应用的迁移文件（version=文件名，checksum=sha256 前 16 位）';\n"
    ),
}
