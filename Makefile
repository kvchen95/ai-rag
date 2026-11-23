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
.PHONY: help dev up up-models down logs ps build pull config health wait reset qdrant-init psql smoke web-build prints-env test-api test-api-db api-dev-image db-migrate db-status index-rebuild index-rebuild-dry samples scan doc verify-scan-api promote rollback vector-sync reconcile reconcile-dry reclaim reclaim-dry ingest-status verify-a3 verify-a3-api verify-a3-migration test-a3 test-a3-vectors test-a3-flip test-a3-sync samples-layout verify-parse a2-mutation test-a2

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
	@echo "  make samples     # 生成开发期样本 PDF（samples/，字节确定，已存在则不动）"
	@echo "  make scan        # 扫 samples/：added/changed/skipped/failed"
	@echo "  make doc DOC_KEY=policy/travel-cn  # 查文档入库状态"
	@echo "  make verify-scan-api  # 实测 /api/v1/ingest/scan 的 200/404/409"
	@echo ""
	@echo "  A2（§3.2–§3.5）— 版面解析 / 表格 / 图表 / 父子切片"
	@echo "  make samples-layout  # 生成 8 份版面夹具（合并单元格/双栏/旋转/扫描页/图表）"
	@echo "  make verify-parse    # 真 PG 验收：解析->切片->写入->读回（36 条断言）"
	@echo "  make a2-mutation     # 27 条判据逐个改坏，确认对应用例真的会红"
	@echo "  make test-a2         # A2 的 12 个用例文件（含落库往返）"
	@echo ""
	@echo "  A3（§3.6、§4）— 双向量入库 / 翻牌 / 同步 / 回收"
	@echo "  make promote VERSION_ID='policy/travel-cn#a91f'  # 索引+翻牌+同步+对账"
	@echo "  make rollback VERSION_ID='policy/travel-cn#a91f' # 24h 内回滚"
	@echo "  make vector-sync    # 把期望状态推给 Qdrant（失败退避重试）"
	@echo "  make reconcile      # 两边对账并按 PG 修好（有残留则非零退出）"
	@echo "  make reclaim-dry    # 看有多少旧版本过窗口（24h）"
	@echo "  make ingest-status  # 版本/切片/同步/漂移一眼看（双 active 即非零）"
	@echo "  make verify-a3      # 端到端演练（真 PG/Qdrant/TEI，8 步）"
	@echo "  make verify-a3-api  # promote/reconcile 的 404/409/422 契约实测"
	@echo "  make test-a3        # A3 集成用例（需真 PG + Qdrant + embedding）"
	@echo ""
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

# --- 摄取（A1）----------------------------------------------------------------
# 样本 PDF 由脚本生成（stdlib，字节确定），随仓库走：`samples/{doc_key}.pdf`（附录 B）。
samples:
	@docker compose exec -T api python scripts/make_samples.py

# 扫 samples/：新文档/变更/跳过/失败。并发扫第二次返回 409。
scan:
	@curl -sS -X POST http://127.0.0.1:$${API_PORT:-8000}/api/v1/ingest/scan | python3 -m json.tool

# 查某文档的入库状态（doc_key 里带 `/`，quoted 传）。
doc:
	@test -n "$(DOC_KEY)" || (echo "usage: make doc DOC_KEY=policy/travel-cn" && exit 1)
	@curl -sS "http://127.0.0.1:$${API_PORT:-8000}/api/v1/documents/$(DOC_KEY)" | python3 -m json.tool

# 契约实测：200 / 404 / 409（含另一个进程持锁）。宿主跑，不需要容器重启。
verify-scan-api:
	@python3 scripts/verify_scan_api.py

# --- A3：双向量入库 / 翻牌 / 同步 / 回收（§3.6、§4）---------------------------
# 版本转正：索引（dense+bm25）-> 翻牌 -> 同步 -> 对账。VERSION_ID 必填。
promote:
	@test -n "$(VERSION_ID)" || (echo "usage: make promote VERSION_ID='policy/travel-cn#a91f'" && exit 1)
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py promote "$(VERSION_ID)"

# 24h 内回滚到旧版。
rollback:
	@test -n "$(VERSION_ID)" || (echo "usage: make rollback VERSION_ID='policy/travel-cn#a91f'" && exit 1)
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py rollback "$(VERSION_ID)"

# 消费期望状态（翻牌后把 payload.status 推给 Qdrant；失败退避重试）。
vector-sync:
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py sync

# 两边对账：默认按 PG 修好。有残留差异 -> 非零退出。
reconcile:
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py reconcile

# 只看差异，不改任何东西。
reconcile-dry:
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py reconcile --dry-run

# 回收过窗口的旧版本（切片 + 向量点 + 切片图）。先 dry-run 看条数。
reclaim-dry:
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py reclaim --dry-run

reclaim:
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py reclaim

# 一眼看现状：版本/切片状态分布、待同步数、未修漂移、集合点数。双 active 即非零退出。
ingest-status:
	@$(COMPOSE) exec -T api python scripts/a3_ingest.py status

# 端到端演练（真 CLI/真 PG/真 Qdrant/真 TEI，8 步走完 §4）。宿主跑。
verify-a3:
	@python3 scripts/verify_a3_pipeline.py

# 契约实测：promote 的 404/409/422、reconcile 的 409。需要先起一个 api。
verify-a3-api:
	@python3 scripts/verify_a3_api.py

# 003 迁移在**全新库**上可复现：建 ai_rag_a3verify -> apply -> 逐条约束验 -> drop。
# 顺带回归 001 的 ux_active_version / activated_at 约束没被 003 弄坏。宿主跑。
verify-a3-migration:
	@python3 scripts/verify_a3_migration.py

# --- A2：版面解析 / 表格 / 图表 / 父子切片（§3.2–§3.5）-----------------------
# 8 份版面夹具（stdlib 手写 PDF，字节确定）。测试会自己现造到 tmp，这条是给人看的。
samples-layout:
	@python3 scripts/make_layout_samples.py --root samples

# 真 PG 验收：解析 -> 切片 -> 写入 -> 读回断言（含表格三表示往返、OCR 来源标记）。宿主跑。
# DSN 优先取 $DATABASE_URL，可用 --dsn 覆盖。
verify-parse:
	@python3 scripts/verify_parse_pipeline.py $(if $(DSN),--dsn "$(DSN)",)

# 27 条判据逐个改坏，确认对应用例真的会红（跑完自动还原）。
# 需要 pdfplumber/pypdfium2/Pillow；两条落库判据还需要真 PG（AI_RAG_TEST_DATABASE_URL）。
a2-mutation:
	@python3 scripts/a2_mutation_check.py

# A2 用例：单元 + 夹具级 e2e + 落库往返（集成用例没 DSN 就 skip，不假装跑过）。
test-a2: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" \
		$(API_DEV_IMAGE) pytest -c api/pytest.ini api/tests/test_parse_chunking.py \
		api/tests/test_parse_dates.py api/tests/test_parse_figure_render.py \
		api/tests/test_parse_figures.py api/tests/test_parse_geometry.py \
		api/tests/test_parse_layout.py api/tests/test_parse_merged_table.py \
		api/tests/test_parse_ocr.py api/tests/test_parse_pipeline_e2e.py \
		api/tests/test_parse_persist_integration.py \
		api/tests/test_parse_tables.py api/tests/test_parse_zero_width.py

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

# --- A3 集成用例 --------------------------------------------------------------
# 需要真 PG + 真 Qdrant + 真 embedding 三个都通。缺一个就会 skip（不假装跑过），
# 所以这里显式把三个地址都传进去，避免"跑完了但全是 skipped"这种假绿。
# 用例自己建/删 a3_test_* 集合，不碰共享 kb_chunks。
test-a3: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" \
		-e QDRANT_URL="http://qdrant:6333" \
		-e EMBEDDING_BASE_URL="http://embedding:8000/v1" \
		-e EMBEDDING_API_KEY=local \
		-e EMBEDDING_MODEL="$${EMBEDDING_MODEL:-BAAI/bge-small-zh-v1.5}" \
		-e EMBEDDING_DIM="$${EMBEDDING_DIM:-512}" \
		$(API_DEV_IMAGE) pytest -c api/pytest.ini api/tests/a3

# 只跑双向量入库那一组（最快能证明"一个点同时有 dense+bm25"）。
test-a3-vectors: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" \
		-e QDRANT_URL="http://qdrant:6333" \
		-e EMBEDDING_BASE_URL="http://embedding:8000/v1" \
		-e EMBEDDING_API_KEY=local \
		-e EMBEDDING_DIM="$${EMBEDDING_DIM:-512}" \
		$(API_DEV_IMAGE) pytest -c api/pytest.ini api/tests/a3/test_vectors.py

# 只跑翻牌事务那一组（无双 active / 事务性 / 并发）。
test-a3-flip: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" \
		$(API_DEV_IMAGE) pytest -c api/pytest.ini api/tests/a3/test_flip.py

# 只跑同步 + 对账 + 回收（不编码正文，只要 PG + Qdrant，最快）。
test-a3-sync: api-dev-image
	@$(DOCKER) run --rm --network $(API_TEST_NETWORK) -e RUN_MIGRATIONS=0 \
		-e AI_RAG_TEST_DATABASE_URL="$(API_TEST_DSN)" \
		-e QDRANT_URL="http://qdrant:6333" \
		-e EMBEDDING_BASE_URL="http://embedding:8000/v1" \
		-e EMBEDDING_API_KEY=local \
		$(API_DEV_IMAGE) pytest -c api/pytest.ini api/tests/a3/test_sync.py api/tests/a3/test_reconcile_reclaim.py
