"""对账：以 PG 为准覆盖向量库（详细设计.md §4.2 / §4.3）。.

验收原句是"同步失败可对账修复"。这句话要成立，得逐条证明四件事：

1. **能看见**：四处典型漂移（状态错、点丢了、孤儿点、版本号错）dry-run
   全都能列出。看不见的漂移等于没发生，所以“能看见”本身是一条断言。
2. **能修好**：修完再对一次账必须干净（不是“修了一次不报错”，是“再查一遍
   没有差异”）。
3. **补点不依赖编码服务**：PG 里有 embedding_dense 时直接补，TEI 挂了也能修。
4. **修不了的不假装修好**：PG 没有向量的切片、版本号错乱这两类会留在 remaining
   里并记进漂移账——`remaining == 0` 才算真的干净。
"""

from __future__ import annotations

import pytest

from api.ingest.flip import flip
from api.ingest.reclaim import reclaim
from api.ingest.reconcile import diff, reconcile
from api.ingest.sync import drain, pending_count, record_drift, task_for
from api.ingest.vectors import QdrantVectorStore, index_version
from api.tests.a3.conftest import (  # 只 import 辅助函数；fixtures 自动发现
    chunk_rows,
    doc_key,
    seed_document,
)

pytestmark = pytest.mark.integration


def _seed_active(conn, store, embedder, *, pool, key: str) -> tuple[str, str]:
    """建一个已上线**且已同步完**的版本，返回 (version_id, doc_key)。.

    翻牌之后必须跑一次 drain：翻牌只写“期望状态”，Qdrant 里的 payload
    还是 pending。不 drain 就做对账，第一条断言会先撞上这处真实的
    pending→active 差异——那是翻牌的正常中间态，不是本用例要验的漂移。
    """
    v1 = seed_document(conn, key=key, version_tag="v1")
    conn.commit()
    index_version(conn, store, embedder, v1)
    flip(conn, v1)
    conn.commit()
    drain(pool, store, max_attempts=4, backoff_s=0.0)
    return v1, doc_key(key)


# --- 1. 能看见
# -----------------------------------------------------------------


def test_dry_run_reports_status_mismatch_without_touching_anything(
    conn, store, embedder, pool
) -> None:
    """状态漂移：dry-run 必须列出来，而且**一个字节都不改**。."""
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="dryrun")
    # 直接改 Qdrant 的 payload：模拟同步漏掉一条
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    store.set_status_by_chunk_ids([chunk_id], "deprecated")

    report = diff(conn, store, doc_key=dk)
    assert report.dry_run is True
    assert report.by_kind() == {"status_mismatch": 1}
    drift = report.drifts[0]
    assert drift.chunk_id == chunk_id
    assert drift.pg_value == "active"
    assert drift.qdrant_value == "deprecated"
    assert not drift.fixed

    # dry-run 不改 Qdrant
    assert store.count_version(v1, status="deprecated") == 1


def test_dry_run_reports_missing_point(conn, store, embedder, pool) -> None:
    """点丢了（被误删）也要能看见。."""
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="missing")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    from scripts.qdrant_common import point_id_for

    store.delete_points([point_id_for(chunk_id)])

    report = diff(conn, store, doc_key=dk)
    assert report.by_kind() == {"missing_point": 1}
    assert report.drifts[0].chunk_id == chunk_id


def test_dry_run_reports_orphan_point(conn, store, embedder, pool) -> None:
    """Qdrant 有点、PG 没有（切片被删/版本被回收）→ 孤儿。."""
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="orphan")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    # 只删 PG 行（绕过回收流程，模拟"删库行时向量库不可用，点留在那里"）
    conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
    conn.commit()

    report = diff(conn, store, doc_key=dk)
    assert report.by_kind() == {"orphan_point": 1}
    assert report.drifts[0].chunk_id == chunk_id
    assert report.drifts[
        0
    ].point_id, "orphan drift must carry the qdrant point id"


def test_dry_run_reports_version_mismatch(conn, store, embedder, pool) -> None:
    """payload.version_id 与 PG 不一致要能看见。.

    **不自动修**：版本号错乱属于数据事故。
    """
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="vermismatch")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    from scripts.qdrant_common import point_id_for

    store._call(
        "POST",
        f"/collections/{store.collection}/points/payload?wait=true",
        {
            "payload": {"version_id": "a3test/vermismatch#bogus"},
            "points": [point_id_for(chunk_id)],
        },
    )

    report = diff(conn, store, doc_key=dk)
    assert "version_mismatch" in report.by_kind()

    healed = reconcile(conn, store, doc_key=dk)
    conn.commit()
    # version_mismatch 不该被"顺手修好"
    assert any(d.kind == "version_mismatch" for d in healed.remaining)
    assert not any(
        d.kind == "version_mismatch" and d.fixed for d in healed.drifts
    )


# --- 2. 能修好
# -----------------------------------------------------------------


def test_reconcile_heals_status_mismatch(conn, store, embedder, pool) -> None:
    """Run test_reconcile_heals_status_mismatch."""
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="healstatus")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    store.set_status_by_chunk_ids([chunk_id], "deprecated")

    report = reconcile(conn, store, doc_key=dk)
    conn.commit()

    assert report.by_kind() == {"status_mismatch": 1}
    assert len(report.healed) == 1
    assert not report.remaining
    assert store.count_version(v1, status="active") == 2
    # 再对一次账必须干净（这才是"修好了"的定义）
    assert not diff(conn, store, doc_key=dk).drifts


def test_reconcile_heals_missing_point_without_calling_embedding(
    conn, store, embedder, pool, monkeypatch
) -> None:
    """补点用 PG 里已存的 embedding_dense，**不调编码服务**。.

    这条很实际：编码服务挂了的时候，"向量库丢了点"仍然可修（§4.2 的分工）。
    """
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="healmiss")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    from scripts.qdrant_common import point_id_for

    store.delete_points([point_id_for(chunk_id)])

    def _boom(*_args, **_kwargs):
        """Run _boom."""
        raise AssertionError("reconcile must not call the embedding service")

    monkeypatch.setattr(QdrantVectorStore, "upsert", QdrantVectorStore.upsert)
    monkeypatch.setattr("api.ingest.vectors.Embedder.embed", _boom)

    report = reconcile(conn, store, doc_key=dk)
    conn.commit()

    assert report.by_kind() == {"missing_point": 1}
    assert not report.remaining, report.as_dict()
    assert store.count_version(v1) == 2
    assert not diff(conn, store, doc_key=dk).drifts


def test_reconcile_heals_orphan_point(conn, store, embedder, pool) -> None:
    """Run test_reconcile_heals_orphan_point."""
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="healorphan")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
    conn.commit()
    before = store.count_version(v1)
    assert before == 2

    report = reconcile(conn, store, doc_key=dk)
    conn.commit()

    assert report.by_kind() == {"orphan_point": 1}
    assert not report.remaining
    assert store.count_version(v1) == 1
    assert not diff(conn, store, doc_key=dk).drifts


def test_reconcile_report_is_json_safe(conn, store, embedder, pool) -> None:
    """报告要能直接进 JSON（运维脚本/日志都要它）。."""
    import json

    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="jsonsafe")
    report = diff(conn, store, doc_key=dk)
    text = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "drift_counts" in text


# --- 3. 修不了的不假装修好 ----------------------------------------------------


def test_missing_dense_vector_stays_unfixed_with_a_reason(
    conn, store, embedder, pool
) -> None:
    """PG 里没有向量、Qdrant 又丢了点：修不了，必须留在 remaining 并说明原因。.

    不假装修好是关键：`remaining == 0` 是"真的干净"的判据，如果这里悄悄放过，
    线上就永远有一个召不回的切片而没人知道。
    """
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="nodense")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    from scripts.qdrant_common import point_id_for

    store.delete_points([point_id_for(chunk_id)])
    conn.execute(
        "UPDATE chunks SET embedding_dense = NULL WHERE chunk_id = %s",
        (chunk_id,),
    )
    conn.commit()

    report = reconcile(conn, store, doc_key=dk)
    conn.commit()

    assert report.by_kind() == {"missing_point": 1}
    drift = report.remaining[0]
    assert "embedding_dense" in (drift.fix_error or "")
    # 漂移账上要有一条 open 记录（否则没人会知道）
    open_rows = conn.execute(
        "SELECT count(*) FROM vector_drift_log "
        "WHERE NOT fixed AND chunk_id = %s",
        (chunk_id,),
    ).fetchone()[0]
    assert open_rows == 1


def test_drift_log_records_and_clears(conn, store, embedder, pool) -> None:
    """修好之后，漂移账上那条 open 记录要被销账（不是留一堆假装还在漂）。."""
    v1, dk = _seed_active(conn, store, embedder, pool=pool, key="clearlog")
    chunk_id = sorted(chunk_rows(conn, v1))[0]
    store.set_status_by_chunk_ids([chunk_id], "deprecated")

    # 先记一条 open（模拟上一轮对账看见了但没修成）
    record_drift(
        conn,
        doc_key=dk,
        chunk_id=chunk_id,
        version_id=v1,
        kind="status_mismatch",
        pg_value="active",
        qdrant_value="deprecated",
        dedupe_key=f"status_mismatch:{chunk_id}",
    )
    conn.commit()
    assert (
        conn.execute(
            "SELECT count(*) FROM vector_drift_log WHERE NOT fixed"
        ).fetchone()[0]
        == 1
    )

    reconcile(conn, store, doc_key=dk)
    conn.commit()

    assert (
        conn.execute(
            "SELECT count(*) FROM vector_drift_log WHERE NOT fixed"
        ).fetchone()[0]
        == 0
    )


# --- 4. 回收（§4.3）----------------------------------------------------------


def test_reclaim_purges_only_versions_past_the_window(
    conn, store, embedder, pool, tmp_path
) -> None:
    """24h 窗口：刚下线的版本不删，过窗口的才删。.

    这是回滚门的实体：窗口内还能翻回来（§4.4），过窗口只能重跑。
    """
    v1 = seed_document(
        conn, key="reclaim", version_tag="v1", version_status="active"
    )
    conn.commit()
    index_version(conn, store, embedder, v1)
    flip_target = seed_document(conn, key="reclaim", version_tag="v2")
    conn.commit()
    index_version(conn, store, embedder, flip_target)
    flip(conn, flip_target)  # v1 -> deprecated(deprecated_at=now)
    conn.commit()
    drain(pool, store, max_attempts=4, backoff_s=0.0)

    # 窗口内：一个都不删
    report = reclaim(
        conn,
        retention_hours=24,
        figures_dir=tmp_path,
        doc_key=doc_key("reclaim"),
    )
    conn.commit()
    assert report.purged == 0
    assert (
        conn.execute(
            "SELECT status FROM doc_versions WHERE version_id = %s", (v1,)
        ).fetchone()[0]
        == "deprecated"
    )

    # 把下线时间推到 25 小时前
    conn.execute(
        "UPDATE doc_versions SET deprecated_at = now() - interval '25 hours' "
        "WHERE version_id = %s",
        (v1,),
    )
    conn.commit()

    report = reclaim(
        conn,
        retention_hours=24,
        figures_dir=tmp_path,
        doc_key=doc_key("reclaim"),
    )
    conn.commit()
    assert report.purged == 1
    assert report.items[0].version_id == v1
    assert report.items[0].chunks == 2

    assert (
        conn.execute(
            "SELECT status FROM doc_versions WHERE version_id = %s", (v1,)
        ).fetchone()[0]
        == "purged"
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM chunks WHERE version_id = %s", (v1,)
        ).fetchone()[0]
        == 0
    )
    # 新 active 完全不受影响
    assert (
        conn.execute(
            "SELECT status FROM doc_versions WHERE version_id = %s",
            (flip_target,),
        ).fetchone()[0]
        == "active"
    )

    # 点由同步器补删（§4.4「先删库行，向量点进待删清单，恢复后补删」）
    assert task_for(conn, v1)["desired_status"] == "purged"
    drain(pool, store, max_attempts=4, backoff_s=0.0)
    assert store.count_version(v1) == 0
    assert pending_count(conn) == 0
    assert not diff(conn, store, doc_key=doc_key("reclaim")).drifts


def test_reclaim_backfills_unknown_deprecated_at_instead_of_deleting(
    conn, store, embedder, tmp_path
) -> None:
    """`deprecated_at IS NULL` 的行：补时间戳并跳过本轮，绝不"立刻删"。.

    这类行是别的模块直接改状态留下的。按"刚下线"处理最多晚一轮，按"已过期"处理
    会删掉一个还能回滚的版本——两者代价不对等。
    """
    v1 = seed_document(conn, key="unknown", version_tag="v1")
    conn.commit()
    # 直接置 deprecated 且不带时间戳（模拟别的模块/旧数据）
    conn.execute(
        "UPDATE doc_versions SET status = 'deprecated', activated_at = now() "
        "WHERE version_id = %s",
        (v1,),
    )
    conn.commit()

    report = reclaim(
        conn,
        retention_hours=0,
        figures_dir=tmp_path,
        doc_key=doc_key("unknown"),
    )
    conn.commit()

    assert (
        report.purged == 0
    ), "unknown deprecation time must not be deleted immediately"
    assert report.skipped_unknown_time == 1
    row = conn.execute(
        "SELECT status, deprecated_at FROM doc_versions WHERE version_id = %s",
        (v1,),
    ).fetchone()
    assert row[0] == "deprecated"
    assert (
        row[1] is not None
    ), "deprecated_at must be backfilled so the window can start"


def test_reclaim_dry_run_touches_nothing(
    conn, store, embedder, pool, tmp_path
) -> None:
    """Run test_reclaim_dry_run_touches_nothing."""
    v1 = seed_document(
        conn, key="dryreclaim", version_tag="v1", version_status="active"
    )
    conn.commit()
    index_version(conn, store, embedder, v1)
    v2 = seed_document(conn, key="dryreclaim", version_tag="v2")
    conn.commit()
    index_version(conn, store, embedder, v2)
    flip(conn, v2)
    conn.commit()
    conn.execute(
        "UPDATE doc_versions SET deprecated_at = now() - interval '48 hours' "
        "WHERE version_id = %s",
        (v1,),
    )
    conn.commit()

    report = reclaim(
        conn,
        retention_hours=24,
        figures_dir=tmp_path,
        doc_key=doc_key("dryreclaim"),
        dry_run=True,
    )
    conn.commit()

    assert report.purged == 1
    assert report.items[0].chunks == 2
    assert (
        conn.execute(
            "SELECT status FROM doc_versions WHERE version_id = %s", (v1,)
        ).fetchone()[0]
        == "deprecated"
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM chunks WHERE version_id = %s", (v1,)
        ).fetchone()[0]
        == 2
    )
    # 翻牌时那条 deprecated 期望还在（本来就该在），但 dry-run 不该把期望改成
    # purged
    row = task_for(conn, v1)
    assert (
        row is None or row["desired_status"] != "purged"
    ), "dry-run must not write the purged desired state"


def test_reclaim_deletes_figures(conn, store, embedder, tmp_path) -> None:
    """切片图一起清；样本原件不删（§4.3）。."""
    v1 = seed_document(conn, key="figs", version_tag="v1")
    conn.commit()
    figures = tmp_path / "figures"
    figures.mkdir()
    chunk_ids = sorted(chunk_rows(conn, v1))
    for chunk_id in chunk_ids:
        (figures / f"{chunk_id}.webp").write_bytes(b"RIFF....WEBP")

    conn.execute(
        "UPDATE doc_versions SET status = 'deprecated', activated_at = now(), "
        "deprecated_at = now() - interval '48 hours' WHERE version_id = %s",
        (v1,),
    )
    conn.commit()

    report = reclaim(
        conn, retention_hours=24, figures_dir=figures, doc_key=doc_key("figs")
    )
    conn.commit()

    assert report.purged == 1
    assert report.items[0].figures == 2
    assert not list(figures.glob("*.webp"))


def test_reclaim_only_version_requires_deprecated(conn, tmp_path) -> None:
    """定向回收也只接受 deprecated。.

    active 版本被“顺手删掉”是最坏的一种误操作。
    """
    from api.ingest.reclaim import ReclaimError

    v1 = seed_document(
        conn, key="onlyver", version_tag="v1", version_status="active"
    )
    conn.commit()
    with pytest.raises(ReclaimError, match="not deprecated"):
        reclaim(conn, only_version=v1, figures_dir=tmp_path)
    conn.rollback()
