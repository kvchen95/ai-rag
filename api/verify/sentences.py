"""答案侧切分：把草稿拆成"可校验的句子"。

§10.1 第二道要求"按 `claims` 逐条看"，§10.1 第五道要求"草稿里同一件事不能出现两个数"。
两句都隐含同一件事：**校验单位是句子**。而 `claims` 不一定覆盖整段 answer——
模型完全可能写了五句、只给了两条 claim（那正是第二道要抓的情形之一）。

所以这里有两条路，且必须两条都走：

1. `claims` 里逐条原文（模型自报的校验单位）；
2. `answer` 按句切出来的每句（**真值口径**）。

只走第 1 条的漏洞很具体：answer = "500 元[1]。另外 800 元[2]。"，claims 只给了
第一条，于是第二句从没被校验过就进了用户屏幕。只走第 2 条的漏洞是模型把一句
拆成三条 claim 时我们会重复报告同一个失败。所以：**answer 的句子是并集的主干**，
claims 里不与任何句子重合的条目单独补进来。

切句规则刻意保守（宁可把两句合成一句，也不要把一句切成两句）：
- 中英文句末标点（。！？!?；;\n）都是边界；
- **不在数字中间切**（`1.5` `3,000` 里的点与逗号不是边界）；
- **角标不是边界**：`每人每天 500 元[1]。` 必须在 `。` 处切，不能把 `[1]` 切丢；
  否则"每句引用"这条判据会因为切句而假红。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 句末标点。`；` 也算：它连接的是两个可以独立成立的分句。
# 角标：[1] / [1,2] / [1, 2] / ［1］（全角）
_CITATION = re.compile(r"[\[［]\s*(\d+(?:\s*[,，、]\s*\d+)*)\s*[\]］]")


@dataclass(frozen=True, slots=True)
class Sentence:
    """一句可校验的话。"""

    text: str
    # 这一句里出现的引用编号（去重、保持出现顺序）。
    citations: tuple[int, ...]
    # 去掉角标后的纯文本：数字与专名校验要在它上面做（`[1]` 里的 1 不是数字证据）。
    bare: str

    @property
    def empty(self) -> bool:
        return not self.bare.strip()


def parse_citations(text: str) -> tuple[int, ...]:
    """抽出一段文本里的全部角标编号。"""
    found: list[int] = []
    for match in _CITATION.finditer(text or ""):
        for part in re.split(r"[,，、]", match.group(1)):
            part = part.strip()
            if part.isdigit():
                value = int(part)
                if value not in found:
                    found.append(value)
    return tuple(found)


def strip_citations(text: str) -> str:
    return _CITATION.sub("", text or "")


def split_sentences(text: str) -> tuple[str, ...]:
    """按句末标点切句，保留标点，跳过空句。

    不用 `re.split` 直接切是因为那样会丢掉标点，而"这一句到底说了什么"在
    报告里要能原样引用给用户看。这里手工扫描，边界后把标点并回前一句。
    """
    if not text or not text.strip():
        return ()
    out: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in "。！？!?；;\n":
            # 连续的句末标点（`！？`、`……`）一起算进这一句。
            end = index + 1
            while end < len(text) and text[end] in "。！？!?；;\n":
                end += 1
            piece = text[start:end].strip()
            if piece:
                out.append(piece)
            start = end
            index = end
            continue
        index += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return tuple(out)


def to_sentences(text: str) -> tuple[Sentence, ...]:
    """把一段草稿切成可校验的句子集合。"""
    sentences: list[Sentence] = []
    for piece in split_sentences(text):
        citations = parse_citations(piece)
        bare = strip_citations(piece).strip()
        sentences.append(Sentence(text=piece, citations=citations, bare=bare))
    return tuple(sentences)


def merge_claim_sentences(
    answer: str, claims: "tuple[str, ...] | list[str] | None" = None
) -> tuple[Sentence, ...]:
    """answer 的句子 ∪ 不与任何句子重合的 claim（见模块头两条路）。

    重合判定用"去掉角标与空白后的子串关系"：claim 是某句的一部分时它已经被那一句
    覆盖了，再单独列一次会让同一个失败被报两遍，而报告条数是给人看的。

    **空 claim 必须保留成一条空句子**：§10.1 第二道"主张为空也算失败"。在这里
    `continue` 掉它，等于让一条"模型给了个空主张"的草稿顺利通过第二道——而模型
    交空主张通常意味着它不知道该说什么，那正是最该被拦的情形。
    """
    primary = list(to_sentences(answer))
    covered = [item.bare.replace(" ", "") for item in primary if item.bare]
    for claim in claims or ():
        bare = strip_citations(claim).strip()
        if not bare:
            primary.append(Sentence(text=claim.strip(), citations=parse_citations(claim), bare=""))
            continue
        needle = bare.replace(" ", "")
        if any(needle in hay or hay in needle for hay in covered):
            continue
        primary.append(
            Sentence(text=claim.strip(), citations=parse_citations(claim), bare=bare)
        )
        covered.append(needle)
    return tuple(primary)


__all__ = [
    "Sentence",
    "merge_claim_sentences",
    "parse_citations",
    "split_sentences",
    "strip_citations",
    "to_sentences",
]
