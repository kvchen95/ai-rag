"""Query 理解（§6）：改写、指代、时间、单多跳判定。

三类判据缺一不可：

1. **结构**：改写照做了什么、没做什么，逐条可读；
2. **不变式**：改写前后的数字集合相同、否定不反转、幂等（再跑一遍不变）；
3. **变异守卫**：把关键约束改坏，对应的用例必须红（`test_mutation_*` 用参数化的
   方式在进程内模拟"改坏"，而不是改文件——改文件那套在 `scripts/b3_mutation_check.py`）。

第 2 类是本文件的核心。改写看起来"对的"太容易了：`住宿费 → 住宿费限额` 是正确的，
`住宿费限额 → 住宿费限额限额` 是同一个规则跑在了自己的产物上——两次都不报错。
所以幂等性必须是被断言的，不是被相信的。
"""

from __future__ import annotations

from datetime import date

import pytest

from api.query import (
    NeedsClarification,
    QueryPlan,
    detect_mode,
    extract_entities,
    is_out_of_scope,
    resolve_pronoun,
    rewrite,
    understand,
)
from api.query import lexicon

NOW = date(2026, 3, 15)


# --- 词表自检 -----------------------------------------------------------------


def test_lexicon_tables_are_self_consistent() -> None:
    """词表里混进禁止项或数字，必须在启动期就炸（§6.2 的三条禁止）。"""
    assert lexicon.validate_tables() == []


def test_lexicon_validation_catches_a_forbidden_mapping() -> None:
    """变异守卫：把"上限→标准"塞进同义表，自检必须报出来。

    这条用例证明 `validate_tables()` 不是恒真函数——它是本文件的元断言。
    """
    original = dict(lexicon.SYNONYMS)
    lexicon.SYNONYMS["上限"] = "标准"
    try:
        problems = lexicon.validate_tables()
    finally:
        lexicon.SYNONYMS.clear()
        lexicon.SYNONYMS.update(original)
    assert any("命中禁止项" in item for item in problems), problems


def test_lexicon_validation_catches_a_digit_mapping() -> None:
    original = dict(lexicon.SYNONYMS)
    lexicon.SYNONYMS["差标"] = "差旅标准500"
    try:
        problems = lexicon.validate_tables()
    finally:
        lexicon.SYNONYMS.clear()
        lexicon.SYNONYMS.update(original)
    assert any("含数字" in item for item in problems), problems


# --- 改写：允许的三件事 -------------------------------------------------------


def test_acronym_is_expanded() -> None:
    assert rewrite("差标是多少")[0] == "差旅标准是多少"


def test_synonym_maps_to_the_official_wording() -> None:
    assert rewrite("住宿费是多少")[0] == "住宿费限额是多少"


def test_colloquial_turns_into_written_form() -> None:
    assert rewrite("报销能报多少")[0] == "报销额度"


def test_rewrite_trace_names_every_change() -> None:
    """§6.4 的 `rewrite_trace` 审计用：每次替换都要能说出"从什么到什么"。"""
    _, steps = rewrite("差标的住宿费")
    assert [step.as_text() for step in steps] == ["差标→差旅标准", "住宿费→住宿费限额"]
    assert {step.kind for step in steps} == {"acronym", "synonym"}


# --- 改写：三条不变式（本文件最重要的部分）-----------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "住宿费限额是多少",
        "住宿费标准是多少",
        "住宿费是多少",
        "差旅标准里的住宿费是多少",
        "住宿费和交通费分别是多少",
        "差标",
        "报销能报多少",
        "住宿费限额的报销能报多少",
    ],
)
def test_rewrite_is_idempotent(text: str) -> None:
    """再跑一遍必须不变。

    实测撞过：`住宿费限额是多少`（输入已是正式说法）会被短左值 `住宿费` 二次替换成
    `住宿费限额限额是多少`。这类错误不报错，只会让 BM25 的 bigram 频率被自己翻倍。
    """
    once, _ = rewrite(text)
    twice, _ = rewrite(once)
    assert twice == once, f"{text!r} → {once!r} → {twice!r}（改写不幂等）"


@pytest.mark.parametrize(
    "text",
    [
        "住宿费限额是多少",
        "报销能报多少",
        "2025年住宿标准",
        "差标 500 元",
        "伙食补贴每人每天 100 元",
    ],
)
def test_rewrite_never_changes_digits(text: str) -> None:
    """§6.2：不改数字。改了数字，§10.2 的数字对齐会拿改写后的数去核原文。"""
    rewritten, _ = rewrite(text)
    assert _digits(rewritten) == _digits(text)


@pytest.mark.parametrize(
    ("text", "must_keep"),
    [
        ("不含住宿的报销", "不含住宿"),
        ("不包括住宿费的补助", "不包括住宿费"),
        ("除了住宿以外的费用", "除了住宿以外"),
    ],
)
def test_negation_is_never_reversed(text: str, must_keep: str) -> None:
    """§6.1 边界表：否定句改写时不反转。"""
    rewritten, _ = rewrite(text)
    assert must_keep in rewritten, f"{text!r} → {rewritten!r} 丢掉了否定"


def test_negation_keeps_the_rest_of_the_sentence_rewritable() -> None:
    """否定只挡住它自己那一处，不该让整句都失去扩展能力。

    否则"不含住宿的差标"里的"差标"也展开不了——那是把一条正确的规则用错了范围。
    """
    rewritten, _ = rewrite("不含住宿的差标")
    assert "住宿" in rewritten and "住宿费限额" not in rewritten
    assert "差旅标准" in rewritten


def test_rewrite_does_not_cascade_across_tables() -> None:
    """跨表级联的守卫：缩写展开的产物不再被同义表二次替换。"""
    rewritten, _ = rewrite("住宿标")
    assert rewritten == "住宿费限额"
    assert "限额限额" not in rewritten


def _digits(text: str) -> list[str]:
    return [ch for ch in text if ch.isdigit()]


# --- 指代消解（§6.1）---------------------------------------------------------


def test_pronoun_is_replaced_when_exactly_one_candidate() -> None:
    replaced, step = resolve_pronoun("那它呢", ["差旅管理办法"])
    assert replaced == "差旅管理办法"
    assert step is not None and step.kind == "pronoun"


def test_pronoun_with_no_history_is_not_guessed() -> None:
    """§6.1："没出现过就不替换，直接按原话检索，宁可查不到也不猜"。"""
    with pytest.raises(NeedsClarification) as excinfo:
        resolve_pronoun("那它呢", None)
    assert excinfo.value.candidates == []


def test_pronoun_with_multiple_candidates_triggers_clarify() -> None:
    """§6.1 边界表："指代不清（多个候选）→ 不替换，触发澄清"。"""
    with pytest.raises(NeedsClarification) as excinfo:
        resolve_pronoun("这个流程要几天", ["差旅管理办法", "报销管理办法"])
    assert len(excinfo.value.candidates) == 2


def test_sentence_without_pronoun_is_untouched() -> None:
    replaced, step = resolve_pronoun("住宿费标准是多少", ["差旅管理办法"])
    assert step is None
    assert replaced == "住宿费标准是多少"


def test_negated_sentence_keeps_its_meaning_through_understand() -> None:
    """端到端：`understand` 不反转否定（§6.1）。"""
    plan = understand("不含住宿的报销怎么报", now=NOW)
    assert "不含" in plan.retrieval_text


# --- 时间（§5.3 + §6 的衔接）--------------------------------------------------


def test_time_window_comes_out_normalised() -> None:
    plan = understand("去年差旅住宿标准", now=NOW)
    assert plan.time.range is not None
    assert plan.time.range.date_from == 20250101
    assert plan.time.range.basis == "relative"


def test_assumed_year_is_flagged_in_notes() -> None:
    """§5.3：补了年份要在答案里注明，所以它必须出现在 plan 的 notes 里。"""
    plan = understand("三月的住宿标准", now=NOW)
    assert plan.time.range is not None
    assert plan.time.range.inferred is True
    assert any("2026" in note for note in plan.notes)


def test_time_word_survives_rewriting() -> None:
    """时间归一比改写先跑：词表里若有"上个月"这类词被替换掉，时间就认不出来了。"""
    plan = understand("上个月出差的餐补是多少", now=NOW)
    assert plan.time.range is not None
    assert plan.time.range.granularity == "month"


# --- 单/多跳判定（§6.3）------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["各部门的差标分别是多少", "住宿费和交通费的差异", "报销需要哪些前置审批", "这个流程对应哪一步"],
)
def test_multi_hop_markers_are_detected(text: str) -> None:
    mode, hits = detect_mode(text)
    assert mode == "multi" and hits


@pytest.mark.parametrize(
    "text",
    ["住宿费标准是多少", "去年差旅标准", "体检费用谁承担"],
)
def test_single_fact_questions_stay_single(text: str) -> None:
    mode, _ = detect_mode(text)
    assert mode == "single"


@pytest.mark.parametrize("text", ["住宿费和交通费各是多少", "住宿费以及交通费"])
def test_bare_conjunctions_alone_do_not_force_multi_hop(text: str) -> None:
    """纯连词不算多跳依据。

    实测过：把"和/与/以及"全收进标记表，几乎所有复合问句都会被判多跳，而多跳
    每跳都要独立过阈、都要花一次检索（§8.3）。判据只有一张显式标记表——
    "多查三次"的代价必须由一个明确的词触发，不能由一个连词顺手触发。
    """
    mode, _ = detect_mode(text)
    assert mode == "single", f"{text!r} 被连词判成了多跳"


def test_out_of_scope_questions_are_flagged() -> None:
    """§6.3 边界表最后一行：明显超出库范围 → 直接拒答，不检索。

    `is_out_of_scope` 返回命中的标记（用最长的那个，日志里更有信息量）。
    """
    assert is_out_of_scope("明天天气怎么样") == "明天天气"
    assert is_out_of_scope("住宿费标准是多少") is None


# R2b 根因回归：研报域"券商预测"是库内事实（明明写在研报里的预测数字），
# 不是 §6.3 那种"本系统永远没有的东西"。裸"预测"标记会把它短路成
# "超出库范围，未检索"，7 道应答题 after_dedup=0。这 7 条原题必须能走到检索。
_BROKER_FORECAST_QUESTIONS = (
    "中原证券给出的中芯国际2025年归母净利润预测是多少？",
    "东方证券对中芯国际2025-2027年归母净利润的预测分别是多少？",
    "东方证券预测的中芯国际2025年营业收入是多少（百万元）？",
    "中原证券预测的中芯国际2025年营业收入是多少（百万元）？",
    "光大证券预测的中芯国际2025年营业收入是多少（百万美元）？",
    "国信证券预测的中芯国际2025年营业收入是多少（百万美元）？",
    "多家券商对中芯国际2025年营收的预测分别是多少？口径是否一致？",
)


@pytest.mark.parametrize("question", _BROKER_FORECAST_QUESTIONS)
def test_broker_forecast_is_not_out_of_scope(question: str) -> None:
    """券商研报里的"预测"数字是本库核心内容，不能当超范围短路掉检索。

    R2b 实测：这 7 题 out_of_scope=True → detail='超出库范围，未检索'、
    filter={}、routes 0/0、after_dedup=0，与语料/时间过滤无关。
    """
    assert is_out_of_scope(question) is None, f"误命中超范围: {question}"
    assert understand(question, now=NOW).out_of_scope is False


def test_genuine_out_of_scope_still_short_circuits() -> None:
    """变异守卫：删掉"预测"标记后，真正的超范围问句仍须短路。

    用"股价"兜底的那条原用例不能退化；"预测未来股价"含"股价"仍被拦。
    """
    assert is_out_of_scope("帮我预测一下明年的股价") == "股价"
    assert understand("帮我预测一下明年的股价", now=NOW).out_of_scope is True


def test_out_of_scope_plan_short_circuits() -> None:
    plan = understand("帮我预测一下明年的股价", now=NOW)
    assert plan.out_of_scope is True


# --- 实体（供覆盖检查）--------------------------------------------------------


def test_entities_are_extracted_and_deduplicated() -> None:
    entities = extract_entities("住宿费和交通费分别是多少")
    assert set(entities) == {"住宿费", "交通费"}


def test_longer_term_wins_over_its_own_prefix() -> None:
    """`出差补助` 与 `补助` 同时命中时只留长的：短的会让覆盖检查以为有两个实体，
    其中一个必然在证据里找不到，于是永远判"未覆盖"。"""
    entities = extract_entities("出差补助标准")
    assert "出差补助" not in entities  # 词表里没有它，但"补助"应被留下
    assert entities == ("补助",) or entities == ("出差", "补助")


def test_entities_empty_when_no_domain_term() -> None:
    assert extract_entities("这个怎么弄") == ()


# --- 出口字段（§6.4）---------------------------------------------------------


def test_plan_carries_every_field_the_next_stage_needs() -> None:
    plan = understand("去年差标的住宿费", previous_entities=None, now=NOW)
    assert isinstance(plan, QueryPlan)
    assert plan.retrieval_text and plan.original  # 改写产物与原文都在
    assert plan.entities
    assert plan.time.range is not None
    assert plan.mode in ("single", "multi")
    assert plan.trace


def test_original_is_never_the_rewritten_text() -> None:
    """改写不参与事实认定（§6.2），所以原文必须原样保留。

    这条看似显然，但把 `retrieval_text` 直接当 `original` 是最省事的写法，
    而它的后果是用户看到"住宿费限额"而不是自己问的那句话。
    """
    plan = understand("差标是多少", now=NOW)
    assert plan.original == "差标是多少"
    assert plan.retrieval_text == "差旅标准是多少"


def test_audit_snapshot_has_no_raw_question() -> None:
    """审计快照里不放原文——原文由 `QaLogEntry.question` 单独存一份。"""
    plan = understand("差标是多少", now=NOW)
    payload = plan.as_audit()
    assert "original" not in payload
    assert payload["retrieval_text"] == "差旅标准是多少"


def test_blank_question_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        understand("   ")
