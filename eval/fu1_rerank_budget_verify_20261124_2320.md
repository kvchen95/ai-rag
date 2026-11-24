# FU1 精排预算修复验证（t_e2610802）

- 验证时间窗：2026-11-24 23:14:31–23:20:15 CST（隔离运行，评测前 `qa_log` 近 30 秒无其他卡写入）
- 代码提交：`14624a3`（零候选 lexicon）、`4e60ae3`（B08 年份区间）、`8010196`（精排截断+配置）、`661e8a0`（预算 12000ms）
- 运行配置（api 容器 `api_started` 日志实测）：`RERANK_BUDGET_MS=12000`、`RERANK_TEXT_MAX_CHARS=400`，门控仍为 `0.20/0.40`

## 一、根因与修复

`800ms` 预算对应的是短文本时代的 TEI CPU 耗时。实测 8 条真实切片正文（含长表格）在 TEI 上 3.6–9.4s，预算必然耗尽；零候选修复后部分问题召回 44 条候选，TEI 单次上限 32 条、必须分两批，截断后两批仍要 8.8–9.7s。

修复由两部分组成，缺一不可：

1. `RERANK_BUDGET_MS` 800 → 12000ms（`.env.example` 与 `infra/docker-compose.yml` 的 api/worker/ingest-worker 默认值）。
2. 进精排的正文按 `RERANK_TEXT_MAX_CHARS=400` 截短：新增 `clip_for_rerank()`，由 `rerank_candidates()` 在组批时调用；证据包仍用原文，引用回查不受影响，门控阈值与 golden 未动，库未清。

## 二、实测数据（只读探针，未改服务配置）

| 输入 | 实测耗时 |
|---|---|
| 8 条真实长切片原文（约 3.2 万字） | 3.6–9.4s |
| 同批截到 400 字（约 3200 字） | 2.0–2.2s |
| 44 条候选截到 400 字、分两批 | 8.8–9.7s |

## 三、TDD 验证

- 先在 `api/tests/test_retrieval_logic.py` 加截断判据，断言替身收到的**实际请求体**已被裁到 400 字；此时 `TypeError: rerank_candidates() got an unexpected keyword argument 'text_max_chars'`（RED）。
- 实现后 `make test-api-db` 实跑：**916 passed, 81 skipped**（GREEN，含真实 PG）。
- 容器内核验：`clip_for_rerank` 在部署代码中；`OUT_OF_SCOPE_MARKERS` 已无裸“预测”；`api_started` 日志显示两项新配置。

## 四、隔离评测验收

评测报告：`eval/reports/redline_20261124_232015.md` / `.json`（`make eval` 退出码 2：RL1 不达标）。

- 窗口内 50 条 `qa_log`（id 1210–1259）逐条核对 `score_snapshot.rerank`：
  - `scored>0` 且 `degrade=null`：**48/50**（达到 ≥48/50 的验收线）
  - `top1` 非 null：**49/50**
  - 仍超时 1 题：`rerank_timeout`、`scored=32`（已打部分分数，按名次垫后），属 budget 边界，非 0 分降级
  - 零候选 1 题：`after_dedup=0`，不在本卡范围
- 四条红线：RL1 **FAIL 13/15**；RL2 **PASS 1/1**；RL3 **PASS 2/2**；RL4 **PASS 1/1**。没有一条因“精排无分数”而 `NOT_MEASURABLE`。

语料快照（评测前实跑 `make ingest-status`）：

```
versions={'active': 9, 'deprecated': 2, 'pending': 10}
chunks={'active': 2136, 'deprecated': 6}
qdrant_points=2142 sync_pending=0 open_drift={}
```

## 五、边界与交接

- 本卡未动 `GATE_REJECT_NONE=0.20` / `GATE_REJECT_LOW=0.40`、未改 golden、未清库、未改语料。
- 剩余问题是**门控/生成层**的：多数应答题虽有有效 top1，仍出现 `reject_none`、`clarify`、`unverifiable`、`model_timeout`，应答题覆盖率仅 1/35，RL1 因陷阱题误判为 13/15。这不是精排预算问题，留给 R2c（t_a60278a3）按“检索没召回 / 门控拦 / 生成 NLI 拦”三类分别归因。
- 复现命令：`make ingest-status` → `make eval` → `python eval/calibrate.py --report eval/reports/redline_20261124_232015.json --name r2c_<时间戳>`。
