# R2b 零候选排查报告（7 道应答题 · 根因与修复）

- 任务：t_ebcaeedc（父任务 t_9f008194）
- 排查时间：2026-11-24 22:09–22:41 +08:00
- 参考基线：`eval/reports/formal_eval_r2b_20261124_220427.md`、`eval/r2b_analysis.json`
- 性质：**只读排查 + 一处词表修正**；未改 golden、未调阈值、未清库

## 1. 结论（一句话）

7 道应答题零候选**不是** golden 年份超语料、**不是**时间过滤器口径错误、**也不是** effective_date 缺失/错位；根因是查询理解词表 `api/query/lexicon.py` 的 `OUT_OF_SCOPE_MARKERS` 收了一个**裸词"预测"**——券商研报域里"券商预测的营收/净利润"恰是库内核心事实，却被当成"本系统永远没有的东西"在 `build_filter` 之前短路成"超出库范围，未检索"（`filter={}`、`routes 0/0`、`after_dedup=0`）。

## 2. 根因链（可复现）

```
问句含"预测"
  → is_out_of_scope() 命中裸标记"预测"
  → understand() 置 QueryPlan.out_of_scope=True
  → retrieval/pipeline.py: if plan.out_of_scope: _reject_without_retrieval(..., "超出库范围，未检索")
  → 不建 filter、不跑 dense/sparse、不进精排 → after_dedup=0, rerank.elapsed=0
```

**证据 A（唯一性）**：50 题 golden 中，问题文本含"预测"的**恰好这 7 题**（B07/B08/C01/C02/C06/C07/M03），其余 43 题（含全部 13 道陷阱题）都不含。这解释了为什么"零候选应答题数"正好是 7。原评测里唯一"检索无候选"的陷阱题 T01（"2026 年全年净利润"）是**另一条路径**——它建了 filter（`effective_date 2026` 窗）但库内无未来数据，属正确拒答，与本报告无关。

**证据 B（只读探针，修复前）**：`eval/repro_zero_candidates.py`（容器内 `python eval/repro_zero_candidates.py`）：

```
B07   预测       True         20250101-20251231/explicit   中原证券给出的中芯国际2025年归母净利润预测是多少？
B08   预测       True         None                         东方证券对中芯国际2025-2027年归母净利润的预测分别是多少？
C01   预测       True         20250101-20251231/explicit   东方证券预测的中芯国际2025年营业收入是多少（百万元）？
...（7 题全部 scope_hit=预测、out_of_scope=True）
反向对照：其余应答题命中超范围 = 空
```

关键读数：6/7 题的时间窗**本身解析正常**（`20250101-20251231/explicit`）。若根因是时间过滤，不该出现 `filter={}` 和 `routes 0/0`——这两项证明检索压根没被调用。

**证据 C（qa_log 修复前快照）**：`eval/qalog_zero_check.sql` 查得旧窗口这 7 题 `after_dedup=0, detail='超出库范围，未检索', retrieved_n=0`。

## 3. 排除的三个假设（SQL 证据）

跑法：`docker compose -f infra/docker-compose.yml exec -T postgres psql -U ai_rag -d ai_rag < eval/corpus_year_coverage.sql`

| 假设 | 证据 | 结论 |
|---|---|---|
| golden 年份超语料 | 7 题年份 2025/2025-2027，活跃语料 effective_year 覆盖 2023(36)/2024(338)/2025(1243)，2025 有 16 条 explicit | 否 |
| 时间过滤器口径错误 | 6/7 题时间窗解析为 `20250101-20251231/explicit`，与其余能召回的应答题（B01/B05 等）同口径 | 否 |
| effective_date 缺失/错位 | 4 份 expected doc 全 active：东方证券 378 片、中原证券 359 片（含 4 条 2025 explicit，min 20250331）、光大 596 片、国信 99 片 | 否 |

补充：B08（"2025-2027年"）时间窗解析为 `None` 是 `time_range.py` 对"年份区间"未覆盖的**独立小问题**——但它是"不加时间条件"（只会多召回），不会导致零候选，不在本任务范围，另记待办。

## 4. 修复（TDD，最小改动）

- **改动**：从 `OUT_OF_SCOPE_MARKERS` 删除裸"预测"，并在原处写清"为何不能加回"；保留"股价/股票/汇率/彩票/星座/明天天气/天气/建议买/写一首/翻译成"——真正"预测未来股价/涨到多少"由"股价/股票"兜底，与本域"券商预测数字"是两个概念。
- **红线**：真正的超范围问句"帮我预测一下明年的股价"仍命中"股价"→ 短路，语义未退化。
- **回归测试**（`api/tests/test_query_understand.py`）：`test_broker_forecast_is_not_out_of_scope`（7 条原题参数化）、`test_genuine_out_of_scope_still_short_circuits`（变异守卫）。
- **TDD 实跑**：修复前 RED（7 条全失败，`assert '预测' is None` 不成立）；修复后 GREEN（8 条新增全绿，整个 `test_query_understand.py` 59 条全绿）。测试用临时 venv（`/tmp/r2b-venv`，pytest 9.2.0 + pydantic）跑——api 容器重建后没了 pytest，纯 Python 模块无需容器依赖。

## 5. 修复验证（重跑 make eval 留快照）

- **报告快照**：`eval/reports/redline_20261124_224023.md` / `.json`（评测窗 22:35:13–22:40:23 CST，`make eval` 退出码 0）。
- **qa_log 修复后窗口**（id 956–982）：7 题 `after_dedup=8 / fused_n=8 / retrieved_n=8`，detail 从"超出库范围，未检索"变为"精排降级/两份文档分数接近"——已从"未检索"推进到"检索到候选"。
- **零候选应答题数：7 → 0**。
- **操作注意**：源码 bind-mount 到容器，但常驻 uvicorn 进程需 `restart ai-rag-api` 才重新 import 新词表；第一次重跑未重启、仍走旧代码（qa_log 947 及之前窗口可见），重启后第二次重跑才生效。

## 6. 与精排修复（t_e2610802）的关系

本次 `make eval` 与精排预算修复并行，四条红线本次均 PASS（覆盖 2/35）。但零候选修复与精排修复是**正交**的：`after_dedup` 由检索层决定（词表短路修复直接改善），`scored/top1` 由精排层决定（预算修复改善）。7 题中 6 条在最新窗口仍显示"精排降级，分数不可用"，属精排层遗留，不是本报告范围；C06 已出现有效 top1=0.7186（两份文档分数接近触发 clarify）。请与精排修复合并后做最终复测与标定。

## 7. 边界与待办

- 未改 golden、未调阈值（0.20/0.40 不动）、未清库；改动仅 `api/query/lexicon.py` 与 `api/tests/test_query_understand.py`。
- 待办（另开任务，本卡不执行）：B08 的"2025-2027年"年份区间时间归一（当前退化为不加时间条件，会多召回但不会零候选）。
