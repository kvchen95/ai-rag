"""文件名 -> `doc_key`，文件字节 -> SHA-256（详细设计.md §2.1 / §3.1）。.

两件事都写成纯函数，因为它们是判重的地基：扫描、将来的上传接口、评测脚本
都要算出**同一个** doc_key 与同一个指纹，算法只能在一个人手里。

### doc_key 的作用

`doc_key` 是"这份材料"的编号，不是文件的编号。要求是：**同一份材料的每次改版
落在同一个 key 下**，否则"2025 版覆盖 2024 版"这件事在库里根本表达不出来
（§2.1）。所以路径参与、但路径里的版本号/日期/序号不参与：

    samples/policy/travel-cn.pdf                    -> policy/travel-cn
    samples/policy/差旅管理办法-2025版.pdf           -> policy/差旅管理办法
    samples/policy/差旅管理办法-2025版-第2稿.pdf     -> policy/差旅管理办法
    samples/policy/差旅管理办法（2）.pdf             -> policy/差旅管理办法
    samples/2025.pdf                                -> 2025   （剥空则回退原名）

只剥**段尾**的修饰，不剥中间：`2025-差旅.pdf` 里的 2025 是主体的一部分
（"2025 年度差旅办法"和"差旅办法"是两份材料），剥掉会误合并。

为什么不做成"读文件标题"：开发期样本只有文件名，读 PDF 内嵌标题要等解析完成，
那时已经晚了——key 必须在入队前就定下来，才有地方挂版本。

### 为什么指纹用整份文件字节

§4.4 的判重口径是"同一内容重复入库返回已存在"，而 `doc_versions.file_hash`
是 `UNIQUE (doc_key, file_hash)` 的成员，有 CHECK 强制 64 位小写十六进制。
用文件字节而不是解析后的正文：解析器换版本会让同一份文件算出不同指纹，于是
同一份材料每换一次解析器就多一个版本，版本线立刻失去意义。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1 << 20

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# 中日韩统一表意文字（含扩展 A 与兼容区）。用来判断"要不要转拼音"，
# 而不是判断"是不是中文"——日文汉字、韩文汉字同样需要转，因为它们在 URL
# 路径参数里同样要百分号编码。
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# 文件名里带书名号/方括号的机构名，例：`【上海证券】中芯国际深度研究报告`。
# 提取出来单独转拼音，让 key 一眼能看出是哪家的报告。
_BRACKET_PREFIX = re.compile(r"^[【\[（(]([^】\]）)]{1,20})[】\]）)]\s*")

# doc_key 里 stem 的最大音节数：8 个音节约等于 16 个汉字，够区分同一机构的
# 不同报告，又不会长到 `make doc DOC_KEY=...` 敲不进去。
MAX_PINYIN_SYLLABLES = 8

# 段尾修饰：版本号、日期、序号、副本标记。全部按"可重复剥离"处理，
# 因为真实文件名经常叠加（`差旅办法-2025版-修订-第2稿`）。
#
# 两条设计约束（都在实测里踩到过）：
#   1. 日期段尾要能吃下 `2025-03-01` / `2025年3月` / `20250301` 三种写法，
#      只写"数字+可选月日"会漏掉 `年3月`（月后面没有日）。
#   2. 修饰词既可能是裸的（`-副本`）也可能被括号包住（`(副本)`）。带括号的必须
#      整组匹配，否则 `制度(副本)` 会被剥成 `制度(副本` 这种半截。
_TAIL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # -v2 / _v2.1 / v3
    re.compile(r"[-_\s]*v\d+(?:[.\-]\d+)*$", re.IGNORECASE),
    # 20250301 / 2025-03-01 / 2025_03 / 2025年3月 / 2025年3月1日 / 2025
    re.compile(
        r"[-_\s]*\d{4}"
        r"(?:[-_.年]\d{1,2}(?:[-_.月]\d{1,2}日?|月)?)?"
        r"(?:版|年度?)?$"
    ),
    # 第2版 / 第2稿 / 2版 / 二稿
    re.compile(r"[-_\s]*(?:第\s*\d+\s*[版稿次]|[二三四五六七八九十]+稿)$"),
    # 修订 / 修订版 / 终版 / 定稿 / final / draft
    re.compile(
        r"[-_\s]*(?:修订版?|终版?|正式版?|定稿版?|最新版?|final|draft)$",
        re.IGNORECASE,
    ),
    # 副本 / 拷贝 / copy / 备份，裸写或被括号包住（括号整组匹配，不留半截）
    re.compile(
        r"[-_\s]*(?:副本|拷贝|备份|copy|副本拷贝)$"
        r"|[-_\s]*[\(（\[【](?:副本|拷贝|备份|copy)[\)）\]】]$",
        re.IGNORECASE,
    ),
    # (2) / （2） / [2]
    re.compile(r"[-_\s]*[\(（\[【]\s*\d+\s*[\)）\]】]$"),
    # 纯序号段尾：-2 / _2
    re.compile(r"[-_\s]+\d{1,2}$"),
)

# 空白统一成单个连字符：文件名里"差旅 管理 办法"与"差旅管理 办法"不该是
# 两个 key。
_WS = re.compile(r"\s+")
# 文件系统里可能出现但 URL 路径参数里会出问题的字符，统一收一收。
_UNSAFE = re.compile(r"[?#%]+")


class BadSamplePathError(ValueError):
    """样本路径不能被接受（绝对路径、跳出根目录、非 .pdf 等）。."""


class DocKeyNeedsPinyinError(RuntimeError):
    """中文文件名需要 pypinyin 才能生成 ASCII 的 doc_key，但依赖缺失。."""


@dataclass(frozen=True, slots=True)
class SampleFile:
    """一个待摄取的样本。`rel_path` 相对 `samples/`，写进文档展示与日志。."""

    path: Path
    rel_path: str
    doc_key: str
    filename: str

    @property
    def size(self) -> int:
        """Run size."""
        return self.path.stat().st_size


def _to_ascii_slug(text: str) -> str:
    """中文 -> 拼音音节串。纯 ASCII 文本原样返回（小写）。.

    为什么用拼音而不是哈希：`doc_key` 是给人看的标识符，`make doc DOC_KEY=...`
    和 `curl /api/v1/documents/{doc_key}` 都要手打。哈希能保证唯一，但没人能凭它
    判断这是哪份文档；拼音至少能读出来。

    为什么不用"标题前 N 个汉字"：那只是把长中文名变短中文名，URL 里仍然是
    百分号编码。真正的问题是"非 ASCII 字符不适合做标识符"，截断没有解决它。

    依赖缺失时的行为是**报错**，不是静默回退。理由来自一次实测：沙箱里没装
    pypinyin 时，`doc_key` 悄悄变回了长中文名，而扫描、入库、检索全都照常跑通——
    直到拿 key 去拼 URL 才发现要百分号编码。静默降级让"配好了"和"没配"看起来
    完全一样，这类错误必须在第一次生成 key 时就炸出来。

    确实要裁剪掉这个依赖的部署，设 `AI_RAG_ALLOW_CJK_DOC_KEY=1` 显式接受
    中文 key。
    """
    if not _CJK.search(text):
        return text.lower()
    try:
        from pypinyin import lazy_pinyin
    except ImportError as exc:  # pragma: no cover - 只在裁剪过的部署里发生
        if os.environ.get("AI_RAG_ALLOW_CJK_DOC_KEY") == "1":
            return text.lower()
        raise DocKeyNeedsPinyinError(
            "doc_key 生成需要 pypinyin 把中文转成 ASCII 标识符"
            "（pip install pypinyin）。"
            "若确实要接受中文 doc_key，设 AI_RAG_ALLOW_CJK_DOC_KEY=1。"
        ) from exc
    # 标点（`，`、`：`、`（）`、`——`）不是音节，拼音化后仍会留在串里，
    # 让 key 变成 `...ti-sheng-，`。先按"非字母数字非中文"切开，只取有内容的段。
    cleaned = re.sub(r"[^0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]+", " ", text)
    syllables = [
        s for s in lazy_pinyin(cleaned) if s.strip() and s.strip().isalnum()
    ]
    picked = syllables[:MAX_PINYIN_SYLLABLES]
    return "-".join(picked).lower()


def doc_key_for(rel_path: str) -> str:
    """相对路径 -> doc_key。调用方保证 `rel_path` 已是相对 `samples/` 的路径。.

    归一化四步：去扩展名 -> 段尾修饰逐个剥到稳定 -> 空白收成连字符 -> 小写。
    小写是为了让 `Travel-CN.pdf` 与 `travel-cn.pdf` 落在同一个 key 下：
    doc_key 是标识符不是展示名，展示名在 `documents.filename` 里。
    """
    posix = Path(rel_path).as_posix().strip("/")
    if not posix:
        raise BadSamplePathError("empty relative path")
    if posix.startswith("../") or "/../" in posix or posix == "..":
        raise BadSamplePathError(
            f"sample path escapes the samples root: {rel_path}"
        )

    parts = posix.split("/")
    stem = Path(parts[-1]).stem or parts[-1]
    original = stem

    # 剥到不再变化为止：`差旅-2025版-第2稿` 要剥两次（第2稿、2025版）。
    changed = True
    while changed:
        changed = False
        for pattern in _TAIL_PATTERNS:
            stripped = pattern.sub("", stem).strip()
            if stripped and stripped != stem:
                stem = stripped
                changed = True

    # 剥空了说明整个文件名都是修饰（`2025.pdf`、`(2).pdf`）。回退到原名：
    # 造一个空 key 或者塞占位符，都会让两份不同材料撞在一起。
    if not stem.strip():
        stem = original

    # 机构名（`【上海证券】`）单独提出来，与标题分开转拼音：
    # `shanghai-zhengquan-zhongxin-guojizhendu` 比
    # `shang-hai-zheng-quan-zhong-xin`
    # 更容易看出"这是上海证券的中芯报告"。
    org = ""
    bracket = _BRACKET_PREFIX.match(stem)
    if bracket:
        org = _to_ascii_slug(bracket.group(1))
        stem = stem[bracket.end() :].strip() or stem

    stem_slug = _to_ascii_slug(stem)
    stem_slug = _WS.sub("-", _UNSAFE.sub("-", stem_slug).strip()).strip("-.")
    # 拼音化会把原本的中文标点变成空格，与既有的连字符相邻时产生 `--`。
    # `2024-差旅` -> `2024--cha-lv` 是典型：中间那个 `-` 是原文件名的分隔符。
    stem_slug = re.sub(r"-{2,}", "-", stem_slug).strip("-.")
    if not stem_slug:
        # 拼音化后为空只可能是空串输入；回退到原文，绝不造空 key。
        stem_slug = (
            _WS.sub("-", _UNSAFE.sub("-", original.strip())).strip("-.").lower()
        )
    if not stem_slug:
        raise BadSamplePathError(f"cannot derive a doc_key from: {rel_path}")

    parents = [p.lower() for p in parts[:-1]]
    leaf = (
        f"{org}-{stem_slug}"
        if org and not stem_slug.startswith(org)
        else stem_slug
    )
    key = "/".join([*parents, leaf])
    return _WS.sub("-", key)


# 以 `_` 开头的目录不参与扫描。用来放"要留着但不入库"的文件：
# 损坏的 PDF、待人工核对的扫描件、体积太大暂不处理的年报。
# 约定优于配置：不需要在 .env 里维护排除列表，也不需要给每份文件加标记。
_EXCLUDED_DIR_PREFIX = "_"


def iter_sample_paths(root: Path) -> Iterator[Path]:
    """递归列出 `samples/` 下的 PDF，按路径排序（同一批样本每次顺序一致）。.

    `_` 开头的目录整棵跳过（`samples/_excluded/` 等）。这条规则让"移出去"成为
    唯一需要的动作——不用改配置、不用改代码，把文件挪进 `_` 目录即退出扫描。
    """
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        rel_parts = path.relative_to(root).parts[:-1]
        if any(part.startswith(_EXCLUDED_DIR_PREFIX) for part in rel_parts):
            continue
        yield path


def list_samples(root: Path) -> list[SampleFile]:
    """列出样本清单。**不读文件内容**——读失败要由扫描逐条记进 ingest_jobs。."""
    root = root.resolve()
    samples: list[SampleFile] = []
    for path in iter_sample_paths(root):
        rel_path = path.relative_to(root).as_posix()
        samples.append(
            SampleFile(
                path=path,
                rel_path=rel_path,
                doc_key=doc_key_for(rel_path),
                filename=path.name,
            )
        )
    return samples


def file_hash(path: Path, *, chunk: int = CHUNK) -> str:
    """文件字节的 SHA-256，64 位小写十六进制。.

    `ck_doc_versions_hash_sha256` 要这个形状。分块读：样本里有几十 MB 的
    扫描件，一次性 read() 会在内存里翻倍。
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def is_sha256(value: str) -> bool:
    """Run is_sha256."""
    return bool(SHA256_RE.match(value))


def version_id_for(doc_key: str, content_hash: str) -> str:
    """`version_id` = doc_key + 内容指纹短码。.

    形状是附录 A.2 的 `policy/travel-cn#a91f`。

    短码取 **12 位**而不是附录示例的 4 位：`version_id` 是主键，4 位十六进制只有
    65536 个取值，一个 doc_key 攒到几十个版本就会开始互相覆盖主键。
    """
    if not is_sha256(content_hash):
        raise ValueError("content_hash must be a 64-char lowercase hex sha256")
    return f"{doc_key}#{content_hash[:12]}"

# Backwards-compatible alias (N818: exception names use Error suffix).
BadSamplePath = BadSamplePathError

# Backwards-compatible alias (N818: exception names use Error suffix).
DocKeyNeedsPinyin = DocKeyNeedsPinyinError
