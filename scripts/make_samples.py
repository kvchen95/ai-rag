#!/usr/bin/env python3
"""生成开发期样本 PDF（纯 stdlib，不引第三方库）。.

为什么自己写 PDF 而不是下载真实制度文件：这份仓库要能 `git clone` 就有可跑的
样本，
且样本必须**稳定**（每次生成字节完全一致，否则 file_hash 天天变，判重永远测不出
"重复扫描跳过"）。自己构造结构还顺带保证了两件事：

- 字节确定：内容按固定顺序写，`/CreationDate` 之类的时间戳一律不写；
- 结构合法：xref 偏移由代码算出来，能用 `qpdf --check` 之类工具校验（本机没有也
能被
  任何 PDF 阅读器打开）。

样本形状照 §3.1 的扫描判定表设计：

    samples/policy/travel-cn.pdf                 差旅管理办法（主体）
    samples/policy/expense-cn.pdf                费用报销办法（主体）
    samples/hr/leave-2025.pdf                    请假制度，文件名带年份 ->
    doc_key 剥掉 2025

用法：

    python scripts/make_samples.py            # 写缺失的文件，已存在的不动
    python scripts/make_samples.py --force    # 全部重写
    python scripts/make_samples.py --root /tmp/samples

"已存在的不动"是默认行为：`samples/` 是测试资产（附录 B），重跑脚本不该把测试者
手改过的文件悄悄换掉。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__:
    from .resources.make_samples_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/make_samples.py
    from resources.make_samples_msgs import MESSAGES as M

REPO_ROOT = Path(__file__).resolve().parent.parent

# 每份样本：(相对路径, 每页正文)。正文用 ASCII + 中文都能写，但为了不依赖字体嵌
# 入，
# 这里用 ASCII 文本——扫描/解析链路的正确性不取决于样本里有没有中文，
# 而中文需要嵌入 CJK 字体子集（几百 KB），对一张扫描卡来说不值得。
SAMPLES: list[tuple[str, list[list[str]]]] = [
    (
        "policy/travel-cn.pdf",
        [
            [
                "Travel Expense Policy (CN) - 2025 revision",
                "Section 1. Scope",
                "This policy applies to all employees on domestic business"
                " travel.",
                "Section 2. Lodging limit",
                "Lodging: CNY 500 per person per night in tier-1 cities,",
                "CNY 380 per person per night elsewhere.",
                "Effective date: 2025-03-01.",
            ],
            [
                "Section 3. Approval",
                "Trips above CNY 5000 require department head approval.",
            ],
        ],
    ),
    (
        "policy/expense-cn.pdf",
        [
            [
                "Expense Reimbursement Rules - 2025",
                "Section 1. Receipts",
                "Itemised receipts are required for any single expense above"
                " CNY 200.",
                "Section 2. Submission window",
                "Claims must be submitted within 60 days of the expense date.",
            ]
        ],
    ),
    (
        # 文件名带年份：doc_key 归一化后应当是 hr/leave（不是 hr/leave-2025）
        "hr/leave-2025.pdf",
        [
            [
                "Leave Policy - 2025",
                "Section 1. Annual leave",
                "Employees accrue 5 days in year one, plus one day per full"
                " year served,",
                "capped at 15 days.",
                "Section 2. Carry-over",
                "At most 5 unused days may be carried into the next calendar"
                " year.",
            ]
        ],
    ),
]


def escape_text(line: str) -> str:
    r"""PDF 字面量字符串里 `(` `)` `\\` 必须转义。."""
    return line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf(pages: list[list[str]]) -> bytes:
    """拼一个合法的 PDF：catalog -> pages -> 每页 -> 单一 font。.

    结构刻意保持最小：1 个 Catalog、1 个 Pages、N 个 Page、每页 1 个 Contents、
    1 个共享 Font。多写一层都会让 xref 偏移更容易算错，而这里最需要的是"确定"。
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        """Run add."""
        objects.append(body)
        return len(objects)  # 对象号从 1 开始

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    # Pages 的 /Kids 要页对象号，页对象要先占位再回填，所以先算布局：
    # 1:CATALOG 2:PAGES 3:FONT 然后每页 2 个对象（Page, Contents）
    catalog_id = 1
    pages_id = 2
    objects.insert(0, b"")  # 占位 catalog
    objects.insert(1, b"")  # 占位 pages
    # **`add()` 返回的是"插入占位符之前"的序号**：上面两次 insert 把字体对象整体
    # 后移两格，
    # 所以 /Font 里的引用必须用移位后的号（3）。用 `font_id` 原值会让 /F1 指向
    # Catalog：
    # pdfplumber 读正文不看字体资源，照样能抽出文字；pdf.js 按字体画字，结果是整
    # 页空白
    # ——"能解析"和"能显示"在这里是两件事。下面这句断言把号算错当场变成异常。
    font_id = 3
    assert objects[font_id - 1].startswith(
        b"<< /Type /Font"
    ), M["BUILD_PDF_01"]

    page_ids: list[int] = []
    for lines in pages:
        # 字号 11、行距 16，从页面顶端 780pt 起排（A4 高 842pt）。
        text_ops = ["BT", "/F1 11 Tf", "16 TL", "72 780 Td"]
        for index, line in enumerate(lines):
            if index:
                text_ops.append("T*")
            text_ops.append(f"({escape_text(line)}) Tj")
        text_ops.append("ET")
        stream = "\n".join(text_ops).encode("latin-1", "replace")

        contents_id = add(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        page_id = add(b"")  # 占位，父节点号已知但 contents 号刚算出来
        objects[page_id - 1] = (
            b"<< /Type /Page /Parent "
            + str(pages_id).encode()
            + b" 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 "
            + str(font_id).encode()
            + b" 0 R >> >> /Contents "
            + str(contents_id).encode()
            + b" 0 R >>"
        )
        page_ids.append(page_id)

    kids = b" ".join(str(pid).encode() + b" 0 R" for pid in page_ids)
    objects[pages_id - 1] = (
        b"<< /Type /Pages /Kids ["
        + kids
        + b"] /Count "
        + str(len(page_ids)).encode()
        + b" >>"
    )
    objects[catalog_id - 1] = (
        b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>"
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root "
        + str(catalog_id).encode()
        + b" 0 R >>\nstartxref\n"
        + str(xref_at).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def main() -> int:
    """Run main."""
    ap = argparse.ArgumentParser(
        description="generate deterministic sample PDFs"
    )
    ap.add_argument(
        "--root", default=str(REPO_ROOT / "samples"), help=M["HELP_01"]
    )
    ap.add_argument("--force", action="store_true", help=M["HELP_02"])
    args = ap.parse_args()

    root = Path(args.root)
    written = skipped = 0
    for rel_path, pages in SAMPLES:
        target = root / rel_path
        if target.exists() and not args.force:
            print(f"keep    : {rel_path} ({target.stat().st_size} bytes)")
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = build_pdf(pages)
        target.write_bytes(data)
        print(f"write   : {rel_path} ({len(data)} bytes, {len(pages)} page(s))")
        written += 1

    print(f"\ndone    : wrote {written}, kept {skipped} under {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
