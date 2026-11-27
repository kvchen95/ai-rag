"""审计快照（§11.2 的 `score_snapshot`）必须真的装得下它声明的东西。

这个文件只钉一件事：**同名字段不许互相覆盖**。B3 的 `score_snapshot()` 里
`"rerank"` 键写了两遍，前一个（精排分数列表）被后一个（降级审计 dict）静默覆盖，
于是审计里永远看不到分数分布——而"分怎么分布"正是排查"这次为什么判成弱相关"的
第一个依据。这类错误不会报错、不会让任何断言变红，除非有一处专门钉它。
"""

from __future__ import annotations

from typing import Any

from api.query import understand
from api.retrieval.gate import GateDecision
from api.retrieval.hybrid import Candidate
from api.retrieval.pipeline import RetrievalSnapshot
from api.retrieval.rerank import RerankOutcome


def _cand(
    chunk_id: str, score: float | None, *, dense: float = 0.5
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        doc_key="policy/travel",
        version_id="policy/travel#v1",
        chunk_type="text",
        page_num=47,
        dense_score=dense,
        sparse_score=1.2,
        rerank_score=score,
        fused_score=0.9,
        fused_rank=1,
    )


def _snapshot(
    candidates: list[Candidate], *, degraded: bool = False
) -> RetrievalSnapshot:
    return RetrievalSnapshot(
        plan=understand("住宿标准是多少"),
        decision=GateDecision(
            verdict="pass", evidence=tuple(candidates), top1=0.62
        ),
        candidates=candidates,
        filter_json={"must": []},
        dense_count=2,
        sparse_count=2,
        dropped_stale=[],
        rerank=RerankOutcome(
            ordered=candidates,
            degraded=degraded,
            reason="rerank_timeout" if degraded else None,
            scored=0 if degraded else len(candidates),
        ),
        elapsed_ms=10.0,
    )


def _dict_keys_unique_in_source() -> None:
    """元判据：`score_snapshot()` 的源码里不许出现重复的字典键。

    这是唯一能在"键被覆盖"这件事**发生前**拦住它的地方——一旦字典构造完成，
    重复键在运行时已经不可见（`{"a": 1, "a": 2}` 就是一个 `{"a": 2}`）。
    """
    import ast
    import inspect

    from api.retrieval.pipeline import RetrievalSnapshot as Cls

    source = inspect.getsource(Cls.score_snapshot)
    tree = ast.parse(source.strip())
    duplicates: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        seen: set[str] = set()
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                if key.value in seen:
                    duplicates.append(key.value)
                seen.add(key.value)
    assert not duplicates, (
        "score_snapshot 里有重复的字典键（后一个会静默覆盖前一个）: "
        f"{duplicates}"
    )


def test_score_snapshot_source_has_no_duplicate_keys() -> None:
    _dict_keys_unique_in_source()


def test_score_snapshot_keeps_the_rerank_score_distribution() -> None:
    """分数分布必须留在审计里：它是"这次为什么判成弱相关"的第一个依据。"""
    snapshot = _snapshot([_cand("ch_a", 0.62), _cand("ch_b", 0.41)])
    audit: dict[str, Any] = snapshot.score_snapshot()
    assert audit["rerank_scores"] == [0.62, 0.41]
    # 降级审计单独一个键，两者共存不互相覆盖
    assert audit["rerank"]["degraded"] is False


def test_degraded_snapshot_records_both_scores_and_the_degrade_reason() -> None:
    snapshot = _snapshot(
        [_cand("ch_a", None), _cand("ch_b", None)], degraded=True
    )
    audit = snapshot.score_snapshot()
    assert audit["rerank_scores"] == [], "降级时没有分数，不能编造"
    assert audit["rerank"]["degraded"] is True
    assert audit["rerank"]["degrade"] == "rerank_timeout"


def test_score_snapshot_has_no_evidence_text() -> None:
    """§11.2：审计只存编号与分数摘要，不存完整原文。"""
    snapshot = _snapshot([_cand("ch_a", 0.62)])
    dumped = str(snapshot.score_snapshot())
    assert "ch_a" in dumped
    assert "500 元" not in dumped
