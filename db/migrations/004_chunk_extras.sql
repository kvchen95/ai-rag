-- ============================================================================
-- 004_chunk_extras.sql — 切片的两列"派生/来源"信息（A2）
--
-- 背景（详细设计.md §3.3 / §3.2 / 附录 A.3）
--
--   §3.3 说表格"在库里存三份东西"：table_html 保真存档、table_md 派生文本、
--   table_cells 单元格矩阵（校验阶段对齐数字用它）。但 001 只建了前两列——
--   A2 的 `Chunk` 对象上一直有 table_cells，`as_row()` 却把它丢了，于是
--   "三表示一致"只在内存里成立，入库即剩两份。校验阶段（B4 的数值校验）要拿
--   矩阵对齐数字，只能去正则拆 HTML——这正是 §3.3 要避免的做法。
--
--   同理 `notes`：A2 把"这段文字是 OCR 认的"标成 `source:ocr`，把跨页续表标成
--   `continued_of:pageN`、超长表格标 `may_exceed`。它只活在 `Chunk` 上，入库后
--   消失，下游（A3 翻牌、检索、前端引用）看不到"这条证据的来源可疑"。
--
-- 为什么是加列而不是新表
--
--   table_cells 与切片是一对一、且总是一起读（渲染/校验都要整条切片）；
--   独立一张表会给每次按 version 批量取切片多加一次 join，而收益只有"省几 KB"。
--   notes 是几个短标记的集合，text[] 比 jsonb 更省事（`&&` 能直接查"有没有 ocr"，
--   GIN 索引可加可不加——它不进检索过滤条件）。
-- ============================================================================

-- 单元格矩阵：jsonb 数组，元素形如
--   {"row":0,"col":0,"text":"城市","is_header":true,"row_span":1,"col_span":3}
-- 允许 NULL（正文/图切片没有矩阵）。
ALTER TABLE chunks ADD COLUMN table_cells jsonb;

COMMENT ON COLUMN chunks.table_cells IS
    '单元格矩阵（行号/列号/值/是否表头/跨行跨列）。表格切片必须带，正文与图切片为 NULL。校验阶段对齐数字用它，不拆 HTML';

-- 只拦"有这个列但不是数组"的行。**不**加"chunk_type=table 必须带矩阵"的 CHECK：
-- 历史夹具与 A3 的集成用例会插只有 table_md 的表格切片，加硬约束会让那些用例
-- 红在一处与本卡无关的地方。这条口径由应用层 `Chunk.require_complete()` 保证
-- （它已经拦 table 切片缺 table_md；矩阵的一致性由 `tables.cells_from_html`
-- 往返断言，见 scripts/verify_parse_pipeline.py）。
ALTER TABLE chunks ADD CONSTRAINT ck_chunks_table_cells_array
    CHECK (table_cells IS NULL OR jsonb_typeof(table_cells) = 'array');

-- 来源与派生标记：source:ocr / source:mixed / continued_of:pageN / may_exceed
-- 默认空数组而不是 NULL：下游判"有没有 ocr"时不必区分 NULL 与 {}。
ALTER TABLE chunks ADD COLUMN notes text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN chunks.notes IS
    '切片来源与派生标记：source:ocr / source:mixed（文字是识别出来的）/ continued_of:pageN（跨页续表）/ may_exceed（超长表格整表成片）';
