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

## 摄取：样本扫描 / 判重 / ingest_jobs（§3.1、§4.4、附录 C.1）

开发期摄取不是上传接口，是**扫目录**（§3.1）。三个模块，每个只管一件事：

| 模块 | 做什么 | 为什么单独拆出来 |
|---|---|---|
| `ingest/fingerprint.py` | 路径 → `doc_key`；字节 → SHA-256；`version_id` | 判重的地基。三个调用方（扫描、将来的上传、评测脚本）必须算出**同一个** key，算法只能在一个人手里 |
| `ingest/decide.py` | 扫描判定表（五格） | 全卡最容易改错的地方，纯函数才能把每格正反都测一遍 |
| `ingest/jobs.py` | `ingest_jobs` 状态机 + 重试计数 | 状态迁移只能在这一处写 SQL，否则"哪几个状态能跳"会散开 |
| `ingest/scan.py` | 扫目录、锁、按结论写库、报计数 | 唯一有副作用的一层 |
| `ingest/lockkeys.py` | 扫描锁 key 的派生 | 必须落在 int8 内；零依赖，避免 `config.py` 反向依赖 psycopg |

### doc_key 归一化

§2.1 要求"去掉版本号、日期、序号后缀，只保留主体名"，具体规则：

```text
samples/policy/travel-cn.pdf              -> policy/travel-cn
samples/policy/差旅管理办法-2025版.pdf      -> policy/差旅管理办法
samples/policy/差旅管理办法-2025版-第2稿.pdf -> policy/差旅管理办法   （剥到稳定）
samples/policy/差旅管理办法（2）.pdf         -> policy/差旅管理办法
samples/hr/leave-2025.pdf                 -> hr/leave
samples/x/2024-差旅.pdf                    -> x/2024-差旅        （中间的日期是主体，不剥）
samples/x/2025.pdf                        -> x/2025            （剥空则回退原名，不产空 key）
```

只剥**段尾**修饰、剥到不再变化为止、剥空则回退原名、最后小写（`doc_key` 是标识符，
展示名在 `documents.filename`）。`policy/travel-cn` 这类带 `/` 的 key 在路由里必须用
`{doc_key:path}`，否则 FastAPI 只匹配到第一段。

### 判定表（§3.1）与跳过原因

**判定只看库里已有的既成事实**（documents 行是否存在、同 `(doc_key, file_hash)` 版本的状态）：

| 情况 | 结论 | 写库动作 | 响应字段 |
|---|---|---|---|
| 新 `doc_key` | `add` | 建 documents + pending 版本 + 入队 | `added+1` |
| 已有文档、新指纹 | `change` | 建 pending 版本 + 入队（**不动旧 active**） | `changed+1` |
| 同指纹且版本 pending/active/skipped | `skip` / `unchanged` | 什么都不写 | `skipped+1` |
| 同指纹但版本 `failed` | `skip` / `version_failed` | 什么都不写 | `skipped+1` |
| 同指纹但版本 deprecated/purged | `skip` / `content_revert` | 什么都不写（回滚走 §4.4 翻牌） | `skipped+1` |
| 两个文件归一化撞同一 key | `fail` / `doc_key_collision` | 两份都不入库 | `failed+1` |
| 文件读不到 | `fail` / `unreadable` | 记一条 `failed` job（`version_id` 为空） | `failed+1` |

后三种跳过必须分开报：只报一个 `skipped` 计数，运维看不出"这个文件其实一直解析失败"。

**扫描永远不把版本置 `active`**：翻牌是 A3 的翻牌事务独占的动作（§4.1）。所以
"指纹变了"只产生 pending 版本，`GET /documents/{doc_key}` 的 `active_version` 保持旧值，
`documents.status` 在有 active 版本时也保持 `active`。

### 单任务锁

`pg_try_advisory_lock`（会话级，不是事务级——整轮扫描跨多个事务，事务级锁在第一个
commit 就释放）。拿不到直接抛 `ScanInProgress` → 接口 409，**不排队**：并发第二次扫描
只会看到同一批文件、算出同样的指纹，纯浪费 IO，还要抢同一批行锁。

两个实现细节都是踩出来的：

- 锁与所有写**共用同一条连接**（`PgDatabase.session()`）。锁挂在连接 A、写走连接 B 时，
  B 可能在池里排在持有锁的 A 后面，形成自己等自己。
- 锁 key 必须落在 **int8** 内。手写十六进制字面量很容易超（`0x61695F7261675F7363616E`
  会被 PG 当 numeric，`pg_try_advisory_lock` 直接报 `function does not exist`），
  所以从固定字符串取 sha256 前 8 字节并显式折成带符号 int8（`ingest/lockkeys.py`）。

### ingest_jobs 状态机（002 迁移把它落成了 CHECK）

```text
         enqueue              claim                succeed
  [*] -> queued --------------------> running --------------> succeeded
            ^                            |
            |  requeue (retry+1)         |  fail：还有额度 -> queued(retry+1)
            +----------------------------+        额度用尽 -> failed
            |
            +--> canceled（queued/running 都能取消）
```

- **重试是同一行原地 `retry_count+1`**，不是新插一条：否则"失败过几次"散在多行里，
  统计口径就说不清。`INGEST_MAX_RETRIES=3` = 1 次尝试 + 3 次重试 = 最多 4 次执行。
- **`failed` 是终态**，不再回 `queued`。否则"一直失败的文件"每轮扫描都重跑一次，
  日志被同一个问题刷满而看不出是同一个问题。
- 数据库层的五条约束（`db/migrations/002_ingest_job_status.sql`）：
  status 枚举、`running` 必须带 `started_at`、终态必须带 `finished_at`、
  `failed` 必须带 `error`、同一版本最多一个未结束任务（部分唯一索引）。
- `claim_next_job` 用 `FOR UPDATE SKIP LOCKED`，多 worker 不用应用层排队。
  可选 `doc_key=` 参数用于按文档串行或定向调试。

### 接口

```bash
curl -sX POST http://127.0.0.1:8000/api/v1/ingest/scan
# {"added":1,"changed":0,"skipped":2,"failed":0,
#  "reasons":{"unchanged":2},
#  "items":[{"doc_key":"policy/travel-cn","filename":"travel-cn.pdf","outcome":"add",
#            "version_id":"policy/travel-cn#7b0bcd16bc6b","job_id":7}]}

curl -s http://127.0.0.1:8000/api/v1/documents/policy/travel-cn
# {"doc_key":"policy/travel-cn","status":"queued","version_count":1,"pending_versions":1,
#  "active_version":null,
#  "latest_job":{"job_id":7,"status":"queued","retry_count":0,"retry_budget_left":3,...},
#  "last_error":null}
```

错误走统一错误表（`errors.py`）：§11.3 之外补了三个契约码——`not_found`(404)、
`scan_in_progress`(409)、`samples_missing`(500)。补码而不是在路由里手搓响应体，
是为了让"409 要不要告警"仍然只有一个来源。

`/healthz` 在非生产环境多返回一段 `ingest`（样本目录、重试上限、扫描锁 key）：
验证脚本用它来制造"另一个进程持锁"的场景，不必把 key 数值抄一份——抄了之后改 key
不会红，脚本只会静静失去测并发的能力。

### 样本

`samples/` 随仓库走（附录 B），由 `scripts/make_samples.py` 生成：纯 stdlib 手写 PDF 结构，
xref 偏移由代码算出来，**字节确定**（不写时间戳）——这样 `file_hash` 稳定，才测得出
"重复扫描跳过"。

## 配置

两类，规则写在 `config.py` 顶部：

- **必填、无代码默认值**（换值必须只改环境变量）：`DATABASE_URL`、`QDRANT_URL`、
  `QDRANT_COLLECTION`、`EMBEDDING_*`、`RERANK_*`、`GATE_REJECT_NONE`、
  `GATE_REJECT_LOW`、`RETRIEVAL_TOP_K`、`RERANK_TOP_N_MIN/MAX`、`MAX_HOPS`、
  `EVIDENCE_MAX_CHARS`。缺了就启动失败，报错点名缺哪个变量。
- **有默认值**（不改变业务语义）：连接池大小、连接/探测/模型调用超时、
  `QA_BUDGET_S`、`MAX_QUESTION_CHARS`、`QA_MAX_CONCURRENCY`、`AUDIT_RETENTION_DAYS`、
  `INGEST_SAMPLES_DIR`（样本目录位置）、`INGEST_MAX_RETRIES`（默认 3，口径见 §4.4）。

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
- 把同指纹 `failed` 版本的跳过原因从 `version_failed` 改成 `unchanged` →
  判定表用例与"失败原因要能报出来"的集成用例共 3 条红。
- 让扫描直接把新版本置 `active` → 冷扫与"旧 active 不受影响"等 4 条红。
- 让 `_acquire_scan_lock` 拿不到锁时静默放行 → 并发扫描的 409 用例红。

## 向量入库 / 转正 / 同步 / 回收（A3：§3.6、§4）

翻牌要改三处（旧版下线、新版生效、切片状态同步），而向量库是副本。核心设计是：
**翻牌事务只写"向量库应该是什么样"，不发 HTTP**；URL 里那一步由同步器在事务外消费。
这样 PG 的事务边界不会被网络抖动绑住，而"翻牌发生过"这件事一定有记录。

| 模块 | 做什么 | 关键取舍 |
|---|---|---|
| `ingest/vectors.py` | 一个切片写一个 point（dense + bm25 + payload），并回填 PG 的 `embedding_dense` | 先写点（payload=pending，检索看不到）再翻牌：反过来会有"版本已生效、向量库还空着"的窗口 |
| `ingest/flip.py` | 一个事务改三处 + 写期望状态 | 空版本拒绝翻（否则旧版下线、新版无片 = 服务翻空）；降级在前、激活在后（否则撞 `ux_active_version`） |
| `ingest/sync.py` | 消费期望状态：按 version_id 批量改 `payload.status` / 删点，失败退避重试 | 用**租约**而非行锁：领任务先提交，再做 HTTP 崩溃后租约到期自动重领，不需要"清理僵尸任务" |
| `ingest/reconcile.py` | 两边对账，以 PG 为准覆盖 | 补点用 PG 已存的 `embedding_dense`，**不调编码服务**（编码挂时仍可修）；修不了的进 `remaining` 不假装修好 |
| `ingest/reclaim.py` | 过 24h 窗口的旧版：切片 + 点 + 图 | 先删库行、点交给同步器补删（§4.4"向量库不可用时先删库行"）；`deprecated_at` 未知时补时间戳并跳过本轮，绝不"立刻删" |
| `ingest/pipeline.py` | index → flip → drain → reconcile | 前置条件不满足抛 `FlipError` 子类（404/409/422 靠它区分），其余失败留在返回值里 |
| `ingest/doclock.py` | 文档级事务锁 | 扫描/翻牌/回收共用同一表达式 `pg_advisory_xact_lock(hashtext(doc_key))`；一处定义，避免表达式漂移成两把锁 |

### 三条会静默失效的判据

这三条都不报错、不抛异常，只是"效果悄悄变差"，所以各自都有专门的用例盯着：

1. **中文 BM25 必须预分词**。实测（Qdrant 1.15.3 + `modifier=idf`）：服务端的
   `qdrant/bm25` 只按空白与标点切词，`住宿费标准每人每天500元` 整句只产出 **1 个
   token**，而 `{"text": "住宿费"}` 的查询跟它对不上。三组对照的 top1 命中率：
   原文↔原文 `1/4`、bigram↔bigram `3/4`、bigram 入库配原文查询 `2/4`。
   所以 `qdrant_common.bm25_text()` 做中文 bigram + 拉丁整词，**入库与查询两侧
   都必须过它**。为什么不是接分词器：分词器带词典版本，换一版就换一套切法，
   历史索引与新查询不再对齐；bigram 无参数，重建前后必然一致。
2. **跳过不是成功**。零宽字符（U+200B/200D/FEFF）能过 `ck_chunks_text_nonempty`
   （它用 `btrim`），但 BM25 切出 0 个 token——Qdrant 收下、那条切片永远召回不到。
   写入侧判据是"有没有可检索字符"，被跳过的切片让 `IndexReport.ok` 为 false。
3. **同步成功不是 HTTP 200**。filter 写错时 Qdrant 照样返回 `completed`（实测：
   一条都不匹配也返回 200）。所以 `verify_desired()` 回读两边的状态分布：
   点总数、`status=desired` 的点数都必须与 PG 一致。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/ingest/versions/{version_id}/promote` | 索引+翻牌+同步+对账。404 无此版本 / 409 已翻过 / 422 空版本 |
| POST | `/api/v1/ingest/versions/{version_id}/rollback` | 24h 内回滚。404 / 409 |
| POST | `/api/v1/ingest/sync` | 消费期望状态 |
| GET | `/api/v1/ingest/reconcile?doc_key=&dry_run=` | 对账。**有残留差异返回 409**（不是 200）——否则调用方会以为一切正常 |

`version_id` 带 `#`（`policy/travel-cn#a91f`），路由用 `{version_id:path}`，
URL 里要编码成 `%23`。

### 运维入口

```bash
make promote VERSION_ID='policy/travel-cn#a91f'   # 一条命令转正
make ingest-status                                # 版本/切片/同步/漂移一眼看（双 active 即非零退出）
make reconcile-dry                                # 只看漂移，不改
make reclaim-dry                                  # 看有多少旧版过了 24h 窗口
make verify-a3                                    # 端到端演练：8 步走完 §4（真 PG/Qdrant/TEI）
make verify-a3-api                                # HTTP 契约：404/409/422/409
make test-a3                                      # 63 个集成用例
```

进展状态的消费者是 `worker/vector_sync_worker.py`（常驻循环，默认 10s 一轮、
每 10 轮一次全量对账）。没有它，翻牌在库里生效而向量库永远不知道——所以它不是
可选的运维便利，是 §4.2"翻牌后一分钟内必须同步完"的必要条件。

## 版面解析 / 表格 / 图表 / 父子切片（A2：§3.2–§3.5）

一份 PDF 进 `parse_document()`，出一批**待转正切片**（`status=pending`，翻牌归 A3）。
分四层，每层只依赖下一层，各自可单测：

```
pipeline.py   切片：父子两级 + 六项随行信息 + 图改译对账的仲裁点
  reader.py   一页 -> PageLayout：四类区域、页眉页脚、跨页续表、OCR 兜底
    layout.py 抽行 / 判分栏 / 找表格 / 找图 / 段落聚类 / 页眉页脚判定
      model.py + geometry.py   Line / Region / PageLayout / DocLayout，坐标换算
```

### 坐标：模型内只存一个空间

模型里的矩形**一律是阅读空间**（未旋转、左上原点、文字横着读的那个空间），
对外输出的 `bbox` **一律是显示空间归一化坐标**，两者只经 `PageSpace` 换算一次
（`Region.bbox()` / `PageLayout.bbox_for()` 是唯一出口）。为什么不是"模型存显示空间、
需要时转阅读空间"：那要在每个聚类函数里各转一次，漏转一处只在旋转页上出错——
最难被测试发现的一类 bug。`test_rotated_page_bbox_matches_the_unrotated_page` 与
`verify_parse_pipeline.py` 的 200 DPI 像素换算（A4 = 1654×2339）是这条的可执行判据。

### 表格：一份存档、两份派生

`TableData` 是**同一个对象**的三个投影（`cells` 派生出 `html` 与 `md`），不是三次解析。
"三表示一致"的可执行定义是往返：`table_html` 反解出的矩阵 == `table_cells` 展开的矩阵，
`table_md` 再解析出的矩阵 == 同一个。合并单元格按"左上角为值、其余格复制该值"展开，
空单元格填 `-`，`col_span`/`row_span` 原样保留在 `table_cells` 里（校验阶段靠它对齐数字）。

跨页续表**只标不并**（`continued=True` + `continued_of:pageN`）：把第 2 页的行并进第 1 页
那条切片，`page_num`/`bbox` 就与 `content_text` 不一致，用户点角标会跳到不含该数字的位置——
切开的表是两条独立证据，错的 bbox 是**假溯源**。

### 图表：转译 + 对账，对不上就不入库

裁片（无损 WebP，`assert_min_dpi` 挡 <200 DPI）→ VLM 转译（Markdown 给人看 / `JSON:` 段
给对账）→ 对账四条：数值在轴范围内、饼图求和 ∈[99,101]、数值能在图上文字或柱高比例里对上、
类别名在图里真实出现过。任一条不过 → `FigureFailure` 进 `doc.figure_failures` 与
`ParseOutcome.rejected_figures`，**这张图不产切片**。没配 VLM 同理（`vlm_unavailable`），
而不是"空转译当空文本入库"——空转译的图切片会让检索命中一条没有内容的证据。

失败的图占的版面**仍算已占用**：那里是刻度与类别标签（`100`/`50`/`0`/`Transport`），
单独变成正文切片就是一批脱离上下文、能命中问句却拿不出依据的数字碎片。

### 切片：父子两级 + 六项随行信息

| 项 | 来源 | 缺了会怎样 |
|---|---|---|
| `page_num` | 区域 | 引用不能跳页 |
| `bbox` | `Region.bbox(space)`（子片取所覆盖行的并集，不是整段） | 高亮框画不出来/框一大块 |
| `chunk_type` | 区域类型 | 取文本时选错列 |
| `effective_date` + `date_confidence` | 切片文本里的日期，文档级兜底 | 过期条款混进时间过滤 |
| `is_public` + `allowed_depts` + `allowed_roles` | 调用方 `Acl`（**必须显式给**） | 越权 / 谁都看不到 |
| `version_id` | A1 的 `version_id_for(doc_key, sha256)` | 检索入口做不了版本兜底 |

`Chunk.require_complete()` 是这六项的可执行判据（`validate_chunks` 一次报全所有缺项）。
子片 256–512 字、重叠 64 字；父片按章节/段落累加到 ≤2048 字，单段超长再切。切片编号
`ch_`+12 位十六进制，由 `sha256(version_id|page|kind|index|part)` 派生——**确定性**（重跑
同一文件得同一批编号）且**文件系统安全**（它要进 `figures/{chunk_id}.webp`：`version_id`
里的 `/` 与 `#` 拼进路径会 404）。

### OCR 兜底（默认关）

`OcrConfig(enabled=True)` 才跑，且只跑"没有文字层但有位图"的页（位图 ≥2% 页面，
只排除水印/logo）。走的页 `PageLayout.ocr_applied=True`，识别出来的 `Line.source="ocr"`、
带 `confidence`、坐标已按渲染缩放比从像素换回 pt，**不猜着修**（`CNY 5o0` 不改成 `CNY 500`）。
来源会继续往下传到切片：`Chunk.notes` 里 `source:ocr` / `source:mixed`——只在 `Line`
上标记等于没标记，入库后 `Line` 就没了，A3 与检索侧只看得到 `Chunk`。

缺 engine 时 `ocr_skipped_reason=engine_unavailable` 并进 `warnings`，不静默降级。

### 验收脚本

```bash
make samples-layout    # 生成 8 份版面夹具（stdlib 手写 PDF，字节确定）
make verify-parse      # 真 PG：三份制度样本 + 两份版面夹具 -> 解析 -> 写入 -> 读回断言 -> 清理
make a2-mutation       # 27 条判据逐个改坏，确认对应用例真的会红（跑完自动还原）
make test-a2           # A2 的 12 个用例文件（单元 + 夹具级 e2e + 落库往返）
```

实测：`make test-a2` 278 passed；`make verify-parse` 36/36 断言；`make a2-mutation`
27/27 检出（两条落库判据需要真 PG，缺 DSN 时脚本**显式判失败**而不是把 skip 当检出）。

`verify_parse_pipeline.py` 的断言覆盖本卡两条验收：**bbox 可映射原页**（归一化矩形换算出
页内像素矩形，含 A4@200DPI 边界）、**表格三表示一致可重建**（HTML→矩阵、MD→矩阵，
且必须**从库里读回来之后**仍成立），外加三件容易"看起来实现了"的事：合并单元格的
`col_span` 真在 HTML 里、页眉页脚真被剔除且报告**如实计数**（`header_footer_lines`，
页眉页脚行不在 `regions` 里，只统计 `regions` 会让"删了 3 行页眉"显示成 0）、
OCR 开了就真出片且 `source:ocr` 进了库。

### 落库：三份表示与来源标记都要活到库里（004 迁移）

`table_cells` 与 `notes` 是 004 补的两列（001 漏了）。为什么这件事必须有一个**落库往返**
的判据：内存里的 `Chunk` 有这两个字段，`as_row()` 少写一列不会让它变样，而校验阶段的
读法完全不同——

| 场景 | 只有 html + md | 有 table_cells |
|---|---|---|
| 校验"500 这个数字在哪一格" | 正则拆 HTML、还要自己还原合并单元格 | 直接用矩阵与 `col_span` |
| 下游判断"这段文字是 OCR 认的" | 无处可查 | `notes @> '{source:ocr}'` |

`api/tests/test_parse_persist_integration.py` 与 `verify_parse_pipeline.py` 的
`_check_stored_tables` / notes 往返就是这条的可执行判据，且各自都被变异测试覆盖
（`as-row-table-cells` / `as-row-notes` 把 `as_row()` 改回丢列，两条用例都会红）。

## 问题理解 / 混合检索 / 精排 / 门控 / 多跳（B3：§6–§8）

一条链路：`understand()` → 两路召回 → RRF → 版本兜底 → 精排 → 门控 →（可选）多跳。
装配只有一处（`retrieval.pipeline.build_pipeline(settings, pg)`），阈值全部来自
`Settings`（`config.py` 里 14 个业务口径，代码里一个默认值都不给）。

```
query/__init__.py      §6 改写（同义/缩写/口语）+ 指代消解 + 时间归一 + 单多跳预判
query/lexicon.py       三张词表 + 一张禁止表（启动期自检）
retrieval/hybrid.py    §7.1/§7.2 两路 prefetch（同一个 filter 对象）+ RRF + 父片去重
retrieval/rerank.py    §7.3 精排（≤32/批、共享 deadline、三种降级原因）
retrieval/gate.py      §8.1/§8.2/§8.4 三段门控 + 五态 verdict + 澄清选项
retrieval/multihop.py  §8.3 黑板 + 三个停止条件 + 断链部分结论
retrieval/pipeline.py  编排：顺序约束（版本兜底在精排之前）+ 审计快照
retrieval/texts.py     回 PG 取正文（向量库不存正文，附录 B）
```

### 两路同 filter 是结构性的，不是约定

`build_batch_body()` 只 dump 一次 filter，两路拿到**同一个 dict 对象**；
`test_both_routes_get_the_same_filter_object` 断言的是 `is` 而不是 `==`。
两份内容相同的 dict 会在未来某次改动里漂移，同一个对象不会。
这条判据刻意放在**纯逻辑层**——只在"恰好装了 Qdrant"时才跑的判据等于没有判据。

### RRF 的常数不是 60

实测（真 Qdrant 1.15.3）原生 `fusion=rrf` 的输出逐值等于 `Σ 1/(1 + 名次)`：
`1.0 / 0.666667 / 0.5 / 0.2 / 0.166667 / 0.142857` 六个值全部对上。所以
`RRF_K = 1` 是**对齐实现的常数**，不是调参旋钮。改成论文里的 60 会让
"两路都第 1"和"一路第 1 一路第 30"几乎打平（0.9839 vs 1.0），门控的 top1 分数
失去区分度。`test_local_rrf_matches_qdrant_native_fusion` 拿同一批数据跑原生融合
与本地融合、比较顺序，改坏常数立刻红。

为什么用 `/points/query/batch` 而不是原生 `fusion`：§7.6 要求每条候选带
`dense_rank`/`sparse_rank`，原生融合只返回合成分数；§7.2 的并列规则也必须是
我们能控制的。两者都是一次 HTTP，"少一跳网络"在这个选择里没有收益。

### 精排的三条实测约束

| 事实（实测） | 实现后果 |
|---|---|
| 33 条直接 `413 batch size 33 > maximum allowed batch size 32` | 必须分批（`TEI_MAX_BATCH=32`） |
| 8 条 184ms / 20 条 355ms / 30 条 484ms（CPU） | 50 条会顶破 800ms 预算，所以分批 + **跨批共享 deadline** |
| `raw_scores=false` 是 sigmoid 后的 0..1 | §8.1 的 0.20/0.40 必须按归一化分判，不能用 logits |

降级分三个原因，不是一个：`rerank_timeout`（预算不够）、`rerank_unavailable`
（服务挂了）、`rerank_partial`（打了一半）。三者的运维处置完全不同。降级时
**不给候选填 0 分**——0 分会让门控判成"无相关"，而真相是"我们没打分"。

### 门控五态与两个"替代 vs 补充"

`pass` / `clarify` / `reject_none` / `reject_low` / `partial`（§8.4）。
0.38–0.42 临界带里辅助条件是**替代**分数判定，不是补充：0.39 的多实体问题在
辅助条件满足时按过线处理，否则 `reject_low`。早先写成"判完辅助条件再走
`top1 < 0.40`"，那样 0.39 会被拒，辅助条件等于白判（实测撞过）。

临界带相对门控阈值的位置由 `GateConfig.effective_borderline` **裁剪**，不在启动期
硬绑：门控阈值按 §12.2 波谷法随模型重标定，硬绑会让"调阈值重标定"直接启动失败。

### 多跳：每跳独立过阈

三个停止条件谁先满足谁生效（§8.3）：本跳不过阈（断链）、证据齐了、跳数到 Max。
断链时交出去的是"已查实的部分 + 断在哪一步"；第一跳就断则直接拒答（`facts` 为空）。

覆盖检查有一个实测坑：实体归一后是 `住宿费限额`，而库里正文写的是 `住宿费标准`——
拿归一后的词去正文里找永远找不到，多跳会空转到 Max Hops。所以 `QueryPlan` 同时带
`entities`（归一后，给审计）与 `coverage_terms`（归一后 + 原文写法，给覆盖检查），
同一实体的多种写法是"或"的关系。

### 跑起来

```
make test-b3-logic   # 纯逻辑：RRF / 门控五态 / 精排四结局 / 多跳五停止原因
make test-b3         # 集成：真 PG + 真 Qdrant + 真 TEI embedding + 真 rerank
make b3-mutation     # 7 条关键判据逐条改坏，确认对应用例真的会红（跑完自动还原）
```

## 生成 / 五道校验 / 三档降级 / SSE（B4：§9–§10、附录 C/D）

一条链路：`retrieval.pipeline` 给出 `verdict` → 组证据包 → 受限生成 → 五道校验 →
三档降级 → SSE 事件 + 一条 `qa_log`。四个模块各管一段，谁都不许跨段改规则：

| 模块 | 管什么 | 为什么不并进别处 |
|---|---|---|
| `generate/evidence.py` | §9.1 的五条组装规则 | 它决定"模型看到什么"，而这一层的错全都表现为"答案读起来对但引用指错了" |
| `generate/llm.py` | 提示词（附录 D.1）、`Temperature=0`、宽松解析一次 | 提示词是常量不是模板；解析层的职责是报格式错，不是判内容对错 |
| `verify/checks.py` | §10.1 五道、§10.3 三档 | 五道的顺序与外键（哪一道失败落哪一档）是跨模块契约，只能有一份 |
| `qa/orchestrator.py` | 判定 → 事件序列 + 审计行 | 出口映射散在两处会出现"reject_low 却走了系统档"这种组合 |

### SSE 契约：`POST /api/v1/qa/stream`

```bash
# AUTH_MODE=mock_header（仅本地开发）时的调用方式：身份来自请求头。
# 默认配置是 fixed_jwt，那时要去掉这三个 X-User-* 头、改带 `Authorization: Bearer <jwt>`，
# 否则会拿到 401（这正是"开流之前回 401"那条口径）。
curl -sN -X POST http://127.0.0.1:8000/api/v1/qa/stream \
  -H 'X-User-Id: u_dev' -H 'X-User-Depts: fin' -H 'X-User-Roles: staff' \
  -H 'content-type: application/json' \
  -d '{"question":"2025年差旅住宿标准是多少","scope":{"doc_keys":["policy/travel-cn"]}}'
```

`-N` 是必须的：curl 默认按块缓冲输出，不加它看不到逐字效果（本地看起来像一次性返回）。

事件顺序（附录 C.2）：`step`（可多次）→ `token`（可多次）**或** `clarify`/`fallback` →
`citations`（可选）→ `done`。`done` 永远最后一条，异常路径也发——前端没有别的结束信号。

```
event: step
data: {"type": "step", "hop": 0, "phase": "retrieval", "elapsed_ms": 12.4}

event: token
data: {"type": "token", "delta": "住宿费"}

event: citations
data: {"type": "citations", "items": [{"n": 1, "doc_key": "policy/travel-cn",
  "doc_name": "差旅管理办法-2025版.pdf", "page_num": 47,
  "bbox": [[0.12,0.34,0.88,0.51]], "snippet": "住宿费标准：每人每天 500 元",
  "chunk_id": "ch_9f2c31"}]}

event: done
data: {"type": "done", "latency_ms": 1840, "hop_count": 0, "verdict": "pass"}
```

四条口径：

- **401/422 在开流之前**。身份或入参错误回 HTTP 401/422 + JSON 错误体，不是一条
  200 的流——前端拿到 401 才知道该去登录，拿到流只会显示"稍后重试"。
- **拒答路径没有 `token`**（§10.4）。`clarify` / `fallback` 与 `token` 互斥。
- **`token` 在校验之后发**。这不是放弃流式，而是 §10 的要求（"答案要等下一章校验
  通过才算数"）：屏幕上不会出现一句后来被撤掉的结论。代价是首字延迟含生成+校验。
- **`token` 拼起来等于校验通过的正文**，一个字不多不少；系统陈述（未覆盖清单、
  "仅依据前 N 条"、补年份假设）作为尾巴另发，它们不需要引用、不参与校验。

`done.verdict` 六个取值：§8.4 的门控五态 + `unverifiable`（门控放行但 §10 校验
不过）。系统档用错误码本身（`model_timeout` / `internal`）——前端与评测都按它区分
"模型超时"与"代码炸了"。

### 证据包（§9.1 五条，顺序不能换）

取文本按类型 → 同父片合并（页码取最靠前）→ 6000 字封顶（按精排分从低往高丢，
**不截断单条**）→ 编号 1..n 按分数降序 → 每条带一行来源标记。

两个被"合并"逼出来的细节：

- **合并要删重叠**。子片本来有 64 字重叠（§2.3），拼完不删会让同一句出现两次：
  后果不是多几个字，而是长度预算被重复内容吃掉，真实证据被挤掉。
- **代表性切片单独保留**。合并后的证据有多个 `chunk_id`，角标只能指一个——取组内
  分数最高的那条，`bbox` 与 `snippet` 跟它走；页码取最靠前那一页。两个字段用途不同：
  页码决定用户翻到哪页，`chunk_id` 决定角标回查哪段。

封顶的长度把来源标记也算进去（8 条 × 一行 ≈ 300 字）。不算的话"封顶 6000"是假的。

### 五道校验（§10.1）与三档降级（§10.3）

| 道次 | 规则 | 实现要点 |
|---|---|---|
| 一 | 引用编号存在且在范围内 | `citations` 字段**与正文**两处都扫：模型可能只改了一处 |
| 二 | 每条主张至少一个引用 | 校验单位是**句子**不是 `claims`；模型没报出来的句子照样要查 |
| 三 | 数字与专名可在**被引**证据定位 | 只查被引那几条：在别的证据里找到不算"有据"，那是引错编号 |
| 四 | 判别器判"支持" | 超时/格式错一律按不支持（Fail-Closed）；没配模型记 `skipped` |
| 五 | 同实体同单位无两个值 | 纯规则，不调模型 |

任何一道不过就停。降级按"用户接下来要做什么"分档，不按"第几道失败"分：

| 档位 | 触发 | 给用户什么 |
|---|---|---|
| 只给原文 | 数字或判别不过、模型输出不可用 | 原文片段 + 页码，明确"未形成结论" |
| 部分结论 | 多跳未覆盖全部 | 已查实部分 + 未覆盖清单 |
| 系统异常 | 生成服务不可用/超时 | 一句"稍后重试"，**不给原文** |

系统档不给原文是有意的（§10.3 原话"不暴露内部细节"）：那一次连"证据是否相关"都没
验完，把原文推出去等于用噪声掩盖故障。档一才给——那时证据已过门控与精排。

### 数字对齐怎么算（§10.2）

`api/verify/numbers.py`，纯规则。三步归一：中文数字转阿拉伯、单位统一（万元→元、
成→%）、去千分位与全角。**不比字符串，比"数值相等且规范单位一致"**——`30 天` 与
`30 个工作日` 判不对上，这是 §10.2 点名的反例。

两个必须的前置处理：

- **日期整段先摘掉**，否则 `2025-03-01` 会被拆成三个裸数字，而证据里写的是
  `2025 年 3 月 1 日`——比对必然失败，每条带日期的答案都被拒。
- **中文数字前面紧邻 `第/这/那/每/某/几/哪` 时不算量**。不挡的话 `这一条` 会被抽成
  `1 条`，第三道拿着一个不存在的"1 条"去证据里找，每条答案都判不过。

### 三条会静默失效的口径

1. **`status=unknown` 不是校验失败**（§9.2 规则 3）。模型自己说"证据不足"是按提示词
   正确行事，走 `no_evidence`；记成 `unverifiable` 会让"没有证据"与"校验不过"的
   告警策略混成一档（前者不告警，后者高于阈值要告警）。
2. **判别器 `skipped` 与 `NOT_SUPPORTED` 处置相同但审计必须能分辨**。没配判别模型
   意味着少了一道闸，这件事要被看见，不能被"答案反正拒了"掩盖。
3. **`partial` 映射成 `gate1_verdict=pass`**（§8.4 说它要带证据给下一步），未覆盖
   清单进 `score_snapshot.uncovered`。DB 的 CHECK 也只认四态。

### 跑起来

```
make test-b4-logic   # 纯逻辑：证据包五规则 / 五道校验 / 数字归一 / 事件顺序 / HTTP 契约
make test-b4         # 集成：真 PG + Qdrant + TEI（生成用脚本替身，见下）
make verify-b4       # 端到端演练：六条判据逐条打印实测出口，不只给绿
make b4-mutation     # 10 条关键判据逐条改坏，确认对应用例真的会红（跑完自动还原）
```

**生成用替身这件事不能含糊**：本机没有配置 LLM 网关（`LLM_BASE_URL` / `LLM_MODEL`
为空），所以 `test-b4` 与 `verify-b4` 里生成与判别都是脚本化替身。它们能证明
"证据包字段来自真库""门控与校验的出口正确""事件顺序与审计落库正确"，**不能证明
生成质量**——后者要配了网关后跑 `eval/run_eval.py`（黄金 50 题）。那条区分写在
`api/tests/b4/test_qa_integration.py` 的头注与 `verify_b4_qa.py` 的输出末尾。

## 还没做（留给后续卡）

- 生成质量评测：`eval/run_eval.py` 还没写（Q1 卡），本卡只保证接口与出口正确。
- 判别器与生成模型现在共用一份配置（`judge_client()` 直接复用 `llm_client()`）。
  接口已经允许分开，将来加 `JUDGE_MODEL` 只改 `api/routers/qa.py` 的那一个函数。
- 审计保留期清理的定时触发：现在只有 `purge_expired()`，由定时任务/worker 调用。
- 并发上限（§11.4 `QA_MAX_CONCURRENCY`）还没落到路由层：配置已就位，执行点待定
  （用信号量还是排队由容量评估决定）。30 秒总预算同理——现在是各段超时相加的兜底。
- 时间归一的"最近 N 天""今年以来"等窗口型说法：B2 只做了卡片点名的四种
  （去年/上个月/前年/三月）+ 绝对日期，其余落到"未限定时间"并说明。
- 多跳的子问题拆分现在是"原问 + 缺口实体"，不是模型拆解：§8.3 说"根据缺口拆子问题"，
  缺口由覆盖检查给出（实体 × 是否已被证据提到），拆解是确定性的。要用模型拆，
  换掉 `pipeline._run_multihop` 里的 `sub_questioner` 即可，`run_multihop` 本身不动。
- 门控的 0.20/0.40 与 0.38–0.42 目前是 `.env.example` 里的初始值，**换模型后要按
  §12.2 波谷法重标定**（默认卡的模型定档说明已记这一条）。标定脚本还没写：
  需要先有一份带标注的评测集（`eval/golden_50.jsonl` 的 `expected_doc_keys` 还是
  TBD），且要能跑真精排拿分数分布。
- 摄取侧没做的（都不是本卡的验收项）：碰撞后的人工处置界面、崩溃 worker 的任务
  回收（超时判失败，§4.4 提到但需要 worker 心跳落地）、文件读不到时的告警升级
  （现在只进 `ingest_jobs.error` 与日志）。
- **A3 的 reclaim 还没接定时触发**：`make reclaim` 是手动/运维入口，
  `scripts/a3_ingest.py reclaim` 本身幂等，挂到 cron 或 worker 的每日一轮即可
  （worker 目前只跑同步 + 对账）。
- `test_ingest_scan_integration::test_unreadable_file_records_failed_job_without_touching_versions`
  在容器里必红：容器以 root 跑，`chmod 0o000` 挡不住 root 读文件。这是 A1 用例的
  环境假设问题（该用例假设"非 root 跑测试"），与检索链路无关。
