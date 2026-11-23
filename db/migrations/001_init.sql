-- ============================================================================
-- 001_init.sql — ai-rag 初始 schema
--
-- 权威依据：详细设计.md 附录 A（字段字典）/ 附录 B（表与索引）
-- 口径文档：db/README.md（含与附录的差异说明，务必一起读）
--
-- 约定
--   * 本文件由 scripts/db_migrate.py 在单个事务里执行；不要在这里写 BEGIN/COMMIT。
--   * 一条语句一个语义块，注释写清"为什么"，不写"是什么"。
--   * PG 是唯一真相：chunks.content_text 是检索文本的来源，向量库只是副本。
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- documents：一份文件一行，doc_key 稳定不随内容变化（附录 A.1）
-- ---------------------------------------------------------------------------
CREATE TABLE documents (
    doc_key     text        PRIMARY KEY,
    filename    text        NOT NULL,
    page_count  int         CHECK (page_count IS NULL OR page_count > 0),
    status      text        NOT NULL DEFAULT 'queued',
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_documents_status
        CHECK (status IN ('queued', 'parsed', 'active', 'failed'))
);

COMMENT ON TABLE  documents IS '文档主表：一份文件一行，doc_key 由路径与标题归一化得到';
COMMENT ON COLUMN documents.doc_key IS '稳定标识，例 policy/travel-cn；不随内容覆盖变化';
COMMENT ON COLUMN documents.status IS 'queued / parsed / active / failed';

-- ---------------------------------------------------------------------------
-- doc_versions：同一 doc_key 的每次内容变化一行（附录 A.2）
-- ux_active_version 是"一份文档只有一个生效版本"的硬约束，翻牌靠它在库里兜底。
-- ---------------------------------------------------------------------------
CREATE TABLE doc_versions (
    version_id    text        PRIMARY KEY,
    doc_key       text        NOT NULL REFERENCES documents (doc_key) ON DELETE RESTRICT,
    file_hash     text        NOT NULL,
    status        text        NOT NULL DEFAULT 'pending',
    activated_at  timestamptz,
    superseded_by text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_doc_versions_doc_hash UNIQUE (doc_key, file_hash),
    -- 供 chunks 做 (version_id, doc_key) 复合外键，防止切片挂到别的文档的版本下
    CONSTRAINT uq_doc_versions_version_doc UNIQUE (version_id, doc_key),
    CONSTRAINT ck_doc_versions_status
        CHECK (status IN ('pending', 'active', 'deprecated', 'purged', 'failed', 'skipped')),
    CONSTRAINT ck_doc_versions_hash_sha256
        CHECK (file_hash ~ '^[0-9a-f]{64}$'),
    -- 生效版本必须有翻牌时间，避免出现"active 但不知道何时生效"的行
    CONSTRAINT ck_doc_versions_active_has_activated_at
        CHECK (status <> 'active' OR activated_at IS NOT NULL)
);

COMMENT ON TABLE  doc_versions IS '版本表：同一 doc_key 每次内容指纹变化一行';
COMMENT ON COLUMN doc_versions.version_id IS 'doc_key + 内容指纹短码，例 policy/travel-cn#a91f';
COMMENT ON COLUMN doc_versions.file_hash IS '文件字节 SHA-256，64 位小写十六进制（用于判重，不用短码）';
COMMENT ON COLUMN doc_versions.status IS 'pending / active / deprecated / purged / failed / skipped';
COMMENT ON COLUMN doc_versions.superseded_by IS '被哪个 version_id 取代；故意不加外键——旧版 purge 后仍要保留指向，供审计追查';
COMMENT ON COLUMN doc_versions.created_at IS '行创建时间（附录 A 未列，判定"最新入库版本"需要，非业务语义）';

-- 一份文档同时只能有一个 active 版本：并发翻牌在数据库层被拦，不靠应用自觉
CREATE UNIQUE INDEX ux_active_version ON doc_versions (doc_key) WHERE status = 'active';

CREATE INDEX ix_doc_versions_doc_status ON doc_versions (doc_key, status);
-- 回收任务扫"已下线超过 24h"的版本
CREATE INDEX ix_doc_versions_deprecated_at
    ON doc_versions (activated_at) WHERE status = 'deprecated';

-- ---------------------------------------------------------------------------
-- chunks：检索单元（附录 A.3）
-- 复合外键 (version_id, doc_key) 保证切片所属版本属于同一 doc_key：权限过滤按
-- doc_key / version_id 分开存两份，二者不自洽就会造成越权命中（§5.2、§7.1）。
-- ---------------------------------------------------------------------------
CREATE TABLE chunks (
    chunk_id        text        PRIMARY KEY,
    doc_key         text        NOT NULL,
    version_id      text        NOT NULL,
    chunk_type      text        NOT NULL,
    parent_id       text,
    page_num        int         NOT NULL,
    bbox            jsonb       NOT NULL,
    image_path      text,
    content_text    text        NOT NULL,
    table_html      text,
    table_md        text,
    effective_date  int,
    effective_year  int,
    date_confidence text        NOT NULL DEFAULT 'inferred',
    is_public       boolean     NOT NULL DEFAULT false,
    allowed_depts   text[]      NOT NULL DEFAULT '{}',
    allowed_roles   text[]      NOT NULL DEFAULT '{}',
    embedding_dense vector(512),
    token_count     int,
    status          text        NOT NULL DEFAULT 'pending',
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_chunks_version FOREIGN KEY (version_id, doc_key)
        REFERENCES doc_versions (version_id, doc_key) ON DELETE CASCADE,
    CONSTRAINT ck_chunks_type     CHECK (chunk_type IN ('text', 'table', 'figure')),
    CONSTRAINT ck_chunks_page     CHECK (page_num >= 1),
    CONSTRAINT ck_chunks_bbox     CHECK (jsonb_typeof(bbox) = 'array'),
    -- 空正文的切片进不了 BM25（稀疏向量为空会被 Qdrant 拒绝），在库里先挡掉
    CONSTRAINT ck_chunks_text_nonempty CHECK (btrim(content_text) <> ''),
    CONSTRAINT ck_chunks_date     CHECK (effective_date IS NULL
                                         OR effective_date BETWEEN 19000101 AND 29991231),
    CONSTRAINT ck_chunks_year     CHECK (effective_year IS NULL
                                         OR effective_year BETWEEN 1900 AND 2999),
    CONSTRAINT ck_chunks_conf     CHECK (date_confidence IN ('explicit', 'inferred')),
    CONSTRAINT ck_chunks_tokens   CHECK (token_count IS NULL OR token_count >= 0),
    CONSTRAINT ck_chunks_status   CHECK (status IN ('pending', 'active', 'deprecated', 'purged'))
);

COMMENT ON TABLE  chunks IS '切片表：检索与引用的最小单元，向量库只存副本';
COMMENT ON COLUMN chunks.chunk_id IS '全局唯一切片编号，例 ch_9f2c31；Qdrant point id 由它派生（见 db/README.md）';
COMMENT ON COLUMN chunks.parent_id IS '子片指向父片 chunk_id；父片为 NULL。故意不加自引用外键：批量装载时父子顺序不该由约束强制';
COMMENT ON COLUMN chunks.bbox IS '归一化矩形数组 [[x0,y0,x1,y1], ...]';
COMMENT ON COLUMN chunks.effective_date IS 'YYYYMMDD，仅显式日期写入';
COMMENT ON COLUMN chunks.effective_year IS '由 effective_date 派生，供年份过滤';
COMMENT ON COLUMN chunks.date_confidence IS 'explicit：原文显式日期；inferred：推断得到（时间过滤只认 explicit）';
COMMENT ON COLUMN chunks.allowed_depts IS '空数组表示不按部门放行；命中判断是"有交集"不是"包含"';
COMMENT ON COLUMN chunks.allowed_roles IS '空数组表示不按角色放行；命中判断是"有交集"';
COMMENT ON COLUMN chunks.embedding_dense IS 'bge-small-zh-v1.5 归一化向量（512 维）；Qdrant 的副本，可空';
COMMENT ON COLUMN chunks.status IS 'pending / active / deprecated / purged；翻牌时按 version_id 批量改（§4.1）';

-- 附录 B 的查询形状：按文档取版本切片
CREATE INDEX ix_chunks_doc_status ON chunks (doc_key, version_id);
-- 翻牌/回收按 version_id 批量改状态、按版本删切片：version_id 必须在最左列
CREATE INDEX ix_chunks_version_status ON chunks (version_id, status);
CREATE INDEX ix_chunks_year ON chunks (effective_year) WHERE effective_year IS NOT NULL;
CREATE INDEX ix_chunks_date ON chunks (effective_date) WHERE effective_date IS NOT NULL;
-- GIN 支撑 "有交集" 判断：allowed_depts && ARRAY['fin','corp']
CREATE INDEX ix_chunks_depts ON chunks USING gin (allowed_depts);
CREATE INDEX ix_chunks_roles ON chunks USING gin (allowed_roles);
-- 同父片最多保留两条子片的去重（§7.2）按 parent_id 找兄弟
CREATE INDEX ix_chunks_parent ON chunks (parent_id) WHERE parent_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- ingest_jobs：摄取任务队列表（附录 B）
-- status 不加 CHECK：状态机归 A1（ingest）所有，建议 queued/running/succeeded/failed/canceled，
-- 定稿后由 A1 提一个加约束的迁移，避免两处各自发明枚举。
-- ---------------------------------------------------------------------------
CREATE TABLE ingest_jobs (
    job_id      bigserial   PRIMARY KEY,
    doc_key     text        NOT NULL,
    version_id  text,
    status      text        NOT NULL DEFAULT 'queued',
    error       text,
    retry_count int         NOT NULL DEFAULT 0,
    started_at  timestamptz,
    finished_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_ingest_jobs_retry    CHECK (retry_count >= 0),
    CONSTRAINT ck_ingest_jobs_timeorder CHECK (finished_at IS NULL OR started_at IS NULL
                                               OR finished_at >= started_at)
);

COMMENT ON TABLE  ingest_jobs IS '摄取任务队列；同一 doc_key 的任务用 advisory lock 串行化，不必靠唯一约束';
COMMENT ON COLUMN ingest_jobs.status IS '状态机由 A1 定义，建议 queued/running/succeeded/failed/canceled（本表暂不加 CHECK）';
COMMENT ON COLUMN ingest_jobs.error IS '最近一次失败的机器可读原因或异常首行，完整栈写日志不写库';

CREATE INDEX ix_ingest_jobs_doc ON ingest_jobs (doc_key, created_at DESC);
-- 取待跑任务的形状：只扫 queued/running，索引小且热
CREATE INDEX ix_ingest_jobs_open
    ON ingest_jobs (created_at) WHERE status IN ('queued', 'running');

-- ---------------------------------------------------------------------------
-- qa_log：一次问答一行（附录 A.4 + §11.2 身份快照）
-- 只存切片编号与分数摘要，不存原文——原文在 chunks 里，重复存就是第二份真相。
-- ---------------------------------------------------------------------------
CREATE TABLE qa_log (
    id             bigserial   PRIMARY KEY,
    question       text        NOT NULL,
    retrieved_ids  text[]      NOT NULL DEFAULT '{}',
    score_snapshot jsonb       NOT NULL DEFAULT '{}'::jsonb,
    gate1_verdict  text        NOT NULL,
    gate2_verdict  text,
    hop_count      int         NOT NULL DEFAULT 0,
    answer         text,
    citations      jsonb,
    reject_reason  text,
    latency_ms     int         NOT NULL,
    model          text,
    user_id        text,
    user_depts     text[]      NOT NULL DEFAULT '{}',
    user_roles     text[]      NOT NULL DEFAULT '{}',
    is_admin       boolean     NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_qa_log_gate1
        CHECK (gate1_verdict IN ('pass', 'reject_none', 'reject_low', 'clarify')),
    CONSTRAINT ck_qa_log_gate2
        CHECK (gate2_verdict IS NULL
               OR gate2_verdict IN ('supported', 'not_supported', 'skipped')),
    CONSTRAINT ck_qa_log_hops      CHECK (hop_count >= 0),
    CONSTRAINT ck_qa_log_latency   CHECK (latency_ms >= 0)
);

COMMENT ON TABLE  qa_log IS '问答审计：一次问答一行，保留一年（§11.2）';
COMMENT ON COLUMN qa_log.retrieved_ids IS '进精排的切片编号，按上游顺序';
COMMENT ON COLUMN qa_log.score_snapshot IS '各路最高分与分布，例 {"top1":0.62,"dense_top1":0.61,"bm25_top1":18.4}';
COMMENT ON COLUMN qa_log.gate1_verdict IS 'pass / reject_none / reject_low / clarify';
COMMENT ON COLUMN qa_log.gate2_verdict IS 'supported / not_supported / skipped';
COMMENT ON COLUMN qa_log.user_id IS '身份快照（§11.2）：用户标识；查询分析时做聚合，不导出个人级明细';
COMMENT ON COLUMN qa_log.user_depts IS '身份快照中的部门列表，用于事后判定"该看到的/不该看到的"';
COMMENT ON COLUMN qa_log.is_admin IS '管理员跨部门访问必须在审计里可辨（§5.1）';

CREATE INDEX ix_qa_log_created ON qa_log (created_at DESC);
CREATE INDEX ix_qa_log_gate1 ON qa_log (gate1_verdict, created_at DESC);
CREATE INDEX ix_qa_log_reject ON qa_log (reject_reason) WHERE reject_reason IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 视图：当前可检索切片 = 生效版本下的生效切片
-- 检索侧、重建脚本、对账脚本都按这个口径取数，避免各自写一遍 JOIN 写歪。
-- ---------------------------------------------------------------------------
CREATE VIEW v_active_chunks AS
SELECT c.chunk_id,
       c.doc_key,
       c.version_id,
       c.chunk_type,
       c.parent_id,
       c.page_num,
       c.bbox,
       c.image_path,
       c.content_text,
       c.table_md,
       c.effective_date,
       c.effective_year,
       c.date_confidence,
       c.is_public,
       c.allowed_depts,
       c.allowed_roles,
       c.token_count
FROM chunks c
JOIN doc_versions v ON v.version_id = c.version_id
WHERE v.status = 'active'
  AND c.status = 'active';

COMMENT ON VIEW v_active_chunks IS '当前可检索切片：version 与 chunk 双 active；向量库重建与对账的唯一取数口径';
