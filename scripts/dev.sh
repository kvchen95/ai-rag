#!/usr/bin/env bash
# ai-rag local dev helper: up / down / logs / reset / health / config / qdrant-init
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE=".env"
if [[ ! -f .env ]]; then
  ENV_FILE=".env.example"
  echo "[dev.sh] .env not found; using .env.example (copy to .env to customize)"
fi

COMPOSE=(docker compose --env-file "$ENV_FILE" -f infra/docker-compose.yml)
CORE_SERVICES=(postgres qdrant api worker web)
MODEL_SERVICES=(embedding rerank)

usage() {
  cat <<'USAGE'
Usage: scripts/dev.sh <command>

Commands:
  up           Build and start core services (postgres, qdrant, api, worker, web)
  up-models    Build and start the local CPU model services (embedding, rerank)
  dev          up + up-models + wait + health (one-shot full stack)
  down         Stop services (keep volumes)
  logs         Tail logs (all services)
  ps           Show compose status
  wait         Block until all started services report healthy (max 300s)
  health       Check containers + Qdrant + PG + API + Web (+ models if running)
  smoke        End-to-end smoke: embedding -> Qdrant dense+BM25 -> RRF -> rerank
  config       Validate compose config
  qdrant-init  Create/verify the kb_chunks collection (dense 512 + bm25 sparse, IDF) + payload indexes
  db-migrate   Apply db/migrations/*.sql (same runner the api entrypoint uses)
  db-status    Show the migration ledger (applied / pending / drifted)
  index-rebuild      Rebuild Qdrant dense+bm25 from PG content_text
  index-rebuild-dry  Show the rebuild plan without writing anything
  psql         Open a psql shell inside the postgres container
  reset        Stop and remove volumes (destructive, asks confirmation)
USAGE
}

load_env() {
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
}

http_code() { curl -s -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null || echo 000; }

cmd_up() { load_env; "${COMPOSE[@]}" up -d --build "${CORE_SERVICES[@]}"; }

cmd_up_models() { load_env; "${COMPOSE[@]}" up -d --build "${MODEL_SERVICES[@]}"; }

cmd_wait() {
  load_env
  echo "== waiting for healthchecks (max 300s) =="
  local deadline=$((SECONDS + 300)) pending
  while (( SECONDS < deadline )); do
    pending=$("${COMPOSE[@]}" ps --format '{{.Name}} {{.State}} {{.Health}}' 2>/dev/null \
      | awk '$2=="running" && $3!="healthy" {print $1" ("$3")"}')
    if [[ -z "$pending" ]]; then echo "all running services healthy"; return 0; fi
    sleep 5
  done
  echo "timeout; still not healthy:" >&2
  "${COMPOSE[@]}" ps >&2
  return 1
}

cmd_health() {
  load_env
  local fail=0
  echo "== docker compose ps =="
  "${COMPOSE[@]}" ps
  echo
  echo "== qdrant healthz =="
  if curl -fsS -m 5 "http://127.0.0.1:${QDRANT_HTTP_PORT:-6333}/healthz"; then echo; else echo "FAILED"; fail=1; fi
  echo
  echo "== postgres =="
  if "${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER:-ai_rag}" -d "${POSTGRES_DB:-ai_rag}"; then
    "${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:-ai_rag}" -d "${POSTGRES_DB:-ai_rag}" -tAc \
      "select 'postgres ok, current_user=' || current_user || ', db=' || current_database();"
  else
    fail=1
  fi
  echo
  echo "== api healthz =="
  local body code
  body=$(curl -fsS -m 5 "http://127.0.0.1:${API_PORT:-8000}/healthz" 2>/dev/null) || body=""
  if [[ -n "$body" ]]; then echo "$body"; else echo "FAILED (curl http://127.0.0.1:${API_PORT:-8000}/healthz)"; fail=1; fi
  echo
  echo "== web =="
  code=$(http_code "http://127.0.0.1:${WEB_PORT:-5173}/healthz")
  echo "HTTP $code"
  [[ "$code" == "200" ]] || fail=1
  echo
  local mfail=0
  if "${COMPOSE[@]}" ps --format '{{.Service}}' | grep -qx embedding; then
    echo "== model services =="
    local ecode rcode
    ecode=$(http_code "http://127.0.0.1:${EMBEDDING_PORT:-7997}/health")
    rcode=$(http_code "http://127.0.0.1:${RERANK_PORT:-7999}/health")
    echo "embedding (${EMBEDDING_MODEL:-BAAI/bge-small-zh-v1.5}) HTTP $ecode"
    echo "rerank    (${RERANK_MODEL:-BAAI/bge-reranker-base})  HTTP $rcode"
    [[ "$ecode" == "200" ]] || { echo "  embedding not ready / not healthy"; mfail=1; }
    [[ "$rcode" == "200" ]] || { echo "  rerank not ready / not healthy"; mfail=1; }
    if (( mfail != 0 )); then
      echo "  hint: first start downloads weights (embedding ~100MB, rerank ~1.1GB); check 'docker logs ai-rag-embedding ai-rag-rerank'"
      echo "  set HF_ENDPOINT=https://hf-mirror.com in .env if huggingface.co is unreachable"
      fail=1
    fi
  else
    echo "== model services: not running (make up-models) =="
  fi
  echo
  echo "== web -> api proxy (/api/healthz) =="
  body=$(curl -fsS -m 5 "http://127.0.0.1:${WEB_PORT:-5173}/api/healthz" 2>/dev/null) || body=""
  if [[ -n "$body" ]]; then echo "$body" | head -c 200; echo; else echo "FAILED"; fail=1; fi
  echo
  if (( fail == 0 )); then echo "HEALTH: OK"; else echo "HEALTH: FAILED"; return 1; fi
}

cmd_smoke() { load_env; python3 "$ROOT_DIR/scripts/smoke.py" "$@"; }

cmd_qdrant_init() {
  load_env
  # 集合形状与 payload 索引只在一处定义：scripts/qdrant_common.py。
  "${COMPOSE[@]}" exec -T api python scripts/qdrant_init.py "$@"
}

cmd_db_migrate() { load_env; "${COMPOSE[@]}" exec -T api python scripts/db_migrate.py "$@"; }

cmd_db_status() { load_env; "${COMPOSE[@]}" exec -T api python scripts/db_migrate.py --status "$@"; }

cmd_index_rebuild() { load_env; "${COMPOSE[@]}" exec -T api python scripts/qdrant_rebuild.py "$@"; }

cmd_psql() { load_env; "${COMPOSE[@]}" exec postgres psql -U "${POSTGRES_USER:-ai_rag}" -d "${POSTGRES_DB:-ai_rag}"; }

cmd_reset() {
  load_env
  echo "WARNING: this removes containers AND named volumes (pg_data/qdrant_data/app_data/model_cache)."
  read -r -p "Type RESET to continue: " confirm
  if [[ "$confirm" != "RESET" ]]; then echo "aborted"; exit 1; fi
  "${COMPOSE[@]}" down -v --remove-orphans
}

main() {
  local cmd="${1:-health}"
  case "$cmd" in
    up) cmd_up ;;
    up-models) cmd_up_models ;;
    dev) cmd_up; cmd_up_models; cmd_wait; cmd_health ;;
    down) load_env; "${COMPOSE[@]}" down ;;
    logs) load_env; "${COMPOSE[@]}" logs -f --tail=200 ;;
    ps) load_env; "${COMPOSE[@]}" ps ;;
    wait) cmd_wait ;;
    health) cmd_health ;;
    config) load_env; "${COMPOSE[@]}" config >/dev/null && echo "compose config OK ($ENV_FILE)" ;;
    qdrant-init) cmd_qdrant_init "${@:2}" ;;
    db-migrate) cmd_db_migrate "${@:2}" ;;
    db-status) cmd_db_status "${@:2}" ;;
    index-rebuild) cmd_index_rebuild "${@:2}" ;;
    index-rebuild-dry) cmd_index_rebuild --dry-run "${@:2}" ;;
    smoke) cmd_smoke ;;
    psql) cmd_psql ;;
    reset) cmd_reset ;;
    help|-h|--help) usage ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
