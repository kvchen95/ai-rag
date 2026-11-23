#!/usr/bin/env bash
# api 容器入口：先确保 schema，再起服务。
#
# 迁移的唯一入口是 scripts/db_migrate.py（db/ 卡的产物），这里只调用它，
# 不自己再实现一套——两套 runner 写同一张 schema_migrations，迟早出现
# "一边记录过、另一边没记录"的分裂。
#
# RUN_MIGRATIONS=0 可以跳过（例如 CI 里 schema 由别处准备好）。
set -euo pipefail

export PYTHONPATH="/app:${PYTHONPATH:-}"

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  attempt=0
  until python /app/scripts/db_migrate.py; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 15 ]; then
      echo "entrypoint: migrations still failing after $attempt attempts, giving up" >&2
      exit 1
    fi
    echo "entrypoint: migration attempt $attempt failed, retrying in 2s" >&2
    sleep 2
  done
fi

exec "$@"
