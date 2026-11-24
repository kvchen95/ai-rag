# draft_status 过度拒答归因：诊断报告（R2c 基线 follow-up）

- 生成：2026-11-25 00:04 CST；任务 `t_e7b858e4`；边界 `[diagnosis]`
- 基线锚点：`eval/reports/redline_20261124_233521.json`、`eval/reports/r2c_final_baseline_20261124_233521.md`
- 评测窗口：`qa_log` id 1260–1294（35 道应答题），筛选 `score_snapshot.verify.failure.check='draft_status'` 得 20 道
- 机器可读结论：`eval/reports/draft_status_diagnosis_20261125_000406.json`

## 1. 结论（一句话）

**任务标题里的假设不成立：这不是生成层过度拒答，而是检索候选召回失败。** 20 道题的证据包里确实没有答案所需事实；模型明确说出"证据包中未包含 X 文档/未找到 X 数值"，是按提示词正确行事。`CHECK_DRAFT_STATUS → no_evidence` 的映射也没有错。真正的断点在检索：含事实的切片没进 dense/sparse 两路的 top-30，因此不可能被精排选进 top-8 证据包。

由此：不要改生成提示词的"证据不足"规则，不要改 `CHECK_DRAFT_STATUS` 映射，也不要动 0.20/0.40。改动方向在检索召回层，且是否实施由 operator 决定；本卡未改代码、未调阈值、未改 golden、未清库、未重跑 `make eval`。

## 2. 两种假设的判定

| 假设 | 判定 | 判据 |
|---|---|---|
| H1：证据包里确实没有答案 | **成立** | 见 §3、§4：事实切片不在候选集；模型逐题说出缺什么 |
| H2：证据包有答案但 LLM 过度保守 / 提示词导致 unknown | **不成立** | 见 §5：证据包用原文、未截断、used_chars 远低于上限；模型指出具体缺失来源 |

## 3. 证据包事实覆盖（可复算）

脚本：`eval/draft_status_evidence_probe.py`（只读，PG 容器执行），输出 `eval/draft_status_evidence_probe.json`。

```bash
cd /workspace/ai-rag
python3 eval/draft_status_evidence_probe.py
head -13 eval/draft_status_evidence_probe.json
```

实测输出：

```json
{
  "n_questions": 20,
  "questions_with_distinctive_facts_defined": 18,
  "questions_with_any_distinctive_fact_in_pack": 0,
  "questions_with_no_distinctive_fact_in_pack": 20,
  "golden_doc_present": 8,
  "golden_doc_absent": 12,
  "truncated_true": 0,
  "top1_min": 0.549555,
  "top1_max": 0.980025
}
```

口径说明：
- "distinctive fact" = golden `key_evidence` 里的小数位/千分位/≥100 的整数（移除页码、`2025E`/`25Q1` 等查询上下文）。两道题（M01、M04）的 golden 只给小百分数区间，没有可判的 distinctive fact，故分母是 18。
- 证据包正文按 `fetch_texts._text_for` 的同一口径取：table 取 `table_md`、其余取 `content_text`。
- **`truncated_true=0`、`used_chars` 992–4259**：6000 字封顶从未触发，`RERANK_TEXT_MAX_CHARS=400` 与证据包长度无关。
- 12/20 的证据包连 golden 目标文档都没有；余下 8/20 虽有目标文档，仍没取到承载事实的那一切片。

模型自己的 `score_snapshot.verify.failure.offenders[0]` 逐题印证（grep 可复算）：

```bash
grep '"model_answer"' eval/draft_status_evidence_probe.json
```

例如 B03：`未找到东方证券对中芯国际目标价的任何可验证依据，证据包中未包含东方证券的相关研报内容。`；B15：`证据包中未包含国信证券相关报告…`；C08：`未找到可验证依据：证据包中未包含中芯国际2024年经营活动现金流（百万美元）的相关数据。` 20 道全部是这种"证据包里没有"的明确陈述，没有一道是"有证据但我保守拒答"。

## 4. 断点定位：检索候选召回，而非精排、去重或权限

脚本：`eval/retrieval_replay_probe.py`（容器内只读重放，monkeypatch `HybridRetriever.retrieve`、`rrf_fuse`、`dedupe_parent_children`、`Reranker.score`），记录去重前的两路原始 top-30、去重丢弃、精排分数与最终 top-8。B03 的重放 `top1=0.980025` 且 top-8 与评测窗口逐项一致，说明重放忠实。

| 题 | golden 事实 | 事实切片 | 事实在 dense top-30 | 在 sparse top-30 | 被父子去重丢弃 | 进最终 top-8 |
|---|---|---|---|---|---|---|
| B03 | 129.48 | `ch_f7237808607b`（东方证券 p2，table） | 否 | 否 | 否 | 否 |
| B05 | 89.6% | `ch_b2846f232d57`（国信 p1） | 否 | 否 | 否 | 否 |
| B11 | 948kwpm | `ch_4748e9b177d8`（华泰 p4） | 否 | 否 | 否 | 否 |

复算：

```bash
D="/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
"$D" compose -f infra/docker-compose.yml exec -T -e TARGET_CHUNK=ch_f7237808607b \
  api python eval/retrieval_replay_probe.py "东方证券给中芯国际的目标价是多少？" \
  > eval/retrieval_replay_B03.json
grep -A4 golden_chunk_in_raw_routes eval/retrieval_replay_B03.json
```

输出为 `golden_chunk_in_raw_routes: [[], []]`、`golden_chunk_dropped_by_dedup: false`。

**差分证据（同一切片、换问法就能召回）**：B18 问"东方证券采用的 WACC 和永续增长率假设分别是多少？"，同一个 `ch_f7237808607b` 在 dense 与 sparse 两路都排第 1（`fused=1.0`），并进入 top-8。说明切片本身可检索、未被过滤；B03 的问法"目标价"与该表格切片的文本（"WACC / 永续增长率 / 129.48"）无词面交集，两路都没把它放进 top-30。

**排除索引/权限问题**：三个事实切片在 Qdrant `kb_chunks` 中均存在，`status=active`、`allowed_depts=[fin]`、`allowed_roles=[staff]`，与探针身份匹配：

```bash
"$D" exec ai-rag-api sh -c "curl -s -X POST http://qdrant:6333/collections/kb_chunks/points/scroll \
  -H 'Content-Type: application/json' \
  -d '{\"filter\":{\"must\":[{\"key\":\"chunk_id\",\"match\":{\"value\":\"ch_f7237808607b\"}}]},\"limit\":2,\"with_payload\":true}'"
```

**排除"调大 top-K 就行"**：以 `RETRIEVAL_TOP_K=100` 重放 B03/B11，事实切片仍未出现在任一原始路（产物 `eval/retrieval_replay_B03_k100.json`、`..._B11_k100.json`）。不能把这条当作已验证的廉价修复。

## 5. 为什么高 top1 不能证明"检索没漏"

`api/retrieval/rerank.py` 的 `clip_for_rerank(..., text_max_chars)` 只截精排输入；`api/qa/orchestrator.py::_build_pack` 用 `fetch_texts()` 取原文组装证据包。所以 H2 描述的"证据包只给了 400 字"不成立。

真正机制是：精排只对**已经召回的**候选打分。B03 拿到的是华泰/兴证/上海证券等"讲估值、股价"但无目标价数值的切片，精排给它们 0.98；这个分数衡量的是"候选与问题的相关性"，不是"候选里有没有答案"。因此 `top1≥0.55` 与"证据包缺事实"可以同时成立。父任务报告里"检索没召回 0"用的是 `top1<0.20` 这一代理判据，按事实覆盖口径应当更正为：生成层被拦的题里事实级召回 0/18（有可判事实的那 18 道）。

## 6. 修复方向（未实施，待 operator 决定）

1. **检索召回层**：问法与表格切片之间的语义/词面鸿沟（"目标价" ↔ WACC 敏感性表里的 129.48；"产能利用率 89.6" ↔ 只在别的文档出现）需要 query 改写/同义扩展或表格切片的事实标注与索引增强；单纯抬 `RETRIEVAL_TOP_K` 未被本诊断证明有效。
2. **不要动的地方**：生成提示词规则 3、`CHECK_DRAFT_STATUS` 映射、门控 0.20/0.40。前两者在本题上是正确行为，后者与本题无关（失败题早已过门控）。
3. **验收方式（实施后）**：独占、无并发地重跑 `make eval`；`draft_status` 拦题数下降、覆盖率从 2/35 上升、RL1 陷阱拒答仍 15/15、四红线不回退；与 `redline_20261124_233521` + 本报告的锚点逐项对比。
4. **归因口径修正**：后续报告不要用 `top1` 当"检索是否漏召回"的判据；应直接核对证据包正文与目标事实。

## 7. 产物与复算入口

- `eval/draft_status_evidence_probe.py` / `.json`：证据包事实覆盖逐题数据
- `eval/retrieval_replay_probe.py`：检索/精排全链路只读重放（可按 `TARGET_CHUNK` 指定目标切片）
- `eval/retrieval_replay_B03.json` / `B05` / `B11` / `B18` / `B03_k100` / `B11_k100`：重放原始输出
- `eval/reports/draft_status_diagnosis_20261125_000406.json`：本报告的机器可读版本
- 本报告所有命令均只读；未对代码、配置、golden、数据库做任何写操作。
