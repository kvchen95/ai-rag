#!/usr/bin/env bash
# 过滤条件的执行计划：灌 20000 行种子 → 打四种身份形态的计划 → 清理种子。
#
# 为什么要在真表上灌数据：小表一律 Seq Scan，看不出索引有没有被用上，
# 那样的"执行计划"什么也证明不了。种子与清理都写成独立脚本（不塞 heredoc），
# 保证中途失败也能用 `python scripts/b2_explain_cleanup.py` 手动收尾。
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKER="${DOCKER:-docker}"
ENV_FILE=".env.example"
[ -f .env ] && ENV_FILE=".env"
COMPOSE=("$DOCKER" compose --env-file "$ENV_FILE" -f infra/docker-compose.yml)

ROWS="${ROWS:-20000}"

"${COMPOSE[@]}" exec -T api python scripts/b2_explain_seed.py --rows "$ROWS" --keep
set +e
"${COMPOSE[@]}" exec -T api python scripts/b2_explain_filters.py
rc=$?
set -e
"${COMPOSE[@]}" exec -T api python scripts/b2_explain_cleanup.py
exit $rc
