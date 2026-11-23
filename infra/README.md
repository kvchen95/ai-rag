# infra — 本地 Docker 开发环境（PG + Qdrant + 模型服务 + api/worker/web）

一套 `docker compose` 起全栈，宿主机只依赖 Docker Desktop。所有服务（含 embedding / rerank 的
CPU 推理）都在容器里跑，本机不装 Postgres / Qdrant / Python 依赖。

## 一键起停

```bash
cp .env.example .env          # 首次：按需改端口/模型网关
make dev                      # 起全栈 + 等健康 + 健康检查
make health                   # 只做健康检查
make smoke                    # 端到端冒烟：embedding -> Qdrant 稠密+BM25 -> RRF -> rerank
make logs                     # 跟随日志
make down                     # 停容器，保留数据卷
make reset                    # 删容器 + 数据卷（要输入 RESET）
```

也可以直接用 compose（仓库根有 `docker-compose.yml` 只做 include，方便裸 `docker compose up -d`）：

```bash
docker compose up -d
docker compose ps
```

`scripts/dev.sh` 提供同一组命令（`up / up-models / dev / down / logs / ps / wait / health / smoke / config / qdrant-init / psql / reset`）。

## 服务与端口

| 服务 | 镜像 | 宿主机端口 | 说明 |
|---|---|---|---|
| postgres | postgres:16-alpine | 5432 | PG 是唯一真相，数据卷 `ai_rag_pg_data` |
| qdrant | qdrant/qdrant:v1.15.3 | 6333 (HTTP) / 6334 (gRPC) | 稠密 + BM25 稀疏同集合，卷 `ai_rag_qdrant_data` |
| embedding | ghcr.io/huggingface/text-embeddings-inference:cpu-1.6 | 7997 | BAAI/bge-small-zh-v1.5，512 维，OpenAI 兼容 `/v1/embeddings` |
| rerank | 同上 | 7998 | BAAI/bge-reranker-base，TEI 原生 `POST /rerank {"query","texts"}` |
| api | build `infra/api`（占位 FastAPI） | 8000 | `/healthz` 实测 PG + Qdrant |
| worker | build `infra/worker`（占位轮询进程） | — | 每 30s 打一次 PG/Qdrant 心跳，健康标记 `/tmp/worker.ready` |
| web | nginx:1.27-alpine | 5173 | 静态 `web/dist`，并把 `/api/` 反代到 api:8000（SSE 不缓冲） |

Qdrant 1.15.3 是**硬性下限**：自托管镜像要 1.15.3 起才支持服务端 `Qdrant/bm25` 稀疏推理；
1.13.x / 1.15.1 会在 upsert 时报 `InferenceService is not initialized`。

## 模型服务

- 首次启动从 Hugging Face 下载权重：embedding ~100MB，rerank ~1.1GB，缓存在命名卷 `ai_rag_model_cache`
  （`HF_HOME=/data`），**重启不重下**。健康检查 `start_period` 300s / 420s，`make wait` 最多等 300s。
- 内网地址：`http://embedding:8000/v1`（embeddings）、`http://rerank:8000`（`/rerank`）。
- 想让容器走镜像站：`.env` 里设 `HF_ENDPOINT=https://hf-mirror.com`。
- 想换外部网关：覆盖 `.env` 的 `EMBEDDING_BASE_URL / EMBEDDING_API_KEY / RERANK_BASE_URL / RERANK_API_KEY`，
  甚至可以把 `embedding`/`rerank` 两个服务从 compose 里删掉，api/worker 只认环境变量。
- 模型改了维度（不是 512）时：`EMBEDDING_DIM` 要同步改，并 `make reset`（或手工删 `kb_chunks` 集合）重建集合。

## 数据与持久化

命名卷固定名字，`docker compose down` 不会删：

- `ai_rag_pg_data` → `/var/lib/postgresql/data`
- `ai_rag_qdrant_data` → `/qdrant/storage`
- `ai_rag_app_data` → api/worker 的 `/app/data`
- `ai_rag_model_cache` → TEI 的 `/data`（HF 权重）

实测：往 PG 写一行、向 Qdrant 写一个点，`up -d --force-recreate` 之后数据都在。
`make reset` 才会删卷，必须输入 `RESET`。

## 热更新

- `api` / `worker` 把仓库根 bind mount 到 `/app`；改 Python 代码后 `docker compose restart api worker` 生效
  （占位镜像没开 `--reload`，接真实现时建议加 `uvicorn --reload`）。
- `web` 挂的是构建产物 `web/dist`（宿主无需 node）：`make web-build` 在 `node:24-bookworm-slim` 里
  `pnpm install && pnpm build`，然后 `docker compose restart web`。
  想要 vite HMR，就在宿主 `cd web && pnpm dev`（5173 已被容器占用，可临时停 web 或改端口）。

## 验收命令

```bash
curl -s http://127.0.0.1:6333/healthz          # healthz check passed
curl -s http://127.0.0.1:8000/healthz          # status=ok, postgres/qdrant 均 true
curl -s http://127.0.0.1:5173/api/healthz      # 经 web 反代到 api
curl -s http://127.0.0.1:7997/info             # 模型信息，应显示 model_id=BAAI/bge-small-zh-v1.5
docker compose exec postgres psql -U ai_rag -d ai_rag -c 'select 1'
python3 scripts/smoke.py                       # 全链路冒烟
```

## 坑与约定

- **端口** 7998 在本机曾被 Docker Desktop 残留占用（`Bind ... port is already allocated`，Windows netstat 看不到）。
  rerank 默认改到 7999；如你机器上 7998 空闲，改 `.env` 的 `RERANK_PORT=7998` 即可。
- 镜像直连 `registry-1.docker.io` 在本机会 EOF，拉镜像用镜像站前缀（如 `docker.m.daocloud.io/...`）后 `docker tag` 成本地名。
- 不要把 `.env` 提交（里面会有真实 api_key）。`.env.example` 只放占位。
- 容器内进程以 root 跑，bind mount 目录里可能留下 root 属主文件；仓库已 `chmod a+rwX`，遇到写入失败先查属主。
- `infra/_stale_web_dir_rootown/` 是早期 bind mount 误建成目录留下的空目录（root 属主），
  需要 `sudo rm -rf infra/_stale_web_dir_rootown` 清掉，与运行无关。
