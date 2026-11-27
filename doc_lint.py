#!/usr/bin/env python3
"""doc_lint.py — 详细设计文档哨兵。.

用法：
  python3 doc_lint.py      # 全文（交付前必跑）
  python3 doc_lint.py 3    # 只查第 3 章（写作时快速反馈）
"""

import pathlib
import re
import sys

from resources import doc_lint_text as text

ROOT = pathlib.Path(__file__).resolve().parent
DOC = ROOT / text.DOC_NAME

CHAPTERS = [str(i) for i in range(1, 13)]  # 1..12
APPENDIX_COUNT = 7
JARGON = [
    "Rerank",
    "Pre-Filtering",
    "NLI",
    "RRF",
    "HNSW",
    "sparsevec",
    "LangGraph",
    "Chunk",
]
FORBIDDEN = [
    "TODO",
    "TBD",
    text.FORBIDDEN_PENDING,
    text.FORBIDDEN_OMITTED,
    "AKIA",
    "BEGIN PRIVATE KEY",
    "sk-",
]
MUST_HAVE = [
    "0.20",
    "0.40",
    "Max Hops = 3",
    "Temperature = 0",
    "200 DPI",
    text.MUST_HAVE_WAN_RANGE,
    "0.38–0.42",
]
KINDS = (
    "flowchart",
    "graph ",
    "sequenceDiagram",
    "stateDiagram",
    "erDiagram",
    "timeline",
    "mindmap",
    "classDiagram",
    "gantt",
    "journey",
)
FENCE = "``" + "`"

failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    """记录一条检查结果：失败时把原因收集起来，不立即中断。."""
    if not ok:
        failures.append(msg)


def blocks(text: str) -> dict[str, str]:
    """按 ## 标题切块：{标题: 该块正文}。."""
    heads = [
        (m.start(), m.group(1).strip())
        for m in re.finditer(r"^## +(.+)$", text, re.M)
    ]
    out: dict[str, str] = {}
    for i, (pos, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        out[title] = text[pos:end]
    return out


def strip_code(body: str) -> str:
    """去掉围栏代码块，只留下正文（代码里的词不算正文）。."""
    return re.sub(FENCE + r"[^\n]*\n.*?" + FENCE, "", body, flags=re.S)


def check_body(
    name: str, body: str, min_chars: int, need_diagram: bool = True
) -> None:
    """校验一节正文：最小字数、是否带图、关键小节是否齐备。."""
    plain = strip_code(body)
    if need_diagram:
        diagrams = re.findall(FENCE + "mermaid\n(.*?)" + FENCE, body, re.S)
        check(bool(diagrams), text.MSG_DIAGRAM_MISSING.format(name))
        for i, blk in enumerate(diagrams, 1):
            first = next(
                (ln.strip() for ln in blk.splitlines() if ln.strip()), ""
            )
            check(
                first.startswith(KINDS),
                text.MSG_DIAGRAM_KIND.format(name, i, first[:40]),
            )
        for word in JARGON:
            hits = len(
                re.findall(
                    rf"(?<![A-Za-z0-9-]){re.escape(word)}(?![A-Za-z0-9-])",
                    plain,
                )
            )
            check(
                hits <= 2,
                text.MSG_JARGON_LIMIT.format(name, word, hits),
            )
    check(
        len(plain) >= min_chars,
        text.MSG_BODY_SHORT.format(name, len(plain), min_chars),
    )


def main() -> int:
    """入口：逐节跑检查并打印结论，返回 0/1 退出码。."""
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if not DOC.exists():
        print(text.MSG_DOC_MISSING.format(DOC))
        return 1
    doc_text = DOC.read_text(encoding="utf-8")
    all_blocks = blocks(doc_text)

    for ch in CHAPTERS:
        if only and only != ch:
            continue
        title = next((t for t in all_blocks if t.startswith(ch + " ")), None)
        check(title is not None, text.MSG_CHAPTER_MISSING.format(ch))
        if title:
            check(
                text.TITLE_BANNED_CHARS[0] not in title
                and text.TITLE_BANNED_CHARS[1] not in title,
                text.MSG_TITLE_BANNED.format(title),
            )
            check(
                len(title) <= 16,
                text.MSG_TITLE_LONG.format(title),
            )
            check_body("## " + title, all_blocks[title], 700)

    if not only:
        apps = [t for t in all_blocks if t.startswith(text.APPENDIX_PREFIX)]
        check(
            len(apps) == APPENDIX_COUNT,
            text.MSG_APPENDIX_COUNT.format(len(apps), APPENDIX_COUNT),
        )
        for t in apps:
            check_body("## " + t, all_blocks[t], 500, need_diagram=False)
        for token in MUST_HAVE:
            check(token in doc_text, text.MSG_MUST_HAVE.format(token))
        for token in FORBIDDEN:
            check(token not in doc_text, text.MSG_FORBIDDEN.format(token))
        check(doc_text.count(FENCE) % 2 == 0, text.MSG_FENCE_OPEN)

    if failures:
        print("DOC LINT: FAIL")
        for msg in failures:
            print(" -", msg)
        return 1
    print("DOC LINT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
