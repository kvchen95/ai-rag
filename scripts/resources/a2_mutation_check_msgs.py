"""Chinese message catalog for scripts/a2_mutation_check.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # a2_mutation_check.py:64 (WHY)
    "WHY_01": "旋转 90 度映射写错 → 旋转页 bbox 与未旋转页对不上",
    # a2_mutation_check.py:73 (WHY)
    "WHY_02": "逆映射写错 → 显示/阅读空间往返不闭合",
    # a2_mutation_check.py:85 (WHY)
    "WHY_03": "去掉页边带判据 → 每页的章节标题与条款被当页眉删掉",
    # a2_mutation_check.py:93 (WHY)
    "WHY_04": "不抹平数字 → 带页码的页眉一个都认不出来",
    # a2_mutation_check.py:101 (WHY)
    "WHY_05": "去掉「跨栏缝即非表格」→ 双栏正文被当成表格",
    # a2_mutation_check.py:110 (WHY)
    "WHY_06": "去掉「长单元格即非表格」→ 一段话被当成表格的一格",
    # a2_mutation_check.py:118 (WHY)
    "WHY_07": "丢掉合并列信息 → 三表示的一致性被破坏",
    # a2_mutation_check.py:127 (WHY)
    "WHY_08": "合并格展开改成「后到先得」→ 左上角的值被覆盖",
    # a2_mutation_check.py:136 (WHY)
    "WHY_09": "md 不转义竖线 → 一行被切成更多列",
    # a2_mutation_check.py:147 (WHY)
    "WHY_10": "去掉占比求和检查 → 32 被读成 74 也能入库",
    # a2_mutation_check.py:156 (WHY)
    "WHY_11": "去掉「数值能在图上找到」→ 模型编的数字也能入库",
    # a2_mutation_check.py:165 (WHY)
    "WHY_12": "去掉标签检查 → 编造的类别名也能入库",
    # a2_mutation_check.py:175 (WHY)
    "WHY_13": "放行「谁都看不到」的切片 → 六项校验少一项",
    # a2_mutation_check.py:184 (WHY)
    "WHY_14": "不校验页码 → 引用不能跳页的切片也能入库",
    # a2_mutation_check.py:193 (WHY)
    "WHY_15": "派生列不自洽 → 时间过滤按错误的年去筛",
    # a2_mutation_check.py:201 (WHY)
    "WHY_16": "去掉重叠 → 一句话被切断后两边都检索不到",
    # a2_mutation_check.py:209 (WHY)
    "WHY_17": "显式日期被降级成推断 → 时间过滤永远不认它",
    # a2_mutation_check.py:217 (WHY)
    "WHY_18": "不补零宽字宽 → 真实样本的行宽为 0，整份文档一条切片都出不来",
    # a2_mutation_check.py:225 (WHY)
    "WHY_19": "不查 DPI → 糊图也能入库（刻度读出来是错的）",
    # a2_mutation_check.py:233 (WHY)
    "WHY_20": "对账失败仍然入库 → 验收第二条破了",
    # a2_mutation_check.py:241 (WHY)
    "WHY_21": "要求位图覆盖半页 → 23% 的真实扫描页被判成非扫描件",
    # a2_mutation_check.py:250 (WHY)
    "WHY_22": (
        "不让调用方提供渲染器 → 拿 pdfplumber 的 Page 去 render，运行时崩"
    ),
    # a2_mutation_check.py:259 (WHY)
    "WHY_23": "OCR 页仍按图区域遮挡 → 识别出的文字全被扣掉，一片都不入库",
    # a2_mutation_check.py:267 (WHY)
    "WHY_24": "不把来源传下去 → 下游分不清文字层与识别结果",
    # a2_mutation_check.py:275 (WHY)
    "WHY_25": (
        "as_row 丢掉矩阵 → §3.3 的第三份表示入库即丢，校验只能正则拆 HTML"
    ),
    # a2_mutation_check.py:284 (WHY)
    "WHY_26": "as_row 丢掉来源标记 → 下游看不出这段文字是 OCR 认的",
    # a2_mutation_check.py:293 (WHY)
    "WHY_27": "编号带上 version_id → 文件路径里有 / 与 #，图必定 404",
    # a2_mutation_check.py:310 (RUN_MUTATION)
    "RUN_MUTATION_01": "锚点未找到（",
    # a2_mutation_check.py:313 (RUN_MUTATION)
    "RUN_MUTATION_02": (
        "这条变异需要真 PG（集成用例会 skip），但没设 "
        "AI_RAG_TEST_DATABASE_URL/DATABASE_URL —— 不把 skip 当检出"
    ),
    # a2_mutation_check.py:339 (RUN_MUTATION)
    "RUN_MUTATION_03": "变异后只用例被 skip 了（没有真 PG）—— 不算检出",
    # a2_mutation_check.py:341 (RUN_MUTATION)
    "RUN_MUTATION_04": "变异后测试仍然全绿 —— 这条判据没有被任何用例覆盖",
    # a2_mutation_check.py:373 (DESCRIPTION)
    "DESCRIPTION_01": "A2 判据变异测试",
    # a2_mutation_check.py:374 (HELP)
    "HELP_01": "只列出变异，不执行",
    # a2_mutation_check.py:375 (HELP)
    "HELP_02": "只跑指定 key",
    # a2_mutation_check.py:377 (HELP)
    "HELP_03": "保留临时副本（调试用）",
}
