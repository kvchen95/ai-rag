-- ============================================================================
-- 006_temporal_facts.sql — 财报时序知识图谱的边/事实表
--
-- 这里的“图”不是图数据库，而是带主体、指标、期间、来源的关系表：
--   (subject) --[metric, period]--> value
-- 每个值必须能回到 chunks 里的原文；派生值单独标 role='derived'。
-- 与 documents/chunks 的报告期字段配合，解决跨期比较和“2030 预测”误过滤。
-- ============================================================================

CREATE TABLE temporal_facts (
    fact_id            text        PRIMARY KEY,
    subject            text        NOT NULL,
    metric             text        NOT NULL,
    value              numeric     NOT NULL,
    unit               text        NOT NULL DEFAULT '',
    period_start       date        NOT NULL,
    period_end         date        NOT NULL,
    period_granularity text        NOT NULL DEFAULT 'range',
    source_chunk_id    text        NOT NULL,
    doc_key            text        NOT NULL,
    role               text        NOT NULL DEFAULT 'reported',
    confidence         text        NOT NULL DEFAULT 'explicit',
    provenance         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_temporal_facts_chunk
        FOREIGN KEY (source_chunk_id)
        REFERENCES chunks (chunk_id) ON DELETE CASCADE,
    CONSTRAINT ck_temporal_facts_period
        CHECK (period_start <= period_end),
    CONSTRAINT ck_temporal_facts_granularity
        CHECK (period_granularity IN ('day', 'month', 'quarter', 'half_year', 'year', 'range')),
    CONSTRAINT ck_temporal_facts_role
        CHECK (role IN ('reported', 'derived')),
    CONSTRAINT ck_temporal_facts_confidence
        CHECK (confidence IN ('explicit', 'inferred', 'filename', 'unknown'))
);

COMMENT ON TABLE temporal_facts IS
    '时序事实：主体 + 指标 + 值 + 报告期 + 来源 chunk；不是向量副本，是跨期比较的权威结构';
COMMENT ON COLUMN temporal_facts.fact_id IS
    '稳定事实编号；同一 chunk/指标/期间重复回填时幂等';
COMMENT ON COLUMN temporal_facts.subject IS
    '主体（公司/机构），来自 documents.company，不由问句临时猜';
COMMENT ON COLUMN temporal_facts.metric IS
    '指标名，如 营业收入/研发投入合计；同义词归一由应用层负责';
COMMENT ON COLUMN temporal_facts.value IS
    '按 unit 解释的数值；不把元换算成万元，换算属于展示层';
COMMENT ON COLUMN temporal_facts.period_start IS
    '该事实覆盖期开始；本期/上年同期由表格列名决定';
COMMENT ON COLUMN temporal_facts.period_end IS
    '该事实覆盖期结束；不是文档发布日期';
COMMENT ON COLUMN temporal_facts.source_chunk_id IS
    '原文切片；每个事实必须可回溯';
COMMENT ON COLUMN temporal_facts.role IS
    'reported=原文直接披露；derived=由工具按公式计算';
COMMENT ON COLUMN temporal_facts.provenance IS
    '列名、表格原文摘要、抽取器等审计信息';

CREATE INDEX ix_temporal_facts_subject_metric_period
    ON temporal_facts (subject, metric, period_start, period_end);
CREATE INDEX ix_temporal_facts_period
    ON temporal_facts (period_start, period_end);
CREATE INDEX ix_temporal_facts_doc
    ON temporal_facts (doc_key);
