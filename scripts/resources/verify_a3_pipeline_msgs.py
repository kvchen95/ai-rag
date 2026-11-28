"""Chinese message catalog for scripts/verify_a3_pipeline.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # verify_a3_pipeline.py:262 (PRINT)
    "PRINT_01": "A3 端到端演练（真 CLI / 真 PG / 真 Qdrant / 真 TEI）",
    # verify_a3_pipeline.py:270 (PRINT)
    "PRINT_02": "\n[1] 造一个待上线版本 v1（3 条切片）",
    # verify_a3_pipeline.py:278 (SEED)
    "SEED_01": "住宿费标准：总监级每人每天 800 元，经理级 600 元。",
    # verify_a3_pipeline.py:282 (SEED)
    "SEED_02": "市内交通费凭票报销，单次不超过 100 元。",
    # verify_a3_pipeline.py:286 (SEED)
    "SEED_03": "出差需提前 3 个工作日在系统内提交申请。",
    # verify_a3_pipeline.py:294 (CHECK)
    "CHECK_01": "v1 有 3 条切片",
    # verify_a3_pipeline.py:297 (PRINT)
    "PRINT_03": "\n[2] promote v1：索引 -> 翻牌 -> 同步 -> 对账",
    # verify_a3_pipeline.py:299 (CHECK)
    "CHECK_02": "promote 成功",
    # verify_a3_pipeline.py:300 (CHECK)
    "CHECK_03": "写进 3 个点",
    # verify_a3_pipeline.py:302 (CHECK)
    "CHECK_04": "翻牌激活 3 条切片",
    # verify_a3_pipeline.py:307 (CHECK)
    "CHECK_05": "同步全部完成",
    # verify_a3_pipeline.py:312 (CHECK)
    "CHECK_06": "对账无残留",
    # verify_a3_pipeline.py:318 (PRINT)
    "PRINT_04": "\n[3] status：看现状",
    # verify_a3_pipeline.py:321 (CHECK)
    "CHECK_07": "版本 1 个 active",
    # verify_a3_pipeline.py:326 (CHECK)
    "CHECK_08": "切片 3 个 active",
    # verify_a3_pipeline.py:329 (CHECK)
    "CHECK_09": "无双 active",
    # verify_a3_pipeline.py:332 (CHECK)
    "CHECK_10": "无待同步",
    # verify_a3_pipeline.py:336 (CHECK)
    "CHECK_11": "无未修漂移",
    # verify_a3_pipeline.py:338 (CHECK)
    "CHECK_12": "集合里 3 个点",
    # verify_a3_pipeline.py:344 (PRINT)
    "PRINT_05": "\n[4] 造 v2（模拟文件字节变了）：v1 仍应继续服务",
    # verify_a3_pipeline.py:352 (SEED)
    "SEED_04": "住宿费标准：总监级每人每天 900 元，经理级 700 元。",
    # verify_a3_pipeline.py:356 (SEED)
    "SEED_05": "市内交通费凭票报销，单次不超过 120 元。",
    # verify_a3_pipeline.py:360 (SEED)
    "SEED_06": "出差需提前 2 个工作日在系统内提交申请。",
    # verify_a3_pipeline.py:366 (CHECK)
    "CHECK_13": "v1 的 3 个点仍存在",
    # verify_a3_pipeline.py:370 (CHECK)
    "CHECK_14": "v2 还没写点（未 promote）",
    # verify_a3_pipeline.py:375 (PRINT)
    "PRINT_06": "\n[5] promote v2：v1 应立刻下线，v2 生效",
    # verify_a3_pipeline.py:377 (CHECK)
    "CHECK_15": "promote v2 成功",
    # verify_a3_pipeline.py:379 (CHECK)
    "CHECK_16": "旧版是 v1",
    # verify_a3_pipeline.py:391 (CHECK)
    "CHECK_17": "v1 的点全部 deprecated",
    # verify_a3_pipeline.py:395 (CHECK)
    "CHECK_18": "v2 的点全部 active",
    # verify_a3_pipeline.py:399 (CHECK)
    "CHECK_19": "版本 1 active + 1 deprecated",
    # verify_a3_pipeline.py:404 (CHECK)
    "CHECK_20": "切片 3 active + 3 deprecated",
    # verify_a3_pipeline.py:409 (CHECK)
    "CHECK_21": "仍然无双 active",
    # verify_a3_pipeline.py:416 (PRINT)
    "PRINT_07": "\n[6] 手工把一条 payload 改错，验证对账能看见并修好",
    # verify_a3_pipeline.py:420 (CHECK)
    "CHECK_22": "dry-run 看见这处漂移",
    # verify_a3_pipeline.py:425 (CHECK)
    "CHECK_23": "dry-run 没修（仍是 1 处）",
    # verify_a3_pipeline.py:431 (CHECK)
    "CHECK_24": "对账修好了",
    # verify_a3_pipeline.py:433 (CHECK)
    "CHECK_25": "修好后再对账是干净的",
    # verify_a3_pipeline.py:438 (PRINT)
    "PRINT_08": "\n[7] 把 v1 的下线时间推到 25 小时前，跑回收",
    # verify_a3_pipeline.py:454 (CHECK)
    "CHECK_26": "回收了 1 个版本",
    # verify_a3_pipeline.py:455 (CHECK)
    "CHECK_27": "删了 3 条切片",
    # verify_a3_pipeline.py:456 (CHECK)
    "CHECK_28": "删了 1 张图",
    # verify_a3_pipeline.py:458 (CHECK)
    "CHECK_29": "图文件真的不在了",
    # verify_a3_pipeline.py:463 (CHECK)
    "CHECK_30": "v1 的点已从 Qdrant 删除",
    # verify_a3_pipeline.py:468 (CHECK)
    "CHECK_31": "v2 的点还在",
    # verify_a3_pipeline.py:473 (CHECK)
    "CHECK_32": "版本状态 = active + purged",
    # verify_a3_pipeline.py:479 (PRINT)
    "PRINT_09": "\n[8] 集合清空后从 PG 全量重建，逐条比对",
    # verify_a3_pipeline.py:497 (PRINT)
    "PRINT_10": "  集合已清空，准备重建",
    # verify_a3_pipeline.py:533 (CHECK)
    "CHECK_33": "重建脚本 rc=0",
    # verify_a3_pipeline.py:540 (CHECK)
    "CHECK_34": "重建了 3 个点（v2 的 3 条 active）",
    # verify_a3_pipeline.py:547 (CHECK)
    "CHECK_35": "重建后点数与重建前一致",
    # verify_a3_pipeline.py:566 (CHECK)
    "CHECK_36": "逐条 payload 一致",
    # verify_a3_pipeline.py:580 (CHECK)
    "CHECK_37": "每个点同时有 dense+bm25",
    # verify_a3_pipeline.py:588 (CHECK)
    "CHECK_38": "bm25 真的切出了多个 token（中文 bigram）",
    # verify_a3_pipeline.py:597 (CHECK)
    "CHECK_39": "重建后对账干净",
    # verify_a3_pipeline.py:603 (PRINT)
    "PRINT_11": "\n[cleanup] 清掉本次演练的行与点",
    # verify_a3_pipeline.py:609 (PRINT)
    "PRINT_12": " 条",
}
