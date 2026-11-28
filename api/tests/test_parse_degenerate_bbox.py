"""研报零面积 bbox 回归：PDF 内嵌流被抽成零尺寸字符，不能让整份文档摄取失败。.

真实样本（华泰证券中芯国际研报）page 5 有 64 个 `size=0` 的 Helvetica 字符，
坐标全在 `(0, -0.08)`、宽高均为 0——是 PDF 内嵌流被当文字层抽出。`extract_lines`
把它们聚成一行宽高为 0 的 `Line`，最终产出 `bbox=[[0,0,0,0]]` 的切片；
`validate_chunks` 正确判它 `bbox(invalid)`，整份文档一条切片都入库不了。

这里的判据必须是"解析成功 + 无零面积 bbox + 切片数稳定"，不能放宽
`_valid_bbox`——校验器没错，错的是让它看到这种行。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from api.parse.chunking import Acl
from api.parse.pipeline import parse_document

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_KEY = (
    "pdf_reports/hua-tai-zheng-quan-zhong-xin-guo-ji-688981-shang-diao-gang"
)
VERSION_ID = DOC_KEY + "#07cf6e6f2d84"
SAMPLE = next((REPO_ROOT / "samples" / "pdf_reports").glob("*华泰*.pdf"), None)

pytestmark = pytest.mark.skipif(
    SAMPLE is None, reason="samples/pdf_reports/ 不在仓库里（华泰样本缺失）"
)

# 修复前 monkeypatch 掉校验器后共 331 条，其中恰好 1 条是零面积垃圾切片；
# 过滤掉那条之后必须稳定在 330，多删/少删都说明过滤范围不对。
EXPECTED_CHUNKS = 330


def test_huatai_sample_parses_with_no_degenerate_bbox() -> None:
    """端到端：这份样本必须能解析成功，且所有切片的 bbox 都是真矩形。."""
    assert (
        SAMPLE is not None
    ), "sample missing (pytest.skip should have caught this)"
    outcome = parse_document(
        SAMPLE,
        doc_key=DOC_KEY,
        version_id=VERSION_ID,
        acl=Acl(is_public=True),
    )

    assert outcome.chunks, "no chunks produced from the Hua-tai report"
    for chunk in outcome.chunks:
        for rect in chunk.bbox:
            x0, y0, x1, y1 = (float(v) for v in rect)
            assert (
                x1 > x0 and y1 > y0
            ), (
                f"degenerate bbox in {chunk.chunk_id} "
                f"(page {chunk.page_num}): {rect}"
            )
    # 切片数稳定：过滤的只是那一条内嵌流垃圾，不能顺带吃掉正文。
    assert (
        len(outcome.chunks) == EXPECTED_CHUNKS
    ), f"chunk count drifted: {len(outcome.chunks)} != {EXPECTED_CHUNKS}"
