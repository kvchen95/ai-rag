SHELL := /bin/bash
# WSL 里通常没有 docker 客户端，可直接覆盖：make test-api DOCKER="/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
DOCKER ?= docker
ENV_FILE := $(if $(wildcard .env),.env,.env.example)
COMPOSE := $(DOCKER) compose --env-file $(ENV_FILE) -f infra/docker-compose.yml
CORE_SERVICES := postgres qdrant api worker web
MODEL_SERVICES := embedding rerank

# api 测试镜像（Dockerfile 的 dev stage，装了 pytest）与集成测试用的 PG 地址
API_DEV_IMAGE := ai-rag-api:dev
POSTGRES_USER ?= ai_rag
POSTGRES_PASSWORD ?= ai_rag_dev_password
POSTGRES_DB ?= ai_rag
POSTGRES_PORT ?= 5432
# 集成测试容器直接接进 compose 网络，用服务名连库（不经宿主端口）
API_TEST_NETWORK ?= ai-rag_default
API_TEST_DSN ?= postgresql+psycopg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@postgres:5432/$(POSTGRES_DB)

.DEFAULT_GOAL := help
.PHONY: help dev up up-models down logs ps build pull config health wait reset qdrant-init psql smoke web-build prints-env test-api test-api-db api-dev-image db-migrate db-status index-rebuild index-rebuild-dry

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
	@echo "  make qdrant-init # 建/校验 kb_chunks 集合（dense 512 + bm25 sparse IDF + payload 索引）"
	@echo "  make db-migrate  # 应用 db/migrations/*.sql（api 容器入口也会自动跑）"
	@echo "  make db-status   # 看迁移台账：已应用/待应用/被改过的"
	@echo "  make index-rebuild      # 从 PG content_text 全量重建 Qdrant dense+bm25"
	@echo "  make index-rebuild-dry  # 只看重建计划，不写不编码正文"
	@echo "  make psql        # 进 PG 交互 shell"
	@echo "  make web-build   # 在 Docker 里构建前端产物到 web/dist（nginx 挂载它）"
	@echo "  make test-api    # 跑 api/tests（单元用例；读 PG 的集成用例自动跳过）"
	@echo "  make test-api-db # 同上，且把宿主 PG 指给集成用例（host.docker.internal）"
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
	@$(COMPOSE) exec -T api python scripts/qdrant_init.py

psql:
	@bash scripts/dev.sh psql

reset:
	@bash scripts/dev.sh reset

# --- schema / index（DA1）-----------------------------------------------------
# 迁移只在 api 容器里跑：宿主没有 psycopg，容器里装了（见 infra/api/requirements.txt）。
db-migrate:
	@$(COMPOSE) exec -T api python scripts/db_migrate.py

db-status:
	@$(COMPOSE) exec -T api python scripts/db_migrate.py --status

# 从 PG content_text 全量重建 Qdrant：dense 走 TEI，bm25 走 Qdrant 服务端推理。
# 全量重建会编码每一条切片，先在 dry-run 里确认条数再动手。
index-rebuild-dry:
	@$(COMPOSE) exec -T api python scripts/qdrant_rebuild.py --dry-run

index-rebuild:
	@$(COMPOSE) exec -T api python scripts/qdrant_rebuild.py

# Build the frontend inside Docker so the host needs no node/pnpm.
web-build:
	@docker run --rm -v "$(CURDIR)/web:/app" -w /app node:24-bookworm-slim \
		sh -c "corepack enable && pnpm install --frozen-lockfile && pnpm build"
	@echo "web/dist rebuilt -> nginx container picks it up on next start"

# --- api tests ---------------------------------------------------------------
# 宿主不需要 python 环境：用 Dockerfile 的 dev stage 跑 pytest。
api-dev-image:
	@$(DOCKER) build -q --target dev -t $(API_DEV_IMAGE) -f infra/api/Dockerfile . >/dev/null
	@echo "built $(API_DEV_IMAGE)"

test-api: api-dev-image
	@$(DOCKER) run --rm -e RUN_MIGRATIONS=0 $(API_DEV_IMAGE) \
		pytest -c api/pytest.ini

# 集成用例要真 PG：把容器接进 compose 网络，用服务名 postgres 直连。
test-api-db: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" $(API_DEV_IMAGE) \
		pytest -c api/pytest.ini

# --- B2: 身份 / 前置过滤 / 时间归一 -------------------------------------------
# 变异测试：把关键判据改坏，确认对应用例真的会红（跑完自动还原）。
# 需要真 PG + 真 Qdrant，所以接进 compose 网络。
test-api-parity: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" \
		-e QDRANT_URL="http://qdrant:6333" $(API_DEV_IMAGE) \
		pytest -c api/pytest.ini api/tests/test_filter_parity.py

b2-mutation: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" \
		-e QDRANT_URL="http://qdrant:6333" $(API_DEV_IMAGE) \
		python scripts/b2_mutation_check.py

# 执行计划：灌 20000 行 → 打四种身份形态的过滤计划 → 清理。
b2-explain:
	@bash scripts/b2_explain.sh
