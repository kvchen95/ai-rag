"""Chinese message catalog for scripts/b2_mutation_check.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # b2_mutation_check.py:60 (MUTATE)
    "MUTATE_01": "变异锚点未找到: ",
    # b2_mutation_check.py:66 (NAME)
    "NAME_01": "depts 交集改包含（&& → <@）",
    # b2_mutation_check.py:72 (NAME)
    "NAME_02": "Qdrant 去掉 date_confidence 条件",
    # b2_mutation_check.py:80 (NEW)
    "NEW_01": "            pass  # 变异：去掉只认显式时间的条件",
    # b2_mutation_check.py:83 (NAME)
    "NAME_03": "admin 仍带 public 分支",
    # b2_mutation_check.py:87 (NEW)
    "NEW_02": (
        "        if self.is_admin:\n"
        "            return (BRANCH_PUBLIC,)  # 变异：改成只放宽到 public"
    ),
    # b2_mutation_check.py:91 (NAME)
    "NAME_04": "去掉版本兜底（§7.4）",
    # b2_mutation_check.py:98 (NEW)
    "NEW_03": (
        "        if True:  # 变异：不做版本兜底\n"
        "            kept.append(item)"
    ),
    # b2_mutation_check.py:108 (PRINT)
    "PRINT_01": "基线不绿，变异测试无意义。先修基线。",
    # b2_mutation_check.py:123 (PRINT)
    "PRINT_02": "] STILL GREEN（判据无效）:: ",
    # b2_mutation_check.py:133 (PRINT)
    "PRINT_03": "还原后不绿：脚本改坏了文件，请检查 git diff。",
    # b2_mutation_check.py:136 (PRINT)
    "PRINT_04": "\n以下变异没有被发现（判据无效）:",
}
