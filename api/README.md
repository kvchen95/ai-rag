# api/ — 后端服务骨架

在线链路的统一底座：配置加载、PG/Qdrant 客户端、结构化日志、错误分类（§11.3）、
审计写入、健康检查。业务分册（身份与过滤、检索、生成与校验）由后续卡挂进来，
这里只提供它们都要用的东西，不提前实现它们的逻辑。

## 目录

```
api/
  main.py            create_app()：唯一装配入口（uvicorn 用 --factory 调用）
  config.py          Settings：业务口径必须来自环境变量，基础设施参数给默认值
  observability.py   一行一条 JSON 的日志 + request_id 上下文
  middleware.py      纯 ASGI 中间件（写入/透传 request_id，记录访问日志）
  errors.py          §11.3 错误表：文案 / HTTP 状态 / 告警口径同源
  audit.py           qa_log 写入（身份快照 + 问答结果）与保留期清理
  clients/pg.py      psycopg 连接池、事务边界、探针
  clients/qdrant.py  Qdrant 探针与延迟创建的 SDK 句柄
  routers/health.py  /healthz、/livez
  tests/             单元用例 + 读真 PG 的集成用例
  pytest.ini         在仓库根目录执行 `pytest -c api/pytest.ini`
```

## 契约

### GET /healthz

依赖全部通 → `200`；任一不通 → `503`，并且**逐个依赖**给出 ok/耗时/错误：

```json
{
  "status": "ok",
  "service": "ai-rag-api",
  "version": "0.1.0",
  "env": "dev",
  "uptime_s": 9.864,
  "checks": {
    "postgres": {"ok": true, "latency_ms": 3.01, "error": null, "server_version": "16.16", "pool": {"size": 1, "idle": 1}},
    "qdrant": {"ok": true, "latency_ms": 5.45, "error": null, "collection": "kb_chunks", "collection_present": true, "collections": 1}
  },
  "config": {"gates": {"reject_none": 0.2, "reject_low": 0.4}, "...": "..."}
}
```

两条口径：

- **Qdrant 集合缺失不算依赖故障**。库是空的但服务是好的，首次入库前不能让 api
  被判成 unhealthy 反复重启；用 `collection_present` 单独报。
- **`config` 只在非生产环境返回**（`APP_ENV!=prod`）。它是排障用的配置指纹：
  换阈值、换模型之后 `curl /healthz` 直接看到跑的是哪套值，不用进容器翻环境变量。

`GET /livez` 只看进程本身，不碰 PG/Qdrant：下游慢了不该把上游一起重启。

### 错误响应

业务代码只抛 `AppError(code, detail=...)`，对外文案与状态码由 `errors.py` 的表决定
（§11.3 原表逐行落地）；`detail` 只进日志，不进响应体。

```json
{"error": {"code": "forbidden", "message": "无权查看", "request_id": "6f1c…"}}
```

| code | HTTP | 用户文案 | 告警 | 流内 fallback |
|---|---:|---|---|---|
| `auth_invalid` | 401 | 请重新登录 | 否 | 否 |
| `forbidden` | 403 | 无权查看 | 连续出现 | 否 |
| `no_evidence` | — | 未找到可验证依据 | 否 | 是 |
| `low_confidence` | — | 现有资料不足以下结论 | 否 | 是 |
| `unverifiable` | — | 本次不作结论，附原文 | 高于阈值 | 是 |
| `model_timeout` | 504 | 稍后重试 | 是 | 是 |
| `index_stale` | 503 | 稍后重试 | 是 | 是 |
| `internal` | 500 | 稍后重试 | 是 | 是 |
| `invalid_request` | 422 | 请求格式不正确 | 否 | 否 |

`invalid_request` 是**契约层扩展**：§11.3 的表只管业务错误，入参不合法既不该算
`internal`（会误触发告警），也不该借 `auth_invalid` 的文案。

"流内 fallback" 表示该码在 SSE 已开流之后以 `fallback` 事件下发（附录 C.2），
表里的 HTTP 状态只用于"还没开流"的时候。

### 审计（qa_log）

一次问答一条，表由 `db/migrations/001_init.sql` 建（见下"与 db/ 的分工"）。
应用层的必填与配对口径：

- `user_id` 必填：没有身份的审计回答不了"谁在什么时候看了什么"，第三道闸等于没做
  （表里该列可空，DB 拦不住，所以在这里拦）。
- 拒答必须带 `reject_reason`；`no_evidence`/`low_confidence` 只能配拒答判定；
  `unverifiable` 不能与 `gate2=supported` 并存。
- 只存切片编号与分数摘要，**不存完整原文**（§11.2）。
- 保留期 `AUDIT_RETENTION_DAYS`（默认 365），清理走 `AuditWriter.purge_expired()`。
- 写失败不抛：调用方拿到 `AuditResult(ok=False)`，日志里是一条 ERROR
  `audit_write_failed`。审计挂掉不能把一次成功的回答变成失败，但也不能没人知道。

## 配置

两类，规则写在 `config.py` 顶部：

- **必填、无代码默认值**（换值必须只改环境变量）：`DATABASE_URL`、`QDRANT_URL`、
  `QDRANT_COLLECTION`、`EMBEDDING_*`、`RERANK_*`、`GATE_REJECT_NONE`、
  `GATE_REJECT_LOW`、`RETRIEVAL_TOP_K`、`RERANK_TOP_N_MIN/MAX`、`MAX_HOPS`、
  `EVIDENCE_MAX_CHARS`。缺了就启动失败，报错点名缺哪个变量。
- **有默认值**（不改变业务语义）：连接池大小、连接/探测/模型调用超时、
  `QA_BUDGET_S`、`MAX_QUESTION_CHARS`、`QA_MAX_CONCURRENCY`、`AUDIT_RETENTION_DAYS`。

跨字段校验在加载时就做：`GATE_REJECT_NONE < GATE_REJECT_LOW`、
`RERANK_TOP_N_MIN <= RERANK_TOP_N_MAX <= RETRIEVAL_TOP_K`、配了网关必须有模型名。
`.env.example` 与必填项之间有一条测试盯着，防止"代码要某变量、示例没写"。

## 身份与前置过滤（§5）

三块，各自独立可测，串起来就是"能看什么由谁决定"：

| 模块 | 做什么 | 关键口径 |
|---|---|---|
| `identity.py` | 凭证 → 快照 | 401 有八种成因；无部门=空集（不是拒绝）；admin 自带审计标记 |
| `time_range.py` | 问句时间 → 绝对闭区间 | 缺年份补当前年并 `inferred=True`；换算不了就不加条件但要说明 |
| `filters.py` | 归一条件 → Qdrant filter + PG WHERE | **同一份条件渲染两条路**，不允许各写一遍 |

### 身份快照

`{user_id, depts, roles, admin}` 四字段，`frozen` 且禁扩展（§5.1）。三条写死的口径：

- **alg 只认配置里那一个**，不按 token 头部自选——后者是算法混淆漏洞；
  无 `exp` 的 token 拒收（等于一张长期通行证）。
- **`admin` 只认布尔与明确的字符串写法**，不用真值转换：`bool("false")` 是 `True`，
  一个把 admin 写成字符串的上游会把所有人变成管理员。
- **无部门/无角色 ≠ 拒绝**：`is_public_only` 为真时仍然能看公开文档。
  新入职没分配部门的人天天碰到这种情况，写成 403 就把人挡在门外了。

两种模式由 `AUTH_MODE` 选：`fixed_jwt`（默认，验签）与 `mock_header`（仅开发，
身份来自 `X-User-Id` 等请求头）。`mint_dev_token()` 给测试与 curl 签发 token，
生产环境直接抛错——把签发能力留在生产进程里就是一个"谁都能当"的入口。

`require_identity`（`deps.py`）是路由侧的唯入口：声明 `identity: IdentityDep`
就得到 401 映射、审计标记与日志，不用记得自己读 Authorization 头。

### 过滤表达式

形状（§5.2）：

```text
status = 'active'                                     # 视图里由 JOIN 保证
AND ( is_public OR allowed_depts && {} OR allowed_roles && {} )    # admin 时整组去掉
AND effective_date BETWEEN {} AND {} AND date_confidence = 'explicit'   # 有时间条件时
AND doc_key = ANY({})                                 # 请求带 scope 时
```

四条刻意写进代码的取舍：

- **部门/角色是"有交集"**（PG `&&`、Qdrant `MatchAny`），不是"被包含"。
  写成"用户部门必须在文档部门列表里"，多部门的人就漏——§5.2 点名了这个坑。
- **空集合的分支不生成**，而不是生成恒假条件。`MatchAny(any=[])` 在 Qdrant 里
  确实什么都不匹配（实测过），但依赖这个未成文的语义等于把正确性押在别人的实现细节上。
- **admin 去掉整条 ACL**，不是"放宽成 public 或全部"。后者会让 `is_public` 与 ACL
  的语义纠缠在一起，日后无法单独调整。
- **两条路的"生效"语义本来就不可能一一对应**：PG 的生效是关系性的（chunk 与
  version 双 active，靠 `v_active_chunks` 的 JOIN），Qdrant 的生效是扁平字段
  （`payload.status`）。所以能且只能要求**结果集合相同**，不能要求条件条数相同。
  `pg_where(source=...)` 把这件事显式化：走视图时不再写 `status`（视图里根本没这列，
  实测会报 `column c.status does not exist`），直连 `chunks` 时必须自己写。

### 版本兜底（§7.4）在过滤之外

前置过滤表达不了"切片所属版本是不是当前生效版本"——那两分钟的翻牌窗口里，
库里旧版本已下线、向量库里那些点还写着 `status=active`（实测：对拍数据里
`ch_oldver` 就是这种行）。`version_guard.drop_stale()` 在召回后逐条剔除：
拿候选的 `version_id` 回查当前生效版本，不一致就丢，查不到生效版本也丢（Fail-Closed）。

为什么不塞进过滤条件：那要先查一次全部生效版本号，把一次查询变成两次，
版本集合还会随语料规模增长，过滤条件跟着膨胀——而过滤条件长度直接吃查询计划。

### 一致性怎么证

`test_filter_parity.py` 在真 PG + 真 Qdrant 上灌同一批边界数据，用同一份
`RetrievalFilter` 两边各查一次，断言 chunk_id 集合相等。光相等不够（两边都漏 ACL
也会相等），所以每个身份形态再钉"该有/该无"几条。payload 由
`scripts/qdrant_common.build_payload` 生成，不在测试里手搓——手搓就是"测试与实现
各写一遍"，而两边不一致正是要防的事。

### 执行计划（20000 行实测）

```bash
make b2-explain   # 灌 20000 行 → 打四种身份形态的计划 → 清理
```

诚实结论：**带时间条件的那条走 `ix_chunks_date` 的 Bitmap Index Scan（1.1ms）；
只带 ACL 的那两条走 Seq Scan（2.5–9.2ms）**。分别试过加 `is_public` 索引、
把 depts/roles 改成部分索引、两者都加，三种组合的计划与耗时都没改善——
2 万行这个规模上 Seq Scan 是计划器的正确选择，不是缺索引。ACL 是
`is_public OR depts && OR roles &&` 三支 OR，要 BitmapOr 合并就得三支都能生成
bitmap，而 `is_public` 是低选择性布尔列。数据量上去（百万级）需要重新量一次。

## 与 db/ 的分工

schema 的唯一权威是 `db/migrations/*.sql` + `scripts/db_migrate.py`（DA1 卡）。
api 容器启动时由 `infra/api/entrypoint.sh` 调用该脚本（`RUN_MIGRATIONS=0` 可跳过），
**不自己实现第二套 runner**：两套 runner 写同一张 `schema_migrations`，迟早出现
"一边记录过、另一边没记录"的分裂。

与附录 A.4 的差异（以 `db/migrations/001_init.sql` 为准）：
`qa_log` 多了身份快照列 `user_id`/`user_depts`/`user_roles`/`is_admin`（§11.2 要求，
A.4 的字段表没列），少一个 `request_id`。审计行与日志的连接点是
`audit_written` 这条日志——它同时带 `audit_id` 与 `request_id`。

## 跑起来

```bash
# 容器内（推荐，compose 已经配好全部环境变量）
docker compose up -d --build api
curl -s http://127.0.0.1:8000/healthz | python3 -m json.tool

# 宿主侧（需要自己给齐必填变量，见 api/tests/conftest.py 的 BASE_ENV）
uvicorn api.main:create_app --factory --app-dir . --port 8000 --env-file your.env
```

## 测试

```bash
make test-api      # 单元用例（Dockerfile 的 dev stage，宿主不需要 python 环境）
make test-api-db   # 追加读真 PG 的集成用例（容器接进 compose 网络，用服务名连库）
```

WSL 里没有 docker 客户端时：`ln -sf "/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe" ~/.local/bin/docker`，
再把 `~/.local/bin` 放进 PATH（Makefile 支持 `DOCKER=...` 覆盖）。

集成用例会先跑 `scripts/db_migrate.py` 把库准备好，然后验：写入的数组/jsonb 能原样
读回、身份快照落库、库里已有的 CHECK 真的拒坏行、审计写失败返回 `ok=False`、
保留期清理只删过期行。

## 关键判据做过变异测试

判据从没红过等于没有判据，所以下面每一处都故意改坏过一次、确认对应用例会红、再改回
（B2 的四组由 `scripts/b2_mutation_check.py` 自动跑：改文件 → 跑用例 → 断言 rc!=0 → 还原）：

- 去掉审计的配对规则（`reject_reason` 与 `gate1_verdict` 的一致性）→ 审计模型用例红。
- 给 `GATE_REJECT_LOW` 加一个代码默认值 → "业务值不许硬编码"与"缺项要点名"两条红。
- 把 `/healthz` 的 `ok` 写死为 `True` → "PG 挂了要 503"与"Qdrant 挂了要 503"红。
- 把 `allowed_depts && {}` 改成 `<@ {}`（有交集 → 被包含）→ 对拍与 PG 结构用例红。
- 去掉 Qdrant 侧的 `date_confidence='explicit'` → 对拍与"只认显式时间"红。
- 把 admin 的"去掉整条 ACL"改成"仍带 public 分支" → 结构用例红（语义错、结果同）。
- 去掉版本兜底 → `ch_oldver` 漏进候选池，对拍红。

## 还没做（留给后续卡）

- Query 理解/检索/门控（B3）、生成/校验/SSE（B4）：路由挂到 `api/routers/` 下，
  错误直接抛 `AppError`。
- 审计保留期清理的定时触发：现在只有 `purge_expired()`，由定时任务/worker 调用。
- 限额的执行（并发上限、30 秒预算）：配置已就位，执行点属于 B4 的流式接口。
- 时间归一的"最近 N 天""今年以来"等窗口型说法：本卡只做了卡片点名的四种
  （去年/上个月/前年/三月）+ 绝对日期，其余落到"未限定时间"并说明。
