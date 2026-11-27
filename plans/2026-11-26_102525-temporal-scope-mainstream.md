# ai-rag 时间筛选主流化改造：一页决策文档

生成：2026-11-26 10:25 CST ｜ 状态：待执行 ｜ 交付物：决策与任务拆解，不动代码

## 0. 定版结论

| 编号 | 决策 | 后果 |
|---|---|---|
| D1 | 时间拆成 `document_time`（文档/报告期，参与检索过滤）与 `content_time`（预测/展望目标，不参与过滤） | “2030 年市场规模预测”不再把 2026 年报告筛掉 |
| D2 | 新增独立目录 `api/temporal/`，统一放时间类型、抽取、归一、过滤、复合问拆分 | 同类实现收拢；旧 `api/time_range.py` 只保留兼容 shim，后续删除 |
| D3 | chunk 增加 `report_period_start/end`、`publication_date`、`period_granularity`、`period_confidence`；保留 `effective_date/year` 作旧数据兼容 | 精确报告期优先，年份过滤降级为 fallback，不再长期依赖 `effective_year` |
| D4 | 过滤优先级：报告期精确区间 → publication/effective date 精确区间 → effective_year → 不应用时间过滤 | 不做“零候选后去掉时间条件重试”；明确时间无证据就是 `no_evidence` |
| D5 | 文档选择改为结构化 metadata 硬过滤：公司 + 报告类型 + 报告期；LLM 仅做歧义消解 | 不再靠 `select_time_docs()` 猜文档名，失败时不隐式回退全库 |
| D6 | 复合问先拆成独立子问题，每个子问题独立时间 scope、检索、门控、证据映射；聚合为 `pass/partial/reject_*` | 不允许一个强子答案替另一个无证据子答案放行 |
| D7 | 时间置信度显式化：explicit 高置信硬过滤；inferred 仍过滤但留痕；低置信/歧义触发 clarify 或 no-filter+note | 不猜、不静默放宽；审计可解释 |
| D8 | 产品口径改为“证据可追溯、多机制降低幻觉”，不承诺绝对 0 幻觉 | 生成、Judge、门控、partial、拒答共同降低风险，而不是只定标准 |

## 1. 现状与差距

| 项 | 现状 | 目标 |
|---|---|---|
| 时间输出 | `TimeNormalisation` 只有一个 `range` + 字符串 `target_time` | `TemporalScope` 结构化输出多个文档区间、目标时间、置信度与依据 |
| 时间过滤 | `year/half_year/quarter` 统一按 `effective_year` 过滤 | 精确 `report_period_start/end`；年份仅 metadata 缺失时 fallback |
| 文档选择 | LLM 从文件名猜，失败回退字符串提及匹配 | 公司/报告类型/期间 metadata 硬过滤，LLM 只消歧 |
| 复合问题 | 全局一个 time plan，无法分别门控 | 子问题独立 scope + 独立 gate + partial 聚合 |
| 元数据 | chunk 主要依赖 `effective_date/effective_year` | doc/chunk 双层报告期与发布时间 metadata，支持回填 |
| 拒答策略 | 无候选直接 `no_evidence` | 保持 Fail-Closed；但不靠 blanket fallback 恢复召回 |

明确不做：不改前端引用交互；不引入新的向量库/数据库；不承诺 0 幻觉；不把时间过滤退化成“全局放开”。

## 2. 目标数据契约

### 2.1 查询时间结构

```text
TemporalScope
├── document_ranges: list[TimeRange]   # 参与检索过滤
├── target_times: list[str]            # 2030/未来三年等，仅用于答案核验
├── basis: explicit|relative|assumed_year|inferred
├── confidence: high|medium|low
└── notes: list[str]                   # 可直接进审计/回答的说明
```

`TimeRange` 保留 `from/to/granularity/matched/basis/inferred/note`，不破坏现有调用。

### 2.2 文档与切片元数据

| 字段 | 层 | 中文口径 |
|---|---|---|
| `company` | documents | 公司主体，结构化字段，不靠后缀正则猜 |
| `report_type` | documents | annual/interim/quarterly/other |
| `report_period_start/end` | documents + chunks | 报告覆盖期间，如 2026-07-01～2026-09-30 |
| `publication_date` | documents + chunks | 报告发布日期 |
| `period_granularity` | documents + chunks | day/month/quarter/half_year/year/range |
| `period_confidence` | documents + chunks | explicit/inferred/filename/unknown |
| `effective_date/year` | chunks | 旧字段保留，仅兼容回退 |

中文键必须配字段字典；文档层级 metadata 可继承到 chunks，避免每片重复解析。

## 3. 文件改动清单

| 文件 | 改法 |
|---|---|
| 新增 `api/temporal/types.py` | `TemporalScope`、`TimeSpan`、`TimeRole`、置信度枚举 |
| 新增 `api/temporal/extract.py` | 规则 + 受约束 LLM 抽取时间实体；输出 role/value/confidence |
| 新增 `api/temporal/normalize.py` | 绝对/相对时间换算；区分文档时间与目标时间；不猜低置信表达 |
| 新增 `api/temporal/filters.py` | 生成统一 `document_time` 过滤条件，精确区间优先、年份 fallback |
| 新增 `api/temporal/decompose.py` | 受约束复合问拆分；校验每个子问题主体/指标/时间完整，拒绝碎片 |
| 修改 `api/query/__init__.py` | `QueryPlan.temporal: TemporalScope`；`subquestions` 可选 |
| 修改 `api/filters.py` | 渲染 `report_period_start/end`、`publication_date`、`effective_year` 三层过滤 |
| 修改 `api/retrieval/pipeline.py` | 每子问题独立 scope/检索/门控；聚合 candidates 与 partial；删除全局 fallback |
| 修改 `api/qa/orchestrator.py` | 支持 partial 输出；逐子问题证据映射；Judge 逐项核验 |
| 新增 `db/migrations/002_temporal_metadata.sql` | 加字段、索引、更新 `v_active_chunks` |
| 修改 `worker/ingest_worker.py`、`worker/vector_sync_worker.py` | 写 doc/chunk metadata；同步 Qdrant payload |
| 修改 `scripts/qdrant_common.py` | payload indexes 同步新字段 |
| 修改 `api/tests/test_*` | 时间抽取、过滤 parity、元数据继承、复合问门控、partial、边界回归 |
| 修改 `README.md`、`概要设计.md`、`详细设计.md` | 更新时间契约、架构图、状态机、验收口径 |

## 4. 分步任务

### T00 基线
- 命令：`/workspace/.hermes/venvs/ai-rag312/bin/pytest -q`
- 期望：既有用例全绿（当前基线 866 passed, 165 skipped），无回归。

### T01 数据迁移与回填
- 先加 doc 层 `company/report_type/report_period_start/end/publication_date`。
- chunks 继承 doc metadata；文件名明确时可标 `period_confidence=filename`。
- 回填现有 6 份样本报告；无法确定写 `unknown` 和 warning，不猜日期。
- 命令：`python scripts/db_migrate.py` + 回填脚本 dry-run/正式两段。
- 期望：6 份样本报告期间正确；旧 `effective_date/year` 保持兼容；视图与 Qdrant payload 对齐。

### T02 时间抽取与归一
- 实现 `extract.py`、`normalize.py`，输出 `TemporalScope`。
- 明确样例：2026 Q3、2026 前三季度、2026 年上半年、2025-2027 年、2030 年预测、2026 年报告预测 2030 年。
- 命令：`pytest -q api/tests/test_temporal_*.py`
- 期望：角色、区间、置信度、note 全部符合样例。

### T03 三层时间过滤
- 实现精确报告期优先；publication/effective date 次之；effective_year fallback。
- 保持 PG/Qdrant filter parity。
- 命令：`pytest -q api/tests/test_filters.py api/tests/test_filter_parity.py`
- 期望：两路条件逐字节一致；明确时间无证据时返回 `reject_none`，不重试放宽。

### T04 结构化文档选择
- 文档选择改为 `company + report_type + period` metadata 查询。
- LLM 只处理歧义，不返回不存在或跨主体的 doc_key。
- 命令：`pytest -q api/tests/test_temporal_doc_selection.py`
- 期望：Q3 只选 Q3 报告；上半年不选三季报；2030 预测不误选 2030 文档。

### T05 复合问拆分与独立门控
- 拆分必须保证每个子问题有主体、指标、时间 scope；禁止把“同比增长多少？”单独检索。
- 每子问题独立 `gate_result`，聚合为 pass/partial/reject。
- 命令：`pytest -q api/tests/test_temporal_decompose.py api/tests/test_compound_gate.py`
- 期望：市场地位 + 2030 预测分别判定；一个失败另一个成功时整体 partial。

### T06 编排与 Judge
- `QaService` 按子问题生成/核验，合并引用并重编号。
- partial 明确输出“已查实部分 + 未覆盖部分”。
- 命令：`pytest -q api/tests/test_qa_api.py api/tests/test_qa_sse.py`
- 期望：SSE 事件、done.verdict、citations、uncovered 均正确。

### T07 端到端自测
- 用真实 PG/Qdrant/rerank/LLM 重放以下问题：
  1. 宇树 2026 Q3 单季度营业收入与同比；
  2. 宇树 2026 Q3 归属净利润与同比；
  3. 长鑫全球 DRAM 地位 + 2030 市场规模预测；
  4. 宇树 2024 年营收（库无 2024 报告）；
  5. 2026 年报告预测 2030 年市场规模。
- 命令：`python scripts/rag_qa_selftest.py --question '...' --expect-verdict pass`
- 期望：1/2/3/5 pass；4 `reject_none/no_evidence`；`filter.built` 与 `qa.exit` 可逐条解释。

## 5. 验收总表

| 层次 | 命令 | 期望 |
|---|---|---|
| 全量单测 | `pytest -q` | 退出码 0；既有 866 passed 不减少 |
| 时间抽取 | `pytest -q api/tests/test_temporal_*.py` | 角色/区间/置信度用例全绿 |
| 过滤对拍 | `pytest -q api/tests/test_filter_parity.py` | PG/Qdrant 条件一致 |
| 复合问 | `pytest -q api/tests/test_compound_gate.py` | pass/partial/reject 聚合正确 |
| 真实链路 | `python scripts/rag_qa_selftest.py ...` | 原失败问题 pass；越期问题 no_evidence |

## 6. 风险与开放项

| 风险 | 对策 / 默认 |
|---|---|
| 历史 chunks 缺报告期 metadata | 优先从 doc metadata 继承；文件名可确定的标 `filename`；其余 unknown + warning |
| 精确区间导致老数据漏召回 | 只允许 `effective_year` 作为旧数据 fallback；不做零候选放宽 |
| LLM 时间抽取误判 | 规则先抽取，LLM 只补歧义；低置信不加过滤并说明 |
| 复合问拆分不完整 | 拆分后校验主体/指标/时间；不合格就保留原问，不拆碎片 |
| partial 引用编号冲突 | 合并证据后统一重编号，并保留子问题映射 |
| 改造影响在线服务 | 先加字段与兼容视图，再切读路径；旧字段迁移完成后删除 shim |

## 7. 完成定义

- 2030 预测年份不再进入文档时间过滤。
- 2026 Q3 与前三季度使用不同精确区间。
- 明确时间无证据时 fail-closed，不靠全局放宽恢复。
- 复合问题各子问题独立门控，部分成功返回 partial。
- 所有时间条件在 `filter.built` 可解释、可回放。
- 原失败样例与新增边界样例均通过真实链路自测。
