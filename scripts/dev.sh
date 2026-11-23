#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE=".env"
if [[ ! -f .env ]]; then
  ENV_FILE=".env.example"
  echo "[dev.sh] .env not found; using .env.example (copy to .env to customize)"
fi

COMPOSE=(docker compose --env-file "$ENV_FILE" -f infra/docker-compose.yml)

usage() {
  cat <<'USAGE'
Usage: scripts/dev.sh <command>

Commands:
  up        Build and start services
  down      Stop services (keep volumes)
  logs      Tail logs
  ps        Show compose status
  health    Check Qdrant/PG/API health
  reset     Stop and remove volumes (destructive, asks confirmation)
  config    Validate compose config
USAGE
}

load_env() {
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
}

cmd_health() {
  load_env
  echo "== docker compose ps =="
  "${COMPOSE[@]}" ps
  echo
  echo "== qdrant healthz =="
  curl -fsS "http://127.0.0.1:${QDRANT_HTTP_PORT:-6333}/healthz" && echo
  echo
  echo "== postgres pg_isready =="
  "${COMPOSE[@]}" exec -T postgres pg_isready -U "${POSTGRES_USER:-ai_rag}" -d "${POSTGRES_DB:-ai_rag}"
  echo
  echo "== api health =="
  curl -fsS "http://127.0.0.1:${API_PORT:-8000}/healthz" && echo
}

cmd_reset() {
  load_env
  echo "WARNING: this removes containers and named volumes (pg/qdrant/app data)."
  read -r -p "Type RESET to continue: " confirm
  if [[ "$confirm" != "RESET" ]]; then
    echo "aborted"
    exit 1
  fi
  "${COMPOSE[@]}" down -v --remove-orphans
}

main() {
  local cmd="${1:-help}"
  case "$cmd" in
    up)
      load_env
      "${COMPOSE[@]}" up -d --build
      ;;
    down)
      load_env
      "${COMPOSE[@]}" down
      ;;
    logs)
      load_env
      "${COMPOSE[@]}" logs -f --tail=200
      ;;
    ps)
      load_env
      "${COMPOSE[@]}" ps
      ;;
    health)
      cmd_health
      ;;
    reset)
      cmd_reset
      ;;
    config)
      load_env
      "${COMPOSE[@]}" config >/dev/null && echo "compose config OK ($ENV_FILE)"
      ;;
    help|-h|--help)
      usage
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main "$@"
