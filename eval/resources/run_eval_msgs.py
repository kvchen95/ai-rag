"""Chinese message catalog for eval/run_eval.py.

Keys are generated; each entry notes its source line in the script.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # run_eval.py:97 (RL1)
    "RL1_01": (
        "陷阱题拒答率 = 安全拒答/澄清的陷阱题数 ÷ 陷阱题总数（要求 100%），"
        "外加判别力闸：应答题中至少 1 题拿到证据，否则本指标是\"全拒答\"真空态"
    ),
    # run_eval.py:100 (RL2)
    "RL2_01": (
        "数值准确率 = 数值断言全部能在被引证据中找到的题数 ÷ "
        "含数值断言的已答题数（≥96%）"
    ),
    # run_eval.py:104 (RL3)
    "RL3_01": (
        "角标可点回率 = 能在 PG 中定位（chunk_id + doc_key + page_num + bbox）"
        "的引用数 ÷ 总引用数（≥98%）"
    ),
    # run_eval.py:109 (RL4)
    "RL4_01": (
        "忠实度优先于覆盖率 = 已答题中\"每句有引用、数字有据、"
        "无禁用措辞\"的比例（要求 100%）；覆盖率单独报告，"
        "不允许用覆盖率换取忠实度"
    ),
    # run_eval.py:162 (BUILD_TOKEN)
    "BUILD_TOKEN_01": "命令行 --token",
    # run_eval.py:197 (BUILD_TOKEN)
    "BUILD_TOKEN_02": "本地 HS256（--jwt-secret/AUTH_JWT_SECRET）",
    # run_eval.py:199 (BUILD_TOKEN)
    "BUILD_TOKEN_03": "拿不到身份凭证（",
    # run_eval.py:200 (BUILD_TOKEN)
    "BUILD_TOKEN_04": "三种解法任选其一：\n",
    # run_eval.py:201 (BUILD_TOKEN)
    "BUILD_TOKEN_05": (
        "  1. 在容器内跑（推荐，secret 与 DSN 由 compose 注入）：\n"
    ),
    # run_eval.py:203 (BUILD_TOKEN)
    "BUILD_TOKEN_06": "  2. 宿主跑但给 secret：\n",
    # run_eval.py:204 (BUILD_TOKEN)
    "BUILD_TOKEN_07": "     AUTH_JWT_SECRET=<与 compose 同值> ",
    # run_eval.py:206 (BUILD_TOKEN)
    "BUILD_TOKEN_08": "     或在 ",
    # run_eval.py:206 (BUILD_TOKEN)
    "BUILD_TOKEN_09": " 里加一行 AUTH_JWT_SECRET=...\n",
    # run_eval.py:207 (BUILD_TOKEN)
    "BUILD_TOKEN_10": "  3. 已有 token：--token <Bearer token>",
    # run_eval.py:221 (NOTICE)
    "NOTICE_01": (
        "（(?:证据超出长度上限|以下问题本次未查到可验证依据|时间口径："
        "|本次仅依据)[^）]*）"
    ),
    # run_eval.py:239 (CLEANED)
    "CLEANED_01": "\\d{4}\\s*[-/年]\\s*\\d{1,2}\\s*[-/月]\\s*\\d{1,2}\\s*日?",
    # run_eval.py:273 (LOAD_GOLDEN)
    "LOAD_GOLDEN_01": " 不是合法 JSON: ",
    # run_eval.py:283 (LOAD_GOLDEN)
    "LOAD_GOLDEN_02": " 缺字段 ",
    # run_eval.py:287 (LOAD_GOLDEN)
    "LOAD_GOLDEN_03": ": 题号重复",
    # run_eval.py:346 (POST_SSE)
    "POST_SSE_01": "base_url 必须是 http(s)，实际 ",
    # run_eval.py:401 (POST_SSE)
    "POST_SSE_02": "流里没有任何 SSE 帧",
    # run_eval.py:403 (POST_SSE)
    "POST_SSE_03": "读取超时（",
    # run_eval.py:405 (POST_SSE)
    "POST_SSE_04": "传输失败: ",
    # run_eval.py:509 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_01": "没有事件",
    # run_eval.py:511 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_02": "done 出现 ",
    # run_eval.py:511 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_03": " 次（必须恰好 1 次）",
    # run_eval.py:513 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_04": "最后一条不是 done，而是 ",
    # run_eval.py:517 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_05": " 却发了 token",
    # run_eval.py:519 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_06": " 却发了 fallback",
    # run_eval.py:521 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_07": "发了 clarify 但 verdict=",
    # run_eval.py:523 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_08": " 却发了 citations",
    # run_eval.py:525 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_09": "同一条流既有 fallback 又有 token",
    # run_eval.py:530 (CONTRACT_VIOLATIONS)
    "CONTRACT_VIOLATIONS_10": "citations 在 done 之后",
    # run_eval.py:548 (REASON)
    "REASON_01": "chunk_id 不在 PG",
    # run_eval.py:553 (RESOLVE_CITATIONS)
    "RESOLVE_CITATIONS_01": "doc_key 不一致（引用 ",
    # run_eval.py:554 (RESOLVE_CITATIONS)
    "RESOLVE_CITATIONS_02": "，库 ",
    # run_eval.py:559 (RESOLVE_CITATIONS)
    "RESOLVE_CITATIONS_03": "page_num 不一致（引用 ",
    # run_eval.py:563 (RESOLVE_CITATIONS)
    "RESOLVE_CITATIONS_04": "引用没有 bbox",
    # run_eval.py:565 (RESOLVE_CITATIONS)
    "RESOLVE_CITATIONS_05": "bbox 与库里的行不一致",
    # run_eval.py:572 (RESOLVE_CITATIONS)
    "RESOLVE_CITATIONS_06": "切片/版本未生效（",
    # run_eval.py:683 (SCORE_QUESTION)
    "SCORE_QUESTION_01": "输出包含禁用措辞：",
    # run_eval.py:689 (SCORE_QUESTION)
    "SCORE_QUESTION_02": "没有 done 事件，无法判定",
    # run_eval.py:705 (SCORE_QUESTION)
    "SCORE_QUESTION_03": "安全降级但输出了内容",
    # run_eval.py:708 (SCORE_QUESTION)
    "SCORE_QUESTION_04": "陷阱题未安全拒答（verdict=",
    # run_eval.py:711 (SCORE_QUESTION)
    "SCORE_QUESTION_05": "系统故障，不是安全拒答（",
    # run_eval.py:719 (SCORE_QUESTION)
    "SCORE_QUESTION_06": "应答题没有引用",
    # run_eval.py:722 (SCORE_QUESTION)
    "SCORE_QUESTION_07": "有 ",
    # run_eval.py:722 (SCORE_QUESTION)
    "SCORE_QUESTION_08": " 句没有角标",
    # run_eval.py:725 (SCORE_QUESTION)
    "SCORE_QUESTION_09": "数值无据：",
    # run_eval.py:730 (SCORE_QUESTION)
    "SCORE_QUESTION_10": "应答题拒答（verdict=",
    # run_eval.py:733 (SCORE_QUESTION)
    "SCORE_QUESTION_11": "应答题未得到答案（verdict=",
    # run_eval.py:965 (RL1)
    "RL1_02": "陷阱题拒答率",
    # run_eval.py:975 (RL1)
    "RL1_03": "无陷阱题",
    # run_eval.py:980 (RL1)
    "RL1_04": "判别力闸：应答题中拿到证据/进入生成层的 ",
    # run_eval.py:982 (RL1)
    "RL1_05": "；为 0 时本指标是\"全拒答\"真空态",
    # run_eval.py:986 (RL1)
    "RL1_06": "；库内 active 切片 ",
    # run_eval.py:986 (RL1)
    "RL1_07": " 条",
    # run_eval.py:1002 (RL2)
    "RL2_02": "数值准确率",
    # run_eval.py:1011 (RL2)
    "RL2_03": (
        "口径：答案中每个数字（已去掉 [n] 角标）都必须出现在被引证据原文里；"
        "没有已答题或答案不含数字时不可测。"
    ),
    # run_eval.py:1014 (RL2)
    "RL2_04": " 本轮无 DB 连接，证据原文取不到。",
    # run_eval.py:1028 (RL3)
    "RL3_02": "角标可点回率",
    # run_eval.py:1032 (RL3)
    "RL3_03": "无引用可查",
    # run_eval.py:1037 (RL3)
    "RL3_04": (
        "逐条回查 PG：chunk_id 存在、doc_key/page_num/bbox 与库一致、"
        "切片与版本均 active。"
    ),
    # run_eval.py:1039 (RL3)
    "RL3_05": " 本轮无 DB 连接。",
    # run_eval.py:1060 (RL4)
    "RL4_02": "忠实度优先于覆盖率",
    # run_eval.py:1064 (RL4)
    "RL4_03": "= 100%（覆盖率单独报告，不得以覆盖率换忠实度）",
    # run_eval.py:1068 (RL4)
    "RL4_04": "覆盖率 = 应答题得到答案/应答题总数 = ",
    # run_eval.py:1074 (RL4)
    "RL4_05": "；0 题已答时忠实度不可测（不是通过）。",
    # run_eval.py:1112 (VERDICT)
    "VERDICT_01": "不通过（至少一条红线不达标）",
    # run_eval.py:1113 (CONCLUSION)
    "CONCLUSION_01": "四条红线未全过：存在不达标项，按 §12.3 不得发布。",
    # run_eval.py:1115 (VERDICT)
    "VERDICT_02": "不可认证（存在不可测红线）",
    # run_eval.py:1117 (CONCLUSION)
    "CONCLUSION_02": (
        "四条红线中有不可测项，本次结果**不能作为发布依据**。"
        "具体见红线表与前提检查；不要把\"未测\"读成\"通过\"。"
    ),
    # run_eval.py:1121 (VERDICT)
    "VERDICT_03": "通过（四条红线全过）",
    # run_eval.py:1123 (CONCLUSION)
    "CONCLUSION_03": (
        "四条红线全部达标，可作为发布依据（需同时留档版本三件套）。"
    ),
    # run_eval.py:1127 (SUMMARY)
    "SUMMARY_01": "题目 ",
    # run_eval.py:1127 (SUMMARY)
    "SUMMARY_02": " 题（应答题 ",
    # run_eval.py:1127 (SUMMARY)
    "SUMMARY_03": "，陷阱 ",
    # run_eval.py:1128 (SUMMARY)
    "SUMMARY_04": "应答题得到答案 ",
    # run_eval.py:1128 (SUMMARY)
    "SUMMARY_05": " 题，",
    # run_eval.py:1129 (SUMMARY)
    "SUMMARY_06": "单题均分 ",
    # run_eval.py:1130 (SUMMARY)
    "SUMMARY_07": "陷阱安全拒答 ",
    # run_eval.py:1133 (GEN_CELL)
    "GEN_CELL_01": "已配置",
    # run_eval.py:1135 (GEN_CELL)
    "GEN_CELL_02": "**未配置**（应答题会落 model_timeout）",
    # run_eval.py:1138 (LLM_CELL)
    "LLM_CELL_01": "LLM 已配置",
    # run_eval.py:1140 (LLM_CELL)
    "LLM_CELL_02": "LLM_BASE_URL/LLM_MODEL 为空",
    # run_eval.py:1143 (LINES)
    "LINES_01": "# 红线验收报告",
    # run_eval.py:1145 (LINES)
    "LINES_02": "- 生成时间：",
    # run_eval.py:1147 (LINES)
    "LINES_03": "- 评测集：`",
    # run_eval.py:1149 (LINES)
    "LINES_04": "- 身份凭证：",
    # run_eval.py:1152 (LINES)
    "LINES_05": (
        "- 评测器：`eval/run_eval.py`（退出码：0 全过 / 1 不达标 / 2 "
        "有不可测项 / 3 评测器失败）"
    ),
    # run_eval.py:1155 (LINES)
    "LINES_06": "## 1. 一句话结论",
    # run_eval.py:1161 (LINES)
    "LINES_07": "## 2. 口径与数据来源",
    # run_eval.py:1163 (LINES)
    "LINES_08": "| 项 | 值 |",
    # run_eval.py:1165 (LINES)
    "LINES_09": "| 题库 | `",
    # run_eval.py:1165 (LINES)
    "LINES_10": " 题；",
    # run_eval.py:1166 (LINES)
    "LINES_11": "TBD doc_key 占位 ",
    # run_eval.py:1166 (LINES)
    "LINES_12": " 题） |",
    # run_eval.py:1167 (LINES)
    "LINES_13": (
        "| 接口 | `POST /api/v1/qa/stream`（SSE，done 必须是最后一条） |"
    ),
    # run_eval.py:1168 (LINES)
    "LINES_14": "| 语料快照 | documents=",
    # run_eval.py:1171 (LINES)
    "LINES_15": "| 生成模型 | ",
    # run_eval.py:1172 (LINES)
    "LINES_16": "| 审计回查 | ",
    # run_eval.py:1172 (LINES)
    "LINES_17": "qa_log 可用",
    # run_eval.py:1172 (LINES)
    "LINES_18": "不可用（无 DB 连接）",
    # run_eval.py:1174 (LINES)
    "LINES_19": "判分口径（与 `eval/README.md` §5 一致，差异处已注明）：",
    # run_eval.py:1176 (LINES)
    "LINES_20": (
        "1. 陷阱题：安全拒答/澄清（`reject_none`/`reject_low`/`clarify`）、"
        "不输出正文、不含 `must_not_contain` 措辞、不以系统故障收场 → "
        "算拒答成功。"
    ),
    # run_eval.py:1179 (LINES)
    "LINES_21": (
        "2. 应答题：`pass/partial` 且引用非空、每句有角标、数字有据 → 得分；"
        "拒答记 0（防止靠全拒答刷忠实度）。"
    ),
    # run_eval.py:1181 (LINES)
    "LINES_22": "3. 数值准确率只统计\"已答题且答案含数字\"的题。",
    # run_eval.py:1182 (LINES)
    "LINES_23": (
        "4. 角标可点回逐条回查 PG（存在性 + doc_key/page/bbox + active）。"
    ),
    # run_eval.py:1184 (LINES)
    "LINES_24": "## 3. 四条红线",
    # run_eval.py:1186 (LINES)
    "LINES_25": "| 红线 | 结果 | 实测 | 阈值 | 分母/样本 | 说明 |",
    # run_eval.py:1198 (LINES)
    "LINES_26": "红线定义（原样落盘，避免报告与门禁两份口径）：",
    # run_eval.py:1205 (LINES)
    "LINES_27": "## 4. 陷阱题逐题（15 题）",
    # run_eval.py:1207 (LINES)
    "LINES_28": (
        "| 题号 | 期望 | 实际 | 安全拒答 | 拒绝码 | fallback docs | 禁用措辞 |"
    ),
    # run_eval.py:1230 (LINES)
    "LINES_29": "## 5. 应答题逐题",
    # run_eval.py:1232 (LINES)
    "LINES_30": (
        "| 题号 | 期望 | 实际 | 引用 | 可点回 | 数值 | 证据命中 | 得分 | "
        "失败原因 |"
    ),
    # run_eval.py:1246 (LINES)
    "LINES_31": "## 6. 限制与风险",
    # run_eval.py:1248 (LINES)
    "LINES_32": "- **语料**：库内 active 切片 ",
    # run_eval.py:1248 (LINES)
    "LINES_33": " 条。",
    # run_eval.py:1249 (LINES)
    "LINES_34": (
        "为 0 时所有题都会走 `no_evidence`，陷阱拒答率再高也没有判别力。"
    ),
    # run_eval.py:1250 (LINES)
    "LINES_35": "- **题库**：",
    # run_eval.py:1250 (LINES)
    "LINES_36": " 题",
    # run_eval.py:1251 (LINES)
    "LINES_37": "的 `expected_doc_keys` 是 `TBD_*` 占位，",
    # run_eval.py:1252 (LINES)
    "LINES_38": "doc_key 精确命中不可评（`evidence_hit=unknown`），",
    # run_eval.py:1253 (LINES)
    "LINES_39": "只能评答案内部一致性与行为。",
    # run_eval.py:1254 (LINES)
    "LINES_40": "- **生成**：",
    # run_eval.py:1255 (LINES)
    "LINES_41": "未配置时 faithful/numeric 两条红线天然不可测——",
    # run_eval.py:1256 (LINES)
    "LINES_42": "报告不会把它们记成通过。",
    # run_eval.py:1257 (LINES)
    "LINES_43": (
        "- **样本量**：50 题里应答题 31、陷阱 15；任何按百分比的门禁（96%/98%）"
        "在小样本上都是\"错一题就不过\"，因此报告同时给出分子/分母，"
        "不做四舍五入后的口头判断。"
    ),
    # run_eval.py:1260 (LINES)
    "LINES_44": (
        "- **单轮**：忠实度优先于覆盖率是跨版本约束；"
        "本报告只给当前版本的覆盖率与忠实度，不与历史版本比较（没有基线快照）。"
    ),
    # run_eval.py:1264 (LINES)
    "LINES_45": "## 7. 建议动作",
    # run_eval.py:1266 (LINES)
    "LINES_46": (
        "1. 先补齐语料：把 golden 的 `TBD_*` 回填成真实 doc_key，"
        "并让对应文档真正入库（active chunks > 0）；"
        "否则四条红线里三条永远不可测。"
    ),
    # run_eval.py:1269 (LINES)
    "LINES_47": (
        "2. 配好生成网关（LLM_BASE_URL/LLM_MODEL）后再跑本脚本；只有那时 "
        "RL2/RL4 才有意义。"
    ),
    # run_eval.py:1271 (LINES)
    "LINES_48": (
        "3. 每次发布候选按 `eval/README.md` §5.2 的四条红线过门禁；"
        "参数/提示词/模型名三者同版本留档。"
    ),
    # run_eval.py:1273 (LINES)
    "LINES_49": (
        "4. 阈值调参后先跑 `eval/calibrate.py` 看 top1 分布是否真有波谷，再动 "
        "`GATE_*`。"
    ),
    # run_eval.py:1276 (LINES)
    "LINES_50": "## 8. 附件",
    # run_eval.py:1278 (LINES)
    "LINES_51": "- 机器可读：`",
    # run_eval.py:1279 (LINES)
    "LINES_52": "（含逐题事件序列，可直接用于 `eval/calibrate.py`）",
    # run_eval.py:1319 (PARSER)
    "PARSER_01": "黄金 50 题评测 + 四条红线报告",
    # run_eval.py:1322 (BUILD_PARSER)
    "BUILD_PARSER_01": "黄金集 JSONL",
    # run_eval.py:1325 (BUILD_PARSER)
    "BUILD_PARSER_02": "API 根地址",
    # run_eval.py:1328 (BUILD_PARSER)
    "BUILD_PARSER_03": "已有 Bearer token（不给则本地签发）",
    # run_eval.py:1331 (BUILD_PARSER)
    "BUILD_PARSER_04": "宿主签发时用的 HS256 secret",
    # run_eval.py:1334 (BUILD_PARSER)
    "BUILD_PARSER_05": "评测身份 user_id",
    # run_eval.py:1340 (BUILD_PARSER)
    "BUILD_PARSER_06": "评测身份部门（可多次）",
    # run_eval.py:1346 (BUILD_PARSER)
    "BUILD_PARSER_07": "评测身份角色（可多次）",
    # run_eval.py:1351 (BUILD_PARSER)
    "BUILD_PARSER_08": "用管理员身份（审计会带 admin_access）",
    # run_eval.py:1356 (BUILD_PARSER)
    "BUILD_PARSER_09": "PG DSN，用于引用回查/top1",
    # run_eval.py:1359 (BUILD_PARSER)
    "BUILD_PARSER_10": "单题超时秒数",
    # run_eval.py:1362 (BUILD_PARSER)
    "BUILD_PARSER_11": "审计行等待秒数",
    # run_eval.py:1365 (BUILD_PARSER)
    "BUILD_PARSER_12": "只跑这些题号，逗号分隔（如 TR01,TR05）",
    # run_eval.py:1370 (BUILD_PARSER)
    "BUILD_PARSER_13": (
        "只跑这个类别（basic_table/chart/temporal_multihop/trap）"
    ),
    # run_eval.py:1373 (BUILD_PARSER)
    "BUILD_PARSER_14": "最多跑多少题（0=全量）",
    # run_eval.py:1377 (BUILD_PARSER)
    "BUILD_PARSER_15": "报告文件名（不含扩展名）",
    # run_eval.py:1400 (MAIN)
    "MAIN_01": "--only 里有不存在的题号：",
    # run_eval.py:1406 (MAIN)
    "MAIN_02": "筛选后没有题目可跑",
    # run_eval.py:1414 (MAIN)
    "MAIN_03": "[FATAL] 签发 token 失败：",
    # run_eval.py:1420 (DB_REASON)
    "DB_REASON_01": "未配置 DATABASE_URL/--dsn",
    # run_eval.py:1425 (DB_REASON)
    "DB_REASON_02": "psycopg 已连接",
    # run_eval.py:1428 (DB_REASON)
    "DB_REASON_03": "DB 不可用：",
    # run_eval.py:1456 (RUBRIC)
    "RUBRIC_01": "eval/README.md §5（细化 §12.1）",
    # run_eval.py:1461 (MAIN)
    "MAIN_04": "[preflight] 题目=",
    # run_eval.py:1467 (MAIN)
    "MAIN_05": (
        "[preflight][WARN] 语料为空：所有题目都会走 no_evidence，"
        "陷阱拒答率是真空态，报告会记 NOT_MEASURABLE（不是通过）。"
    ),
    # run_eval.py:1472 (MAIN)
    "MAIN_06": (
        "[preflight][WARN] 生成模型未配置：应答题会落 model_timeout，RL2/RL4 "
        "将不可测。"
    ),
    # run_eval.py:1492 (SOURCE)
    "SOURCE_01": "qa_log 查询失败: ",
    # run_eval.py:1506 (MAIN)
    "MAIN_07": "\n=== 四条红线 ===",
    # run_eval.py:1510 (MAIN)
    "MAIN_08": "(阈值 ",
}
