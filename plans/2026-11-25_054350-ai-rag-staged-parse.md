# ai-rag 分阶段解析改造：一页决策文档

生成：2026-11-25 05:43 CST ｜ 状态：待执行 ｜ 交付物：决策，不动代码

## 0. 定版结论

| 编号 | 决策 | 后果 |
|---|---|---|
| D1 | MinerU 用**官方云精准解析 API**，token 由用户填进本地 `.env` | 按量付费；代码只留 `MINERU_API_KEY` 引用，密钥不进仓库/对话 |
| D2 | 官方单文件接口 `/api/v4/extract/task` **只收 URL，不收本地文件**；本地 PDF 走 `/api/v4/file-urls/batch`，一次放 1 个文件也合法 | "单文件解析"与"全量解析"共用同一条上传→轮询→下载 ZIP 路径，不写两套；ZIP 内取 `content_list.json` / `layout.json` / `full.md` |
| D3 | Docling 用 **Python 包本地进程**，pin `docling==2.151.1` | 不新增服务；首次运行下载表格模型，镜像需留 model cache |
| D4 | 语义与结构修复只用**确定性规则**，不调 LLM | 不产生额外模型费用；OCR 纠正限白名单与上下文规则，修不动的写 `warnings` 不猜 |
| D5 | 每一步产物落 `data/parse/<doc_key>/<version_id>/`，带 `manifest.json`；**人工确认前不写 PG/Qdrant** | 可按阶段 replay/比对；`doc_key`/`version_id` 复用 `api/ingest/fingerprint.py`，不另写派生规则 |
| D6 | 并发默认 2、上限 4；付费闸门默认 0，必须显式给页数上限才提交任务 | `--max-workers 5` 直接报错；`--max-pages` 未给时拒绝提交，防止误烧额度 |
| D7 | **顺序硬闸门**：当前只实现 `single`；`all`/批量命令在用户确认单文件产物前不实现、不暴露 | 单文件产物确认后，才解锁并发；避免一次跑全量浪费 token |

已核实的接口事实（不靠记忆）：<https://mineru.net/apiManage/docs>、<https://docling.org/doc/>。MinerU 响应字段以 `code/data.task_id/data.state/data.full_zip_url` 为准；Docling 表格取 `DocumentConverter().convert(path).document.tables` + `table.export_to_dataframe(doc=doc)`。

## 1. 现状与差距

| 项 | 现状 | 改造要求 |
|---|---|---|
| 解析入口 | `api/parse/pipeline.py::parse_document` 走 pdfplumber 版面链，一次跑完 | 新增独立分阶段链，每阶段单独落盘 |
| 外部解析 | 无 MinerU / Docling 接入 | MinerU 云 API 产出 JSON；Docling 做表格结构增强 |
| 修复 | 无段落合并/OCR 纠错/标题列表还原 | 规则化修复，产出可比对的前后版本 |
| 切片 | child 256–512 字、重叠 64，表格有长度上限 | LangChain `SemanticChunker`（本地/TEI embedding），超长语义块再用 500 字/50 重叠兜底；**表格整块不切**；每片带 `page_num` |
| 并发 | `worker/ingest_worker.py` 串行领任务 | 独立 CLI 并发解析，默认 2、最大 4 |
| 入库闸门 | 扫描即入 `ingest_jobs` | 解析完成只到 `READY_FOR_REVIEW`；人工确认后才进现有 ingest 链 |

明确不做：不改检索/门控/前端；不动 `api/parse/` 旧链；不把 token、产物原文写进数据库或聊天。

## 2. 阶段产物（目录即契约）

`data/parse/<doc_key>/<version_id>/` 下固定六个文件；每层都能单独重跑，重跑覆盖同名文件：

| 文件 | 阶段 | 关键字段（英文键 / 中文口径） |
|---|---|---|
| `01_mineru_raw.json` | 提交任务、轮询、下载 ZIP、解出内容列表 | `task_id` 任务号；`state` 终态；`blocks[]` 原始块（含 `page_idx`、`type`、`text/bbox`） |
| `02_repaired.json` | 合并断段、OCR 白名单纠错、还原标题/列表层级 | `blocks[]` 增 `block_id`、`heading_level`、`list_parent_id`、`repairs[]`（每处修复的 `rule`/`before`/`after`） |
| `03_docling_tables.json` | Docling 对每张表结构解析 | `tables[]`：`page_num`、`table_index`、`cells[][]`、`row_spans`/`col_spans`、`caption` |
| `04_enriched.json` | 表格写回 + 抽取正文/标题/表格/列表 | 通用层 `doc_key`/`version_id`/`source_sha256`；场景层 `sections[]`、`tables[]`（结构化信息块，含前后文 `context_before`/`context_after`）、`lists[]` |
| `05_chunks.json` | 语义切分 | `chunks[]`：`chunk_id`、`text`、`page_num`、`chunk_type`、`char_count`、`source_block_ids[]`；表格片 `chunk_type=table` 且整块保留 |
| `manifest.json` | 状态与审计 | `stage_states{}`、`source_sha256`、`created_at`、`updated_at`、`warnings[]`、`errors[]`、`state` |

`state` 只取 `MINERU_DONE → REPAIRED → TABLES_DONE → ENRICHED → CHUNKED → READY_FOR_REVIEW → APPROVED`，失败写 `FAILED` 并带失败阶段名；重跑从最后一个成功阶段之后继续。

## 3. 改动清单（文件 → 改法）

| 文件 | 改法 |
|---|---|
| 新增 `api/docparse/`（单文件夹收拢） | `mineru_client.py`（上传/轮询/下载）、`repair.py`（规则修复）、`docling_tables.py`（表格增强）、`chunker.py`（LangChain SemanticChunker + 500/50 兜底）、`stages.py`（阶段编排）、`manifest.py`（状态与幂等） |
| 新增 `scripts/parse_doc.py` | 薄 CLI；**当前只有 `single` 子命令**，`all`/`report` 等单文件确认后再加 |
| `api/config.py`、`.env.example`、`infra/docker-compose.yml` | 加 `MINERU_API_KEY`、`MINERU_BASE_URL`、`MINERU_MODEL_VERSION=vlm`、`MINERU_POLL_INTERVAL_S`、`MINERU_POLL_TIMEOUT_S`、`DOCLING_TABLE_MODE=accurate`、`PARSE_DATA_DIR=data/parse`、`PARSE_MAX_WORKERS=2`（校验 1–4）。密钥用 `SecretStr` |
| `Makefile` | 当前只加 `parse-one`；`parse-all` / `parse-report` / `ingest-parsed` 等单文件确认后再加，后者是唯一写库入口 |
| `infra/api/requirements-parse.txt` | 加 `docling==2.151.1`（表格链单独装，不塞进在线 api 镜像） |
| 新增 `api/tests/test_docparse_*.py` | 每阶段纯函数用例 + fake HTTP/假 Docling 结果 + 并发上限与幂等用例 |
| `api/parse/`、`api/ingest/` | **不改**；`ingest-parsed` 只把 `READY_FOR_REVIEW` 的产物喂给现有 `index → flip → sync` 链 |

## 4. 命令与验收

```bash
# 用户在本机 .env 填 MINERU_API_KEY；这里不做密钥交互
# 0) 先看页数与产出路径：不发请求、不花 token；只在目标目录写 dry_run.json
make parse-one FILE=samples/policy/travel-cn.pdf MAX_PAGES=10 DRY_RUN=1

# 1) 单文件付费解析：只跑 MinerU 阶段（当前唯一入口）
make parse-one FILE=samples/policy/travel-cn.pdf MAX_PAGES=10
# 等价 CLI：python3 scripts/parse_doc.py single samples/policy/travel-cn.pdf --max-pages 10
# 期望：data/parse/<doc_key>/<version_id>/ 下出现 01_mineru_raw.json、01_mineru_raw.zip、manifest.json
#       manifest.state=MINERU_DONE；spent_tokens=true
# 不给 MAX_PAGES 或页数超限：在发任何请求前报错退出，退出码 2

# 2) 用户确认单文件产物后，才解锁后续修复/Docling/分块与并发入口（尚未实现）

# 下载失败时用已有 batch_id/zip_url 恢复，不重新提交付费任务：
# make parse-resume DIR='data/parse/<doc_key>/<hash>' BATCH_ID='...' ZIP_URL='...'
```

并发命令目前**不存在**；确认单文件输出后再实现，避免误烧 token。下载重试只重试 ZIP GET；每次任务提交后立即写 `mineru_task.json`，下载失败写 `manifest.state=MINERU_DOWNLOAD_FAILED`，可用 `parse-resume` 恢复。

## 5. 风险与开放项

| 风险/开放项 | 对策 / 默认 |
|---|---|
| 云 API 按量付费 | D6 付费闸门；单价/套餐未提供，先按页数估算上限，不写死价格；用户提供价目后再补成本列 |
| MinerU 单文件接口不支持本地文件 | D2 用 batch 上传 1 个文件；不要求用户公网托管 PDF |
| Docling 首次下载模型体积大 | 镜像留持久 model cache；离线部署再配 `artifacts_path` |
| OCR 错别字规则误改 | 只走白名单 + 上下文规则，所有改动进 `repairs[]` 供人工复核；不静默改写 |
| 表格超长 | 表格片不切分、不受 500 字约束；`char_count` 如实记录，报告里标出超长表格 |
| token / 接口权限 | 用户需提供 MinerU token 与账号额度；未填时 CLI 在提交前失败，不发任何请求 |
