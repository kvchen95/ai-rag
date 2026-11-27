"""图表转译与数值对账（详细设计.md §3.4 / 附录 D.2-D.3）。.

三步：裁图（`figures.py`）→ 交 VLM 转译 → **对账**。对账是硬要求，不是优化：

> 模型读图最常见的错是把 47% 读成 74%，或者把两条曲线搞反；没有对账，这种错会
> 一路带到答案里，而校验阶段只看"数字在不在证据里"，证据本身错了就拦不住。

对账做四件事，任何一条不过就判**这次转译失败、这条图不入库**：

| 检查 | 判据 | 为什么 |
|---|---|---|
| 数值落在刻度范围内 | 每个 series 值在轴范围内 | 47 读成 74 常越上限 |
| 饼图求和 | 类别是"占比/percent"时，求和 ∈ [99, 101] | 和不拢说明读错了某一块 |
| 数值能在图里找到 | 每个数在图区文字或柱高比例里对上 | 模型凭空补数 |
| 标签对得上 | 类别名须在图区文字里出现过 | 编错类别 → 引用是假的 |

"柱高比例"这一条是针对矢量柱状图的：柱子的高度 / 轴高 ≈ 数值 / 轴跨度，
实测误差 < 2%。它让"图上没印数值的柱图"也能对账——这类图恰恰是最容易被读错的。

### 没有 VLM 时怎么办

`VLMClient` 是协议（`transcribe(image_path, prompt) -> str`）。没配 `VLM_*` 时
`build_vlm_client` 返回 `None`，解析器把图判成 **failed**（不入库、进
`DocLayout.warnings`），而不是"没转译就当空文本入库"——空转译的图切片会让检索
命中一条没有任何内容的证据，比缺一条更糟。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from api.parse.geometry import Rect
from api.parse.model import FigureData

# 饼图/占比求和的容差（§3.4：99 到 101）。
PERCENT_SUM_MIN = 99.0
PERCENT_SUM_MAX = 101.0
# 柱高比例重建的容差：实测矢量柱误差 < 2%，给 3% 留渲染与取整余量。
BAR_HEIGHT_TOLERANCE = 0.03
# 数值与标签"能在图里找到"的容差（相对误差）：
# 转译会把 500 写成 500.0，也会四舍五入。
NUMBER_MATCH_TOLERANCE = 0.01
# 提示词版本：改动必须递增，审计里记它（否则"这条转译是哪版提示词出的"说不清）。
TRANSCRIBE_PROMPT_VERSION = "figure-transcribe-v1"

STAT_TRANSCRIBE_PROMPT = (
    "读取图中坐标轴名称、单位、刻度，以及每个数据点的类别与数值。\n"
    "多条曲线逐条列出并注明图例。\n"
    "无法确定的数值写 null，不要估算。\n"
    "输出 Markdown 表格，不解释。"
)

STRUCTURE_TRANSCRIBE_PROMPT = (
    "列出图中节点（名称、类型：系统/模块/外部服务）。\n"
    "列出连线（起点、终点、方向、连线文字）。\n"
    '看不清的节点写"无法识别"，不要补全。\n'
    "输出两段：节点表、关系表。"
)

# 让模型在 Markdown 之外再给一段结构化 JSON：Markdown 给人看/进 content_text，
# JSON 给对账用。硬要求"只输出 JSON"会让 content_text 失去可读性；
# 只输出 Markdown 又没法可靠对账（要再解析一次表格）。
_TRANSCRIBE_SCHEMA = (
    '{"kind":"bar|line|pie|structure|table|photo",'
    '"axes":{"x":{"label":..,"unit":..,"min":..,"max":..},'
    '"y":{...}},"series":[{"name":..,"unit":..,"points":'
    '[{"label":..,"value":..}]}],"legend_missing":false}'
)

TRANSCRIBE_PROMPT_SUFFIX = (
    "\n先输出 Markdown 表格，然后输出一行以 `JSON:` 开头的结构化结果：\n"
    + _TRANSCRIBE_SCHEMA
    + "\n数值必须来自图上的刻度与标签；"
    "无法确定写 null，不要估算，不要补全。"
)


class VLMClient(Protocol):
    """读图模型的唯一入口。.

    `image` 是**无损 WebP 的字节**（不是路径）：客户端可能跑在另一个进程/
    容器里，传路径就得让两边共享文件系统；传字节则只需一次请求。`prompt`
    由本模块给出，客户端只管发送与返回文本。
    """

    def transcribe(self, image: bytes, prompt: str) -> str:
        """把图片字节与提示词交给模型，返回模型输出的文本。."""


class VLMUnavailableError(RuntimeError):
    """没配 VLM。不是错误配置，是"这个部署没开图转译"。."""


# 旧名保留给既有调用方（CLI、脚本、测试仍按这个名字导入）。
VLMUnavailable = VLMUnavailableError


@dataclass(frozen=True, slots=True)
class CheckFailure:
    """一条对账失败。`code` 机器可读（进日志与 warnings），`detail` 给人看。."""

    code: str
    detail: str

    def __str__(self) -> str:
        """Return a human-readable representation."""
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """ReconciliationResult."""
    ok: bool
    failures: tuple[CheckFailure, ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)

    def reason(self) -> str:
        """Run reason."""
        return "; ".join(str(failure) for failure in self.failures)


@dataclass(frozen=True, slots=True)
class Transcript:
    """VLM 输出的两半：`raw_markdown`（进 content_text）与结构化字段。."""

    kind: str
    raw_markdown: str
    axes: dict[str, Any]
    series: tuple[dict[str, Any], ...]
    legend_missing: bool = False

    def values(self) -> list[float]:
        """Run values."""
        out: list[float] = []
        for curve in self.series:
            for point in curve.get("points") or ():
                value = point.get("value")
                if isinstance(value, int | float):
                    out.append(float(value))
        return out

    def labels(self) -> list[str]:
        """Run labels."""
        out: list[str] = []
        for curve in self.series:
            for point in curve.get("points") or ():
                label = point.get("label")
                if isinstance(label, str) and label.strip():
                    out.append(label.strip())
        return out


# --- 提示词 ----------------------------------------------------------------


def prompt_for(kind: str) -> str:
    """按图的初判选提示词。.

    `vector`/`image` 这种来源标签不是"图的类型"，所以这里按 `kind`（初判给
    `vector` 的图先按统计图试一次；结构图由 `kind=structure` 触发）。两条提示词
    都要求"无法确定写 null"，这是对账能起作用的前提：模型不猜，我们才敢拿
    对账结果当"可以入库"。
    """
    if kind in {"structure", "flow", "diagram"}:
        return STRUCTURE_TRANSCRIBE_PROMPT + TRANSCRIBE_PROMPT_SUFFIX
    return STAT_TRANSCRIBE_PROMPT + TRANSCRIBE_PROMPT_SUFFIX


def parse_transcript(text: str) -> Transcript:
    """把 VLM 的返回拆成 Markdown + 结构化字段。.

    容忍三种常见偏差（都实测见过）：
      * 整段被 ``` 代码块包住；
      * `JSON:` 换成 `json:` / ```` ```json ````；
      * 只有 Markdown 没有 JSON —— 这时结构化字段为空，对账会因为它"没有数值"
        而失败（**不入库**），这是有意的：没有结构化输出就无法对账。
    """
    cleaned = _strip_code_fences(text).strip()
    markdown, payload = _split_json(cleaned)
    if payload is None:
        raise ValueError("transcript carries no JSON block; cannot reconcile")
    kind = str(payload.get("kind") or "unknown")
    raw_axes = payload.get("axes")
    axes: dict[str, Any] = dict(raw_axes) if isinstance(raw_axes, dict) else {}
    raw_series = payload.get("series")
    series: tuple[dict[str, Any], ...] = ()
    if isinstance(raw_series, list):
        series = tuple(
            dict(item) for item in raw_series if isinstance(item, dict)
        )
    return Transcript(
        kind=kind,
        raw_markdown=markdown,
        axes=axes,
        series=series,
        legend_missing=bool(payload.get("legend_missing", False)),
    )


def _strip_code_fences(text: str) -> str:
    """Run _strip_code_fences."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


def _split_json(text: str) -> tuple[str, dict[str, Any] | None]:
    """从 `JSON:`（或 ```json 块）之后取结构化结果。."""
    match = re.search(r"(?im)^\s*(?:json\s*:?|```json)\s*$|\bJSON\s*:", text)
    if match is None:
        return text, None
    head = text[: match.start()].strip()
    tail = text[match.end() :].strip()
    tail = _strip_code_fences(tail)
    depth = 0
    start = tail.find("{")
    if start < 0:
        return head, None
    for index in range(start, len(tail)):
        if tail[index] == "{":
            depth += 1
        elif tail[index] == "}":
            depth -= 1
            if depth == 0:
                candidate = tail[start : index + 1]
                try:
                    payload = json.loads(candidate)
                except json.JSONDecodeError:
                    return head, None
                return head, payload if isinstance(payload, dict) else None
    return head, None


# --- 对账 -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FigureEvidence:
    """对账需要的"图里到底有什么"。.

    `texts`：图区域内的所有文字（刻度、类别名、图注）；
    `numbers`：从中提取出的数值；
    `bar_ratios`：矢量柱的（高度 / 轴高, x 中心）；`axis_height` 是轴的
    物理高度。这三样都来自**原始 PDF**，不是模型说的——这正是对账能独立于
    模型的原因。
    """

    texts: tuple[str, ...] = ()
    numbers: tuple[float, ...] = ()
    bar_ratios: tuple[tuple[float, float], ...] = ()
    axis_height: float = 0.0
    area_rect: Rect | None = None


# 数字：**不吃连字符当负号**，除非它前面不是数字。`2025-03-01` 必须抽成
# (2025, 03, 01)，而不是 (2025, -3, -1) —— 后者会把日期变成负数混进对账依据
# （实测：本模块第一版就是这样，日期里的连字符被当成负号）。
_NUM = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?")


def extract_numbers(texts: Iterable[str]) -> tuple[float, ...]:
    """Run extract_numbers."""
    out: list[float] = []
    for text in texts:
        for match in _NUM.finditer(text):
            try:
                out.append(float(match.group(0)))
            except ValueError:  # pragma: no cover - 正则已保证可解析
                continue
    return tuple(out)


def reconcile(
    transcript: Transcript,
    evidence: FigureEvidence,
    *,
    percentile_kinds: Sequence[str] = ("pie", "percent", "share"),
) -> ReconciliationResult:
    """对账。任何一条失败都返回 ok=False，调用方据此判"不入库"。.

    四条检查顺序固定（从便宜到贵，也是从"一定错"到"可能错"）：
      1. 有数值吗（没有数值的转译没有可校验内容，等价于空转译）；
      2. 每个数值落在轴的刻度范围内；
      3. 占比型求和落在 [99, 101]；
      4. 每个数值与标签都能在图的文字/柱高里对上。
    """
    failures: list[CheckFailure] = []
    values = transcript.values()
    labels = transcript.labels()
    stats: dict[str, Any] = {
        "values": len(values),
        "labels": len(labels),
        "kind": transcript.kind,
    }

    if not values:
        failures.append(
            CheckFailure(
                "no_values",
                "transcript has no numeric value; nothing to reconcile",
            )
        )
        return ReconciliationResult(
            ok=False, failures=tuple(failures), stats=stats
        )

    axis = _axis_range(transcript.axes, evidence)
    stats["axis"] = axis
    if axis is not None:
        low, high = axis
        # 轴范围要能让刻度文字自洽：模型给的 min/max 与图上的刻度对不上时，
        # 用证据里的刻度替代——模型的 axes 字段是最容易抄错的一项。
        outside = [
            value
            for value in values
            if value < low - _range_slack(low, high)
            or value > high + _range_slack(low, high)
        ]
        stats["outside_axis"] = outside
        if outside:
            failures.append(
                CheckFailure(
                    "value_outside_axis",
                    f"{outside} outside axis range [{low:g}, {high:g}]",
                )
            )

    if _is_percentile(transcript, percentile_kinds):
        total = sum(values)
        stats["percent_sum"] = round(total, 2)
        if not (PERCENT_SUM_MIN <= total <= PERCENT_SUM_MAX):
            failures.append(
                CheckFailure(
                    "percent_sum",
                    f"percent values sum to {total:.2f}, expected "
                    f"{PERCENT_SUM_MIN:g}-{PERCENT_SUM_MAX:g}",
                )
            )

    unmatched = [
        value for value in values if not _value_supported(value, evidence, axis)
    ]
    stats["unmatched_values"] = unmatched
    if unmatched:
        failures.append(
            CheckFailure(
                "value_not_in_figure",
                f"{unmatched} not found in figure text or bar heights",
            )
        )

    missing_labels = [
        label for label in labels if not _label_supported(label, evidence)
    ]
    stats["unmatched_labels"] = missing_labels
    if missing_labels:
        failures.append(
            CheckFailure(
                "label_not_in_figure",
                f"{missing_labels} not found in figure text",
            )
        )

    return ReconciliationResult(
        ok=not failures, failures=tuple(failures), stats=stats
    )


def _range_slack(low: float, high: float) -> float:
    """轴范围的容差：刻度是 0..100，值 101 是"读高了一点"还是"读错了"？.

    给跨度 1% 的余量。比这更大的越界只能是读错。
    """
    return max(0.5, abs(high - low) * 0.01)


def _axis_range(
    axes: dict[str, Any], evidence: FigureEvidence
) -> tuple[float, float] | None:
    """轴范围：优先用模型给的 y 轴 min/max，缺一项就用证据里的刻度补。.

    只有刻度文字不足以定范围时返回 None（比如结构图根本没有轴），这时跳过
    "落在范围内"这条检查，但第 4 条（能对上）仍然生效。
    """
    y_axis = axes.get("y")
    y_axis_dict: dict[str, Any] = y_axis if isinstance(y_axis, dict) else {}
    low = _as_float(y_axis_dict.get("min"))
    high = _as_float(y_axis_dict.get("max"))
    # 从证据里的数值推：刻度的最小/最大常常就是轴范围（0..100、0..5000）。
    if (low is None or high is None) and evidence.numbers:
        low = min(evidence.numbers) if low is None else low
        high = max(evidence.numbers) if high is None else high
    if low is None or high is None or high <= low:
        return None
    return (low, high)


def _as_float(value: Any) -> float | None:
    """Run _as_float."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().rstrip("%％"))
        except ValueError:
            return None
    return None


def _is_percentile(transcript: Transcript, kinds: Sequence[str]) -> bool:
    """占不占比，看 kind 与单位两处。只看 kind 会漏掉 `unit="%"` 的柱图。."""
    if any(token in transcript.kind.lower() for token in kinds):
        return True
    for curve in transcript.series:
        unit = str(curve.get("unit") or "").strip()
        if unit in {"%", "％", "percent"}:
            return True
    for axis in transcript.axes.values():
        if isinstance(axis, dict) and str(axis.get("unit") or "").strip() in {
            "%",
            "％",
            "percent",
        }:
            return True
    return False


def _value_supported(
    value: float, evidence: FigureEvidence, axis: tuple[float, float] | None
) -> bool:
    """这个数值能不能在图上找到依据：印出来的数字，或柱高比例。."""
    for candidate in evidence.numbers:
        if _close(value, candidate):
            return True
    if axis is not None:
        low, high = axis
        span = high - low
        if span > 0:
            for ratio, _cx in evidence.bar_ratios:
                expected = low + ratio * span
                if _close(value, expected, tolerance=BAR_HEIGHT_TOLERANCE):
                    return True
    return False


def _close(
    left: float, right: float, *, tolerance: float = NUMBER_MATCH_TOLERANCE
) -> bool:
    """Run _close."""
    if left == right:
        return True
    scale = max(abs(left), abs(right), 1e-9)
    return abs(left - right) / scale <= tolerance


def _label_supported(label: str, evidence: FigureEvidence) -> bool:
    """标签必须出现在图区域的文字里（大小写与空白归一后比较）。.

    中文与英文混排时标签常被拆成多行（`Monthly` / `limit`），所以用"互相包含"
    而不是相等：`Monthly limit` 在证据里是两行，任一方向的子串命中都算。
    """
    needle = _normalize_label(label)
    if not needle:
        return False
    for text in evidence.texts:
        haystack = _normalize_label(text)
        if not haystack:
            continue
        if needle in haystack or haystack in needle:
            return True
    return False


def _normalize_label(text: str) -> str:
    """Run _normalize_label."""
    return re.sub(r"\s+", "", text).strip().lower()


# --- 组装 FigureData --------------------------------------------------------


def build_figure_data(
    *,
    image_path: str,
    width_px: int,
    height_px: int,
    dpi: float,
    transcript: Transcript,
    evidence: FigureEvidence,
) -> FigureData:
    """转译 + 对账都过了之后才调用。返回的对象直接进 `Region.figure`。."""
    return FigureData(
        image_path=image_path,
        width_px=width_px,
        height_px=height_px,
        dpi=dpi,
        raw_markdown=transcript.raw_markdown,
        kind=transcript.kind,
        axes=dict(transcript.axes),
        series=transcript.series,
        labels_in_region=evidence.texts,
        numbers_in_region=evidence.numbers,
        bar_heights=tuple(ratio for ratio, _cx in evidence.bar_ratios),
        legend_missing=transcript.legend_missing,
    )


def figure_content_text(
    figure: FigureData, *, caption: str | None = None
) -> str:
    """图切片的 `content_text`：图注 + 转译的 Markdown。.

    不拼结构化字段（那些给对账与审计用）：`{"axes": {...}}` 进检索文本是纯噪声。
    图注放最前，因为"这张图讲什么"通常是问句最可能命中的一句。
    """
    parts: list[str] = []
    if caption:
        parts.append(caption.strip())
    if figure.raw_markdown.strip():
        parts.append(figure.raw_markdown.strip())
    return "\n".join(part for part in parts if part)


__all__ = [
    "CheckFailure",
    "FigureEvidence",
    "PERCENT_SUM_MAX",
    "PERCENT_SUM_MIN",
    "ReconciliationResult",
    "STAT_TRANSCRIBE_PROMPT",
    "STRUCTURE_TRANSCRIBE_PROMPT",
    "TRANSCRIBE_PROMPT_VERSION",
    "Transcript",
    "VLMClient",
    "VLMUnavailable",
    "VLMUnavailableError",
    "build_figure_data",
    "extract_numbers",
    "figure_content_text",
    "parse_transcript",
    "prompt_for",
    "reconcile",
]
