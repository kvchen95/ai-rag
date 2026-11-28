"""Chinese message catalog for scripts/verify_a3_migration.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # verify_a3_migration.py:39 (CHECK)
    "CHECK_01": " 被拒",
    # verify_a3_migration.py:39 (CHECK)
    "CHECK_02": "没有报错（应当被约束拦住）",
    # verify_a3_migration.py:42 (CHECK)
    "CHECK_03": " 被拒",
    # verify_a3_migration.py:61 (PRINT)
    "PRINT_01": "=== 新建 database ",
    # verify_a3_migration.py:61 (PRINT)
    "PRINT_02": "（等价于从零装机）",
    # verify_a3_migration.py:68 (CHECK)
    "CHECK_04": "001+002+003 全新库 apply 成功",
    # verify_a3_migration.py:78 (CHECK)
    "CHECK_05": "重跑 no-op",
    # verify_a3_migration.py:94 (CHECK)
    "CHECK_06": (
        "vector_sync_tasks / vector_drift_log / chunks / v_active_chunks "
        "都在"
    ),
    # verify_a3_migration.py:108 (CHECK)
    "CHECK_07": "doc_versions.deprecated_at 存在",
    # verify_a3_migration.py:120 (CHECK)
    "CHECK_08": "三个新索引都在",
    # verify_a3_migration.py:138 (REJECTS)
    "REJECTS_01": "desired_status 非法",
    # verify_a3_migration.py:145 (REJECTS)
    "REJECTS_02": "sync status 非法",
    # verify_a3_migration.py:152 (REJECTS)
    "REJECTS_03": "failed 无 error",
    # verify_a3_migration.py:159 (REJECTS)
    "REJECTS_04": "done 无 synced_at",
    # verify_a3_migration.py:166 (REJECTS)
    "REJECTS_05": "attempts 为负",
    # verify_a3_migration.py:173 (REJECTS)
    "REJECTS_06": "drift kind 非法",
    # verify_a3_migration.py:189 (CHECK)
    "CHECK_09": "合法行能插入",
    # verify_a3_migration.py:193 (REJECTS)
    "REJECTS_07": "未修复漂移重复",
    # verify_a3_migration.py:208 (CHECK)
    "CHECK_10": "修好之后可以再记一条",
    # verify_a3_migration.py:221 (CHECK)
    "CHECK_11": "删版本级联删同步任务",
    # verify_a3_migration.py:243 (REJECTS)
    "REJECTS_08": "第二个 active 版本（ux_active_version）",
    # verify_a3_migration.py:252 (REJECTS)
    "REJECTS_09": "active 版本无 activated_at",
    # verify_a3_migration.py:278 (PRINT)
    "PRINT_03": "\n=== 清理 database ",
}
