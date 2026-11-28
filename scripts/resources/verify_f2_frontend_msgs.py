"""Chinese message catalog for scripts/verify_f2_frontend.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # verify_f2_frontend.py:40 (DEMO_QUESTIONS)
    "DEMO_QUESTIONS_01": "住宿标准是多少",
    # verify_f2_frontend.py:40 (DEMO_QUESTIONS)
    "DEMO_QUESTIONS_02": "报销标准是多少",
    # verify_f2_frontend.py:40 (DEMO_QUESTIONS)
    "DEMO_QUESTIONS_03": "差旅和报销的区别",
    # verify_f2_frontend.py:163 (CHECK)
    "CHECK_01": ": 有事件",
    # verify_f2_frontend.py:167 (CHECK)
    "CHECK_02": ": done 是最后一帧",
    # verify_f2_frontend.py:167 (CHECK)
    "CHECK_03": "最后是 ",
    # verify_f2_frontend.py:169 (CHECK)
    "CHECK_04": ": step 打头",
    # verify_f2_frontend.py:169 (CHECK)
    "CHECK_05": "第一帧 ",
    # verify_f2_frontend.py:172 (CHECK)
    "CHECK_06": ": 拒答路径无 token",
    # verify_f2_frontend.py:175 (CHECK)
    "CHECK_07": ": citations 先于 done",
    # verify_f2_frontend.py:191 (CHECK)
    "CHECK_08": ": 本轮无引用（无需检查角标）",
    # verify_f2_frontend.py:195 (CHECK)
    "CHECK_09": ": 引用字段齐",
    # verify_f2_frontend.py:195 (CHECK)
    "CHECK_10": "缺 ",
    # verify_f2_frontend.py:205 (CHECK)
    "CHECK_11": ": 角标全部可点回",
    # verify_f2_frontend.py:207 (CHECK)
    "CHECK_12": "正文 ",
    # verify_f2_frontend.py:207 (CHECK)
    "CHECK_13": " / 点不动 ",
    # verify_f2_frontend.py:211 (CHECK)
    "CHECK_14": ": bbox 是数组或缺省（缺省即降级展示）",
    # verify_f2_frontend.py:225 (CHECK)
    "CHECK_15": "不泄露：",
    # verify_f2_frontend.py:225 (CHECK)
    "CHECK_16": "命中 ",
    # verify_f2_frontend.py:242 (PRINT)
    "PRINT_01": (
        "F2 验收：SSE 顺序 / 角标可点回 / 样本四态 / 错误体不泄露  (api="
    ),
    # verify_f2_frontend.py:257 (PRINT)
    "PRINT_02": "\n问：",
    # verify_f2_frontend.py:258 (PRINT)
    "PRINT_03": "  事件序列：",
    # verify_f2_frontend.py:264 (CHECK)
    "CHECK_17": ": 流式响应 200",
    # verify_f2_frontend.py:267 (APPEND)
    "APPEND_01": "问答流「",
    # verify_f2_frontend.py:269 (PRINT)
    "PRINT_04": "\n[接口四态]",
    # verify_f2_frontend.py:271 (FOUR)
    "FOUR_01": "样本：无凭证 401",
    # verify_f2_frontend.py:272 (FOUR)
    "FOUR_02": "样本：有权限 200",
    # verify_f2_frontend.py:277 (FOUR)
    "FOUR_03": "样本：无权限 403",
    # verify_f2_frontend.py:283 (FOUR)
    "FOUR_04": "样本：不存在 404",
    # verify_f2_frontend.py:289 (FOUR)
    "FOUR_05": "切片图：无权限 403",
    # verify_f2_frontend.py:291 (FOUR)
    "FOUR_06": "切片图：不存在 404",
    # verify_f2_frontend.py:302 (CHECK)
    "CHECK_18": "：content-type 是 ",
    # verify_f2_frontend.py:318 (CHECK)
    "CHECK_19": "问答：坏凭证 401",
    # verify_f2_frontend.py:319 (APPEND)
    "APPEND_02": "坏凭证 401",
    # verify_f2_frontend.py:321 (PRINT)
    "PRINT_05": "\n[不泄露]",
    # verify_f2_frontend.py:326 (PRINT)
    "PRINT_06": "判据 ",
    # verify_f2_frontend.py:326 (PRINT)
    "PRINT_07": " 条，失败 ",
    # verify_f2_frontend.py:326 (PRINT)
    "PRINT_08": " 条",
}
