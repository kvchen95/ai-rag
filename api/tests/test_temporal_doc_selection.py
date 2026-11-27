from api.temporal.documents import select_documents

DOCS = [
    {
        "doc_key": "yu-shu-q3",
        "company": "宇树科技",
        "report_type": "quarterly",
        "report_period_start": "2026-07-01",
        "report_period_end": "2026-09-30",
        "filename": "宇树科技2026三季报.pdf",
    },
    {
        "doc_key": "yu-shu-h1",
        "company": "宇树科技",
        "report_type": "interim",
        "report_period_start": "2026-01-01",
        "report_period_end": "2026-06-30",
        "filename": "宇树科技2026半年报.pdf",
    },
    {
        "doc_key": "changxin-2030",
        "company": "长鑫科技",
        "report_type": "other",
        "report_period_start": "2030-01-01",
        "report_period_end": "2030-12-31",
        "filename": "长鑫科技2030预测.pdf",
    },
]


def test_q3_only_selects_q3():
    selected = select_documents(
        "宇树科技 2026 Q3 营业收入",
        DOCS,
        date_from=20260701,
        date_to=20260930,
    )
    assert selected == ("yu-shu-q3",)


def test_half_year_does_not_select_quarterly():
    selected = select_documents(
        "宇树科技 2026 年上半年营业收入",
        DOCS,
        date_from=20260101,
        date_to=20260630,
    )
    assert "yu-shu-q3" not in selected
    assert selected == ("yu-shu-h1",)


def test_2030_without_structured_target_is_treated_as_document_year():
    # 没有结构化 target_time 时，问句里的 2030 只能按文档年份处理；
    # 不再用“预测/市场规模”关键词猜目标年份。
    selected = select_documents("长鑫全球DRAM地位 + 2030市场规模预测", DOCS)
    assert "changxin-2030" in selected
