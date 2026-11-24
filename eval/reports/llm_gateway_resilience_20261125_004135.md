# LLM 网关 SSL EOF 导致 5/35 model_timeout 与 RL1 13↔15 抖动 — 根因与修复报告

- 任务：`t_962f62ba`（父任务 `t_a60278a3`）
- 端点：`https://api-slb.micuapi.ai/v1`，模型 `deepseek-flash`
- 报告时间：2026-11-25（UTC），配套 JSON：`llm_gateway_resilience_20261125_004135.json`

## 0. 结论（TL;DR）

**根因不在门控、不在 golden、不在评测并发，而在 LLM 客户端本身：每次 `complete()` 都新建
`httpx.Client`，等于每次生成 / 每次 NLI 判别都做一次全新的 TCP+TLS 握手；第三方 SLB 在握手阶段
随机返回 `SSL: UNEXPECTED_EOF_WHILE_READING`，且当时**零重试**，于是每一次瞬时握手失败都直接
变成 `fallback(model_timeout)`。**

- 同一现象解释了两个问题：应答题 `model_timeout`，以及 RL1 在 13/15 ↔ 15/15 之间跳变。
- 修复：共享 keep-alive 连接池 + 有界重试（6 次、指数退避、总预算 45s）。
- 验收：**连续 3 轮独占串行 `make eval`，`model_timeout = 0`，RL1 稳定 15/15**，DB 独立复核 150 次调用 0 次 SSL EOF。

**但本任务只解决稳定性，不改变发布结论：覆盖率仍为 2/35 = 5.7%，仍不可发布。**

---

## 1. 现象与证据

### 1.1 qa_log 窗口（只读复核）

`qa_log` id 1150–1309 共 21 条 `reject_reason='model_timeout'`：

| 分类 | 条数 | 特征 |
|---|---|---|
| SSL EOF（`[SSL: UNEXPECTED_EOF_WHILE_READING] ... (_ssl.c:1018)`） | **20** | 延迟 1290–12740ms，**快速失败** |
| 真正的 30s 超时（`llm timed out after 30.0s`） | 1 | 32875ms |

按 50 题窗口统计：`1151` 窗口 3 条、`1201` 窗口 13 条、`1251` 窗口 5 条 —— **同配置下逐轮剧烈波动**，符合第三方网关抖动特征。

### 1.2 RL1 13/15 ↔ 15/15 的直接归因

对比 `redline_20261124_232015.json`（FAIL 13/15）与 `redline_20261124_233521.json`（PASS 15/15），
逐题 `qid` 比对后发现只有 **T03、T05** 两题的 `trap_safe` 发生翻转，且：

| 题 | top1（两轮完全相同） | FAIL 轮 | PASS 轮 |
|---|---|---|---|
| T03 | 0.424983 | `model_timeout`，`gate1=pass`，`trap_safe=False` | `no_evidence`，`gate1=reject_none`，`trap_safe=True` |
| T05 | 0.832604 | `model_timeout`，`gate1=pass`，`trap_safe=False` | `no_evidence`，`gate1=reject_none`，`trap_safe=True` |

检索分数完全一致 → 检索/门控没有变化；**唯一变量是 LLM 调用是否成功**。网关 SSL EOF 时，
陷阱题走 `fallback(model_timeout)` 被计为"未安全拒答"，红线因此 FAIL；网关正常时，NLI 判别给出
`reject_none`，红线 PASS。

> 结论：RL1 的 13/15 是基础设施伪影，不是门控退化。单轮 PASS 此前不可信，正是因为缺了这层归因。

### 1.3 现场复现（tight loop）

在 api 容器内、使用与生产完全相同的 env，对目标网关做 30 次极小请求：

| 探测 | 失败率 | 中位延迟 |
|---|---|---|
| 冷连接（每次新建 `httpx.Client`，即修复前代码路径）#1 | **7/30 = 23%** | 3.10s |
| 冷连接 #2 | **5/30 = 17%** | — |
| 共享 keep-alive 客户端（修复后路径） | **1/30 = 3%** | **2.26s** |

失败类型全部是 `ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]`。异常类是 `ConnectError`
而非读响应体时的错误 —— **失败发生在 TLS 握手阶段**，直接指向"每次都新建连接"这一实现缺陷。

---

## 2. 根因链

```
每次问答 → LlmClient.complete() 内 `with httpx.Client(...)`  →  全新 TCP+TLS 握手
        → 第三方 SLB 偶发在握手中回 SSL EOF（httpx.ConnectError）
        → except httpx.HTTPError → GenerationUnavailable("llm unreachable: ...")
        → 无重试 → orchestrator._system_fallback(MODEL_TIMEOUT)
        → 应答题：不计入 answered（覆盖率下降）
        → 陷阱题：gate1=pass + fallback → trap_safe=False（RL1 FAIL）
```

### 已排除的假设

| 假设 | 排除依据 |
|---|---|
| 连接池复用导致拿到坏连接 | 修复前**根本没有池**，每次新建客户端；改成共享 keep-alive 后失败率反而从 5/30 降到 1/30。 |
| 评测并发 / `qa_budget_s=30` | 评测严格串行、无并发；20/21 次失败在 1.3–12.7s 内完成，不是 30s 预算触发；`qa_budget_s` 在代码中只存不用，没有硬截断。 |
| 门控 / 阈值 / golden 改动 | T03/T05 的 `top1` 两轮逐位相同；0.20/0.40 未改；golden 未改。 |
| 网关整体不可用 | 30 次探测 23–29 次成功，是**间歇性握手抖动**，不是持续宕机。 |

---

## 3. 修复内容

| 文件 | 改动 |
|---|---|
| `api/generate/llm.py` | 新增按 `(base_url, api_key, timeout)` 缓存的**共享 keep-alive `httpx.Client`**（`max_keepalive_connections=8`、`keepalive_expiry=30s`）；生成与 NLI 判别共用同一池；`complete()` 改为有界重试：最多 6 次、指数退避 0.3s 起并加抖动、总预算 45s。仅重试 `GenerationUnavailable`（SSL EOF / 超时 / 5xx / 空响应），**4xx 与 `DraftFormatError` 不重试**。 |
| `api/config.py` / `api/routers/qa.py` | 新增 `LLM_MAX_ATTEMPTS` / `LLM_RETRY_BACKOFF_S` / `LLM_RETRY_BUDGET_S` 三个配置项并注入两个客户端。 |
| `infra/docker-compose.yml` / `.env.example` | 暴露上述旋钮（默认 6 / 0.3 / 45）。`QA_BUDGET_S=30` **未改**。 |
| `api/tests/test_generate_llm_retry.py` | 4 条回归测试：瞬态 SSL EOF 后成功、4xx 不重试、Attempt 上限、共享池复用。 |

**为什么预算是 45s（> `LLM_TIMEOUT_S=30`）：** 实测出现过约 20s 的 SLB 连续失败窗口；在 4 次/30s 的中间配置下，
某轮首题（B01）连续 4 次 SSL EOF 后仍失败。45s + 6 次可跨过该窗口。该预算只在失败路径生效，
成功请求不受影响；`qa_budget_s` 在代码里没有硬截断，因此不存在语义冲突。

**测试证据：**

- 新增 4 条测试先 RED（4 failed：`_shared_client` / `reset_shared_clients` 不存在），实现后 GREEN（4 passed）。
- 全量 `pytest -c api/pytest.ini -q` 退出码 **0**（无回归）。

---

## 4. 验收验证

### 4.1 修复后端到端探测

走**真实 `LlmClient` 路径**（含共享池 + 重试）30 次调用：**30/30 成功，0 次最终失败**，
仅记录 1 次 `llm_retry`；延迟 min/median/max = 1.42 / 1.81 / 19.37s（最坏仍在预算内）。

### 4.2 连续 3 轮独占串行 `make eval`

每轮开始前确认无 `run_eval` / `make eval` 进程；退出码均 0。

| 轮次 | 报告 | qa_log 窗口 | model_timeout | RL1 | 四条红线 | 覆盖率 |
|---|---|---|---|---|---|---|
| 1 | `redline_20261125_003237` | 1410–1459 | **0** | **PASS 15/15** | 全 PASS | 2/35 = 5.7% |
| 2 | `redline_20261125_003715` | 1460–1509 | **0** | **PASS 15/15** | 全 PASS | 2/35 = 5.7% |
| 3 | `redline_20261125_004135` | 1510–1559 | **0** | **PASS 15/15** | 全 PASS | 2/35 = 5.7% |

### 4.3 数据库独立复核（不依赖评测报告）

```sql
SELECT count(*) AS n_calls,
       count(*) FILTER (WHERE reject_reason='model_timeout') AS mt,
       count(*) FILTER (WHERE score_snapshot->>'detail' ILIKE '%UNEXPECTED_EOF%') AS ssl_eof
FROM qa_log WHERE id BETWEEN 1410 AND 1559;
-- 结果：n_calls=150, mt=0, ssl_eof=0
```

**验收标准达成：`model_timeout = 0` × 3 轮，RL1 稳定 15/15，不再出现 13/15 抖动。**

---

## 5. 对评测结论的影响

1. **红线结果现在可复现**：13/15 那轮是基础设施伪影，不是真实门控退化。RL1 单轮 PASS 自此可作为可信门禁信号。
2. **发布结论不变：仍不可发布**。本任务只修"评测被网关抖动污染"，没有修"26/35 应答题在生成/NLI 层被拦"。覆盖率仍是 2/35 = 5.7%；`draft_status` 过度拒答由 backend 跟进卡 `t_e7b858e4` 负责，阈值是否重标定由 data-analyst 负责。
3. **未触碰**：golden、0.20/0.40、库数据、`qa_budget_s`、检索与门控逻辑。
4. **残余风险**：若 SLB 连续失败超过 45s 或 6 次重试，仍会退回 `model_timeout`。这是 **fail-closed** 行为——不会发出未经核实的答案；每次重试都有 `llm_retry` 日志可告警。

## 6. 降级策略（网关进一步劣化时）

- **当前策略**：共享池 + 有界重试 → 仍失败则 `fallback(model_timeout)`，不造答案。
- **不建议**把传输失败降级成"只给原文"：陷阱题在 `gate1=pass` 时会因此拿到内容，等价于把 RL1 重新变成 13/15 式的泄漏，直接违背"忠实度优先于覆盖率"红线。
- **运维升级路径**：监控 `llm_retry` 日志速率；若 > 5 次/分钟持续 5 分钟，判定网关异常，暂停发布门禁评测并联系网关提供方，而不是放宽评测口径。
- **兜底切换**：可将 `LLM_BASE_URL` 指向稳定的 OpenAI 兼容端点（含内置 stub），但必须重跑整条红线并重新标定阈值，**不得**把它当作"修复"直接用于发布门禁。

## 7. 回滚

```bash
cd /workspace/ai-rag
# 1) 关闭重试（恢复单次调用语义）
#    在 .env 追加/改回：LLM_MAX_ATTEMPTS=1
# 2) 如需完全回退到旧实现，把 api/generate/llm.py 中 _shared_client 调用
#    恢复为 `with httpx.Client(base_url=self.base_url.rstrip("/"), ...) as client:`
# 3) 重建并重启（务必带 --env-file，否则容器里的 LLM_* 会变空）
docker compose --env-file .env -f infra/docker-compose.yml up -d api
curl -sf http://127.0.0.1:8000/healthz
```

回滚后 RL1 将重新出现 13/15 ↔ 15/15 抖动，**不要**用回滚后的单轮结果做门禁。

## 8. 复现命令

```bash
cd /workspace/ai-rag
# 修复前路径的现场复现（冷连接 SSL EOF 率）
docker compose --env-file .env -f infra/docker-compose.yml exec -T api python eval/_gateway_probe.py
# 冷连接 vs 共享 keep-alive 对比
docker compose --env-file .env -f infra/docker-compose.yml exec -T api python eval/_gateway_pool_probe.py
# 修复后端到端验证
docker compose --env-file .env -f infra/docker-compose.yml exec -T api python eval/_gateway_resilience_probe.py
# 验收：3 轮独占串行
make eval && make eval && make eval
# DB 复核
docker compose --env-file .env -f infra/docker-compose.yml exec -T postgres psql -U ai_rag -d ai_rag -f /dev/stdin < eval/_devops_qalog.sql
```
