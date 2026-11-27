"""图转译与数值对账（§3.4）。验收第二条："数值对不上下标失败不入库"。

对账是这个系统里唯一能拦住"VLM 把 47% 读成 74%"的关卡——校验阶段只看"数字
在不在证据里"，证据本身错了它看不出来。所以这里每一个检查都要有**能通过的
正例**和**必须被拦下的反例**：只测正例的话，检查被删掉不会有人发现。

六个检查，逐条覆盖：
1. `no_values`：转译里没有数值（等价于空转译）；
2. `value_outside_axis`：数值越过轴刻度范围（47 → 74 常常会越界）；
3. `percent_sum`：占比求和不在 99–101（读错一块，和就不拢）；
4. `value_not_in_figure`：数值在图里找不到依据（既不是印出来的数，也对不上
   柱高）；
5. `label_not_in_figure`：类别名编错了（"哪一档"是假的）；
6. 通过路径：数值落在刻度上 + 和正确 + 标签都在 → ok。
"""

from __future__ import annotations

import pytest

from api.parse.figures_transcribe import (
    PERCENT_SUM_MAX,
    PERCENT_SUM_MIN,
    CheckFailure,
    FigureEvidence,
    Transcript,
    extract_numbers,
    figure_content_text,
    parse_transcript,
    prompt_for,
    reconcile,
)
from api.parse.model import FigureData

# 夹具里的真实值：32 / 28 / 24 / 16，刻度 0..100，四根柱。
EVIDENCE = FigureEvidence(
    texts=(
        "100",
        "50",
        "32",
        "28",
        "24",
        "16",
        "0",
        "Transport Lodging Meals Other",
        "percent",
    ),
    numbers=(100.0, 50.0, 32.0, 28.0, 24.0, 16.0, 0.0),
    bar_ratios=(
        (0.32 / 1.15, 130.0),
        (0.28 / 1.15, 220.0),
        (0.24 / 1.15, 310.0),
        (0.16 / 1.15, 400.0),
    ),
    axis_height=320.0,
)


def _transcript(
    points: list[tuple[str, float | None]],
    *,
    kind: str = "bar",
    percent: bool = True,
) -> Transcript:
    unit = "%" if percent else "count"
    return Transcript(
        kind=kind,
        raw_markdown="| x | y |",
        axes={"y": {"label": "percent", "unit": unit, "min": 0, "max": 100}},
        series=(
            {
                "name": "share",
                "unit": unit,
                "points": [
                    {"label": label, "value": value} for label, value in points
                ],
            },
        ),
    )


GOOD = _transcript(
    [("Transport", 32), ("Lodging", 28), ("Meals", 24), ("Other", 16)]
)


# --- 1. 通过路径 -------------------------------------------------------------|


def test_good_transcript_passes() -> None:
    result = reconcile(GOOD, EVIDENCE)
    assert result.ok, result.reason()
    assert result.stats["percent_sum"] == 100.0


# --- 2. 数值越界 -------------------------------------------------------------


def test_value_beyond_the_axis_is_rejected() -> None:
    """刻度 0..150 的图上出现 400 —— 越界。

    注意 74 在 0..100 的图上**不越界**：它能被拦下是因为占比求和不拢（见
    下一条）。
    两个检查各管一类错，这里用真正越界的值单独验"越界"这一条。
    """
    wide = FigureEvidence(
        texts=("150", "100", "50", "0"), numbers=(150.0, 100.0, 50.0, 0.0)
    )
    transcript = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"label": "amount", "unit": "CNY", "min": 0, "max": 150}},
        series=(
            {
                "name": "x",
                "unit": "CNY",
                "points": [{"label": "150", "value": 400}],
            },
        ),
    )
    result = reconcile(transcript, wide)
    assert not result.ok
    assert any(
        failure.code == "value_outside_axis" for failure in result.failures
    )
    assert result.stats["outside_axis"] == [400.0]


def test_value_below_the_axis_is_rejected() -> None:
    """负值/低于下界同样越界。"""
    transcript = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"unit": "%", "min": 0, "max": 100}},
        series=(
            {
                "name": "x",
                "unit": "%",
                "points": [{"label": "Transport", "value": -20}],
            },
        ),
    )
    result = reconcile(transcript, EVIDENCE)
    assert any(f.code == "value_outside_axis" for f in result.failures)


def test_value_slightly_above_the_axis_is_tolerated() -> None:
    """刻度 0..100 而读成 100.4 —— 那是四舍五入，不是读错。

    给跨度 1% 的余量；比这更大的越界只能是读错。
    """
    near = _transcript(
        [("Transport", 100.4), ("Lodging", 0), ("Meals", 0), ("Other", 0)]
    )
    result = reconcile(near, EVIDENCE)
    assert not any(f.code == "value_outside_axis" for f in result.failures)


# --- 3. 占比求和 -------------------------------------------------------------


def test_percent_sum_outside_the_window_is_rejected() -> None:
    """把 32 读成 74：总和变成 142，饼图/百分比的"和不拢"立刻暴露。"""
    bad = _transcript(
        [("Transport", 74), ("Lodging", 28), ("Meals", 24), ("Other", 16)]
    )
    result = reconcile(bad, EVIDENCE)
    assert any(failure.code == "percent_sum" for failure in result.failures)
    assert result.stats["percent_sum"] == 142.0


def test_percent_window_lower_edge_is_rejected() -> None:
    low = _transcript(
        [("Transport", 30), ("Lodging", 28), ("Meals", 24), ("Other", 16)]
    )
    result = reconcile(low, EVIDENCE)
    assert result.stats["percent_sum"] == 98.0
    assert any(failure.code == "percent_sum" for failure in result.failures)


def test_percent_window_is_inclusive() -> None:
    """99 与 101 都在窗口内：读图允许 ±1 的取整误差。"""
    assert PERCENT_SUM_MIN == 99.0
    assert PERCENT_SUM_MAX == 101.0
    ok_low = _transcript(
        [("Transport", 32), ("Lodging", 28), ("Meals", 24), ("Other", 15)]
    )
    result = reconcile(ok_low, EVIDENCE)
    assert not any(f.code == "percent_sum" for f in result.failures)


def test_non_percent_charts_skip_the_sum_check() -> None:
    """金额型柱状图不该被"求和必须 99-101"约束。"""
    money = FigureEvidence(
        texts=("500", "380", "320"),
        numbers=(500.0, 380.0, 320.0),
    )
    transcript = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"label": "amount", "unit": "CNY", "min": 0, "max": 500}},
        series=(
            {
                "name": "cap",
                "unit": "CNY",
                "points": [
                    {"label": "500", "value": 500.0},
                    {"label": "380", "value": 380.0},
                    {"label": "320", "value": 320.0},
                ],
            },
        ),
    )
    result = reconcile(transcript, money)
    assert not any(f.code == "percent_sum" for f in result.failures)


# --- 4. 数值能否在图上找到 ---------------------------------------------------


def test_value_not_printed_and_not_a_bar_height_is_rejected() -> None:
    """`45` 既不是印出来的数、也对不上任何柱高 —— 它就是模型编的。"""
    fabricated = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"label": "percent", "unit": "%", "min": 0, "max": 100}},
        series=(
            {
                "name": "share",
                "unit": "%",
                "points": [
                    {"label": "Transport", "value": 45},
                    {"label": "Lodging", "value": 28},
                    {"label": "Meals", "value": 11},
                    {"label": "Other", "value": 16},
                ],
            },
        ),
    )
    result = reconcile(fabricated, EVIDENCE)
    assert any(
        failure.code == "value_not_in_figure" for failure in result.failures
    )
    assert 45.0 in result.stats["unmatched_values"]


def test_value_matching_a_bar_height_is_accepted() -> None:
    """图上没印数值的柱图：靠柱高比例对账（容差 3%）。"""
    bare = FigureEvidence(
        texts=("Transport", "Lodging"),
        numbers=(100.0, 0.0),
        bar_ratios=((0.60, 100.0), (0.40, 200.0)),
    )
    transcript = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"label": "percent", "unit": "%", "min": 0, "max": 100}},
        series=(
            {
                "name": "share",
                "unit": "%",
                "points": [
                    {"label": "Transport", "value": 60},
                    {"label": "Lodging", "value": 40},
                ],
            },
        ),
    )
    result = reconcile(transcript, bare)
    assert not any(f.code == "value_not_in_figure" for f in result.failures)


def test_bar_height_drift_beyond_tolerance_is_rejected() -> None:
    """柱高 60% 却被读成 72% —— 超出 3% 容差就是读错。"""
    bare = FigureEvidence(
        texts=("Transport", "Lodging"),
        numbers=(100.0, 0.0),
        bar_ratios=((0.60, 100.0), (0.28, 200.0)),
    )
    transcript = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"label": "percent", "unit": "%", "min": 0, "max": 100}},
        series=(
            {
                "name": "share",
                "unit": "%",
                "points": [
                    {"label": "Transport", "value": 72},
                    {"label": "Lodging", "value": 28},
                ],
            },
        ),
    )
    result = reconcile(transcript, bare)
    assert any(f.code == "value_not_in_figure" for f in result.failures)


# --- 5. 标签 -----------------------------------------------------------------


def test_invented_label_is_rejected() -> None:
    """类别名编错 → 引用的"哪一档"是假的。"""
    bad = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"label": "percent", "unit": "%", "min": 0, "max": 100}},
        series=(
            {
                "name": "share",
                "unit": "%",
                "points": [
                    {"label": "Flights", "value": 32},
                    {"label": "Lodging", "value": 28},
                    {"label": "Meals", "value": 24},
                    {"label": "Other", "value": 16},
                ],
            },
        ),
    )
    result = reconcile(bad, EVIDENCE)
    assert any(
        failure.code == "label_not_in_figure" for failure in result.failures
    )
    assert "Flights" in result.stats["unmatched_labels"]


def test_split_labels_still_match() -> None:
    """中文/英文标签常被拆成多行（`Monthly` / `limit`），互相包含也算匹配。"""
    evidence = FigureEvidence(
        texts=("Monthly", "limit", "2000"), numbers=(2000.0,)
    )
    transcript = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={},
        series=(
            {
                "name": "x",
                "unit": "",
                "points": [{"label": "Monthly limit", "value": 2000}],
            },
        ),
    )
    result = reconcile(transcript, evidence)
    assert not any(f.code == "label_not_in_figure" for f in result.failures)


# --- 6. 空转译 ---------------------------------------------------------------


def test_transcript_without_values_is_rejected() -> None:
    """没有数值的转译没有可校验内容，等价于空转译 → 不入库。"""
    empty = Transcript(kind="bar", raw_markdown="只有文字", axes={}, series=())
    result = reconcile(empty, EVIDENCE)
    assert not result.ok
    assert result.failures[0].code == "no_values"


def test_null_values_are_skipped_not_counted() -> None:
    """模型按提示词写了 `null`（"无法确定"），那些点不参与对账。"""
    partial = Transcript(
        kind="bar",
        raw_markdown="x",
        axes={"y": {"label": "percent", "unit": "%", "min": 0, "max": 100}},
        series=(
            {
                "name": "share",
                "unit": "%",
                "points": [
                    {"label": "Transport", "value": 32},
                    {"label": "Lodging", "value": None},
                    {"label": "Meals", "value": 24},
                    {"label": "Other", "value": 16},
                ],
            },
        ),
    )
    result = reconcile(partial, EVIDENCE)
    assert partial.values() == [32.0, 24.0, 16.0]
    # 缺一个数会让占比求和不拢 —— 这正是它该被拦下的原因。
    assert any(f.code == "percent_sum" for f in result.failures)


# --- 7. 解析 VLM 输出 --------------------------------------------------------


def test_parse_transcript_splits_markdown_and_json() -> None:
    raw = """| 类别 | 百分比 |
| --- | --- |
| Transport | 32 |

JSON: {"kind":"bar","axes":{"y":{"min":0,"max":100}},
"series":[{"points":[{"label":"Transport","value":32}]}]}"""
    transcript = parse_transcript(raw)
    assert transcript.kind == "bar"
    assert "| Transport | 32 |" in transcript.raw_markdown
    assert transcript.values() == [32.0]


def test_parse_transcript_strips_code_fences() -> None:
    raw = """```markdown
| a | b |

```json
{"kind":"bar","series":[{"points":[{"label":"a","value":1}]}]}
```"""
    transcript = parse_transcript(raw)
    assert transcript.values() == [1.0]
    assert "```" not in transcript.raw_markdown


def test_parse_transcript_without_json_raises() -> None:
    """只有 Markdown 没有结构化输出 → 对账无从下手 → 必须判失败。

    这是有意的：没有结构化输出就无法证明模型没读错。
    """
    with pytest.raises(ValueError, match="no JSON block"):
        parse_transcript("| a | b |\n| --- | --- |\n| 1 | 2 |")


def test_parse_transcript_tolerates_missing_axes() -> None:
    transcript = parse_transcript(
        'text\nJSON: {"kind":"structure","series":[]}'
    )
    assert transcript.axes == {}
    assert transcript.series == ()


# --- 8. 提示词 ---------------------------------------------------------------


def test_prompts_forbid_estimation() -> None:
    """两条提示词都必须写"无法确定写 null，不要估算"。

    这是对账能起作用的前提：模型不猜，我们才敢拿对账结果当"可以入库"。
    """
    for kind in ("bar", "structure"):
        prompt = prompt_for(kind)
        assert "null" in prompt
        assert "不要估算" in prompt


def test_structure_prompt_asks_for_nodes_and_edges() -> None:
    prompt = prompt_for("structure")
    assert "节点" in prompt and "连线" in prompt


def test_prompts_ask_for_the_structured_json_block() -> None:
    for kind in ("bar", "structure"):
        assert "JSON:" in prompt_for(kind)


# --- 9. content_text ---------------------------------------------------------


def test_figure_content_text_puts_caption_first() -> None:
    r"""图注放最前：\"这张图讲什么\"通常是问句最可能命中的一句。"""
    figure = FigureData(
        image_path="figures/ch_x.webp",
        width_px=800,
        height_px=600,
        dpi=220.0,
        raw_markdown="| Transport | 32 |",
    )
    text = figure_content_text(
        figure, caption="Figure 1. Share of travel spend"
    )
    assert text.splitlines()[0] == "Figure 1. Share of travel spend"
    assert "| Transport | 32 |" in text


def test_figure_content_text_omits_structured_fields() -> None:
    """结构化字段不进 content_text：`{"axes": {...}}` 进检索文本是纯噪声。"""
    figure = FigureData(
        image_path="x",
        width_px=1,
        height_px=1,
        dpi=200.0,
        raw_markdown="table",
        axes={"y": {"min": 0}},
    )
    assert "axes" not in figure_content_text(figure)


# --- 10. 工具函数 ------------------------------------------------------------


def test_extract_numbers_handles_mixed_text() -> None:
    assert extract_numbers(["每人每天 500 元", "80%"]) == (500.0, 80.0)


def test_extract_numbers_does_not_turn_date_hyphens_into_negatives() -> None:
    """`2025-03-01` 是三个正数，不是 `2025, -3, -1`。

    第一版的正则吃了连字符当负号，日期因此在对账依据里变成负数——而负数会让
    "数值落在轴范围内"这个检查以最莫名其妙的方式失败。
    """
    assert extract_numbers(["生效日期 2025-03-01"]) == (2025.0, 3.0, 1.0)


def test_extract_numbers_keeps_real_negatives() -> None:
    """真负数要保留：`-12.5%` 前面不是数字，所以连字符是负号。"""
    assert extract_numbers(["同比 -12.5%"]) == (-12.5,)


def test_check_failure_str_is_machine_readable() -> None:
    failure = CheckFailure("percent_sum", "sum to 142")
    assert str(failure) == "percent_sum: sum to 142"
