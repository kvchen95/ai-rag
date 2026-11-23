# db — PostgreSQL schema 与 Qdrant 集合（DA1）

面向实现者的口径文档。**字段与索引以本文件和 `db/migrations/001_init.sql` 为准**，
`详细设计.md` 附录 A/B 已同步（见文末"与附录的差异"）。

一句话：**PostgreSQL 是唯一真相，Qdrant 只是它的投影**。切片正文、权限、生效日期都在 PG；
Qdrant 里放 `chunk_id` + 向量 + 过滤字段，随时可以从 PG 的 `content_text` 全量重建。

---

## 1. 怎么用

宿主不需要 python / psql，所有脚本都在 `api` 容器里跑（容器内有 `psycopg`、`httpx`）。

```bash
cd <repo 根>

make db-migrate            # 应用 db/migrations/*.sql（api 容器启动时也会自动跑一次）
make db-status             # 迁移台账：applied / pending / DRIFT
make qdrant-init           # 建/校验 kb_chunks 集合 + 11 个 payload 索引（幂等）
make index-rebuild-dry     # 只看重建计划：条数、embedding 尺寸、批次，不写
make index-rebuild         # 真跑：从 PG content_text 全量重建 dense+bm25

# 一次性/定向用（参数见 --help）
docker compose exec -T api python scripts/qdrant_rebuild.py --doc-key policy/travel-cn --prune
docker compose exec -T api python scripts/qdrant_init.py --recreate     # 维度不匹配时重建集合
```

前提：`docker compose up -d` 后 postgres / qdrant / embedding 三个服务 healthy（`make health`）。

**PG 镜像必须是带 pgvector 的**：`POSTGRES_IMAGE=pgvector/pgvector:pg16`（compose 默认值）。
`postgres:16-alpine` 没有 vector 扩展，`CREATE EXTENSION vector` 会直接失败。

### 迁移工具

`scripts/db_migrate.py`，规则就三条：

1. `db/migrations/*.sql` 按文件名顺序执行，**一个文件一个事务**；
2. 已应用的记进 `schema_migrations`（version + sha256 前 16 位 + 耗时），再跑是 no-op；
3. **已应用的文件改了就报 DRIFT 并中止**——要改 schema 就加新文件，不改历史。

`infra/api/entrypoint.sh` 调用的也是这个 runner，别再加第二套（两套 runner 写同一张台账，
迟早出现"一边记录过、另一边没记录"的分裂）。

### 迁移清单

| 文件 | 内容 | 作者 |
|---|---|---|
| `001_init.sql` | 五张表 + 视图 + 索引/约束 | DA1 |
| `002_ingest_job_status.sql` | `ingest_jobs` 状态枚举 CHECK、`running` 必须有 `started_at`、终态必须有 `finished_at`、`failed` 必须带 `error`、同一版本最多一个未结束任务 | A1 |
| `003_vector_sync.sql` | `doc_versions.deprecated_at`（回收窗口起点）+ `vector_sync_tasks`（向量库期望状态）+ `vector_drift_log`（漂移账） | A3 |
| `004_chunk_extras.sql` | `chunks.table_cells`（jsonb 单元格矩阵，§3.3 的第三份表示）+ `chunks.notes`（`source:ocr` / `continued_of:pageN` / `may_exceed`） | A2 |

### 造一个干净库

```bash
# 一次性容器 + tmpfs，验完即删（不碰 ai_rag 数据卷）
docker run -d --name ai-rag-migtest --network ai-rag_default --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_USER=ai_rag -e POSTGRES_PASSWORD=ai_rag_dev_password -e POSTGRES_DB=ai_rag \
  pgvector/pgvector:pg16

docker compose exec -T api python scripts/db_migrate.py \
  --dsn postgresql://ai_rag:ai_rag_dev_password@ai-rag-migtest:5432/ai_rag
```

---

## 2. 表与关系

```
documents (doc_key PK)
   └─ doc_versions (version_id PK, doc_key FK)
        └─ chunks (chunk_id PK, (version_id, doc_key) FK → doc_versions)
ingest_jobs   — 摄取队列，按 doc_key 索引导航（不加外键，见 §4）
qa_log        — 一次问答一行（审计）
schema_migrations — 迁移台账（由 runner 建）
v_active_chunks   — 视图：version 与 chunk 双 active 的切片 = 可检索集合
```

`chunks` 用**复合外键 `(version_id, doc_key)`** 而不是只挂 `version_id`：切片同时存了
`doc_key` 与 `version_id`，只挂版本就会出现"切片的 doc_key 和它版本所属文档不一致"的行，
而这种行在按权限过滤时是**越权命中**（§5.2 的过滤条件按 doc_key/depts 走，版本校验按 version_id 走）。
复合外键让两者不可能不自洽，代价只是一个唯一索引。

---

## 3. 关键口径（实现时必须按这个来）

| 主题 | 口径 | 出处 |
|---|---|---|
| 文档状态 | `documents.status ∈ queued / parsed / active / failed` | A.1 |
| 版本状态 | `pending / active / deprecated / purged / failed / skipped`；**`active` 必须带 `activated_at`**（CHECK 强制） | A.2 |
| 单一生效版本 | `ux_active_version`：`doc_key` 上 `WHERE status='active'` 的唯一索引。并发翻牌由它兜底，应用层再加 `pg_advisory_xact_lock(doc_key)` 串行化 | §4.1 |
| 判重 | `UNIQUE (doc_key, file_hash)`；`file_hash` 是文件字节 SHA-256，**64 位小写十六进制**（CHECK 强制，短码请另设列） | §4.4 |
| 切片状态 | `pending / active / deprecated / purged`；翻牌时按 `version_id` 批量改（`ix_chunks_version_status`） | §4.1 |
| 权限 | `is_public` 为真则跳过部门/角色；否则 `allowed_depts` 或 `allowed_roles` **有交集**即通过（不是包含）。两个数组都空 = 只认 `is_public` | §5.2 |
| 时间过滤 | 只认 `date_confidence='explicit'` 的 `effective_date/effective_year`；推断的时间不参与过滤 | §5.3 |
| 版本兜底 | 检索后按 `version_id` 回查当前 active 版本，不一致丢弃；**不要**把版本号放进 Qdrant filter（选择性差，查询退化） | §7.4 |
| 空正文 | `content_text` 必须非空白（CHECK）。空白文本进 BM25 会得到空稀疏向量，Qdrant 直接拒收 | §7.1 |
| 向量维度 | `chunks.embedding_dense vector(512)` —— bge-small-zh-v1.5。**1024 是过期口径**（BGE-M3 时代） | 模型定档 2026-11-23 |
| 审计 | 一次问答一行，含身份快照（`user_id` / `user_depts` / `user_roles` / `is_admin`），保留 1 年；只存切片编号与分数摘要，不存原文 | §11.2 |

`chunks.embedding_dense` 是**可选缓存列**：检索走 Qdrant，PG 里这一列只用于离线分析
（聚类、去重、相似切片排查）。**A3 之后它还会被写入**：每次索引完把稠密向量回填进来，
这样"向量库丢了点"可以只靠库内数据补回（对账的 `missing_point` 修复路径），
不需要重新调编码服务。仍然可以为 `NULL`（老数据/未索引版本），不影响任何链路。

`doc_versions.deprecated_at`（003 加的列）是**回收窗口的起点**：§4.3 的 24h 从这一刻
起算。001 里那个索引原本建在 `activated_at` 上，那列记的是"生效时间"——拿下线时长
去比生效时间，会把"生效 30 天才被取代"的版本在翻牌瞬间判成已过期、直接删掉仍在
窗口内的回滚门。003 把索引改到 `deprecated_at` 并回填了历史行。

### 向量库副本的两张表（003，A3）

| 表 | 一行代表 | 谁写 | 谁读 |
|---|---|---|---|
| `vector_sync_tasks` | 一个版本的**期望状态**（active / deprecated / purged） | 翻牌、回滚、回收（**同一个事务**） | `sync.drain`（worker 循环 / API） |
| `vector_drift_log` | 一处"两边不一致" | 同步重试耗尽、对账 | 对账、告警、`GET /api/v1/ingest/reconcile` |

`vector_sync_tasks` 是**期望状态**而不是任务流水（`version_id` 主键，一个版本一行）：
同一版本 24h 内可能被反复翻牌，流水会引入"两条相反指令谁先谁后"的问题；期望状态
永远只有一句"它现在应该是 X"，重放天然幂等。期望**变了**才重置 `attempts` / `status`，
没变就不动——否则每小时的对账会把已经同步好的版本反复重推。

`vector_drift_log` 有 `uq_vector_drift_open_dedupe`（`dedupe_key` 上的部分唯一索引，
`WHERE NOT fixed`）：同一处未修复的漂移只留一行。没有它，每小时的巡检会在账上刷出
24 条同样的记录，"漂移次数"这个指标就废了。

---

`ingest_jobs.status` 的枚举已由 **A1 定稿并落成 CHECK**（`db/migrations/002_ingest_job_status.sql`）：

| 状态 | 含义 | 进入方式 |
|---|---|---|
| `queued` | 已入队，等 worker 取 | 扫描新建 / `running` 重试退避回来 |
| `running` | 正在跑（**必须带 `started_at`**） | `claim_next_job` |
| `succeeded` | 跑完（**必须带 `finished_at`**） | `succeed_job` |
| `failed` | 终态失败（**必须带 `finished_at` 与 `error`**） | 重试耗尽 / 直接判失败 |
| `canceled` | 人工取消（**必须带 `finished_at`**） | `cancel_job` |

重试是 `running → queued` 原地 `retry_count+1`（`INGEST_MAX_RETRIES=3` = 1 次尝试 + 3 次重试）；
`failed` 是终态，不再回 `queued`；同一版本同时只能有一个未结束任务
（`uq_ingest_jobs_open_version`，部分唯一索引）。状态机与重试口径写在 `api/ingest/jobs.py`。

---

## 4. 与附录 A/B 的差异（6 处，均有理由）

| # | 差异 | 为什么 |
|---|---|---|
| 1 | `chunks` 增加 `status text NOT NULL DEFAULT 'pending'`（`pending/active/deprecated/purged`） | §4.1 要求"把新版本名下的切片状态同步为生效"、§7.1 的 filter 要求 `status=active`，但附录 A/B 没有这一列——**附录漏字段**。缺了它，翻牌只能靠 join 推导，Qdrant 侧也没法按状态过滤 |
| 2 | `qa_log` 增加 `user_id` / `user_depts` / `user_roles` / `is_admin` | §11.2 明确要求审计里带"身份快照"，附录 A.4 漏了。没有身份快照就无法事后回答"这条答案是谁以什么权限看到的" |
| 3 | `doc_versions` 增加 `created_at` | 判定"最新入库版本"与回收顺序需要；纯技术列，非业务语义 |
| 4 | `chunks.parent_id` **不加自引用外键**；`doc_versions.superseded_by` **不加外键** | 批量装载时父子顺序不该由约束强制；旧版 `purge` 后 `superseded_by` 仍需保留指向供审计追查 |
| 5 | `chunks` 增加 `table_cells jsonb`（004） | §3.3 写明"表格在库里存三份东西"，其中第三份是单元格矩阵（"校验阶段对齐数字用它，比在 HTML 里找子串可靠"）。附录 A.3 只有 `table_html` / `table_md`——**附录漏了第三份**。缺了它，数值校验只能正则拆 HTML，正是 §3.3 要避免的做法 |
| 6 | `chunks` 增加 `notes text[] NOT NULL DEFAULT '{}'`（004） | §3.2 要求标出"这段文字是识别出来的"（OCR 有错字，下游的数值校验要据此放宽或拒绝），§3.3 要求续表/超长表格带标记。附录 A.3 没有任何来源字段，标记只活在解析进程的内存对象里，入库即丢 |

---

## 5. Qdrant 集合形态

集合名 `kb_chunks`（`QDRANT_COLLECTION`），形状在 `scripts/qdrant_common.py::collection_spec` 单点定义：

```python
{
  "vectors":        {"dense": {"size": 512, "distance": "Cosine"}},
  "sparse_vectors": {"bm25": {"modifier": "idf"}},     # Qdrant 服务端 BM25 推理
  "on_disk_payload": True,
  "hnsw_config": {"m": 16, "ef_construct": 100},
}
```

- 写入时同一 point 带两路：`vector.dense = [512 float]`，`vector.bm25 = qdrant_common.bm25_vector(content_text)`
- **中文必须先 bigram 切分再进 BM25**（A3 实测）。服务端 `qdrant/bm25` 只按空白和
  标点切词，整句中文只产出 1 个 token → 关键词路对中文等于废掉。三组对照的 top1
  命中率：原文↔原文 `1/4`、bigram↔bigram `3/4`、bigram 入库配原文查询 `2/4`。
  所以 `bm25_vector()` / `bm25_text()` 是唯一入口，**入库与查询两侧都要过它**；
  拉丁字母与数字整词保留（拆成 bigram 会让 `SKU-99213` 命中 `SKU-99214`）。
- 正文**不进 payload**（避免第二份真相），payload 只有标识与过滤字段
- 环境下限：Qdrant **≥ 1.15.3** 才支持自托管服务端 BM25；1.13.x 会在 upsert 报 `InferenceService is not initialized`

### payload 字段与索引

| 字段 | 索引类型 | 用途 |
|---|---|---|
| `doc_key`, `version_id`, `chunk_id`, `chunk_type`, `status`, `parent_id` | keyword | 过滤 / 回查 / 同父片去重（§7.2） |
| `is_public` | bool | 公开分支 |
| `allowed_depts`, `allowed_roles` | keyword（数组） | `match.any` = 有交集 |
| `effective_year`, `effective_date`, `page_num` | integer | 年份/日期过滤、引用定位 |
| `date_confidence` | keyword | 时间过滤只认 `explicit`（§5.2） |

### point id 映射（**必读，附录 B 的 `id=chunk_id` 存不进去**）

Qdrant 的 point id 只能是 **无符号整数或 UUID**，`ch_9f2c31` 两者都不是。
所以：

```python
point_id = str(uuid.uuid5(POINT_NAMESPACE, chunk_id))   # 确定性、可重放
payload["chunk_id"] = chunk_id                          # 真 id 放 payload
```

`POINT_NAMESPACE` 是 `scripts/qdrant_common.py` 里的固定常量。好处是 upsert 天然幂等
（重跑覆盖同一点而不是产生重复），代价是读回来必须用 `payload.chunk_id` 对 PG，
不能拿 `point.id` 当业务 id。**摄取卡（A3）必须复用 `point_id_for()`，不要自己拼。**

### 检索过滤的形状（§5.2，实现方照抄）

```python
filter = {
  "must": [
    {"key": "status", "match": {"value": "active"}},
    {"key": "doc_key", "match": {"any": [doc_key]}},        # 可选，scope 限定
  ],
  "should": [                                               # 三个分支取或
    {"key": "is_public",     "match": {"value": True}},
    {"key": "allowed_depts", "match": {"any": user_depts}},
    {"key": "allowed_roles", "match": {"any": user_roles}},
  ],
  # 时间条件（仅当问句有时间限定）
  # "must" += {"key": "effective_year", "range": {"gte": y0, "lte": y1}}
}
```

两路 prefetch **必须带完全相同的 filter**（§7.1），否则会出现"一路漏了权限"的裂缝。

---

## 6. 重建 runbook

`scripts/qdrant_rebuild.py`：从 PG 读 `content_text` → TEI 生成 512 dim dense → Qdrant 服务端算 BM25
→ 同一 point 写入。默认只取**双 active**（`v_active_chunks`），`--all-status` 可取非 purged。

```bash
make index-rebuild-dry                       # 先看条数与维度（会真的探一次 embedding）
make index-rebuild                           # 全量重建
docker compose exec -T api python scripts/qdrant_rebuild.py --doc-key policy/travel-cn --prune
```

参数要点：

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只出计划：条数按 chunk_type 分布、embedding dim/norm、集合现状、批次数；不编码正文、不写 |
| `--doc-key` / `--version-id` | 定向重建（可重复），配合 `--prune` 只清范围内的过期点 |
| `--prune` | 删掉"PG 里已不存在但集合里还在"的点；**只在同一 scope 内删**，不会误伤别的文档 |
| `--batch-size` | 默认 32，与 TEI `--max-client-batch-size 32` 对齐；调大只会被拆 |
| `--all-status` | 含 pending/deprecated（灰度、排查"为什么查不到"时用） |
| `--ensure-collection` | 集合不存在时顺手建（含 payload 索引） |

耗时估算：CPU 单条编码数十毫秒量级，**全量 40–60 万切片按小时计**，别在业务高峰跑。
中断可直接重跑——point id 确定性，重跑是覆盖不是叠加。

**什么时候必须重建**：换 embedding 模型或维度（size 不同不能混用）、BM25 分词策略变了、
集合被 `--recreate` 重建过、对账发现漂移且补丁修不回来。

---

## 7. 验收记录（DA1，2026-11-24）

环境：`pgvector/pgvector:pg16` + `qdrant/qdrant:v1.15.3` + TEI `bge-small-zh-v1.5`，
临时库 `ai-rag-migtest`（tmpfs，验完删除），临时集合 `kb_chunks_selftest`。

| 项 | 期望 | 实测 |
|---|---|---|
| 新库迁移 | 成功，台账 1 行 | `apply 001_init.sql ... ok (17 ms)` |
| 迁移重跑 | no-op | `nothing to do: schema is up to date` |
| 台账状态 | applied=1 pending=0 | 同上 |
| 同 doc_key 第二个 active | 被拒 | `duplicate key ... ux_active_version` |
| 重复内容入库 | 被拒 | `uq_doc_versions_doc_hash` |
| active 无 activated_at | 被拒 | `ck_doc_versions_active_has_activated_at` |
| 非 SHA-256 指纹 | 被拒 | `ck_doc_versions_hash_sha256` |
| 切片挂到别文档的版本 | 被拒 | `fk_chunks_version`（`Key (version_id, doc_key)=... is not present`） |
| 空白正文 | 被拒 | `ck_chunks_text_nonempty` |
| bbox 非数组 / 非法 chunk_type | 被拒 | `ck_chunks_bbox` / `ck_chunks_type` |
| 1024 维写入 `vector(512)` | 被拒 | `expected 512 dimensions, not 1024` |
| `qa_log` 非法 gate1 | 被拒 | `ck_qa_log_gate1` |
| 集合创建 | dense 512/Cosine + bm25/idf + 11 索引 | `verify : OK ... payload_indexes=11` |
| 集合幂等 | 第二次 0 新建 | `created 0 index(es) (all present)` |
| 维度不匹配的旧集合 | 拒绝并退出 2 | `MISMATCH: dense size=1024 != 512` |
| `--recreate` | 重建为 512 | `verify : OK` |
| 重建 dry-run | 计划正确 | `rows: 4 chunks {'figure': 1, 'table': 1, 'text': 2}` |
| 重建执行 | 4 点，pending 版本不入库 | `upserted=4 ... collection_points=4` |
| 混合检索 | RRF 命中 | `ch_t1 score=0.5 / ch_t3 0.33 / ch_l1 0.25 / ch_l2 0.2` |
| 权限过滤 | 公开 ∪ depts~fin ∪ roles~staff | 公开两条 + `roles=['staff']` 一条；`depts=['hr']` 的私有切片被排除 |
| 年份过滤 | 只回 2026 | `effective_year=2026 → ch_l1` |
| `--prune` | 删 1 个 PG 已无的点，范围外不动 | `prune : deleted 1 stale point(s) in scope` |
| `--all-status` 分支 | 含 pending（2 条），默认只 1 条 | `status=all-but-purged → rows: 2` / `active only → rows: 1` |
| 真实集合 `kb_chunks` 写入 | 2 点可写可查 | `upserted=2 ... collection_points=2`，按 `doc_key` 过滤 count=2（验完已删，集合回到 0 点） |
| api 容器启动时的迁移 | entrypoint 跑 runner，已是 no-op | `target: postgres:5432/ai_rag` / `nothing to do: schema is up to date` |

---

## 8. 已知限制 / 待定

1. **`scripts/smoke.py` 会往真实 `kb_chunks` 里写一个 payload 不全的点**（`id=9000`，无 `status`）。
   它不影响检索（filter 恒带 `status=active`），但会污染 `points_count`。建议后续把 smoke 改到
   独立集合（如 `kb_chunks_smoke`），或每次 smoke 后删点。DA1 已手工清掉遗留点，集合当前 0 点。
2. **`chunks.doc_key` 与 `doc_versions.doc_key` 的一致性由复合外键保证**，但 `chunks` 不再单独
   外键到 `documents`——删文档要先删版本（`ON DELETE RESTRICT` 会拦），这是有意的。
3. **`embedding_dense` 无 HNSW 索引**：单机试点阶段检索全走 Qdrant，PG 侧不做近邻查询；
   若将来要在 PG 里做向量检索，需另加 `USING hnsw (embedding_dense vector_cosine_ops)`。
4. **`qa_log` 保留一年**：没有自动清理任务。上线前加一个定时 `DELETE`（或按月分区），
   放在 §4.3 回收任务旁边。
5. **`ingest_jobs.status` 枚举已在 002 里落成 CHECK**（见 §3 的状态机表），不再是待定项。
