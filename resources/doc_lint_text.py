"""Chinese text catalog for the detailed-design document linter."""

DOC_NAME = "详细设计.md"

APPENDIX_PREFIX = "附录 "
TITLE_BANNED_CHARS = ("？", "！")
FORBIDDEN_PENDING = "待补充"
FORBIDDEN_OMITTED = "此处省略"
MUST_HAVE_WAN_RANGE = "40–60 万"

MSG_DIAGRAM_MISSING = "{} 没有 Mermaid 图"
MSG_DIAGRAM_KIND = "{} 第 {} 张图首行不是已知图类型：{}"
MSG_JARGON_LIMIT = "{} 术语 {} 出现 {} 次（正文上限 2）"
MSG_BODY_SHORT = "{} 正文过短（{} 字，至少 {}）"
MSG_DOC_MISSING = "DOC LINT: FAIL\n - 文件不存在：{}"
MSG_CHAPTER_MISSING = "缺少章节 ## {}"
MSG_TITLE_BANNED = "## {} 标题含问号或感叹号"
MSG_TITLE_LONG = "## {} 标题超过 16 字"
MSG_APPENDIX_COUNT = "附录数量为 {}，应为 {}"
MSG_MUST_HAVE = "缺少关键约束：{}"
MSG_FORBIDDEN = "出现禁用串：{}"
MSG_FENCE_OPEN = "代码栅栏未闭合"
