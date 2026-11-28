"""运行期中文文案：错误提示、审计说明与提示词外壳。.

只放"会被用户或日志看到"的字符串；注释与文档字符串仍然写在各自模块里。
需要插值的文案统一用 `str.format` 模板，占位符名与调用点一一对应。
"""

from __future__ import annotations

# --- api/errors.py：§11.3 对外文案 -------------------------------------------
ERR_AUTH_INVALID = "请重新登录"
ERR_FORBIDDEN = "无权查看"
ERR_NO_EVIDENCE = "未找到可验证依据"
ERR_LOW_CONFIDENCE = "现有资料不足以下结论"
ERR_UNVERIFIABLE = "本次不作结论，附原文"
ERR_RETRY_LATER = "稍后重试"
ERR_INVALID_REQUEST = "请求格式不正确"
ERR_NOT_FOUND = "未找到该文档"
ERR_SCAN_IN_PROGRESS = "扫描正在进行中"
ERR_VERSION_CONFLICT = "该版本状态已变更，请刷新后重试"

# --- api/identity.py：凭证校验 ----------------------------------------------

CLAIM_ITEM_TYPE = "{claim} 里的元素必须是字符串，实际是 {type_name}"
CLAIM_SHAPE = "{claim} 必须是数组或逗号分隔字符串，实际是 {type_name}"
USER_ID_BLANK = "user_id 不能为空"
MISSING_AUTHORIZATION = "缺少 Authorization 头"
SCHEME_NOT_BEARER = "Authorization 方案必须是 Bearer，实际是 {scheme}"
EMPTY_SCHEME = "空"
MISSING_BEARER_TOKEN = "Bearer 后面没有 token"
TOKEN_EXPIRED = "token 已过期"
TOKEN_BAD_SIGNATURE = "token 签名校验失败"
TOKEN_BAD_ALG = "token alg 不是 {alg}"
TOKEN_UNPARSEABLE = "token 无法解析: {error_type}"
TOKEN_PAYLOAD_NOT_OBJECT = "token payload 不是对象"
TOKEN_MISSING_CLAIM = "token 缺少 {claim} 声明"
CLAIM_NOT_NONEMPTY = "{claim} 声明必须是非空字符串"
MISSING_USER_HEADER = "缺少 {header} 头（AUTH_MODE=mock_header）"

# 身份快照的 pydantic 字段说明（会进 OpenAPI schema）。
DESC_USER_ID = "用户标识；空串等于没有身份"
DESC_DEPTS = "部门列表，去重排序"
DESC_ROLES = "角色列表，去重排序"
DESC_ADMIN = "管理员可跨部门，但审计必须可辨"

MINT_TOKEN_PROD_FORBIDDEN = "mint_dev_token 不允许在生产环境使用"
MINT_TOKEN_WRONG_MODE = "mint_dev_token 只适用于 fixed_jwt，当前是 {mode}"

# --- api/audit.py：§11.2 ----------------------------------------------------

DESC_AUDIT_USER_ID = "用户标识；空串等于没有审计"
DESC_AUDIT_QUESTION = "原问，不改写"
DESC_AUDIT_RETRIEVED = "进精排的切片编号"
DESC_AUDIT_SCORES = "各路最高分与分布"
AUDIT_REJECT_REASON_REQUIRED = (
    "gate1_verdict={verdict} 必须带 reject_reason（§11.3）"
)
AUDIT_REJECT_MISMATCH = (
    "reject_reason={reason} 只能配拒答判定，实际 gate1={verdict}"
)

# --- api/generate/calculator.py --------------------------------------------

CALC_BAD_CHARS = "表达式只能包含数字、括号和四则运算符"
CALC_UNPARSABLE = "无法解析表达式: {expression}"
CALC_TRAILING = "表达式存在多余内容"
CALC_MISSING_TOKEN = "表达式缺少 {expected}"
OPERAND = "操作数"
CALC_DIV_ZERO = "除数不能为 0"
CALC_BAD_NUMBER = "无效数字: {token}"
CALC_RESULT_SINGLE = "result 必须且只能包含一个数值"

# --- api/generate/evidence.py：§9.1 ----------------------------------------

SOURCE_LINE = "《{doc_name}》第 {page_num} 页 · {chunk_id}"
PROMPT_BLOCK = "[{n}] 来源：{source}\n{text}"
TRUNCATION_NOTICE = "（证据超出长度上限，本次仅依据前 {count} 条证据作答）"
EVIDENCE_MAX_CHARS_INVALID = "max_chars 必须 > 0，实际 {value}"
EVIDENCE_CAP_INVALID = "cap 必须 >= 0，实际 {value}"

# --- api/generate/llm.py：§9.2 ---------------------------------------------

FIELD_MUST_BE_LIST = "{field} 必须是数组，实际是 {type_name}"
FIELD_BAD_INT_ITEM = "{field} 里有非整数元素: {item!r}"
FIELD_BAD_STR_ITEM = "{field} 里有非字符串元素: {item!r}"
LLM_NOT_JSON = "模型输出不是 JSON"
LLM_NOT_OBJECT = "模型输出不是对象: {type_name}"
LLM_STATUS_INVALID = "status 缺失或非法: {status!r}"
LLM_FACTS_MISSING = "缺少 facts 字段"
LLM_FACTS_NOT_LIST = "facts 必须是数组，实际 {type_name}"
LLM_FACT_NOT_OBJECT = "fact 不是对象: {item!r}"
LLM_FACT_KIND_INVALID = "fact.kind 非法: {kind!r}"
LLM_CALC_INCOMPLETE = "calculation 缺少 name/expression/result"
LLM_CALC_TEXT_EMPTY = "status=answered 但 fact.text 为空"
LLM_FACT_NO_EVIDENCE = "status=answered 但 fact 缺少 evidence_ids"
LLM_ANSWERED_NO_FACTS = "status=answered 但 facts 为空"
GENERATION_USER_PROMPT = (
    "【证据包】\n{evidence}\n\n【问题】\n{question}\n\n只输出 JSON。"
)

# --- api/generate/render.py -------------------------------------------------

RENDER_FACT_NO_EVIDENCE = "fact 缺少 evidence_ids"
RENDER_UNKNOWN_EVIDENCE = "evidence_id 不在 EvidencePack 中: {evidence_id}"

# --- api/generate/judge.py：§10.1 第四道 ------------------------------------

JUDGE_USER_PROMPT = (
    "【问题】\n{question}\n\n【证据】\n{evidence}\n\n【答案】\n{answer}\n\n"
    "只输出 JSON。"
)
JUDGE_VERDICT_INVALID = "verdict 缺失或非法: {verdict!r}"
JUDGE_TWO_VERDICTS = "判别器同时给出了两种结论: {verdicts}"
JUDGE_UNPARSABLE = "判别器输出无法解析: {body}"
JUDGE_NOT_CONFIGURED = "未配置判别模型"
JUDGE_UNAVAILABLE = "判别器不可用（按不支持处理）: {error_type}"
JUDGE_BAD_OUTPUT = "判别器输出无法解析（按不支持处理）: {error}"

# --- api/verify/checks.py：§10.1 -------------------------------------------

CHECK_CITATION_OUT_OF_PACK = "引用的 evidence_id 不在证据包中"
CHECK_NO_CLAIMS = "草稿没有任何可校验的主张（主张为空）"
CHECK_NAKED_FACTS = "{count} 条 fact 没有 evidence_ids"
CHECK_NUMBER_PREFIX = "数字 "
CHECK_NAME_PREFIX = "专名 "
CHECK_CALC_UNVERIFIABLE = "「{text}」的计算无法由被引证据复算"
CHECK_UNLOCATABLE = "「{text}」里的{missing}在被引证据里定位不到"
CHECK_UNLOCATABLE_DETAIL = "{count} 条 fact 的数字或专名无法在被引证据里定位"
CHECK_NO_FACTS = "无 fact 可校验"
CHECK_JUDGE_NOT_SUPPORTED = "判别器判不支持"
CHECK_JUDGE_SKIPPED = "判别器未运行（未配置），按不支持处理"
CHECK_CONFLICT_DETAIL = "{count} 处同一属性出现两个值"
CHECK_CONFLICT_ITEM = "{attribute} 同时是 {left} 与 {right}（单位 {unit}）"
CHECK_DRAFT_UNKNOWN = "模型判定证据不足（status=unknown）"

# --- api/retrieval/gate.py：§8.1 / §8.2 ------------------------------------

GATE_THRESHOLD_INVALID = (
    "门控阈值必须 0 < reject_none < reject_low <= 1，"
    "实际 {reject_none} / {reject_low}"
)
GATE_BORDERLINE_INVALID = "临界带必须递增，实际 [{low}, {high}]"
GATE_CLARIFY_LABEL = "{name}（第 {page} 页）"
GATE_CLARIFY_CLOSE = "两份文档分数接近（Δ={delta:.3f} < {threshold}）"
GATE_NO_CANDIDATES = "检索无候选"
GATE_RERANK_DEGRADED = "（精排降级，分数不可用）"
GATE_BORDERLINE_FEW_PARAS = (
    "临界带 top1={top1:.3f}：多实体问题但只有 {paragraphs} 段证据"
)
GATE_BORDERLINE_NO_ENTITY = "临界带 top1={top1:.3f}：关键实体未出现在证据里"
GATE_BORDERLINE_PASSED = "临界带 top1={top1:.3f}：辅助条件满足，按过线处理"
GATE_BELOW_LOW = "top1={top1:.3f} 落在 [{low}, {high})"
GATE_FEW_PARAS = (
    "多实体问题但只有 {paragraphs} 段证据（要求 >= {required}）"
)
GATE_UNCOVERED = "多跳未覆盖：{items}"

# --- api/retrieval/hybrid.py -----------------------------------------------

PARENT_CHILD_KEEP_INVALID = "parent_child_keep 必须 >= 0，实际 {keep}"
QUERY_TEXT_EMPTY = "query_text 不能为空：空的 BM25 查询会退化成'全库等权'"
DENSE_QUERY_EMPTY = "dense_query 不能为空"

# --- api/retrieval/multihop.py ---------------------------------------------

MAX_HOPS_INVALID = "max_hops 必须 >= 0，实际 {value}"

# --- api/retrieval/pipeline.py ---------------------------------------------

OUT_OF_SCOPE_NO_RETRIEVAL = "超出库范围，未检索"
DOC_SELECT_MISS = "文档选择无命中"
PRONOUN_CANDIDATES = "指代「{pronoun}」有 {count} 个候选，未检索"

# --- api/qa/orchestrator.py ------------------------------------------------

NO_LLM_MODEL = "未配置生成模型（LLM_MODEL 为空）"
DRAFT_UNUSABLE = "模型输出不可用（{reason}）"
VERIFY_FAILED = "校验未通过"
CLARIFY_DOC = "请确认要查的是哪一份资料："
CLARIFY_CONDITIONS = "请补充查询条件（例如所属部门或年份）："
UNCOVERED_NOTICE = "（以下问题本次未查到可验证依据：{items}）"
TIME_NOTE = "（时间口径：{note}）"
MISSING_IDENTITY = "QaOutcome 缺身份快照：审计无法回答「谁看了什么」"

# --- api/qa/events.py ------------------------------------------------------

CHUNK_SIZE_INVALID = "chunk size 必须 > 0，实际 {size}"

# --- api/query/__init__.py：§6.1 -------------------------------------------

PRONOUN_AMBIGUOUS = "指代 {pronoun!r} 有多个候选: {candidates}"
QUESTION_REQUIRED = "question 不能为空"
MULTIHOP_NOTE = "多跳预判命中: {hits}"
OUT_OF_SCOPE_NOTE = "超出库范围: {hit}"

# --- api/temporal/*.py -----------------------------------------------------

EMPTY_QUESTION = "空问题"
COMPOUND_REASON = "复合问各子问题独立门控"
COMPOUND_MISMATCH = "子问题与门控结果数量不一致，拒绝聚合"

# --- api/docparse/cli.py：命令行帮助 ----------------------------------------

CLI_HELP_SINGLE = "解析单个 PDF，先落本地阶段产物"
CLI_HELP_MAX_PAGES = "付费闸门：必须显式给"
CLI_HELP_DRY_RUN = "只报页数与产出路径，不发请求"
CLI_HELP_RESUME = "从已有输出目录恢复下载，不重提交"
CLI_HELP_FROM_RAW = "只跑后续阶段，不调 MinerU"
CLI_HELP_SOURCE = "from-raw 时的原始 PDF 路径"
CLI_HELP_BATCH_ID = "恢复下载时的 MinerU batch_id"
CLI_HELP_ZIP_URL = "恢复下载时的 full_zip_url"
CLI_REFUSE_NO_ARGUMENT = "拒绝执行：必须给 FILE 或 --resume/--from-raw"
CLI_REFUSE = "拒绝执行：{error}"

# --- api/docparse/mineru_client.py -----------------------------------------

MINERU_KEY_EMPTY = "MINERU_API_KEY 不能为空"
MINERU_NETWORK_ERROR = "MinerU 网络错误：{reason}"
MINERU_NOT_JSON = "MinerU 返回非 JSON: {error}"
MINERU_UPLOAD_SHAPE = "申请上传链接失败：batch_id 或 file_urls 形状不对"
MINERU_UPLOAD_FAILED = "上传 PDF 失败：HTTP {status}"
MINERU_NO_EXTRACT_RESULT = "批量查询结果里没有 extract_result"
MINERU_DONE_NO_ZIP = "done 但没有 full_zip_url"
MINERU_PARSE_FAILED = "MinerU 解析失败：{reason}"
MINERU_UNKNOWN_REASON = "未知原因"
MINERU_POLL_TIMEOUT = "轮询超时（{timeout:.0f}s），最后状态 {state}"
MINERU_DOWNLOAD_HTTP = "下载解析结果失败：HTTP {code}"
MINERU_DOWNLOAD_ERROR = "下载解析结果失败：{reason}"

# --- api/docparse/single.py ------------------------------------------------

SINGLE_LOAD_FAILED = "无法加载 {path}"
SINGLE_PAGE_COUNT_FAILED = (
    "无法从 {path} 读出页数；请安装 pypdfium2 后重试"
    "（dry-run 不能瞎猜页数）"
)
MINERU_KEY_UNSET = "MINERU_API_KEY 未设置：不提交任何付费任务"
ZIP_NO_CONTENT_LIST = "MinerU ZIP 里没有 content_list.json"
SINGLE_DOWNLOAD_FAILED = "下载失败（已重试 {attempts} 次）：{error}"
SINGLE_OUTPUT_MISSING = "输出目录不存在：{target}"
SINGLE_RESUME_NEEDS_BATCH = (
    "恢复下载需要 batch_id（--batch-id 或目录内 mineru_task.json）"
)
SINGLE_FILE_MISSING = "文件不存在：{source}"
SINGLE_DRY_RUN_NOTE = (
    "预检：未调用 MinerU，未产生解析结果；去掉 DRY_RUN=1 才会提交付费任务"
)
SINGLE_MAX_PAGES_REQUIRED = "必须显式给 --max-pages，才允许提交付费解析任务"
SINGLE_PAGE_LIMIT = "页数 {pages} 超过 --max-pages {max_pages}，未提交任务"

# --- api/docparse/docling_tables.py ----------------------------------------

TABLE_PAGE = "表格（第 {page} 页）"
TABLE_TITLE = "表题：{caption}"
TABLE_FOOTNOTE = "注：{footnote}"
TABLE_BEFORE = "前文：{before}"
TABLE_AFTER = "后文：{after}"

# --- api/docparse/{chunker,finalize,stages}.py -----------------------------

TEI_SHAPE_MISMATCH = "TEI embedding 返回数量或维度不匹配"
CHART_PLACEHOLDER = "> [图表]"
MINERU_RAW_MISSING = "找不到 MinerU 原始 JSON：{path}"

# --- api/ingest/*.py ------------------------------------------------------

DOC_KEY_COLLISION = (
    "doc_key_collision: {path} 与 {others} 归一化到同一个 doc_key，拒绝入库"
    "（§4.4）"
)
DOC_KEY_NEEDS_PINYIN = (
    "doc_key 生成需要 pypinyin 把中文转成 ASCII 标识符"
    "（pip install pypinyin）。"
    "若确实要接受中文 doc_key，设 AI_RAG_ALLOW_CJK_DOC_KEY=1。"
)

# --- api/routers/qa.py ----------------------------------------------------

ROUTER_RANGE_ORDER = "to 不能早于 from"
ROUTER_QUESTION_BLANK = "question 不能为空白"

# --- api/main.py ----------------------------------------------------------

API_DESCRIPTION = "金融条款问答：在线链路 API（详细设计.md 附录 C）"
