# 企业级零幻觉多模态 RAG 详细设计文档 — 编写计划

> **本文件是 Plan Mode 交付物**：用于规划“如何编写详细设计文档”，不是详细设计文档本身。  
> **状态**：待评审 / 未执行  
> **生成时间**：2026-11-21 06:06:29 CST  
> **目标交付物**：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`  
> **输入文档（只读）**：  
> - `/workspace/.hermes/attachments/architecture.md`  
> - `/workspace/.hermes/attachments/design_spec.md`

## 1. 目标

基于两份输入文档，产出一份可直接指导实现的**中文详细设计文档**，覆盖企业级零幻觉多模态 RAG 系统的架构、数据模型、流程、接口、安全、测试与里程碑。存储底座按已确认决策重基线为：**PostgreSQL + pgvector（dense + sparse）+ Elasticsearch（BM25）+ MinIO + PostgreSQL（元数据/版本/审计）**。

## 2. 当前上下文与已确认决策

### 已确认决策（不再重复询问）

| 决策项 | 结论 | 说明 |
|---|---|---|
| 详细设计落盘路径 | `/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 与未来项目放在同一目录下 |
| 项目模式 | 绿场新建 | 建议仓库根目录 `/workspace/enterprise-rag-zero-hallucination/`；本计划不创建业务代码 |
| 向量存储 | **pgvector**（替代源文档中的 Qdrant/Milvus） | 必须覆盖 dense + sparse；设计中需验证 `sparsevec`/索引/PG 版本可行性 |
| BM25 / 词法检索 | **保留 Elasticsearch** | 保持原双路召回中的 BM25/关键词职责 |
| 对象存储 | **MinIO**（私有化自托管，S3 兼容） | 内网部署、预签名 URL、HTTP Range 分片加载 |
| 元数据/版本/审计库 | **PostgreSQL** | 可与 pgvector 共用 PG 集群起步；设计需明确资源隔离/拆分边界 |
| 输入依据 | `architecture.md` + `design_spec.md` | 不得臆造业务需求；缺口标注为待确认项 |
| 文档语言 | **中文** | 最终详细设计文档使用中文编写；代码、字段名、协议名、SQL、Mermaid 保留必要英文标识 |

### 需继承的源文档硬约束

- **零幻觉**：Fail-Closed、Evidence-Only、强制 `[N]` 引用、禁止参数化知识猜测。
- **三道防线**：Rerank 阈值门控（`<0.20` 硬拒答、`0.20~0.40` 低置信度拒答、`>=0.40` 放行）、`Temperature=0` 受限生成、生成后 NLI/事实核查。
- **严格 RBAC Pre-Filtering**：召回前强制 `status == ACTIVE AND (is_public OR 部门命中 OR 角色命中)`；无权限文档不可见、不可泄露存在性。
- **原子版本切换**：`doc_key + file_hash`；`PENDING -> ACTIVE / DEPRECATED -> PURGED`；解析失败不影响旧版 ACTIVE。
- **多模态摄取**：MinerU 版面+BBox；二维表 HTML 无损；统计图/架构图经 VLM 结构化转译并绑定 `image_url`。
- **分块与时间**：Parent-Child 分块；多粒度业务时间抽取；动态时间 DSL；时间过滤要求 `date_confidence == explicit`。
- **前端溯源**：React + TypeScript + PDF.js；BBox 高亮；SSE 事件 `step/clarify/token/citations/fallback/done`。
- **规模/SLA**：PoC 100 份 -> 全量 10,000 份 PDF；约 40-60 万 child chunk；百万级 Pre-Filter 目标 <30ms；单跳 TTFT ~1.0s / 总计 1.5-2.5s；多跳 4-8s、Max Hops=3。

### pgvector 替换影响（设计必须显式覆盖）

| 模块 | 影响 | 设计动作 |
|---|---|---|
| 架构/部署 | 中 | 移除 Qdrant/Milvus；增加 `pgvector` 扩展；定义单 PG 集群逻辑隔离或独立向量 PG 的边界 |
| 数据模型 | **大** | 将 Qdrant Payload Schema 改为 PG DDL：向量列、标量过滤列、GIN/BTree 索引、`bbox jsonb` |
| dense+sparse | **大** | 验证 pgvector `vector` + `sparsevec`（及 PG 版本要求）；BGE-M3 dense 1024 维；sparse 权重表示与索引/查询策略单独设计 |
| Pre-Filtering | **大/最高风险** | SQL `WHERE` 与 ANN 执行计划必须保证正确性与延迟；评估 partial HNSW、分区、迭代扫描参数；必须有基准压测门禁 |
| 原子版本切换 | 正向/中 | 向量可见性可与 `doc_versions/chunks` 在 PG 事务内切换；ES 仍外部系统，需补偿/对账/GC |
| 检索查询 | 中 | Dense 召回改为 PG SQL（`ORDER BY embedding <=> $q LIMIT K` + 过滤）；BM25 仍走 ES；RRF+Rerank 不变 |
| GC / VACUUM | 中大 | 版本覆盖产生更新/删除；需 autovacuum 调优、批量 purge、必要时分区、索引维护策略 |
| 容量/SLA | 中 | 用 PG 内存/磁盘/索引放大重新估算；重测 Pre-Filter+ANN P95 |

## 3. 架构/方案概要

详细设计文档将作为 `/workspace/enterprise-rag-zero-hallucination/docs/` 下的单一权威 Markdown 文档，按源文档结构逐节编写并汉化表达。系统行为保持源文档的零幻觉约束与接口语义不变；向量存储章节重构为 pgvector-first：PostgreSQL 承载元数据/版本状态、dense+sparse 向量与审计表，Elasticsearch 保留 BM25 词法召回，MinIO 存储原始 PDF 与图表切片。所有受 pgvector 影响的设计必须包含：表结构、索引、查询形状、失败模式、运维策略和验证标准；每项关键需求应可追溯到源文档或标注为实现决策/待确认项。

## 4. 分步任务

> 按顺序执行。每个任务控制在 2-5 分钟聚焦工作量。  
> **本计划不创建业务代码**；产物是详细设计文档。  
> 命令默认在 `/workspace` 执行。

### 任务 1：创建文档骨架与元信息

**文件：**
- 创建：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`

**步骤 1：创建目录**

```bash
mkdir -p /workspace/enterprise-rag-zero-hallucination/docs
ls -ld /workspace/enterprise-rag-zero-hallucination/docs
```
预期：目录存在，退出码 0。

**步骤 2：写入中文骨架**

```markdown
# 企业级零幻觉多模态知识库系统 — 详细设计文档

> **版本**：v0.1-draft  
> **日期**：2026-11-21  
> **状态**：草稿 / 待评审  
> **输入文档**：`architecture.md`、`design_spec.md`（工作区附件）  
> **项目模式**：绿场（`/workspace/enterprise-rag-zero-hallucination/`）  
> **定版存储**：PostgreSQL 16+ + pgvector（dense+sparse）+ Elasticsearch 8.x（BM25）+ MinIO（S3 兼容）+ PostgreSQL（元数据/版本/审计）

## 0. 文档说明
## 1. 背景、目标与范围
## 2. 需求边界与非功能约束
## 3. 总体架构与部署拓扑
## 4. 核心数据模型与存储设计
## 5. 离线摄取与版本同步详细设计
## 6. 在线检索、推理与零幻觉生成详细设计
## 7. 前后端接口与溯源工作台详细设计
## 8. 安全、权限、审计与合规
## 9. 可观测性、容量规划与 SLA
## 10. 测试、验收与 PoC 评估方案
## 11. 风险、待决策项与里程碑
## 附录 A：DDL / Index / SQL 模板
## 附录 B：Prompt 模板
## 附录 C：SSE / API 契约示例
## 附录 D：源文档需求追踪矩阵
```

**步骤 3：校验骨架**

```bash
grep -n "^## " /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
```
预期：15 个章节标题（`## 0.` 到 `## 附录 D`）。

**步骤 4：提交（项目初始化后执行）**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 添加详细设计文档骨架"
```

### 任务 2：编写 §0-§2 — 范围、约束、术语与追踪

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`

**步骤 1：编写文档说明**
- 文档目的、读者（后端/前端/Infra/QA/PM）、范围外事项。
- 源文档追踪表：`design_spec.md` / `architecture.md` 章节 -> 详细设计章节。
- 术语表：`doc_key`、`file_hash`、`version_id`、`chunk_id`、`parent_id`、`bbox`、`Pre-Filtering`、`Fail-Closed`、`NLI`、`RRF`、`TTFT`。

**步骤 2：编写背景目标与边界**
- 目标：零幻觉多模态问答、原文溯源工作台、严格 RBAC 隔离。
- 边界：允许外部商业 VLM/LLM API；本地轻量 MinerU + 检索/精排；100 PoC -> 10k PDF；每周增量/覆盖；二维表+统计图+架构图；严格 RBAC；禁止猜测。
- 非目标：无参数化知识兜底；无未授权存在性泄露；无跨版本混召回；无图表像素目测估算。

**步骤 3：校验关键约束**

```bash
grep -nE "Fail-Closed|Pre-Filtering|doc_key \+ file_hash|BBox|Temperature = 0|ACTIVE" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md | head -n 20
```
预期：六类关键约束均能检索到。

**步骤 4：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 补充范围与需求边界"
```

### 任务 3：编写 §3 — 总体架构与部署拓扑（pgvector 重基线）

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`
- 参考：`architecture.md` §2、§7

**步骤 1：组件职责矩阵**

必须将 Qdrant/Milvus 替换为 pgvector，示例行：

| 层 | 组件 | 职责 | 技术 | 部署 | 关键约束 |
|---|---|---|---|---|---|
| 存储 | 向量+标量检索表 | dense/sparse 向量存储、元数据 Pre-Filter、ANN 召回 | PostgreSQL 16+ + pgvector（`vector`、`sparsevec`） | 同 PG 集群或独立向量 PG | Pre-Filter 与 ANN 正确性/延迟需压测；目标 <30ms |
| 存储 | 词法检索 | BM25/关键词、中文分词、型号/人名/编码精确命中 | Elasticsearch 8.x | ES 集群 | 与 dense 双路召回；镜像 ACL/时间/状态过滤 |
| 存储 | 对象存储 | 原始 PDF、页图、图表切片、签名 URL | MinIO（S3 兼容） | 私有集群 | 签名前鉴权；预签名 TTL；HTTP Range |
| 存储 | 元数据/版本/审计 | 文档版本状态机、审计日志、任务记录 | PostgreSQL 16+ | 主从 | 事务化状态翻转；审计追加写 |

**步骤 2：总体架构 Mermaid**
- 将 `Qdrant` 节点替换为 `PostgreSQL + pgvector`。
- 标注：PG 内版本状态与向量可见性可事务翻转；ES 仍需补偿同步。

**步骤 3：部署拓扑 Mermaid**
- 替换 `QdrantCluster` 为 `PostgreSQL 主从（启用 pgvector）`。
- 保留 Nginx/API、Rerank、Celery/Temporal、MinerU、ES、MinIO、出向 AI 网关。

**步骤 4：校验无活动选型残留**

```bash
grep -nE "Qdrant 集群|Milvus 集群|selected: Qdrant|当前选型: Qdrant" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
```
预期：无活动选型残留；若提及 Qdrant/Milvus，仅作为“源文档对比/替换说明”。

**步骤 5：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 重基线总体架构为 pgvector+ES+MinIO"
```

### 任务 4：编写 §4 — 数据模型与存储 DDL（pgvector 核心）

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`

**步骤 1：扩展与类型**

```sql
CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector: vector + sparsevec
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
  CREATE TYPE chunk_status AS ENUM ('PENDING','ACTIVE','DEPRECATED','PURGED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
  CREATE TYPE chunk_type AS ENUM ('text','table','figure');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
```

设计说明：`sparsevec` 需要支持稀疏向量的 pgvector 版本（建议 >=0.7.x，实施时 pin 精确版本）；不支持时回退到“pgvector dense + ES BM25”基线，并保留迁移路径。

**步骤 2：`documents` / `doc_versions` DDL**
沿用源文档 §6.3 字段，保留版本状态机与索引。

**步骤 3：`chunks` 表（替代 Qdrant Payload）**

```sql
CREATE TABLE chunks (
    chunk_id         TEXT PRIMARY KEY,
    doc_key          VARCHAR(255) NOT NULL,
    doc_id           VARCHAR(64) NOT NULL,
    version_id       VARCHAR(64) NOT NULL REFERENCES doc_versions(version_id),
    doc_name         VARCHAR(512) NOT NULL,
    status           chunk_status NOT NULL DEFAULT 'PENDING',
    chunk_type       chunk_type NOT NULL,
    parent_id        TEXT,
    page_num         INTEGER NOT NULL CHECK (page_num >= 1),
    bbox             JSONB NOT NULL, -- [[x0,y0,x1,y1], ...] 归一化 0..1
    image_url        TEXT,
    content_text     TEXT NOT NULL,
    table_html       TEXT,
    effective_date   INTEGER,        -- YYYYMMDD
    effective_year   INTEGER,
    date_confidence  VARCHAR(16) NOT NULL CHECK (date_confidence IN ('explicit','inferred')),
    is_public        BOOLEAN NOT NULL DEFAULT FALSE,
    allowed_depts    TEXT[] NOT NULL DEFAULT '{}',
    allowed_roles    TEXT[] NOT NULL DEFAULT '{}',
    embedding_dense  VECTOR(1024),   -- BGE-M3 dense，维度可配置
    embedding_sparse SPARSEVEC,      -- BGE-M3 sparse；回退策略下可空
    token_count      INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**步骤 4：索引与查询计划说明**
- BTree：`status`、`(doc_key,status)`、`version_id`、`effective_year`、`effective_date`、`date_confidence`、`is_public`。
- GIN：`allowed_depts`、`allowed_roles`。
- Dense ANN：HNSW `embedding_dense vector_cosine_ops`，`m/ef_construction/ef_search` 作为可调参数。
- Sparse：明确标注“验证所选 pgvector 版本的 sparsevec 索引/算子支持”；不满足则 feature flag 关闭。
- 候选 partial index：`WHERE status='ACTIVE'`，需验证维护成本与 planner 行为。

**步骤 5：ES Mapping 与 MinIO 路径**
沿用源文档 §6.2、§6.4，命名改为 MinIO/S3 兼容；ES 镜像 ACL/时间/状态过滤字段，PG 为版本真相源。

**步骤 6：校验字段覆盖**

```bash
grep -nE "allowed_depts|allowed_roles|date_confidence|bbox|embedding_dense|embedding_sparse|chunk_status" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md | wc -l
```
预期：>= 7。

**步骤 7：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 补充 pgvector 数据模型与 DDL"
```

### 任务 5：编写 §5 — 离线摄取、VLM 转译、编码与版本同步

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`
- 参考：`design_spec.md` §2、`architecture.md` §3、§6

**步骤 1：摄取流水线 Mermaid**
- PENDING 写入 `PostgreSQL+pgvector` 和 `Elasticsearch`；MinIO 存原 PDF/页图/切片。
- 原子翻转更新 PG；ES 通过同步/补偿任务收敛。

**步骤 2：MinerU 解析与分块规则**
- 版面区域、归一化 bbox、页眉页脚剔除。
- Parent-Child：Child 256-512 overlap 64；Parent 1024-2048 或完整章节。
- 表格 HTML 无损、整表防截断；图表裁剪 >=200 DPI。

**步骤 3：VLM 转译与双路编码**
- 统计图/架构图 Prompt（源文档 §5.4）。
- `chunk_type=figure` + `image_url`。
- BGE-M3 dense+sparse；业务时间与 RBAC 元数据抽取。

**步骤 4：版本状态机与原子翻转**

```text
BEGIN;
  INSERT/UPSERT doc_versions(new_version, status=PENDING, ...);
  INSERT chunks(..., status=PENDING, version_id=new_version);
COMMIT;

-- 全部写入与校验通过后
BEGIN;
  UPDATE chunks SET status='DEPRECATED', updated_at=now()
    WHERE doc_key=$1 AND status='ACTIVE' AND version_id <> $new_version;
  UPDATE doc_versions SET status='DEPRECATED' WHERE doc_key=$1 AND status='ACTIVE' AND version_id <> $new_version;
  UPDATE chunks SET status='ACTIVE', updated_at=now()
    WHERE doc_key=$1 AND version_id=$new_version AND status='PENDING';
  UPDATE doc_versions SET status='ACTIVE', activated_at=now() WHERE version_id=$new_version;
  UPDATE documents SET active_version=$new_version, updated_at=now() WHERE doc_key=$1;
COMMIT;

-- 异步：同步 ES（upsert ACTIVE, 删除/标记 DEPRECATED），再执行 PG/MinIO GC
```

**步骤 5：幂等、失败、重试、GC**
- 同 `doc_key+file_hash` 跳过；失败保留旧 ACTIVE；PENDING 清理；ES 补偿对账；PURGED 批量 GC（默认 24h 保留窗）；VACUUM/autovacuum 说明。

**步骤 6：校验**

```bash
grep -nE "PENDING -> ACTIVE|DEPRECATED -> PURGED|统计数据图转译 Prompt|系统/业务架构图转译 Prompt|doc_key \+ file_hash" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
```

**步骤 7：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 补充离线摄取与版本同步设计"
```

### 任务 6：编写 §6 — 在线检索、推理与零幻觉门控

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`
- 参考：`design_spec.md` §3-§5、`architecture.md` §4、§5

**步骤 1：查询理解与路由**
- JWT 部门/角色解析；系统时钟注入；指代消解/同义词扩展；相对时间 -> 绝对时间 DSL；`semantic_query` + `metadata_filter`；单跳/多跳分流。

**步骤 2：Pre-Filter 混合检索 SQL/API 形状**

```sql
SELECT chunk_id, doc_id, doc_name, page_num, bbox, chunk_type, image_url,
       content_text, table_html,
       1 - (embedding_dense <=> $1::vector) AS dense_score
FROM chunks
WHERE status = 'ACTIVE'
  AND (is_public = TRUE OR allowed_depts && $2::text[] OR allowed_roles && $3::text[])
  AND ($4::int IS NULL OR effective_year = $4)
  AND ($5::int IS NULL OR effective_date >= $5)
  AND ($6::int IS NULL OR effective_date <= $6)
  AND date_confidence = 'explicit' -- 时间过滤存在时强制
ORDER BY embedding_dense <=> $1::vector
LIMIT $7;
```

同时给出 ES BM25 镜像过滤查询、RRF 融合伪代码、Top-30/Top-5~8 参数。

**步骤 3：sparse 策略（已确认决策）**
- 选定模式：**pgvector 同时存 dense+sparse**。
- Dense：`VECTOR(1024)` cosine/HNSW。
- Sparse：`SPARSEVEC` 存 BGE-M3 词法权重；查询算子/索引能力按部署版本 pin。
- 回退规则：若 sparse 性能或支持不足，经 benchmark 确认后关闭 sparse feature flag，回退“dense(pgvector)+BM25(ES)”基线；不得静默降级且无指标。
- 验证清单：pgvector 版本、sparse 索引支持、执行计划、延迟、召回差异。

**步骤 4：Rerank、歧义澄清、多跳状态机**
- 阈值：`<0.20` 硬拒答；`0.20~0.40` 低置信度拒答+有权限文档提示；`>=0.40` 歧义评估/生成。
- 多跳：Max Hops=3，Evidence Blackboard，每跳独立阈值，断链降级，SSE step 事件。

**步骤 5：零幻觉生成与 NLI 门控**
- System Prompt（源文档 §5.2）、`temperature=0, top_p=1`、句末 `[N]`。
- NLI/事实核查覆盖数字/金额/百分比/日期/人名/专有名词；Contradiction/Neutral 触发 fallback。

**步骤 6：端到端时序 Mermaid**
改编 `architecture.md` §5，命名对齐 pgvector/ES/MinIO。

**步骤 7：校验**

```bash
grep -nE "Score < 0.20|0.20 <= Score < 0.40|Score >= 0.40|Max Hops = 3|Temperature = 0|event: fallback" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
```

**步骤 8：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 补充在线检索与零幻觉门控设计"
```

### 任务 7：编写 §7 — API/SSE 契约与前端溯源工作台

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`
- 参考：`design_spec.md` §7

**步骤 1：API 端点**
- `POST /api/v1/qa/stream`：请求体、鉴权、SSE 事件。
- `GET /api/v1/docs/{doc_id}/preview`：JWT+RBAC 校验、5 分钟 MinIO 预签名 URL、HTTP Range。
- 管理端（设计级）：上传/扫描注册、版本状态、重建索引、健康检查；需管理员鉴权。

**步骤 2：SSE Payload Schema**
沿用源文档 `step/clarify/token/citations/fallback/done` 示例，增加字段表：类型、必填、说明、安全备注。

**步骤 3：前端交互**
- 左侧问答栏：范围筛选、`@文档` 自动补全（仅 ACTIVE+有权限）、步骤条、figure 缩略图、可点击 `[N]`。
- 右侧 PDF.js：文档切换、页跳转、bbox overlay。

```text
left = x0 * viewport.width
top = y0 * viewport.height
width = (x1 - x0) * viewport.width
height = (y1 - y0) * viewport.height
```

**步骤 4：错误/拒答 UX 映射**
硬拒答、低置信度拒答+文档提示、clarify chips、fallback 替换、403 无权限预览、404/410 过期资产。

**步骤 5：校验**

```bash
grep -nE "event: step|event: clarify|event: token|event: citations|event: fallback|event: done" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
```

**步骤 6：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 补充 API、SSE 与前端溯源设计"
```

### 任务 8：编写 §8-§10 — 安全、可观测性、容量、测试验收

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`

**步骤 1：安全与合规**
- JWT 鉴权；RBAC Pre-Filter；MinIO 短 TTL 预签名；无存在性泄露；审计日志字段（问题标识、用户、部门/角色快照、chunk ids、分数、门控判定、模型/版本、延迟、拒答原因）；密钥管理；外部 LLM/VLM 出向代理；敏感数据处理。

**步骤 2：可观测性与 SLO**
- 指标/日志/链路：摄取耗时失败率、embedding 吞吐、PG/ES 查询延迟、rerank 分数分布、门控命中率、拒答率、NLI 失败率、TTFT/总耗时、GC lag。
- 告警：ES 同步积压、PG bloat/autovacuum lag、MinIO 签名失败、外部 API 错误/超时。

**步骤 3：容量与 pgvector 基准门禁**
- 沿用源文档容量估算，并补充 pgvector 行尺寸、HNSW 内存/磁盘放大、PG `shared_buffers/work_mem/maintenance_work_mem` 调优项。
- 强制基准：

| 基准项 | 数据集 | 通过标准 |
|---|---:|---|
| Dense ANN + ACL/时间过滤 | 60 万 chunk，真实 ACL 偏斜 | P95 < 30ms，recall@50 与无过滤基线可接受 |
| Sparse 查询（启用时） | 同上 | P95 在预算内；与 ES BM25-only 的召回差异可量化 |
| 版本翻转 + ES 同步 | 1 文档覆盖，5k chunks | 无混合 ACTIVE 版本可见；ES 收敛 < 目标 |
| GC/VACUUM | 连续 4 周 10% 覆盖 | 表/索引膨胀受控；延迟稳定 |

**步骤 4：测试与 PoC 验收**
- Golden Dataset 50 题：20 基础/表格、10 图表/架构、5 时间/多跳、15 陷阱题。
- 红线：陷阱题拒答率 100%；数值准确率 >=96%；角标溯源有效率 >=98%；Rerank 阈值标定 0.38~0.42 波谷。
- pgvector 回归：过滤正确性（无越权行）、版本隔离、sparse 回退等价性。

**步骤 5：校验**

```bash
grep -nE "拒答率（Refusal Recall）.*100%|Numerical Accuracy.*96%|Citation Precision.*98%|P95 < 30ms" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
```

**步骤 6：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 补充安全、可观测、容量与验收设计"
```

### 任务 9：编写 §11 + 附录 — 风险、待决策、里程碑、追踪矩阵

**文件：**
- 修改：`/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`

**步骤 1：风险登记表**

| 风险 | 影响 | 缓解措施 | 责任方 | 门禁 |
|---|---|---|---|---|
| pgvector Pre-Filter+ANN 未达 <30ms | SLA 不达标 | partial index/分区/调优；基准先行；实测失败后再评估独立向量 PG 或回退专用向量库 | Infra+Backend | 基准通过 |
| `sparsevec` 版本/索引行为不达预期 | sparse 路径不可用或慢 | pin PG/pgvector 版本；先实现 dense+ES BM25 基线；sparse feature flag | Backend | Spike 报告 |
| PG bloat（版本覆盖/GC） | 延迟漂移 | 批量 purge、autovacuum 调优、必要时分区、监控 dead tuples | DBA/Infra | 4 周 soak test |
| ES-PG 双写一致性 | 检索到过期/已删文档 | outbox/补偿同步；对账扫描；两侧 ACTIVE 过滤 | Backend | 对账测试 |
| VLM 图表转译错误 | 数值幻觉/错值 | Prompt 约束、置信标注、人工抽检、NLI 门控 | AI/QA | 数值准确率 >=96% |

**步骤 2：待决策项（实现前需确认）**
- 目标 PG/pgvector 精确版本及 `sparsevec`/HNSW/迭代扫描能力。
- 同 PG 实例还是独立向量 PG。
- BGE-M3 sparse 表示细节：最大非零项、查询编码路径、是否必须优于 ES BM25。
- ES 中文分词插件（`ik_max_word`/`ik_smart`）可用性。
- MinIO bucket/TLS/内网 endpoint/预签名 TTL（默认 5 分钟）/生命周期策略。
- 外部 LLM/VLM 模型名、配额、数据出域审批、脱敏策略、超时重试预算。
- IAM/RBAC JWT claim 契约（`user_depts/user_roles`、组嵌套、刷新/过期）。

**步骤 3：里程碑（本轮不写代码）**

| 里程碑 | 输出 | 退出标准 |
|---|---|---|
| M0 设计评审通过 | 本详细设计文档获批 | 待决策项有 owner 与截止时间 |
| M1 存储 Spike | pgvector dense+sparse + ES BM25 基准报告 | Pre-Filter P95/召回指标达标 |
| M2 摄取 MVP | 100 份 PDF 的 MinerU+VLM+版本状态机 | 原子翻转、无混合 ACTIVE、图表/表格切片正确 |
| M3 查询 MVP | 单跳 Fast-Path + 三道防线 | Golden 子集陷阱题拒答率 100% |
| M4 多跳+工作台 | LangGraph Max-3-Hop + PDF.js BBox 溯源 | 引用精度 >=98% |
| M5 PoC 验收 | 50 题 Golden 评估报告 | 全部红线通过 |

**步骤 4：附录 D 需求追踪矩阵**
逐条映射 `design_spec.md`/`architecture.md` 关键需求到详细设计章节与验证方式。

**步骤 5：最终一致性检查**

```bash
rg -n "Qdrant|Milvus" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
rg -n "pgvector|sparsevec|MinIO|Elasticsearch" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md | wc -l
grep -n "^## " /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md
```
预期：Qdrant/Milvus 仅出现在替换说明/对比中；pgvector/MinIO/Elasticsearch 全文覆盖；章节完整。

**步骤 6：提交**

```bash
# git add docs/detailed-design.md && git commit -m "docs: 完成详细设计风险、里程碑与追踪矩阵"
```

## 5. 测试 / 验证

本交付物为文档，验证以结构完整性、一致性和需求覆盖为主。

| 检查项 | 命令 | 预期 |
|---|---|---|
| 文件存在 | `test -f /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md && echo ok` | `ok` |
| 章节完整 | `grep -c "^## " /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | `>= 15` |
| pgvector 已定版 | `rg -n "pgvector|embedding_dense|sparsevec" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 有匹配 |
| 无活动 Qdrant/Milvus 选型 | `rg -n "^### .*Qdrant|当前选型.*Qdrant|QdrantCluster" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 无活动选型匹配 |
| 三道防线阈值 | `grep -nE "Score < 0.20|0.20 <= Score < 0.40|Score >= 0.40|Max Hops = 3|Temperature = 0" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 全部存在 |
| SSE 契约 | `grep -nE "event: step|event: clarify|event: token|event: citations|event: fallback|event: done" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 6+ 匹配 |
| 版本状态机 | `grep -nE "PENDING|ACTIVE|DEPRECATED|PURGED|doc_key|file_hash" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 全部存在 |
| RBAC Pre-Filter | `grep -nE "Pre-Filtering|allowed_depts|allowed_roles|is_public|ACTIVE" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 全部存在 |
| Mermaid 图存在 | `grep -n "```mermaid" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | >= 4 |
| 无敏感信息泄露 | `rg -n "AKIA|BEGIN PRIVATE KEY|sk-[A-Za-z0-9]" /workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md` | 无匹配 |

## 6. 风险、权衡与待确认问题

### 风险与权衡

- **pgvector Pre-Filter + ANN 延迟是首要风险**。源文档 <30ms 目标偏向专用向量库语境；pgvector 在本规模可行但必须通过真实数据压测门禁。
- **dense+sparse 双向量增加复杂度**。`sparsevec` 能力与版本强相关；安全基线是 dense(pgvector)+BM25(ES)，sparse 通过 feature flag 灰度。
- **单 PG 承载元数据+向量简化一致性但扩大故障域**。设计需允许逻辑隔离，并在压测不达标时支持物理拆分（元数据 PG + 向量 PG/只读副本）。
- **ES 仍是外部一致性边界**。PG 可事务翻转版本可见性，但 ES 需补偿同步与对账扫描。

### 待确认问题（实现前确认，设计中显式标注）

1. **PG/pgvector 精确版本**：确认 `sparsevec`、HNSW、迭代扫描参数能力。
2. **PG 部署形态**：单实例共用还是元数据/向量物理隔离。
3. **BGE-M3 sparse 细节**：稀疏表示、最大非零项、查询编码、是否必须优于 ES BM25 才启用。
4. **ES 分词插件**：私有部署是否具备 `ik_max_word`/`ik_smart`。
5. **MinIO 细节**：bucket/TLS/endpoint/预签名 TTL（默认 5 分钟）/生命周期策略。
6. **外部模型契约**：VLM/LLM 端点、配额、出域审批、脱敏、超时重试预算。
7. **IAM/RBAC 契约**：JWT 中 `user_depts/user_roles` 字段、组嵌套、刷新/过期策略。

## 7. 本地 Docker 开发可行性（已确认）

本地 PoC/开发阶段使用 Docker Compose 可行，建议作为 M1 存储 Spike 的默认环境：

| 组件 | 本地可行性 | 建议镜像/形态 | 关键注意点 |
|---|---|---|---|
| PostgreSQL + pgvector | 可行 | `pgvector/pgvector:pg16`（pin 支持 `sparsevec` 的版本，建议 pgvector >= 0.7.x） | 启用 `CREATE EXTENSION vector`; dense+sparse 先在该镜像验证；named volume 持久化 |
| Elasticsearch (BM25) | 可行，但资源占用高 | `docker.elastic.co/elasticsearch/elasticsearch:8.x` 单节点 dev | Linux/WSL2 需 `vm.max_map_count>=262144`；dev 用 `discovery.type=single-node`；限制 heap（如 1g）；IK 插件需自定义镜像或启动安装 |
| MinIO | 可行 | `minio/minio` | 设置 `MINIO_ROOT_USER/PASSWORD`；挂数据 volume；分离 S3 API 与 Console 端口 |
| 元数据 PostgreSQL | 可行 | 与 pgvector **同一 PG 实例**起步 | dev 共用实例最省；压测或隔离需求出现后再拆分 |

开发建议：一个 `docker-compose.yml` 拉起 `pgvector-postgres + elasticsearch + minio`，仅暴露必要端口；ES 至少 1-2GB heap，16GB 内存机器较稳；dev 配置不得直接当生产（生产需 TLS、鉴权、密钥管理、备份）。

## 8. 执行交接

计划评审通过后，按任务 1-9 顺序编写 `/workspace/enterprise-rag-zero-hallucination/docs/detailed-design.md`，每完成主要章节运行 §5 校验命令；项目初始化 git 后按任务提交。**本轮不实现业务代码。**