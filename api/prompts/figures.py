"""读图转译提示词（api/parse/figures_transcribe.py）。.

提示词是运行期文案，放在 prompts/ 下；转译版本号仍在调用模块里。
"""

from __future__ import annotations

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
TRANSCRIBE_SCHEMA = (
    '{"kind":"bar|line|pie|structure|table|photo",'
    '"axes":{"x":{"label":..,"unit":..,"min":..,"max":..},'
    '"y":{...}},"series":[{"name":..,"unit":..,"points":'
    '[{"label":..,"value":..}]}],"legend_missing":false}'
)

TRANSCRIBE_PROMPT_SUFFIX = (
    "\n先输出 Markdown 表格，然后输出一行以 `JSON:` 开头的结构化结果：\n"
    + TRANSCRIBE_SCHEMA
    + "\n数值必须来自图上的刻度与标签；"
    "无法确定写 null，不要估算，不要补全。"
)
