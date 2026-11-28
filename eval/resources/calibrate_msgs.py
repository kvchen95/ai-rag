"""Chinese message catalog for eval/calibrate.py.

Keys are generated; each entry notes its source line in the script.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # calibrate.py:53 (FIXTURE_SOURCE)
    "FIXTURE_SOURCE_01": "详细设计.md",
    # calibrate.py:101 (SOURCE)
    "SOURCE_01": "run_eval 报告 ",
    # calibrate.py:147 (SOURCE)
    "SOURCE_02": "qa_log 最近 ",
    # calibrate.py:147 (SOURCE)
    "SOURCE_03": " 分钟",
    # calibrate.py:277 (REASON)
    "REASON_01": "应答题或拒答题没有有效 top1，无法标定",
    # calibrate.py:310 (TITLE)
    "TITLE_01": "前言",
    # calibrate.py:382 (RUN_FIXTURE)
    "RUN_FIXTURE_01": "夹具演练需要 DATABASE_URL 或 --dsn",
    # calibrate.py:406 (SOURCE)
    "SOURCE_04": "夹具演练（",
    # calibrate.py:406 (SOURCE)
    "SOURCE_05": " 切片入库，",
    # calibrate.py:407 (SOURCE)
    "SOURCE_06": "生产同一条写入/检索路径）",
    # calibrate.py:423 (RUN_FIXTURE)
    "RUN_FIXTURE_02": " 已存在（上次演练没清干净？）。",
    # calibrate.py:424 (RUN_FIXTURE)
    "RUN_FIXTURE_03": "先清场再跑，脚本拒绝在脏基线上继续。",
    # calibrate.py:465 (RUN_FIXTURE)
    "RUN_FIXTURE_04": "索引失败：",
    # calibrate.py:520 (RUN_FIXTURE)
    "RUN_FIXTURE_05": "[fixture][WARN] 删 Qdrant 点失败：",
    # calibrate.py:555 (RUN_FIXTURE)
    "RUN_FIXTURE_06": "[fixture][WARN] PG 清场失败：",
    # calibrate.py:567 (RUN_FIXTURE)
    "RUN_FIXTURE_07": "[fixture] 清场：",
    # calibrate.py:629 (LINES)
    "LINES_01": "# 门控阈值标定报告",
    # calibrate.py:631 (LINES)
    "LINES_02": "- 生成时间：",
    # calibrate.py:633 (LINES)
    "LINES_03": "- 数据来源：",
    # calibrate.py:634 (LINES)
    "LINES_04": "- 样本：应答题 ",
    # calibrate.py:634 (LINES)
    "LINES_05": "，拒答题 ",
    # calibrate.py:635 (LINES)
    "LINES_06": "澄清题 ",
    # calibrate.py:637 (LINES)
    "LINES_07": "## 1. 一句话结论",
    # calibrate.py:641 (RENDER_REPORT)
    "RENDER_REPORT_01": "**不可标定**：",
    # calibrate.py:644 (RENDER_REPORT)
    "RENDER_REPORT_02": "在本样本上，Youden 最优点 **",
    # calibrate.py:646 (RENDER_REPORT)
    "RENDER_REPORT_03": "观测波谷 ",
    # calibrate.py:650 (RENDER_REPORT)
    "RENDER_REPORT_04": (
        "**不存在**（两组分布重叠，任何阈值都会同时误伤应答题与放行拒答题）。"
    ),
    # calibrate.py:659 (RENDER_REPORT)
    "RENDER_REPORT_05": (
        "该区间**包含**设计先验 0.38–0.42，可作为本次候选区间。"
    ),
    # calibrate.py:661 (RENDER_REPORT)
    "RENDER_REPORT_06": (
        "该区间与设计先验 0.38–0.42 **不重合**——先别改配置，先看限制一节："
        "语料/样本与生产不一致时，先验仍是默认值。"
    ),
    # calibrate.py:667 (LINES)
    "LINES_08": "## 2. 口径",
    # calibrate.py:669 (LINES)
    "LINES_09": (
        "- `top1` = 门控看到的精排最高分（`RetrievalSnapshot.decision.top1`，"
        "即 qa_log 的 `score_snapshot->>'top1'`）。没有候选时记 `None`，"
        "统计时按 0.0 计入并单独报 `n_none`。"
    ),
    # calibrate.py:673 (LINES)
    "LINES_10": (
        "- 应答题 = `expected_verdict` 为 `pass`/`partial`；拒答题 = "
        "`reject_none`/`reject_low`；`clarify` 不参与二分类，单独列。"
    ),
    # calibrate.py:676 (LINES)
    "LINES_11": (
        "- 阈值语义：`top1 < t` 预测为 `reject_none`。扫描区间 0.20–0.60，步长 "
        "0.01（覆盖两档门控带）。"
    ),
    # calibrate.py:679 (LINES)
    "LINES_12": "## 3. top1 分布",
    # calibrate.py:681 (LINES)
    "LINES_13": (
        "| 组 | n | 有分数 | None | min | p10 | 中位 | p90 | max | 均值 |"
    ),
    # calibrate.py:685 (RENDER_REPORT)
    "RENDER_REPORT_07": "应答题",
    # calibrate.py:686 (RENDER_REPORT)
    "RENDER_REPORT_08": "拒答题",
    # calibrate.py:687 (RENDER_REPORT)
    "RENDER_REPORT_09": "澄清题",
    # calibrate.py:702 (LINES)
    "LINES_14": "### 直方图（0.05 一档）",
    # calibrate.py:710 (RENDER_REPORT)
    "RENDER_REPORT_10": "  应 ",
    # calibrate.py:711 (RENDER_REPORT)
    "RENDER_REPORT_11": "  拒 ",
    # calibrate.py:715 (RENDER_REPORT)
    "RENDER_REPORT_12": (
        "`#` 应答题，`+` 拒答题；观察两峰之间是否有计数接近 0 的连续区间。"
    ),
    # calibrate.py:720 (LINES)
    "LINES_15": "## 4. 阈值扫描与波谷",
    # calibrate.py:739 (LINES)
    "LINES_16": "## 5. 建议",
    # calibrate.py:744 (LINES)
    "LINES_17": "1. 候选阈值：**",
    # calibrate.py:745 (LINES)
    "LINES_18": "（Youden 最优；TPR ",
    # calibrate.py:747 (LINES)
    "LINES_19": (
        "2. 若观测波谷是一个区间且与 0.38–0.42 有交集，取交集；否则**保留先验 "
        "0.38–0.42**，先把语料与样本补齐再标定。"
    ),
    # calibrate.py:750 (LINES)
    "LINES_20": (
        "3. 每次调阈值都必须重跑 `eval/run_eval.py` 四条红线："
        "阈值下降换来的覆盖率，不得以忠实度/数值准确率下降为代价（§12.2 "
        "第四条）。"
    ),
    # calibrate.py:753 (LINES)
    "LINES_21": (
        "4. 阈值、提示词、模型名三者同版本留档；回滚以版本为单位，"
        "不做局部回滚（§12.3）。"
    ),
    # calibrate.py:758 (RENDER_REPORT)
    "RENDER_REPORT_13": (
        "1. 先补齐能产生 top1 的数据：语料 active chunks > 0、生成模型可用，"
        "再重跑本脚本。"
    ),
    # calibrate.py:764 (LINES)
    "LINES_22": "## 6. 限制与风险",
    # calibrate.py:766 (LINES)
    "LINES_23": (
        "- **样本量**：二分类的 TPR/FPR 都是点估计；n<30 时一个样本就能移动 "
        "3–7 个百分点。本报告不附置信区间，因此只用于\"区间候选\"，"
        "不用于精确验收。"
    ),
    # calibrate.py:769 (LINES)
    "LINES_24": (
        "- **语料代表性**：夹具演练只有一篇设计文档，且问题集是为生产语料写的；"
        "分数分布会随语料长度、切片粒度、精排模型变化。"
        "**夹具结论不能直接改为生产配置**。"
    ),
    # calibrate.py:771 (LINES)
    "LINES_25": (
        "- **top1=None**：没有候选的题按 0.0 计入。若 `n_none` 占比高，"
        "说明问题不是阈值，而是召回/入库/权限过滤——先查那三处。"
    ),
    # calibrate.py:774 (LINES)
    "LINES_26": (
        "- **漂移**：§12.2 明说波谷随语料漂移；每次语料大版本更新后重新跑，"
        "不沿用旧阈值。"
    ),
    # calibrate.py:804 (PARSER)
    "PARSER_01": "门控阈值标定（top1 分布 + 波谷）",
    # calibrate.py:806 (MAIN)
    "MAIN_01": "run_eval 产出的 JSON",
    # calibrate.py:810 (MAIN)
    "MAIN_02": "直接查 qa_log 时用",
    # calibrate.py:816 (MAIN)
    "MAIN_03": "夹具演练：设计文档切片入库→检索→清场",
    # calibrate.py:822 (MAIN)
    "MAIN_04": "合成分布自检（验证波谷算法，不是数据）",
    # calibrate.py:831 (SOURCE)
    "SOURCE_07": "**合成分布自检**（不是语料数据；仅验证算法）",
    # calibrate.py:839 (MAIN)
    "MAIN_05": "[FATAL] 夹具演练失败：",
    # calibrate.py:849 (MAIN)
    "MAIN_06": "需要 --report / --dsn / --fixture / --selftest 之一",
    # calibrate.py:861 (MAIN)
    "MAIN_07": "[calibration] 应答题 ",
    # calibrate.py:862 (MAIN)
    "MAIN_08": "拒答题 ",
    # calibrate.py:868 (MAIN)
    "MAIN_09": "波谷=",
    # calibrate.py:872 (MAIN)
    "MAIN_10": "[calibration] 不可标定：",
    # calibrate.py:880 (MAIN)
    "MAIN_11": "[selftest] PASS：合成的波谷（真值约 0.42）被恢复在 t=",
    # calibrate.py:884 (MAIN)
    "MAIN_12": "[selftest] FAIL：合成波谷应在 0.34–0.45，实际 ",
}
