"""证据包组装（详细设计.md §9.1 五条规则）。

五条规则每一条都有一个"改了它不会报错、只会让证据包悄悄变错"的方向：
合并错了 → 同段话重复引用；封顶错了 → 提示词超长被网关截断；编号错了 →
角标指向别的原文。所以这一组用例逐条钉住，并且都用**可断言的具体值**。
"""

from __future__ import annotations

from api.generate.evidence import build_evidence_pack
from api.retrieval.hybrid import Candidate
from api.retrieval.texts import ChunkText


def cand(
    chunk_id: str,
    *,
    doc_key: str = "policy/travel",
    page_num: int = 47,
    parent_id: str | None = None,
    rerank_score: float | None = 0.6,
    fused_rank: int = 1,
    chunk_type: str = "text",
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_key=doc_key,
        version_id=f"{doc_key}#v1",
        chunk_type=chunk_type,
        page_num=page_num,
        parent_id=parent_id,
        rerank_score=rerank_score,
        fused_rank=fused_rank,
    )


def text_of(
    chunk_id: str,
    body: str,
    *,
    doc_key: str = "policy/travel",
    page_num: int = 47,
    chunk_type: str = "text",
    bbox: list | None = None,
) -> ChunkText:
    return ChunkText(
        chunk_id=chunk_id,
        doc_key=doc_key,
        doc_name="差旅管理办法-2025版.pdf",
        page_num=page_num,
        bbox=[[0.1, 0.2, 0.9, 0.3]] if bbox is None else bbox,
        text=body,
        chunk_type=chunk_type,
    )


# --- 第 4 条：编号从 1 开始、按分数降序 ---------------------------------------


def test_numbers_start_at_one_and_follow_the_input_order() -> None:
    """编号是本次问答内的局部编号，顺序 = 候选顺序（= 精排分降序）。"""
    pack = build_evidence_pack(
        [cand("ch_a"), cand("ch_b", rerank_score=0.5), cand("ch_c", rerank_score=0.4)],
        {"ch_a": text_of("ch_a", "A"), "ch_b": text_of("ch_b", "B"), "ch_c": text_of("ch_c", "C")},
        max_chars=6000,
    )
    assert pack.numbers() == (1, 2, 3)
    assert [item.chunk_id for item in pack.items] == ["ch_a", "ch_b", "ch_c"]


def test_candidates_without_text_are_skipped_and_do_not_consume_a_number() -> None:
    """取不到正文的候选不能占编号：那会让角标指向空白。"""
    pack = build_evidence_pack(
        [cand("ch_a"), cand("ch_missing"), cand("ch_c")],
        {"ch_a": text_of("ch_a", "A"), "ch_c": text_of("ch_c", "C")},
        max_chars=6000,
    )
    assert [item.n for item in pack.items] == [1, 2]
    assert [item.chunk_id for item in pack.items] == ["ch_a", "ch_c"]


# --- 第 2 条：同父片合并、页码取最靠前 ---------------------------------------


def test_same_parent_chunks_merge_into_one_evidence() -> None:
    """同一父片的多条子片合并成一条，否则模型会重复引用同一段话。"""
    pack = build_evidence_pack(
        [
            cand("ch_p1", parent_id="ch_par", page_num=47, rerank_score=0.62),
            cand("ch_p2", parent_id="ch_par", page_num=47, rerank_score=0.60),
        ],
        {
            "ch_p1": text_of("ch_p1", "第一段子片。"),
            "ch_p2": text_of("ch_p2", "第二段子片。"),
        },
        max_chars=6000,
    )
    assert pack.n_items == 1
    item = pack.items[0]
    assert item.chunk_ids == ("ch_p1", "ch_p2")
    # 代表性切片是组内分数最高那条：bbox / snippet 都跟着它，否则高亮框会落错位置
    assert item.chunk_id == "ch_p1"
    assert "第一段子片。第二段子片。" == item.text


def test_merge_removes_the_known_chunk_overlap() -> None:
    """子片之间有 64 字重叠（§2.3）：拼接时必须删掉，否则同一句出现两次。"""
    head = "住宿费标准：每人每天 500 元。"
    tail = "每人每天 500 元。超出部分由个人承担。"
    pack = build_evidence_pack(
        [
            cand("ch_p1", parent_id="ch_par", rerank_score=0.62),
            cand("ch_p2", parent_id="ch_par", rerank_score=0.6),
        ],
        {"ch_p1": text_of("ch_p1", head), "ch_p2": text_of("ch_p2", tail)},
        max_chars=6000,
    )
    body = pack.items[0].text
    assert body == "住宿费标准：每人每天 500 元。超出部分由个人承担。"
    assert body.count("每人每天 500 元") == 1


def test_merged_group_page_is_the_earliest_page_not_the_best_scoring_one() -> None:
    """§9.1 第 2 条原话：页码取子片里最靠前的一页。

    代表性切片（分数最高）与页码（最靠前）可能不是同一条：这时页码跟"最靠前"，
    `chunk_id` 仍跟分数最高那条。两者的用途不同——页码决定用户翻到哪一页，
    chunk_id 决定角标回查哪一段。
    """
    pack = build_evidence_pack(
        [
            cand("ch_late", parent_id="ch_par", page_num=50, rerank_score=0.7),
            cand("ch_early", parent_id="ch_par", page_num=47, rerank_score=0.55),
        ],
        {
            "ch_late": text_of("ch_late", "第二页上的话。", page_num=50),
            "ch_early": text_of("ch_early", "第一页上的话。", page_num=47),
        },
        max_chars=6000,
    )
    item = pack.items[0]
    assert item.chunk_id == "ch_late", "代表性切片应是最佳分数的"
    assert item.page_num == 47, "页码应取最靠前的一页"
    assert item.text.startswith("第一页上的话"), "文本按页码升序拼接"


def test_chunks_without_parent_are_not_merged_together() -> None:
    """没有父片的候选各自成组：把它们拼一起等于把不相干段落合成一条证据。"""
    pack = build_evidence_pack(
        [cand("ch_a"), cand("ch_b")],
        {"ch_a": text_of("ch_a", "A 段。"), "ch_b": text_of("ch_b", "B 段。")},
        max_chars=6000,
    )
    assert pack.n_items == 2


# --- 第 3 条：封顶 6000 字，按精排分从低往高丢，不截断单条 --------------------


def test_budget_drops_the_lowest_scoring_evidence_first() -> None:
    bodies = {f"ch_{i}": text_of(f"ch_{i}", f"{i}" * 100) for i in range(4)}
    pack = build_evidence_pack(
        [
            cand("ch_0", rerank_score=0.9),
            cand("ch_1", rerank_score=0.8),
            cand("ch_2", rerank_score=0.7),
            cand("ch_3", rerank_score=0.6),
        ],
        bodies,
        # 每条约 100 字 + 来源标记约 80 字 → 只装得下两条
        max_chars=400,
    )
    assert [item.chunk_id for item in pack.items] == ["ch_0", "ch_1"]
    assert pack.truncated is True
    assert pack.dropped == ("ch_2", "ch_3"), "从最低分开始丢"


def test_single_evidence_longer_than_budget_is_kept_not_truncated() -> None:
    """不截断单条证据是硬规则：截一半的条款比没有更危险。"""
    pack = build_evidence_pack(
        [cand("ch_a")],
        {"ch_a": text_of("ch_a", "长" * 500)},
        max_chars=100,
    )
    assert pack.n_items == 1
    assert len(pack.items[0].text) == 500
    assert pack.truncated is True, "单条超预算也算截过，答案要说明只依据前 N 条"


def test_no_truncation_flag_when_everything_fits() -> None:
    pack = build_evidence_pack([cand("ch_a")], {"ch_a": text_of("ch_a", "短句。")}, max_chars=6000)
    assert pack.truncated is False
    assert pack.notice() is None, "没截就不该在答案里加一句免责声明"


def test_notice_names_the_kept_count() -> None:
    pack = build_evidence_pack(
        [cand("ch_a"), cand("ch_b", rerank_score=0.5)],
        {"ch_a": text_of("ch_a", "长" * 300), "ch_b": text_of("ch_b", "短")},
        max_chars=400,
    )
    notice = pack.notice()
    assert notice is not None
    assert f"前 {pack.n_items} 条" in notice


def test_cap_limits_the_number_of_evidence_without_marking_truncation() -> None:
    """`cap` 是"进证据包 5–8 条"的上限（§7.5），不是超长裁剪，不该置 truncated。"""
    pack = build_evidence_pack(
        [cand(f"ch_{i}", rerank_score=0.9 - i / 100) for i in range(5)],
        {f"ch_{i}": text_of(f"ch_{i}", f"正文{i}") for i in range(5)},
        max_chars=6000,
        cap=3,
    )
    assert pack.n_items == 3
    assert pack.truncated is False


def test_invalid_max_chars_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        build_evidence_pack([], {}, max_chars=0)


# --- 第 1 / 5 条：按类型取文本、来源标记 -------------------------------------


def test_table_evidence_uses_the_derived_text_not_html() -> None:
    """§9.1 第 1 条：表格取转成文本的那一版，不取 HTML。"""
    pack = build_evidence_pack(
        [cand("ch_t", chunk_type="table")],
        {"ch_t": text_of("ch_t", "| 城市 | 标准 |\n| 一线 | 600 元 |", chunk_type="table")},
        max_chars=6000,
    )
    body = pack.items[0].text
    assert "<table" not in body
    assert "| 一线 | 600 元 |" in body


def test_source_line_carries_title_page_and_chunk_id() -> None:
    """§9.1 第 5 条：每条证据带一行来源标记，供模型引用时抄。"""
    pack = build_evidence_pack([cand("ch_a")], {"ch_a": text_of("ch_a", "正文。")}, max_chars=6000)
    block = pack.items[0].as_prompt_block()
    assert block.startswith("[1] 来源：")
    assert "差旅管理办法-2025版.pdf" in block
    assert "第 47 页" in block
    assert "ch_a" in block


def test_render_joins_blocks_in_number_order() -> None:
    pack = build_evidence_pack(
        [cand("ch_a"), cand("ch_b", rerank_score=0.5)],
        {"ch_a": text_of("ch_a", "A。"), "ch_b": text_of("ch_b", "B。")},
        max_chars=6000,
    )
    rendered = pack.render()
    assert rendered.index("[1]") < rendered.index("[2]")
    assert "A。" in rendered and "B。" in rendered


# --- 附录 C.2 的 citations.items[] 字段 --------------------------------------


def test_citation_item_has_every_field_the_contract_names() -> None:
    """附录 C.2：n / doc_key / doc_name / page_num / bbox / snippet / chunk_id。"""
    pack = build_evidence_pack(
        [cand("ch_a")], {"ch_a": text_of("ch_a", "住宿费标准：每人每天 500 元。")}, max_chars=6000
    )
    item = pack.citation_items()[0]
    assert set(item) == {"n", "doc_key", "doc_name", "page_num", "bbox", "snippet", "chunk_id"}
    assert item["n"] == 1
    assert item["page_num"] == 47
    assert item["bbox"] == [[0.1, 0.2, 0.9, 0.3]]
    assert item["snippet"].startswith("住宿费标准")
    assert item["chunk_id"] == "ch_a"


def test_citation_bbox_is_a_plain_list_not_a_specialised_type() -> None:
    """bbox 必须能直接 json 序列化（它要进 SSE 与审计）。"""
    import json

    pack = build_evidence_pack(
        [cand("ch_a")],
        {"ch_a": text_of("ch_a", "x", bbox=[[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]])},
        max_chars=6000,
    )
    payload = json.dumps(pack.citation_items()[0]["bbox"])
    assert payload == "[[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]"


def test_missing_bbox_degrades_to_empty_list_not_an_error() -> None:
    """坐标缺失时前端展示页码与片段，不静默失败（附录 C.2 末段）。"""
    pack = build_evidence_pack(
        [cand("ch_a")], {"ch_a": text_of("ch_a", "无坐标的表格。", bbox=[])}, max_chars=6000
    )
    assert pack.citation_items()[0]["bbox"] == []


# --- 审计口径 -----------------------------------------------------------------


def test_audit_pack_has_no_body_text() -> None:
    """§11.2：审计只存切片编号与分数摘要，不存完整原文。"""
    pack = build_evidence_pack(
        [cand("ch_a")], {"ch_a": text_of("ch_a", "住宿费标准：每人每天 500 元。")}, max_chars=6000
    )
    dumped = str(pack.as_audit())
    assert "500" not in dumped
    assert "ch_a" in dumped
