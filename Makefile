SHELL := /bin/bash
ENV_FILE := $(if $(wildcard .env),.env,.env.example)
COMPOSE := docker compose --env-file $(ENV_FILE) -f infra/docker-compose.yml
CORE_SERVICES := postgres qdrant api worker web
MODEL_SERVICES := embedding rerank

.DEFAULT_GOAL := help
.PHONY: help dev up up-models down logs ps build pull config health wait reset qdrant-init psql smoke web-build prints-env

help:
	@echo "ai-rag local dev — targets"
	@echo ""
	@echo "  make dev         # 一键起全栈（核心服务 + embedding/rerank 本地 CPU 推理）"
	@echo "  make up          # 起核心服务（PG/Qdrant/api/worker/web）"
	@echo "  make up-models   # 只起 embedding/rerank 推理服务"
	@echo "  make health      # 健康检查（容器状态 + Qdrant + PG + API + Web + 模型）"
	@echo "  make smoke       # 端到端冒烟（embedding -> Qdrant 稠密+BM25 -> RRF -> rerank）"
	@echo "  make logs        # 跟随全部容器日志"
	@echo "  make ps          # 查看容器状态"
	@echo "  make down        # 停容器，保留数据卷"
	@echo "  make reset       # 停容器并删除数据卷（需输入 RESET 确认）"
	@echo "  make config      # 校验 compose 配置"
	@echo "  make qdrant-init # 建 kb_chunks 集合（dense 512 + bm25 sparse IDF）"
	@echo "  make psql        # 进 PG 交互 shell"
	@echo "  make web-build   # 在 Docker 里构建前端产物到 web/dist（nginx 挂载它）"
	@echo ""
	@echo "Using env file: $(ENV_FILE)"

dev: up up-models wait health

up:
	$(COMPOSE) up -d --build $(CORE_SERVICES)

up-models:
	$(COMPOSE) up -d $(MODEL_SERVICES)

wait:
	@bash scripts/dev.sh wait

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
	@$(COMPOSE) config >/dev/null && echo "compose config OK ($(ENV_FILE))"

health:
	@bash scripts/dev.sh health

smoke:
	@bash scripts/dev.sh smoke

qdrant-init:
	@bash scripts/dev.sh qdrant-init

psql:
	@bash scripts/dev.sh psql

reset:
	@bash scripts/dev.sh reset

# Build the frontend inside Docker so the host needs no node/pnpm.
web-build:
	@docker run --rm -v "$(CURDIR)/web:/app" -w /app node:24-bookworm-slim \
		sh -c "corepack enable && pnpm install --frozen-lockfile && pnpm build"
	@echo "web/dist rebuilt -> nginx container picks it up on next start"
