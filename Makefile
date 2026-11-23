SHELL := /bin/bash
ENV_FILE := $(if $(wildcard .env),.env,.env.example)
COMPOSE := docker compose --env-file $(ENV_FILE) -f infra/docker-compose.yml

.PHONY: help up down logs ps health reset build pull config

help:
	@echo "Targets: up down logs ps health reset build pull config"
	@echo "Using env file: $(ENV_FILE)"

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

build:
	$(COMPOSE) build

pull:
	$(COMPOSE) pull

config:
	$(COMPOSE) config >/dev/null && echo "compose config OK ($(ENV_FILE))"

health:
	@set -euo pipefail; \
	echo "== docker compose ps =="; \
	$(COMPOSE) ps; \
	echo; \
	echo "== qdrant healthz =="; \
	QDRANT_PORT=$${QDRANT_HTTP_PORT:-6333}; \
	curl -fsS "http://127.0.0.1:$${QDRANT_PORT}/healthz" && echo; \
	echo; \
	echo "== postgres pg_isready =="; \
	$(COMPOSE) exec -T postgres pg_isready -U $${POSTGRES_USER:-ai_rag} -d $${POSTGRES_DB:-ai_rag}; \
	echo; \
	echo "== api health =="; \
	API_PORT=$${API_PORT:-8000}; \
	curl -fsS "http://127.0.0.1:$${API_PORT}/healthz" && echo

reset:
	@echo "WARNING: this removes containers and named volumes (pg/qdrant/app data)."
	@read -r -p "Type RESET to continue: " confirm; \
	if [ "$$confirm" != "RESET" ]; then echo "aborted"; exit 1; fi
	$(COMPOSE) down -v --remove-orphans
