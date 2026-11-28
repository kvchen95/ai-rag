"""Chinese message catalog for scripts/verify_a3_api.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # verify_a3_api.py:131 (PRINT)
    "PRINT_01": "A3 API 契约实测（真 HTTP @ ",
    # verify_a3_api.py:149 (PRINT)
    "PRINT_02": "\n[1] 404：库里没有这个版本",
    # verify_a3_api.py:153 (CHECK)
    "CHECK_01": "不存在的版本 -> 404",
    # verify_a3_api.py:155 (PRINT)
    "PRINT_03": "\n[2] 422：空版本拒绝转正",
    # verify_a3_api.py:162 (CHECK)
    "CHECK_02": "空版本 -> 422",
    # verify_a3_api.py:168 (CHECK)
    "CHECK_03": "空版本没有被翻成 active",
    # verify_a3_api.py:170 (PRINT)
    "PRINT_04": "\n[3] 造一个有切片的版本，走 promote",
    # verify_a3_api.py:178 (ENUMERATE)
    "ENUMERATE_01": "住宿费标准：总监级每人每天 800 元，经理级 600 元。",
    # verify_a3_api.py:179 (ENUMERATE)
    "ENUMERATE_02": "市内交通费凭票报销，单次不超过 100 元。",
    # verify_a3_api.py:205 (CHECK)
    "CHECK_04": "返回体含 index/flip/drain/reconcile 四段",
    # verify_a3_api.py:216 (CHECK)
    "CHECK_05": "索引了 2 个点",
    # verify_a3_api.py:221 (CHECK)
    "CHECK_06": "对账无残留",
    # verify_a3_api.py:226 (PRINT)
    "PRINT_05": "\n[4] 409：重复 promote 同一个版本",
    # verify_a3_api.py:233 (CHECK)
    "CHECK_07": "已 active 的版本再 promote -> 409",
    # verify_a3_api.py:235 (PRINT)
    "PRINT_06": "\n[5] reconcile：干净时 200，有漂移时 409",
    # verify_a3_api.py:239 (CHECK)
    "CHECK_08": "干净 -> 200",
    # verify_a3_api.py:270 (CHECK)
    "CHECK_09": "有漂移且 dry-run -> 409（不是 200）",
    # verify_a3_api.py:272 (CHECK)
    "CHECK_10": "dry-run 看见了 1 处 status_mismatch",
    # verify_a3_api.py:280 (CHECK)
    "CHECK_11": "真修好后 -> 200",
    # verify_a3_api.py:284 (CHECK)
    "CHECK_12": "再查一次仍干净",
    # verify_a3_api.py:286 (PRINT)
    "PRINT_07": "\n[6] sync 端点：没有待同步任务时 processed=0",
    # verify_a3_api.py:289 (CHECK)
    "CHECK_13": "没有可疑的失败",
    # verify_a3_api.py:291 (PRINT)
    "PRINT_08": "\n[7] rollback：把已 deprecated 的版本翻回来",
    # verify_a3_api.py:313 (CHECK)
    "CHECK_14": "回滚到了那个版本",
    # verify_a3_api.py:322 (CHECK)
    "CHECK_15": "回滚后对账干净",
}
