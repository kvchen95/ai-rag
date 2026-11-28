"""Chinese message catalog for scripts/b3_mutation_check.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # b3_mutation_check.py:67 (MUTATE)
    "MUTATE_01": "变异锚点未找到: ",
    # b3_mutation_check.py:73 (NAME)
    "NAME_01": "BM25 路不再带 ACL（两路 filter 分裂）",
    # b3_mutation_check.py:90 (NAME)
    "NAME_02": "去掉版本兜底（§7.4）",
    # b3_mutation_check.py:97 (NEW)
    "NEW_01": (
        "        if True:  # 变异：不做版本兜底\n"
        "            kept.append(item)"
    ),
    # b3_mutation_check.py:101 (NAME)
    "NAME_03": "RRF 常数改成 60（与 Qdrant 原生不再等价）",
    # b3_mutation_check.py:109 (NAME)
    "NAME_04": "精排降级时伪造 0 分",
    # b3_mutation_check.py:111 (OLD)
    "OLD_01": (
        "    else:\n"
        "        # 一条都没打上分：整体按融合名次（§7.3\"退回按名次取前 "
        "8\"）。\n"
        "        merged = list(ordered)"
    ),
    # b3_mutation_check.py:114 (NEW)
    "NEW_02": (
        "    else:\n"
        "        # 变异：给没打分的候选填 0 分（伪造一个\"无相关\"的结论）\n"
        "        merged = [item.with_rerank(0.0) for item in ordered]"
    ),
    # b3_mutation_check.py:119 (NAME)
    "NAME_05": "临界带改成补充而不是替代（0.39 仍按 <0.40 拒）",
    # b3_mutation_check.py:122 (NEW)
    "NEW_03": "    if top1 < cfg.reject_low:  # 变异：临界带没被当成替代",
    # b3_mutation_check.py:125 (NAME)
    "NAME_06": "多跳不看本跳阈值（只看已有事实）",
    # b3_mutation_check.py:129 (NEW)
    "NEW_04": (
        "        if False:  # 变异：不看本跳是否过阈\n"
        "            stop = \"broken\""
    ),
    # b3_mutation_check.py:133 (NAME)
    "NAME_07": "改写允许级联（去掉幂等守卫）",
    # b3_mutation_check.py:135 (OLD)
    "OLD_02": (
        "            if _inside_target(text, pos, key, targets):\n"
        "                # "
        "命中的原文已经处在某个正式说法内部，替换只会制造重复片段。\n"
        "                break"
    ),
    # b3_mutation_check.py:138 (NEW)
    "NEW_05": "            pass  # 变异：去掉幂等守卫",
    # b3_mutation_check.py:147 (PRINT)
    "PRINT_01": "基线不绿，变异测试无意义。先修基线。",
    # b3_mutation_check.py:162 (PRINT)
    "PRINT_02": "] STILL GREEN（判据无效）:: ",
    # b3_mutation_check.py:172 (PRINT)
    "PRINT_03": "还原后仍然不绿——变异脚本没把文件改回去，先查这个。",
    # b3_mutation_check.py:175 (PRINT)
    "PRINT_04": "\n以下变异没有被检出（对应判据形同虚设）: ",
}
