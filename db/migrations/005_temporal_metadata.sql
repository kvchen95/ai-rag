-- ============================================================================
-- 005_temporal_metadata.sql — 文档/切片的三层时间元数据
--
-- 目标：把“报告所属期间”“发布时间”“旧生效日期”分开，避免把预测目标年份
-- 当作文档有效日期。文档层是权威，chunks 继承文档层字段；旧 effective_date/year
-- 保留作兼容，不再作为首选过滤口径。
-- ============================================================================

ALTER TABLE documents
    ADD COLUMN company text,
    ADD COLUMN report_type text,
    ADD COLUMN report_period_start date,
    ADD COLUMN report_period_end date,
    ADD COLUMN publication_date date;

ALTER TABLE chunks
    ADD COLUMN report_period_start date,
    ADD COLUMN report_period_end date,
    ADD COLUMN publication_date date,
    ADD COLUMN period_granularity text,
    ADD COLUMN period_confidence text NOT NULL DEFAULT 'unknown';

COMMENT ON COLUMN documents.company IS '文档主体公司/机构；结构化值，不靠名称后缀猜测';
COMMENT ON COLUMN documents.report_type IS 'annual/interim/quarterly/other';
COMMENT ON COLUMN documents.report_period_start IS '报告覆盖期开始日；不是预测目标年份';
COMMENT ON COLUMN documents.report_period_end IS '报告覆盖期结束日；不是预测目标年份';
COMMENT ON COLUMN documents.publication_date IS '报告发布日期；与报告覆盖期分开';
COMMENT ON COLUMN chunks.report_period_start IS '从 documents 继承的报告覆盖期开始日';
COMMENT ON COLUMN chunks.report_period_end IS '从 documents 继承的报告覆盖期结束日';
COMMENT ON COLUMN chunks.publication_date IS '从 documents 继承的报告发布日期';
COMMENT ON COLUMN chunks.period_granularity IS 'day/month/quarter/half_year/year/range';
COMMENT ON COLUMN chunks.period_confidence IS 'explicit/inferred/filename/unknown';

ALTER TABLE documents
    ADD CONSTRAINT ck_documents_report_type
        CHECK (report_type IS NULL OR report_type IN ('annual', 'interim', 'quarterly', 'other')),
    ADD CONSTRAINT ck_documents_period_order
        CHECK (report_period_start IS NULL OR report_period_end IS NULL OR report_period_start <= report_period_end);

ALTER TABLE chunks
    ADD CONSTRAINT ck_chunks_period_order
        CHECK (report_period_start IS NULL OR report_period_end IS NULL OR report_period_start <= report_period_end),
    ADD CONSTRAINT ck_chunks_period_granularity
        CHECK (period_granularity IS NULL OR period_granularity IN ('day', 'month', 'quarter', 'half_year', 'year', 'range')),
    ADD CONSTRAINT ck_chunks_period_confidence
        CHECK (period_confidence IN ('explicit', 'inferred', 'filename', 'unknown'));

CREATE INDEX ix_documents_company_period
    ON documents (company, report_type, report_period_start, report_period_end);
CREATE INDEX ix_chunks_period
    ON chunks (report_period_start, report_period_end)
    WHERE report_period_start IS NOT NULL AND report_period_end IS NOT NULL;
CREATE INDEX ix_chunks_publication
    ON chunks (publication_date) WHERE publication_date IS NOT NULL;

-- v_active_chunks 是检索/对账唯一取数视图：新字段从 documents JOIN 后带出，
-- 不再要求每个 chunk 单独复制一份文档元数据。旧视图列集合不同，CREATE OR REPLACE
-- 会因列顺序变更失败，因此显式 DROP 后重建；查询只在迁移事务内短暂不可用。
DROP VIEW IF EXISTS v_active_chunks;
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
       c.table_html,
       c.table_md,
       c.effective_date,
       c.effective_year,
       c.date_confidence,
       c.report_period_start,
       c.report_period_end,
       c.publication_date,
       c.period_granularity,
       c.period_confidence,
       c.is_public,
       c.allowed_depts,
       c.allowed_roles,
       c.token_count,
       c.table_cells,
       c.notes,
       d.company,
       d.report_type
FROM chunks c
JOIN doc_versions v ON v.version_id = c.version_id
JOIN documents d ON d.doc_key = c.doc_key
WHERE v.status = 'active'
  AND c.status = 'active';

COMMENT ON VIEW v_active_chunks IS '当前可检索切片：version 与 chunk 双 active；文档时间元数据从 documents 继承';
