#!/usr/bin/env python3
"""doc_lint.py — 详细设计文档哨兵。

用法：
  python3 doc_lint.py      # 全文（交付前必跑）
  python3 doc_lint.py 3    # 只查第 3 章（写作时快速反馈）
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
DOC = ROOT / "详细设计.md"

CHAPTERS = [str(i) for i in range(1, 13)]  # 1..12
APPENDIX_COUNT = 7
JARGON = ["Rerank", "Pre-Filtering", "NLI", "RRF", "HNSW", "sparsevec", "LangGraph", "Chunk"]
FORBIDDEN = ["TODO", "TBD", "待补充", "此处省略", "AKIA", "BEGIN PRIVATE KEY", "sk-"]
MUST_HAVE = ["0.20", "0.40", "Max Hops = 3", "Temperature = 0", "200 DPI", "40–60 万", "0.38–0.42"]
KINDS = ("flowchart", "graph ", "sequenceDiagram", "stateDiagram", "erDiagram",
         "timeline", "mindmap", "classDiagram", "gantt", "journey")
FENCE = "``" + "`"

failures: list[str] = []


def check(ok: bool, msg: str) -> None:
    if not ok:
        failures.append(msg)


def blocks(text: str) -> dict[str, str]:
    """按 ## 标题切块：{标题: 该块正文}。"""
    heads = [(m.start(), m.group(1).strip()) for m in re.finditer(r"^## +(.+)$", text, re.M)]
    out: dict[str, str] = {}
    for i, (pos, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        out[title] = text[pos:end]
    return out


def strip_code(body: str) -> str:
    return re.sub(FENCE + r"[^\n]*\n.*?" + FENCE, "", body, flags=re.S)


def check_body(name: str, body: str, min_chars: int, need_diagram: bool = True) -> None:
    plain = strip_code(body)
    if need_diagram:
        diagrams = re.findall(FENCE + "mermaid\n(.*?)" + FENCE, body, re.S)
        check(bool(diagrams), f"{name} 没有 Mermaid 图")
        for i, blk in enumerate(diagrams, 1):
            first = next((ln.strip() for ln in blk.splitlines() if ln.strip()), "")
            check(first.startswith(KINDS), f"{name} 第 {i} 张图首行不是已知图类型：{first[:40]}")
        for word in JARGON:
            hits = len(re.findall(rf"(?<![A-Za-z0-9-]){re.escape(word)}(?![A-Za-z0-9-])", plain))
            check(hits <= 2, f"{name} 术语 {word} 出现 {hits} 次（正文上限 2）")
    check(len(plain) >= min_chars, f"{name} 正文过短（{len(plain)} 字，至少 {min_chars}）")


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    if not DOC.exists():
        print("DOC LINT: FAIL\n - 文件不存在：" + str(DOC))
        return 1
    text = DOC.read_text(encoding="utf-8")
    all_blocks = blocks(text)

    for ch in CHAPTERS:
        if only and only != ch:
            continue
        title = next((t for t in all_blocks if t.startswith(ch + " ")), None)
        check(title is not None, "缺少章节 ## " + ch)
        if title:
            check("？" not in title and "！" not in title, "## " + title + " 标题含问号或感叹号")
            check(len(title) <= 16, "## " + title + " 标题超过 16 字")
            check_body("## " + title, all_blocks[title], 700)

    if not only:
        apps = [t for t in all_blocks if t.startswith("附录 ")]
        check(len(apps) == APPENDIX_COUNT, f"附录数量为 {len(apps)}，应为 {APPENDIX_COUNT}")
        for t in apps:
            check_body("## " + t, all_blocks[t], 500, need_diagram=False)
        for token in MUST_HAVE:
            check(token in text, "缺少关键约束：" + token)
        for token in FORBIDDEN:
            check(token not in text, "出现禁用串：" + token)
        check(text.count(FENCE) % 2 == 0, "代码栅栏未闭合")

    if failures:
        print("DOC LINT: FAIL")
        for msg in failures:
            print(" -", msg)
        return 1
    print("DOC LINT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())