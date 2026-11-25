# 企业级零幻觉知识库 — 详细设计文档编写计划（v1）

> **本文件是 Plan Mode 交付物**：规划"怎么写详细设计"，不是详细设计本身。
> **取代**：`plans/2026-11-21_060629-rag-detailed-design-plan.md`（那份把基线重设为 pgvector-only，与本次决策相反，作废）
> **生成**：2026-11-23 21:12 CST ｜ **状态**：待执行
> **执行者假设**：对本仓库零上下文。本文件之外不需要读任何东西；原件读不到时以本文件的「事实基线」为准。

## 1. Goal

产出两份中文文档：一份面向开发、以图为主、少用术语的《详细设计文档》（`详细设计.md`，12 章 + 7 附录，标题就叫「详细设计文档」），并把现有概设 `概要设计.md` 就地改写成与目标架构同一口径的 v1.1。

## 2. 已拍板的决策（不要再问）

| 决策点 | 结论 |
|---|---|
| 基线 | **以「系统架构图集」的目标态为准**：PostgreSQL + Qdrant、严格 RBAC 前置过滤、MinerU + VLM 图表转译、受控多跳（≤3 跳）；**不引入独立全文引擎、不引入独立对象存储、本期不做文件下发服务**（样本文件只放项目内） |
| 概要文档 | **直接改原文件**，不另建版本文件；文首升到 v1.1 并加变更记录 |
| 文档目录 | `/workspace/ai-rag/`，三篇平铺：`架构图集.md`、`概要设计.md`、`详细设计.md`（单文件，不拆册） |
| 概设 | 已在 `/workspace/ai-rag/概要设计.md`，就地改写为 v1.1，不另建版本 |
| 仓库 | 在 `/workspace/ai-rag/` 建 git 仓库，每完成一个任务提交一次 |
| 文风 | 正文用自然语言 + 图；术语、字段名、DDL、事件名只进附录 |
| 范围 | 只写设计，**不写排期/任务清单/执行命令**（那是实施计划的事） |

## 3. 事实基线（所有章节的唯一数字来源）

写任何一章都只用这张表里的数和词，不得自行发明新指标。

| 事项 | 值 |
|---|---|
| 规模 | PoC 100 份 PDF → 全量 10,000 份；约 40–60 万 child chunk |
| 更新 | 每周增量/覆盖；身份 = `doc_key` + 文件指纹（SHA-256） |
| 切片 | child 256–512 字、重叠 64；parent 1024–2048 字或整章 |
| 图 | 图表裁剪 ≥200 DPI；坐标归一化 0~1 |
| 召回 | 语义、关键词各取 30；融合去重后精排取 5–8 |
| 分数门 | < 0.20 硬拒；0.20–0.40 低置信拒答 + 疑似文档；≥ 0.40 继续 |
| 多跳 | 最多 3 跳（`Max Hops = 3`），每跳独立过阈值，断链即熔断 |
| 生成 | `Temperature = 0`；每句事实句末带 [N] |
| 时间 | `effective_date`(YYYYMMDD) / `effective_year` / `date_confidence` ∈ {explicit, inferred}；做时间过滤只认 explicit |
| 权限 | `is_public` / `allowed_depts` / `allowed_roles`；身份来自 JWT 的 `user_depts` / `user_roles` |
| 性能 | 预过滤 + 向量召回 P95 < 30ms；单跳首字 ~1.0s、总 1.5–2.5s；多跳 4–8s |
| 前端事件 | `step` / `clarify` / `token` / `citations` / `fallback` / `done` |
| 文件 | 本期只处理项目内测试样本：`samples/*.pdf`、`figures/*.png`；不做上传落库、不做对象存储、不做签名下发 |
| 验收 | 50 题（20 基础/表格、10 图表、5 时间/多跳、15 陷阱）；陷阱拒答 100%、数值 ≥96%、角标 ≥98%；阈值在 0.38–0.42 波谷处标定 |
| 状态 | `SCANNING / SKIPPED / PARSING / PENDING / ACTIVE / DEPRECATED / PURGED / FAILED` |
| 存储 | 两库：PostgreSQL（版本真相 + 正文 + 中文词法 + 审计）/ Qdrant（向量 + 过滤字段）。文件不进系统，测试样本随项目存放 |

## 4. 文档骨架（写进 `详细设计.md` 的标题必须与这里逐字一致）

```
# 详细设计文档
## 1 概述
## 2 总体流程
## 3 文档摄取
## 4 数据存储
## 5 检索
## 6 证据判断与多跳
## 7 生成与校验
## 8 前端溯源
## 9 权限与审计
## 10 运行与容量
## 11 验收
## 12 待确定事项
## 附录 A 字段字典
## 附录 B 表与索引
## 附录 C 接口与事件
## 附录 D 提示词
## 附录 E 术语表
## 附录 F 需求追踪矩阵
## 附录 G 概设变更登记
```

### 4.1 每章怎么写

1. 第一段一句话说清这一章管什么，不用问句、不用口号；
2. 接着一张图，图在文字前面；
3. 然后按步骤讲一遍，带数字；
4. 最后用一张小表收边界与失败（最多 5 行）。

正文不用加粗做装饰，不用破折号串长句，一页不超过两张表。

### 4.2 文风硬规则（lint 会查）

1. 正文首次出现的英文名词写成「中文说法（English）」，同一章最多再出现 1 次；其余交给附录 E。
2. 表格、建表语句、字段名、事件名保留英文（机器要稳定），中文名与口径只在附录 A/E。
3. 每章先图后文；没有图的章不算写完。
4. 标题不带问号、感叹号，不超过 16 字；不写"原文存档 + 对齐说明"，不列函数签名。

## 5. 分步任务

> 通用约定：所有命令在 `/workspace/ai-rag/` 下执行。
> 每个任务结束时提交一次：`git add -A && git commit -m "<消息>"`。

### T0 确认文档就位、起仓库（3 分钟）

三篇文档已在 `/workspace/ai-rag/` 平铺（搬运动作已由规划者完成，无需重做）：

```bash
cd /workspace/ai-rag && ls -la
git init -q && git add -A && git commit -qm "docs: 架构图集与概要设计 v1.0 入库"
git log --oneline
```

预期：`ls` 列出 `架构图集.md`、`概要设计.md`；`git log` 恰好 1 行。
若 `架构图集.md` 缺失，用 `cp /root/.hermes/attachments/architecture-2.md /workspace/ai-rag/架构图集.md` 补上（该目录只读，只能拷出）。

> v1.0 原文留存于首个 commit，后续改写随时可用 `git diff` 复核。

### T1 写文档哨兵脚本（先跑红，10 分钟）

**创建** `/workspace/ai-rag/doc_lint.py`，全文照抄：

```python
#!/usr/bin/env python3
"""doc_lint.py — 详细设计文档哨兵。

用法：
  python3 doc_lint.py      # 全文（交付前必跑）
  python3 doc_lint.py 3    # 只查第 3 章（写作时快速反馈）
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DOC = ROOT / "详细设计.md"

CHAPTERS = [str(i) for i in range(1, 13)]  # 1..12
APPENDIX_COUNT = 7
JARGON = ["Rerank", "Pre-Filtering", "NLI", "RRF", "HNSW", "sparsevec", "LangGraph", "Chunk"]
FORBIDDEN = ["TODO", "TBD", "待补充", "此处省略", "AKIA", "BEGIN PRIVATE KEY", "sk-"]
MUST_HAVE = ["0.20", "0.40", "Max Hops = 3", "Temperature = 0", "200 DPI", "40–60 万", "0.38–0.42"]
KINDS = ("flowchart", "graph ", "sequenceDiagram", "stateDiagram", "erDiagram",
         "timeline", "mindmap", "classDiagram", "gantt", "journey")
FENCE = "``" + "`"

failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    if not ok:
        failures.append(msg)


def blocks(text: str) -> dict[str, str]:
    """按 ## 标题切块：{标题: 该块正文}。"""
    heads = [(m.start(), m.group(1).strip()) for m in re.finditer(r"^## +(.+)$", text, re.M)]
    out: dict[str, str] = {}
    for i, (pos, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        out[title] = text[pos:end]
    return out


def strip_code(body: str) -> str:
    return re.sub(FENCE + r"[^\n]*\n.*?" + FENCE, "", body, flags=re.S)


def check_body(name: str, body: str, min_chars: int, need_diagram: bool = True) -> None:
    plain = strip_code(body)
    if need_diagram:
        diagrams = re.findall(FENCE + "mermaid\n(.*?)" + FENCE, body, re.S)
        check(bool(diagrams), f"{name} 没有 Mermaid 图")
        for i, blk in enumerate(diagrams, 1):
            first = next((ln.strip() for ln in blk.splitlines() if ln.strip()), "")
            check(first.startswith(KINDS), f"{name} 第 {i} 张图首行不是已知图类型：{first[:40]}")
        for word in JARGON:
            hits = len(re.findall(rf"(?<![A-Za-z0-9-]){re.escape(word)}(?![A-Za-z0-9-])", plain))
            check(hits <= 2, f"{name} 术语 {word} 出现 {hits} 次（正文上限 2）")
    check(len(plain) >= min_chars, f"{name} 正文过短（{len(plain)} 字，至少 {min_chars}）")


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if not DOC.exists():
        print("DOC LINT: FAIL\n - 文件不存在：" + str(DOC))
        return 1
    text = DOC.read_text(encoding="utf-8")
    all_blocks = blocks(text)

    for ch in CHAPTERS:
        if only and only != ch:
            continue
        title = next((t for t in all_blocks if t.startswith(ch + " ")), None)
        check(title is not None, "缺少章节 ## " + ch)
        if title:
            check("？" not in title and "！" not in title, "## " + title + " 标题含问号或感叹号")
            check(len(title) <= 16, "## " + title + " 标题超过 16 字")
            check_body("## " + title, all_blocks[title], 700)

    if not only:
        apps = [t for t in all_blocks if t.startswith("附录 ")]
        check(len(apps) == APPENDIX_COUNT, f"附录数量为 {len(apps)}，应为 {APPENDIX_COUNT}")
        for t in apps:
            check_body("## " + t, all_blocks[t], 500, need_diagram=False)
        for token in MUST_HAVE:
            check(token in text, "缺少关键约束：" + token)
        for token in FORBIDDEN:
            check(token not in text, "出现禁用串：" + token)
        check(text.count(FENCE) % 2 == 0, "代码栅栏未闭合")

    if failures:
        print("DOC LINT: FAIL")
        for msg in failures:
            print(" -", msg)
        return 1
    print("DOC LINT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

```bash
python3 doc_lint.py; echo "exit=$?"
```

**预期**：`DOC LINT: FAIL` + `- 文件不存在：...` + `exit=1`（这是 TDD 的红）。

提交：`git add -A && git commit -qm "chore: 文档哨兵脚本"`

### T2 写文档骨架与页眉（5 分钟）

**创建** `详细设计.md`，只放页眉和第 4 节的标题（正文留空）：

```markdown
# 详细设计文档

> 版本：v1.0 ｜ 日期：2026-11-23 ｜ 状态：草稿
> 基线：以《系统架构图集》的目标态为准（PostgreSQL + Qdrant，权限前置过滤，图表经模型转译，受控多跳）；关键词走数据库词法；文件下发不在本期范围，样本文件只放项目内
> 输入：《系统架构图集》、《企业级 RAG 系统架构设计 v1.1》（同目录 `概要设计.md`）
> 读者：后端 / 前端 / 平台 / 测试 / 评审
> 口径：本文档为准；概设中与本文冲突的条款已在附录 G 登记并就地替换

## 1 概述
## 2 总体流程
## 3 文档摄取
## 4 数据存储
## 5 检索
## 6 证据判断与多跳
## 7 生成与校验
## 8 前端溯源
## 9 权限与审计
## 10 运行与容量
## 11 验收
## 12 待确定事项

## 附录 A 字段字典
## 附录 B 表与索引
## 附录 C 接口与事件
## 附录 D 提示词
## 附录 E 术语表
## 附录 F 需求追踪矩阵
## 附录 G 概设变更登记
```

```bash
python3 doc_lint.py | head -5; echo "exit=${PIPESTATUS[0]}"
```

**预期**：`DOC LINT: FAIL`，首条是 `- ## 1 概述 没有 Mermaid 图`，末行 `exit=1`（章节齐了、内容还没有，属预期）。

提交：`git commit -am "docs: 详设骨架"`

### T3 概设就地改写为 v1.1（15 分钟）

**修改** `概要设计.md`。动作只有两类：**替换冲突条款**、**文首加变更记录**；章节结构与编号不动。

**1. 文首插入**（放在原有引用块之后）：

```markdown
> **变更记录**
> v1.1（2026-11-23）：基线对齐《系统架构图集》目标态——引入 MinerU 版面解析与图表模型转译、严格权限前置过滤、受控多跳、两库（PostgreSQL/Qdrant），关键词走数据库词法；不引入独立全文引擎与独立对象存储，本期不做文件下发服务（样本文件只放项目内）；撤回"纯 CPU 单机、一期无权限、不做表格与多模态"的旧口径。替换明细见详细设计附录 G。
```

**2. 十七处就地替换**（左=原文要点，右=改成）：

| # | 位置 | 原文要点 | 改为 |
|---|---|---|---|
| 1 | 文档头·已确认约束 | 纯 CPU / 单租户一期无 ACL / 不做表格 / 不做多模态 | 本地 MinerU 版面解析 + 向量/精排（轻量 GPU 或高主频 CPU）；生成与大模型能力走外部商业 API；严格部门/角色隔离；表格与图表纳入解析 |
| 2 | §1.2 目标表 | "单机 CPU 可运行、架构简洁" | 增加两行：「原文像素级可点回」「复杂问题受控多跳」；部署姿态改为私有云可弹性 |
| 3 | §1.3 非目标 | 排除 ACL、表格还原、多模态、大规模 | 整段重写：不做自由发挥、不做图表目测估算、不做未授权可见性、不做跨版本混答；规模上限改为全量 1 万份 |
| 4 | §2 原则 | 五条 | 增补两条：「先筛后答：权限与时间必须在召回前生效」「拒答要分层：硬拒 / 低置信 / 澄清」 |
| 5 | §3.1 架构总览图 | 摄取只有 PyMuPDF/OCR；在线单路检索；存储单库 | 重画：摄取加 MinerU、图表转译、版本状态机；在线加前置过滤、多跳、澄清；存储改三件套 |
| 6 | §4.1 解析路由 | PyMuPDF 主路径 | MinerU 主路径（版面/坐标/表格/图），RapidOCR 保留为扫描件兜底，PyMuPDF 仅用于快速判型 |
| 7 | §4.3 切分 | 单层 300–500 字、重叠 10–20% | 父子两层：子片 256–512 字（重叠 64）用于检索，父片 1024–2048 字或整章用于作答；子片继承父片坐标 |
| 8 | §4.4 向量与索引 | PostgreSQL + pgvector HNSW | 向量库 Qdrant（payload 承载过滤字段）+ 数据库三元组词法通道；双路各 30 条 |
| 9 | §4.5 状态机 | queued→parsed→indexed | 改为 `PENDING → ACTIVE → DEPRECATED → PURGED`，另有 SKIPPED/FAILED；以 `doc_key + 指纹` 驱动 |
| 10 | §5.3 门控 1 | Top1 ≥ T_rel 单一阈值 | 三段式：<0.20 硬拒、0.20–0.40 低置信拒答并给有权限的疑似文档、≥0.40 继续；新增澄清与多跳分流 |
| 11 | §5.6 门控 2 | 规则必做 + Judge 可选 | 规则必做 + 忠实性判别默认开启；数字/日期/专名必须在被引原文中就地可查；失败降级为给原文不给结论 |
| 12 | §6.1 标定方法 | 保守设 T_rel | 在 0.38–0.42 波谷区间标定，并保留可配置下发 |
| 13 | §7.1 数据表 | documents / chunks / ingest_jobs / qa_log | 增 `doc_versions`；chunks 增坐标、类型、图表地址、表格 HTML、时间与权限字段；qa_log 增两道门判定与跳数 |
| 14 | §8 技术栈表 | 整表（pgvector 单库、无编排、无队列） | 整表替换：LangGraph 编排 + Celery/Redis 队列 + MinerU + Qdrant + PostgreSQL（含词法与审计）+ BGE-M3 + BGE-Reranker-v2-m3；不引入独立全文引擎与独立对象存储；文件下发服务另行设计（详细设计附录 B 为字段级依据） |
| 15 | §9.2 指标表 | 六项 | 增「角标溯源有效率」「数值准确率」「陷阱题拒答率」三项，并标注为发布红线 |
| 16 | §10.1 部署 | Docker Compose 单机 | 生产为私有云 K8s；开发与单机试点仍用 Compose；新增统一出向模型网关 |
| 17 | §11.3 演进 | MVP→V1.1→V1.2→V2 | 一期即目标态一期（摄取+双门控+溯源）；V1.1 补增量与权限运营；V1.2 多跳与图表增强；V2 跨库联邦 |

**3. 校验**

```bash
for w in "纯 CPU" "一期无 ACL" "pgvector" Qdrant "本地样本" MinerU "独立全文引擎"; do
  printf '%-14s %s\n' "$w" "$(grep -o "$w" 概要设计.md | wc -l)"
done
grep -c '^## ' 概要设计.md
```

**预期**：`纯 CPU` 0、`一期无 ACL` 0、`pgvector` 0、`Qdrant` ≥3、`本地样本` ≥2、`MinerU` ≥2、`独立全文引擎` ≥1；末行章节数与改写前一致（改写前先跑一次记下数字）。

提交：`git commit -am "docs: 概设对齐目标态 v1.1"`

### T4 §1 概述（8 分钟）

**必须写到**：
- 一句话定位：企业内部 PDF 知识库问答，答得上就答、答不上就说不知道、每条结论能点回原文；
- 三条硬线，每条一句白话加一句"违反会怎样"：不知道就说不知道、只许用证据、每句可点回原文；
- 做与不做两栏短表，各 5 行；不做栏写：自由发挥、图表目测估数、未授权可见、跨版本混答、无出处结论；
- 边界表：规模（100 → 10,000 份，40–60 万子片）、语言、部署、外部模型、权限、更新节奏；文件本期只处理项目内测试样本，一句话写清；
- 优先序一行：忠实度 > 拒答准确 > 引用正确 > 覆盖率；
- 阅读约定两行：术语只在附录 E，字段名用英文但中文名在附录 A。

**图 1 张**：`flowchart TB` 目标 → 约束 → 不做，三层。

```bash
python3 doc_lint.py 1
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: 第 1 章 概述"`

### T5 §2 总体流程（12 分钟）

**必须写到**：
- 模块表 11 行，列：编号 / 模块 / 干什么 / 吃什么 / 吐什么 / 依赖谁 / 详见第几章；M1 接入与身份 … M8 存储 … M11 出向模型网关；
- 八步走一遍：进门 → 听懂 → 找资料 → 判够不够 → 追问或再找 → 作答 → 自查 → 送回，每步 1–3 句；
- 三条分叉及触发条件：直接拒答 / 澄清 / 多跳；
- 时间预算表：预过滤加召回 P95 < 30ms、单跳首字约 1.0s 总 1.5–2.5s、多跳 4–8s；
- 一段典型提问走查：问题 → 命中哪类证据 → 谁打分 → 谁拦下。

**图 3 张**：
- `flowchart LR` 全景：三种角色 → 三条链路（进库 / 问答 / 回看）；
- `flowchart TB` 模块依赖：M1→M2→M3→M4→M5，M6→M7→M8，M9 依赖 M1/M5，M10 依赖 M5，M11 被 M5/M6 调用；
- `sequenceDiagram` 一次提问的时序，三条分叉画成 `alt`。

```bash
python3 doc_lint.py 2
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: 第 2 章 总体流程"`

### T6 §3 前半：扫描与版面解析（10 分钟）

**必须写到**：
- 扫描三种结果（指纹没变→跳过；新文档→解析；指纹变了→走覆盖），以及为什么"覆盖"不能先删旧版（正在被线上用、解析可能失败）；本期触发方式是扫项目内 `samples/` 目录，生产形态（每周增量/网盘对接）只在边界里提一句；
- 一页纸被切成四类区域（正文 / 表格 / 图 / 页眉页脚），坐标统一归一化 0~1 及其理由（换分辨率、换缩放都不用重算）；
- 扫描件兜底：整页字符太少才走识别，识别结果同样带页码与坐标；
- 解析失败的处置：标 FAILED 并记原因，绝不把空文本送进索引。

**图 2 张**：
- `flowchart TD` 扫描判定三分支；
- `flowchart LR` 一页纸切四类区域（示意 + 坐标落在哪个字段）。

```bash
python3 doc_lint.py 3
```
**预期**：`DOC LINT: OK`（本章后半还没写也没关系，lint 只查本章整体达标线）

提交：`git commit -am "docs: §3 扫描与解析"`

### T7 §3 后半：表格、图表与切片（10 分钟）

**必须写到**：
- 表格为什么整表转 HTML 不切断（跨行表头、合并单元格、合计行）；
- 图表三步：原图裁 ≥200 DPI 落到项目内 `figures/` → 模型转译（统计图抽数值表、结构图抽节点与调用关系）→ 转译结果与原图路径绑定；
- "模型说的每个数都要能在原图上对得上"，对不上就算转译失败（这是零幻觉在上游的落点）；
- 父子切片：子片检索、父片作答；子片 256–512 字重叠 64，父片 1024–2048 或整章；
- 每片随行信息六项：页码、坐标、类型、时间、权限、版本。

**图 2 张**：
- `flowchart LR` 表格与图表两条处理分支，汇到"带随行信息的切片"；
- `flowchart TB` 父子切片示意（一个父片包 3 个子片，坐标继承）。

```bash
python3 doc_lint.py 3
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §3 表格图表与切片"`

### T8 §3 收尾：版本转正与清理（10 分钟）

**必须写到**：
- 状态机八个状态的白话解释（扫描 / 跳过 / 解析中 / 待转正 / 生效 / 已下线 / 已清除 / 失败）；
- "先影子后转正"：新片先以 PENDING 写入，写完并抽检通过，才在一个事务里翻牌（同一 `doc_key` 不能同时存在两份 ACTIVE，说明为什么）；
- 失败时旧版继续服务；
- 清理顺序：先标 DEPRECATED → 24 小时保留窗 → 异步物理删除向量与数据库行；样本文件与切片图**不删**（它们是测试资产），并说明为什么先标后删（可回滚、可审计）。

**图 2 张**：
- `stateDiagram-v2` 完整状态机（含 FAILED、SKIPPED 的出口）；
- `flowchart LR` 清理三步顺序。

```bash
python3 doc_lint.py 3
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §3 版本转正与清理"`

### T9 §4 数据存储（10 分钟）

**必须写到**：
- 两库分工表：`存什么 / 为什么是它 / 不能放什么`（PostgreSQL 存版本真相、正文、审计与词法索引；Qdrant 存向量与过滤字段）；
- "真相在哪"：版本状态与正文以 PostgreSQL 为准，向量是副本，允许短暂落后；落后多久可接受要写明；
- 一致性：事务翻牌 → 按 `doc_key` 一次性改 Qdrant 里的状态字段 → 对账扫描兜底；再补一句"检索入口用当前生效版本做一次廉价校验"，堵住中间窗口；
- 样本文件：本期只认项目内 `samples/` 与 `figures/`，由摄取任务直接读目录；不做上传落库、不做文件服务、不做签名下发；
- "为什么文件先不进系统"：现在只有测试样本，进一套对象存储或共享卷意味着多一套要备份、要鉴权、要清理的东西，收益为零；等有真实文档接入再设计；
- 代价也写清：溯源工作台只能跑测试样本；真实文档一接入，文件服务、权限复核、按页流式就是必须先补的缺口，不能当成"以后再说"；
- "为什么能少一个库"：关键词通道放在 PostgreSQL 里，版本翻牌与词法可见性同一个事务，少一套跨系统双写；把代价也写清楚（长句关键词召回弱一档，靠语义通道补）；
- "什么时候再加回来"：关键词通道——片数上千万、并发上几十、或评测显示它明显拖后腿时，才引入独立全文引擎，且只做旁路；文件服务——接入第一份真实文档、或多副本部署、或需要按页流式时，必须补上（形态另行设计）。

**图 1 张**：`flowchart TB` 两库 + 本地样本目录 + 两条路径（写入路径 / 读取路径），并标出对账任务与"文件服务（本期不建）"的虚线。

```bash
python3 doc_lint.py 4
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §4 存储底座"`

### T10 §5 检索（10 分钟）

**必须写到**：
- 先筛后搜（前置过滤）白话解释：权限、时间、是否生效三条必须在召回之前生效；并说明后置过滤的两个恶果（把有权限的好证据挤掉、泄露不该知道的文档存在）；
- 两路召回各 30：语义（向量）+ 关键词（数据库三元组匹配），各自擅长什么（换个说法 vs 编号/型号/条款号/专名）；
- 说明关键词这一路为什么放在数据库里而不是独立检索引擎：同一事务里翻版本，不会出现"已废止条款还能被搜到"；
- 合并去重后按名次融合（只看排名不看绝对分，所以两路分数不可比也不影响融合）；
- 精排：逐对打分取 5–8，说明为什么精排比召回分数更可信；
- 参数表：各路 30 / 融合后 30 / 精排 5–8 / 预过滤+召回 P95 <30ms。

**图 2 张**：
- `flowchart LR` 检索五步（筛 → 语义 → 关键词 → 融合 → 精排）；
- `flowchart LR` 「先筛后搜 vs 先搜后筛」对比，标出被挤掉的好片与被泄露的坏片。

```bash
python3 doc_lint.py 5
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §5 检索链路"`

### T11 §6 证据判断与多跳（10 分钟）

**必须写到**：
- 三段式分数门（<0.20 / 0.20–0.40 / ≥0.40）各自的动作与用户可见结果；为什么中间要留一档（不误杀但也不放行）；
- 澄清只许基于检索到的事实生成选项，附"不许凭空提问"的反例一句；澄清后怎么重新检索；
- 多跳：拆子问题 → 查 → 判 → 记黑板 → 下一跳；最多 3 跳；中间某跳不过阈即熔断，输出已查实部分 + 断链点；每跳单独过阈；
- 一张"什么时候不该多跳"的判定句（单实体常规问题直接走单跳，多跳是成本项）。

**图 2 张**：
- `flowchart TD` 决策树（判分 → 硬拒 / 低置信 / 澄清 / 单跳 / 多跳）；
- `stateDiagram-v2` 多跳状态机（含每跳过阈、断链、达上限三种出口）。

```bash
python3 doc_lint.py 6
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §6 门控与多跳"`

### T12 §7 生成与校验（10 分钟）

**必须写到**：
- 三条生成约束（只用证据 / 每句角标 / 不足就说不知道）+ 温度 0 + 结构化输出（`answered | unknown`）；
- 自查五关，按顺序、按成本从低到高：引用编号存在 → 每句都有引用 → 数字/日期/专名能在被引原文里查到 → 模型判别是否被证据支持 → 内部无自相矛盾；任一不过即不放行；
- 失败降级：不给结论，给原文与页码；
- 四类话术表：无证据 / 无法验证 / 超范围 / 系统异常，各一句用户可见文案。

**图 1 张**：`flowchart TD` 生成 → 五道自查 → 三条出口（放行 / 降级 / 拒答）。

```bash
python3 doc_lint.py 7
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §7 生成与自查"`

### T13 §8 前端溯源（10 分钟）

**必须写到**：
- 双栏布局与交互：左边问答、右边原文；角标 [N] 点一下跳页并画框；
- 坐标换算：`左 = x0 * 视口宽`、`上 = y0 * 视口高`、`宽 = (x1-x0) * 视口宽`、`高 = (y1-y0) * 视口高`，并说明为什么用 0~1 坐标；
- 六类事件在界面上各长什么样：`step` 进度条、`clarify` 可选按钮、`token` 逐字、`citations` 角标、`fallback` 降级提示、`done` 收尾；
- 三种异常界面：无权限、样本文件缺失、文档已下线，各一句提示文案。

**图 1 张**：`flowchart LR` 点角标 → 取本地样本路径（开发期直读） → 跳页 → 画高亮框（含文件缺失回退）。

```bash
python3 doc_lint.py 8
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §8 前端溯源"`

### T14 §9 权限与审计（8 分钟）

**必须写到**：
- 身份从哪来（登录凭证里的部门与角色），一句话讲清为什么不用"先查后判"；
- 三道闸：召回前过滤、取样本文件前复核、全程留痕；
- 不许泄露存在性：低置信拒答时只能列"你有权限的疑似文档"；
- 审计留什么：问题、命中的证据、分数、两道门的判定、最终答案或拒答原因、耗时、模型版本；
- 出向网关：限流、脱敏、密钥托管、出域审批。

**图 1 张**：`flowchart TD` 三道闸路径（含一次越权尝试被拦的走向）。

```bash
python3 doc_lint.py 9
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §9 权限与审计"`

### T15 §10 运行与容量（8 分钟）

**必须写到**：
- 预算表：入库吞吐（纯 CPU 编码慢→异步消化）、单跳、多跳、预过滤+召回；
- 六个指标与告警线：拒答率突变、副本同步积压、向量查询变慢、模型超时率、清理滞后、回收空间；
- 扩容顺序：先加解析与编码工人 → 再拆向量库 → 再分片全文库；并说明为什么按这个顺序。

**图 1 张**：`flowchart LR` 观测三件套（指标 / 日志 / 链路灯）汇到告警与处置动作。

```bash
python3 doc_lint.py 10
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §10 容量与观测"`

### T16 §11 验收（8 分钟）

**必须写到**：
- 50 题构成表（20 / 10 / 5 / 15）与每类要抓什么；
- 四条红线：陷阱题拒答 100%、数值 ≥96%、角标可点回 ≥98%、忠实度优先于覆盖率；
- 阈值标定：0.38–0.42 波谷法（白话：把可答与不可答的分数分布画出来，取中间低谷）；
- 发布门禁：任一红线回退即不发布，参数必须可回滚。

**图 1 张**：`flowchart LR` 评测闭环（出题 → 跑 → 判分 → 调参 → 回写配置）。

```bash
python3 doc_lint.py 11
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §11 验收"`

### T17 §12 待确定事项（8 分钟）

**必须写到**：
- 待决策表 8 行：向量库 Qdrant 与 Milvus 的切换条件、队列 Celery 与 Temporal、生成模型与读图模型的具体型号、关键词通道是否升级为独立全文引擎（触发条件写清）、**真实文档接入后的文件服务形态**（对象存储 / 共享卷 / 直连 NAS）、身份字段契约、数据库版本、模型出域审批；
- 演进阶梯四段（V1.1 / V1.2 / V2），每段一句"什么时候才做"。

**图 1 张**：`timeline` 四段演进，标注触发条件。

```bash
python3 doc_lint.py 12
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: §12 待决策与演进"`

### T18 附录 A + 附录 B（12 分钟）

**附录 A**：五张表逐字段，列固定为 `英文键 / 类型 / 中文名 / 口径（怎么算、从哪来）/ 示例`。
覆盖：`documents`、`doc_versions`、`chunks`、`ingest_jobs`、`qa_log`。
必含字段（一个都不能少）：
- `chunks`：`chunk_id`、`doc_key`、`version_id`、`status`、`chunk_type`（text/table/figure）、`parent_id`、`page_num`、`bbox`（归一化，数组）、`image_path`（项目内相对路径，如 `figures/{chunk_id}.png`）、`content_text`、`table_html`、`effective_date`、`effective_year`、`date_confidence`、`is_public`、`allowed_depts`、`allowed_roles`、`embedding_dense`、`embedding_sparse`、`token_count`；
- `doc_versions`：`version_id`、`doc_key`、`file_hash`、`status`、`activated_at`、`superseded_by`；
- `qa_log`：`question`、`retrieved_ids`、`score_snapshot`、`gate1_verdict`、`gate2_verdict`、`hop_count`、`answer`、`citations`、`reject_reason`、`latency_ms`、`model`。

**附录 B**：PostgreSQL 建表语句（`documents/doc_versions/chunks/ingest_jobs/qa_log`）+ 索引清单（状态、`doc_key+status`、`version_id`、时间、权限数组、向量）；Qdrant 集合的 payload 字段表；可选全文旁路的字段表（仅在触发升级时启用，且必须能从数据库全量重建）；样本目录规范（`samples/{doc_key}.pdf`、`figures/{chunk_id}.png`）+ 备份与清理策略（只备份数据库与向量快照，样本随项目走）。

```bash
python3 doc_lint.py
```
**预期**：报错只剩 `附录 C 缺失` 到 `附录 G 缺失` 五条；不得再出现章节、图、字数、术语类报错。

提交：`git commit -am "docs: 附录 A/B 契约与表结构"`

### T19 附录 C + 附录 D（10 分钟）

**附录 C**：五个接口，每个给 `方法 + 路径 + 请求字段 + 返回字段 + 失败码`：
- `POST /api/v1/documents`（上传，返回排队号）
- `GET /api/v1/documents/{id}`（入库状态）
- `POST /api/v1/qa/stream`（问答，SSE）
- `GET /api/v1/samples/{doc_key}.pdf`（开发期直出测试样本，无鉴权，仅供本地工作台）
- `GET /healthz`
SSE 六类事件的 payload 字段表（`step` 带第几跳与阶段名；`clarify` 带选项数组；`token` 带增量文本；`citations` 带文档/页码/坐标/图地址；`fallback` 带降级原因；`done` 带总耗时）。

**附录 D**：四段提示词全文——作答约束（含三条不可弱化的规则）、统计图转译、结构图转译、忠实性判别（只输出 `SUPPORTED / NOT_SUPPORTED`）。

```bash
grep -c "event: " 详细设计.md
```
**预期**：≥ 6

提交：`git commit -am "docs: 附录 C/D 接口与提示词"`

### T20 附录 E + F + G（10 分钟）

**附录 E**：≤30 条，列 `中文说法 / English / 一句话解释 / 首次出现章节`；必须覆盖：前置过滤、双路召回、名次融合、精排、证据充足性、忠实性判别、父子切片、影子转正、归一化坐标、断链熔断、样本目录。

**附录 F**：两段矩阵——架构图集 §1–§7 与概设 §1–§11 各自映射到详设章节，每行给"验证方式"（指向哪张图或哪张表）。

**附录 G**：把 T3 的十七行替换表原样搬入（列：位置 / 原口径 / 新口径 / 原因），并在表前一句话说明"概设已就地改为 v1.1，此处仅做可追溯登记"。

```bash
python3 doc_lint.py
```
**预期**：`DOC LINT: OK`

提交：`git commit -am "docs: 附录 E/F/G"`

### T21 交付前终检（10 分钟）

```bash
python3 doc_lint.py
grep -c '```mermaid' 详细设计.md
grep -c '^## ' 详细设计.md
wc -m 详细设计.md 概要设计.md
grep -nE "TODO|TBD|待补充|此处省略" 详细设计.md || echo "无占位残留"
```

**预期**：`DOC LINT: OK`；图 ≥ 15；`## ` 标题 19（12 章 + 7 附录）；详设 ≥ 25000 字符；无占位残留。

最后人工过一遍三条：每章第一屏是不是图；正文里还有没有没解释的英文缩写；有没有把"任务清单/排期"写进详设（有就删）。

提交：`git commit -am "docs: 详设 v1.0 交付"`

## 6. 验收清单

| 项 | 判据 |
|---|---|
| 结构 | 12 章 + 7 附录，标题与第 4 节逐字一致 |
| 图 | 每章 ≥1 张 Mermaid，全文 ≥15 张，首行是合法图类型 |
| 标题 | 不带问号、感叹号，不超过 16 字；不用「先看这一页」这类口语标题 |
| 文风 | 章节内每个术语 ≤2 次；无函数签名清单；无任务/排期 |
| 数字 | 0.20 / 0.40 / Max Hops = 3 / Temperature = 0 / 200 DPI / 5 分钟 / 40–60 万 全部出现 |
| 契约 | 附录 A 字段字典覆盖第 5 节列出的全部字段；附录 C 六类事件齐全 |
| 概设 | `纯 CPU`、`一期无 ACL`、`pgvector` 归零；Qdrant、本地样本、MinerU 出现；MinIO 与 Elasticsearch 零出现 |
| 可追溯 | 附录 F 覆盖两份源文档全部章节；附录 G 十七行 |
| 洁净 | 无 TODO/占位/密钥串 |

## 7. 风险、权衡与待确认

- **两份输入的冲突面很大**：概设原为"单机 CPU、单库、无权限"，目标态是三件套 + 权限 + 多模态。T3 的十七行替换是唯一权威清单，执行时不得顺手改别处，改动超出清单要回填附录 G。
- **"少用术语"与"可实施"存在张力**：解决办法是把契约压到附录（A/B/C/D），正文只讲机制与取舍；若某章必须出现英文，按「中文说法（English）」写一次。
- **图表转译的精度是最大不可控项**：正文里必须写死"数字对不上就判转译失败"，不许在 doc 里承诺模型一定准。
- **Qdrant 与 Milvus、Celery 与 Temporal 尚未定版**：本计划按 Qdrant + Celery 写默认口径，切换条件写进 §12，不写进正文。
- **待确认（写进 §12，不要在正文里猜）**：读图与生成模型的具体型号、词法扩展选型、真实文档接入后的文件服务形态、身份字段契约、数据库版本、模型出域审批。
- **概设改写不可逆风险**：v1.0 原文靠首个 commit 保全；改写完成后必须 `git diff --stat` 确认只动了预期章节。
- **去掉独立全文引擎与对象存储是本次最大的选型取舍**：换来的是版本切换单事务、过滤条件只写一处、少两套需要单独备份与运维的系统；代价是长句关键词召回弱一档。因此详设 §4 必须把代价和升级触发条件一起写，不能只写好处，否则评审会当成"漏设计"。
- **文件范围收窄到测试样本是显式欠账**：溯源工作台只对样本可用，真实文档一接入就会出现"文件从哪来、怎么鉴权、怎么按页流"三个缺口。这条要同时写进 §4 的边界和 §12 的待决策，不许含糊过去。
