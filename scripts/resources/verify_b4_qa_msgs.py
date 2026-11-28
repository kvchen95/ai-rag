"""Chinese message catalog for scripts/verify_b4_qa.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # verify_b4_qa.py:56 (PRINT)
    "PRINT_01": "需要 AI_RAG_TEST_DATABASE_URL 或 DATABASE_URL",
    # verify_b4_qa.py:60 (PRINT)
    "PRINT_02": "需要 ",
    # verify_b4_qa.py:60 (PRINT)
    "PRINT_03": "（本脚本要真检索链路）",
    # verify_b4_qa.py:195 (PRINT)
    "PRINT_04": "\n问：",
    # verify_b4_qa.py:216 (PRINT)
    "PRINT_05": "B4 端到端验收（真 PG + Qdrant + TEI；生成/判别为脚本替身）",
    # verify_b4_qa.py:226 (TEXT)
    "TEXT_01": "住宿费标准：每人每天 500 元，按职级限额报销。",
    # verify_b4_qa.py:237 (ANSWER)
    "ANSWER_01": "住宿费标准为每人每天 500 元[1]。",
    # verify_b4_qa.py:240 (TEXT)
    "TEXT_02": "住宿费标准为每人每天 500 元",
    # verify_b4_qa.py:246 (PRINT)
    "PRINT_06": "\n--- 判据 1：正常答案与引用 ---",
    # verify_b4_qa.py:247 (RUN)
    "RUN_01": "住宿费标准是多少",
    # verify_b4_qa.py:250 (CHECK)
    "CHECK_01": "有 token 与 citations，事件以 done 收尾",
    # verify_b4_qa.py:267 (CHECK)
    "CHECK_02": "citations 含 doc/page/bbox/snippet/chunk_id",
    # verify_b4_qa.py:278 (CHECK)
    "CHECK_03": "citations 的页码与 bbox 与库里的行一致",
    # verify_b4_qa.py:286 (CHECK)
    "CHECK_04": "生成与判别都在 Temperature=0 调用",
    # verify_b4_qa.py:296 (ANSWER)
    "ANSWER_02": "住宿费标准一般是 400 元[1]。",
    # verify_b4_qa.py:299 (TEXT)
    "TEXT_03": "住宿费标准一般是 400 元",
    # verify_b4_qa.py:305 (PRINT)
    "PRINT_07": "\n--- 判据 2：不可验证不输出结论 ---",
    # verify_b4_qa.py:306 (RUN)
    "RUN_02": "住宿费标准是多少",
    # verify_b4_qa.py:309 (CHECK)
    "CHECK_05": "校验不过时不发 token",
    # verify_b4_qa.py:311 (CHECK)
    "CHECK_06": "降级码是 unverifiable",
    # verify_b4_qa.py:313 (CHECK)
    "CHECK_07": "无 fallback",
    # verify_b4_qa.py:316 (CHECK)
    "CHECK_08": "档一给了原文片段",
    # verify_b4_qa.py:324 (SCRIPTED)
    "SCRIPTED_01": "不应被调用",
    # verify_b4_qa.py:325 (PRINT)
    "PRINT_08": "\n--- 判据 3：无证据不硬答 ---",
    # verify_b4_qa.py:326 (RUN)
    "RUN_03": "如何申报个人所得税专项附加扣除",
    # verify_b4_qa.py:328 (CHECK)
    "CHECK_09": "拒答路径没有 token",
    # verify_b4_qa.py:329 (CHECK)
    "CHECK_10": "拒答路径没有调模型",
    # verify_b4_qa.py:331 (CHECK)
    "CHECK_11": "verdict 是拒答两态之一",
    # verify_b4_qa.py:337 (PRINT)
    "PRINT_09": "\n--- 判据 4：系统异常不暴露内部细节 ---",
    # verify_b4_qa.py:338 (RUN)
    "RUN_04": "住宿费标准是多少",
    # verify_b4_qa.py:342 (CHECK)
    "CHECK_12": "有证据但模型不可用 → 系统档",
    # verify_b4_qa.py:348 (CHECK)
    "CHECK_13": "降级码是 model_timeout",
    # verify_b4_qa.py:353 (CHECK)
    "CHECK_14": "系统档不给原文",
    # verify_b4_qa.py:358 (CHECK)
    "CHECK_15": "系统档文案是稍后重试",
    # verify_b4_qa.py:359 (CHECK)
    "CHECK_16": "稍后重试",
    # verify_b4_qa.py:364 (PRINT)
    "PRINT_10": "\n--- 判据 5：审计落库 ---",
    # verify_b4_qa.py:368 (RUN)
    "RUN_05": "住宿费标准是多少",
    # verify_b4_qa.py:373 (ANSWER)
    "ANSWER_03": "住宿费标准为每人每天 500 元[1]。",
    # verify_b4_qa.py:377 (TEXT)
    "TEXT_04": "住宿费标准为每人每天 500 元",
    # verify_b4_qa.py:389 (CHECK)
    "CHECK_17": "qa_log 插入成功（真表 CHECK 通过）",
    # verify_b4_qa.py:401 (PRINT)
    "PRINT_11": "  qa_log 行：gate1=",
    # verify_b4_qa.py:404 (PRINT)
    "PRINT_12": "\n--- 判据 6：SSE 帧形状 ---",
    # verify_b4_qa.py:419 (CHECK)
    "CHECK_18": "每帧都是 event:/data: 且 data.type 与事件名一致",
    # verify_b4_qa.py:444 (PRINT)
    "PRINT_13": " 条判据不成立：",
    # verify_b4_qa.py:450 (PRINT)
    "PRINT_14": (
        "注意：生成与判别是脚本替身。生成质量（模型真的会说错并被拦住）需要"
        "配了 LLM 网关后跑 eval/run_eval.py。"
    ),
}
