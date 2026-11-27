#!/usr/bin/env python3
"""门控阈值标定：top1 分布 + 波谷搜索（详细设计.md §12.2 / §8.1）。.

阈值不拍脑袋：把"应当答"与"应当拒"的 top1 分数画在一起，找两峰之间的低谷。
这个脚本只做三件事：

1. **读分布**：从 `eval/run_eval.py` 的 JSON（或 qa_log 窗口、或一次夹具演练）
   拿每题 top1，按期望判定分成 应答题 / 拒答题 / 澄清题。
2. **算波谷**：逐 0.01 扫阈值，给 TP/FN/FP/TN、TPR/FPR、Youden J；
   另给"观测波谷区间" = 拒答题 95 分位 ~ 应答题 5 分位。
3. **拒绝在样本不足时给结论**：n 太小、top1 全空、语料只有一篇，都写进限制；
   报告不会把一次 1 篇语料的演练说成生产标定。

三种数据来源：

    # A. 读已跑完的评测报告（真接口、真语料，最常用）
    python eval/calibrate.py --report eval/reports/redline_xxx.json

    # B. 直接查 qa_log 时间窗（给已经有流量的环境用）
    python eval/calibrate.py --dsn "$DATABASE_URL" --since-minutes 60

    # C. 夹具演练：把 详细设计.md 切片入库 → 跑 50 题检索 → 算分布 → 清场
    #    验证"标定链路能不能跑通"，**不是**生产标定（语料只有 1 篇）。
    #    必须在 api 容器里跑（要 PG/Qdrant/TEI）。
    docker compose exec -T api python eval/calibrate.py --fixture

退出码：0 分析完成；2 输入不足（如 top1 全空）；1 夹具/数据源失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = REPO_ROOT / "eval" / "golden_questions_v1.jsonl"
DEFAULT_OUT_DIR = REPO_ROOT / "eval" / "reports"

ANSWERABLE = ("pass", "partial")
REFUSABLE = ("reject_none", "reject_low")
FIXTURE_DOC_KEY = "evalfix/design-doc"
FIXTURE_SOURCE = REPO_ROOT / "详细设计.md"


# ------------------------------------------------------------------------
# --- 数据模型


@dataclass
class Point:
    """一道题的一个观测点：期望/实际判定与 top1 分数。."""
    qid: str
    category: str
    expected: str
    verdict: str
    top1: float
    top1_none: bool
    reason: str = ""


def load_golden(path: Path = DEFAULT_GOLDEN) -> dict[str, dict[str, Any]]:
    """读 golden JSONL，按 id 建索引。."""
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                out[str(row["id"])] = row
    return out


def points_from_report(path: Path) -> tuple[list[Point], dict[str, Any]]:
    """从 run_eval 的 JSON 报告里取出每题一个 Point。."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    points: list[Point] = []
    for item in payload.get("questions") or []:
        top1 = item.get("top1")
        points.append(
            Point(
                qid=str(item.get("qid")),
                category=str(item.get("category")),
                expected=str(item.get("expected")),
                verdict=str(item.get("actual") or ""),
                top1=0.0 if top1 is None else float(top1),
                top1_none=top1 is None,
                reason=str((item.get("done") or {}).get("code") or ""),
            )
        )
    meta = {
        "source": f"run_eval 报告 {path}",
        "base_url": payload.get("base_url"),
        "counts": payload.get("counts"),
        "generated_at": payload.get("generated_at"),
        "red_lines": payload.get("red_lines"),
    }
    return points, meta


def points_from_qa_log(
    dsn: str, since_minutes: int, golden: dict[str, dict[str, Any]]
) -> tuple[list[Point], dict[str, Any]]:
    """从 qa_log 拉最近窗口的问答，匹配 golden 后建 Point。."""
    import psycopg

    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    sql = """
        SELECT question, gate1_verdict, reject_reason,
               (score_snapshot->>'top1')::float8
        FROM qa_log
        WHERE created_at >= now() - (%s || ' minutes')::interval
        ORDER BY id
    """
    points: list[Point] = []
    with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql, (str(int(since_minutes)),))
        for question, gate1, reject_reason, top1 in cur.fetchall():
            gold = _match_question(question, golden)
            qid = (
                gold["id"]
                if gold
                else f"adhoc:{hashlib.sha1(question.encode()).hexdigest()[:8]}"
            )
            expected = gold["expected_verdict"] if gold else "unknown"
            category = gold["category"] if gold else "adhoc"
            points.append(
                Point(
                    qid=qid,
                    category=category,
                    expected=expected,
                    verdict=str(gate1 or ""),
                    top1=0.0 if top1 is None else float(top1),
                    top1_none=top1 is None,
                    reason=str(reject_reason or ""),
                )
            )
    return points, {"source": f"qa_log 最近 {since_minutes} 分钟"}


def _match_question(
    question: str, golden: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """按问题原文在 golden 里找对应行。."""
    for row in golden.values():
        if str(row.get("question")) == question:
            return row
    return None


# --------------------------------------------------------------------------- 统
# 计


def quantile(values: Sequence[float], q: float) -> float | None:
    """线性插值分位数；空序列返回 None。."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def group_stats(points: Sequence[Point]) -> dict[str, Any]:
    """一组 Point 的样本量、缺失数与分位数统计。."""
    values = [point.top1 for point in points if not point.top1_none]
    return {
        "n": len(points),
        "n_with_score": len(values),
        "n_none": len(points) - len(values),
        "mean": (sum(values) / len(values)) if values else None,
        "p10": quantile(values, 0.10),
        "median": quantile(values, 0.50),
        "p90": quantile(values, 0.90),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def histogram(
    points: Sequence[Point], bin_width: float = 0.05
) -> list[dict[str, Any]]:
    """按固定宽度分桶，统计应答题与拒答题条数。."""
    bins = int(round(1.0 / bin_width))
    rows = []
    for index in range(bins):
        low = index * bin_width
        high = low + bin_width
        rows.append(
            {
                "low": round(low, 2),
                "high": round(high, 2),
                "answerable": sum(
                    1
                    for p in points
                    if p.expected in ANSWERABLE and low <= p.top1 < high
                ),
                "refusable": sum(
                    1
                    for p in points
                    if p.expected in REFUSABLE and low <= p.top1 < high
                ),
                "clarify": sum(
                    1
                    for p in points
                    if p.expected == "clarify" and low <= p.top1 < high
                ),
            }
        )
    return rows


def sweep(
    points: Sequence[Point],
    lo: float = 0.20,
    hi: float = 0.60,
    step: float = 0.01,
) -> list[dict[str, Any]]:
    """在阈值区间上扫描 TPR/FPR/Youden。."""
    answerable = [p for p in points if p.expected in ANSWERABLE]
    refusable = [p for p in points if p.expected in REFUSABLE]
    rows: list[dict[str, Any]] = []
    if not answerable or not refusable:
        return rows
    steps = int(round((hi - lo) / step))
    for index in range(steps + 1):
        threshold = round(lo + index * step, 2)
        tp = sum(1 for p in answerable if p.top1 >= threshold)
        fn = len(answerable) - tp
        fp = sum(1 for p in refusable if p.top1 >= threshold)
        tn = len(refusable) - fp
        tpr = tp / len(answerable)
        fpr = fp / len(refusable)
        rows.append(
            {
                "threshold": threshold,
                "tp": tp,
                "fn": fn,
                "fp": fp,
                "tn": tn,
                "tpr": round(tpr, 4),
                "fpr": round(fpr, 4),
                "youden": round(tpr - fpr, 4),
            }
        )
    return rows


def recommend(
    points: Sequence[Point], sweep_rows: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """按 Youden 与波谷给候选阈值，并判断是否与先验重合。."""
    answerable = [
        p.top1 for p in points if p.expected in ANSWERABLE and not p.top1_none
    ]
    refusable = [
        p.top1 for p in points if p.expected in REFUSABLE and not p.top1_none
    ]
    if not answerable or not refusable or not sweep_rows:
        return {
            "available": False,
            "reason": "应答题或拒答题没有有效 top1，无法标定",
        }

    best = max(sweep_rows, key=lambda row: (row["youden"], -row["threshold"]))
    # 观测波谷：拒答题 95 分位 ~ 应答题 5
    # 分位。两者交叉说明分布重叠，没有干净的低谷。
    low = quantile(refusable, 0.95)
    high = quantile(answerable, 0.05)
    overlap = bool(low is not None and high is not None and low >= high)
    return {
        "available": True,
        "youden_threshold": best["threshold"],
        "youden": best["youden"],
        "tpr": best["tpr"],
        "fpr": best["fpr"],
        "valley_low": low,
        "valley_high": high,
        "overlap": overlap,
        "valley_interval": None if overlap else [low, high],
    }


# ------------------------------------------------------------------------
# --- 夹具演练


def _sections(markdown: str) -> list[tuple[str, str]]:
    """按 `##`/`###` 切节；返回 (标题, 正文)。.

    标题进正文，否则 BM25 看不到章节词。.
    """
    lines = markdown.splitlines()
    sections: list[tuple[str, str]] = []
    title = "前言"
    body: list[str] = []
    for line in lines:
        if re.match(r"^#{2,3}\s+", line):
            if body:
                sections.append((title, "\n".join(body).strip()))
            title = line.strip("# ").strip()
            body = [line.strip()]
        else:
            body.append(line)
    if body:
        sections.append((title, "\n".join(body).strip()))
    return [(name, text) for name, text in sections if text]


def build_fixture_chunks(
    markdown: str, max_chars: int = 700
) -> list[dict[str, Any]]:
    """把设计文档按 `##`/`###` 切成夹具切片。."""
    chunks: list[dict[str, Any]] = []
    for index, (title, text) in enumerate(_sections(markdown), 1):
        paragraphs = [
            para.strip() for para in re.split(r"\n\s*\n", text) if para.strip()
        ]
        buffer: list[str] = []
        size = 0
        for para in paragraphs:
            if buffer and size + len(para) > max_chars:
                chunks.append(
                    {
                        "title": title,
                        "text": "\n".join(buffer),
                        "page_num": index,
                    }
                )
                buffer, size = [], 0
            # 单段超长就硬切，保证切片不会大到挤掉精排窗口
            for start in range(0, len(para), max_chars):
                piece = para[start : start + max_chars]
                if buffer and size + len(piece) > max_chars:
                    chunks.append(
                        {
                            "title": title,
                            "text": "\n".join(buffer),
                            "page_num": index,
                        }
                    )
                    buffer, size = [], 0
                buffer.append(piece)
                size += len(piece)
        if buffer:
            chunks.append(
                {"title": title, "text": "\n".join(buffer), "page_num": index}
            )
    return chunks


def run_fixture(args: argparse.Namespace) -> tuple[list[Point], dict[str, Any]]:
    """把设计文档切片入库 → 跑 50 题检索 → 清场。.

    用生产同一条写入路径（`index_version` + `flip`），不自己拼 Qdrant 请求：
    这样才能证明"标定链路和线上检索是同一条"。
    """
    from api.clients.pg import PgDatabase
    from api.config import Settings
    from api.identity import IdentitySnapshot
    from api.ingest.flip import flip
    from api.ingest.vectors import Embedder, QdrantVectorStore, index_version
    from api.retrieval.pipeline import build_pipeline

    dsn = args.dsn or os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("夹具演练需要 DATABASE_URL 或 --dsn")
    settings = Settings()
    pg = PgDatabase(dsn, min_size=1, max_size=4, connect_timeout_s=5)
    pg.open()
    store = QdrantVectorStore(
        base_url=settings.qdrant_url,
        collection=settings.qdrant_collection,
        api_key=settings.qdrant_api_key.get_secret_value(),
        timeout_s=settings.qdrant_timeout_s,
    )
    embedder = Embedder(
        base_url=settings.embedding_base_url,
        model=settings.embedding_model,
        dim=settings.embedding_dim,
        api_key=settings.embedding_api_key.get_secret_value(),
        timeout_s=settings.embedding_timeout_s,
    )
    markdown = FIXTURE_SOURCE.read_text(encoding="utf-8")
    doc_sha = hashlib.sha256(markdown.encode()).hexdigest()
    version_id = f"{FIXTURE_DOC_KEY}#{doc_sha[:12]}"
    chunks = build_fixture_chunks(markdown)
    baseline: dict[str, Any] = {}
    meta: dict[str, Any] = {
        "source": (
            f"夹具演练（{FIXTURE_SOURCE.name} 切片入库，"
            "生产同一条写入/检索路径）"
        ),
        "doc_key": FIXTURE_DOC_KEY,
        "version_id": version_id,
        "chunks": len(chunks),
        "golden": str(args.golden),
    }
    pipeline = build_pipeline(settings, pg)
    try:
        with pg.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM documents WHERE doc_key = %s",
                (FIXTURE_DOC_KEY,),
            )
            if int(cur.fetchone()[0]) != 0:
                raise SystemExit(
                    f"{FIXTURE_DOC_KEY} 已存在（上次演练没清干净？）。"
                    "先清场再跑，脚本拒绝在脏基线上继续。"
                )
            cur.execute(
                "INSERT INTO documents (doc_key, filename, page_count, status) "
                "VALUES (%s, %s, %s, 'parsed')",
                (
                    FIXTURE_DOC_KEY,
                    FIXTURE_SOURCE.name,
                    max(c["page_num"] for c in chunks),
                ),
            )
            cur.execute(
                "INSERT INTO doc_versions "
                "(version_id, doc_key, file_hash, status) "
                "VALUES (%s, %s, %s, 'pending')",
                (version_id, FIXTURE_DOC_KEY, doc_sha),
            )
            for index, chunk in enumerate(chunks, 1):
                chunk_id = (
                    f"ch_evfix_{index:04d}_"
                    + hashlib.sha1(chunk["text"].encode()).hexdigest()[:8]
                )
                cur.execute(
                    """
                    INSERT INTO chunks (chunk_id, doc_key, version_id,
                                        chunk_type, page_num, bbox,
                                        content_text, is_public, status)
                    VALUES (%s, %s, %s, 'text', %s,
                            '[[0.10,0.10,0.90,0.90]]'::jsonb,
                            %s, true, 'pending')
                    """,
                    (
                        chunk_id,
                        FIXTURE_DOC_KEY,
                        version_id,
                        chunk["page_num"],
                        chunk["text"],
                    ),
                )
            report = index_version(conn, store, embedder, version_id)
            if not report.ok:
                raise SystemExit(f"索引失败：{report.as_dict()}")
            flip(conn, version_id)
            meta["indexed_points"] = report.points
        store.set_status_by_version(version_id, "active")
        meta["points_active"] = store.count_version(version_id, status="active")

        rows = [
            json.loads(line)
            for line in args.golden.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        identity = IdentitySnapshot(
            user_id="calib_fixture", depts=("fin",), roles=("staff",)
        )
        points: list[Point] = []
        for row in rows:
            qid = str(row["id"])
            try:
                snapshot = pipeline.run(str(row["question"]), identity)
                top1 = snapshot.decision.top1
                points.append(
                    Point(
                        qid=qid,
                        category=str(row["category"]),
                        expected=str(row["expected_verdict"]),
                        verdict=str(snapshot.verdict),
                        top1=0.0 if top1 is None else float(top1),
                        top1_none=top1 is None,
                        reason=str(snapshot.decision.reason or ""),
                    )
                )
                print(
                    f"[fixture] {qid} verdict={snapshot.verdict} top1="
                    f"{'None' if top1 is None else f'{top1:.3f}'} "
                    f"evidence={len(snapshot.decision.evidence)}"
                )
            except Exception as exc:  # noqa: BLE001 - 一题的故障不该毁掉整轮演练
                points.append(
                    Point(
                        qid=qid,
                        category=str(row["category"]),
                        expected=str(row["expected_verdict"]),
                        verdict="error",
                        top1=0.0,
                        top1_none=True,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                )
                print(f"[fixture] {qid} ERROR {type(exc).__name__}: {exc}")
        return points, meta
    finally:
        try:
            store.delete_version_points(version_id)
        except Exception as exc:  # noqa: BLE001
            print(
                "[fixture][WARN] 删 Qdrant 点失败："
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        try:
            with pg.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vector_drift_log WHERE doc_key = %s",
                    (FIXTURE_DOC_KEY,),
                )
                cur.execute(
                    "DELETE FROM doc_versions WHERE doc_key = %s",
                    (FIXTURE_DOC_KEY,),
                )
                cur.execute(
                    "DELETE FROM documents WHERE doc_key = %s",
                    (FIXTURE_DOC_KEY,),
                )
                cur.execute(
                    "SELECT count(*) FROM chunks WHERE doc_key = %s",
                    (FIXTURE_DOC_KEY,),
                )
                baseline["chunks_left"] = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT count(*) FROM doc_versions WHERE doc_key = %s",
                    (FIXTURE_DOC_KEY,),
                )
                baseline["versions_left"] = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT count(*) FROM documents WHERE doc_key = %s",
                    (FIXTURE_DOC_KEY,),
                )
                baseline["documents_left"] = int(cur.fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            print(
                f"[fixture][WARN] PG 清场失败：{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        try:
            meta["points_left"] = store.count_version(version_id)
        except Exception:  # noqa: BLE001
            meta["points_left"] = None
        meta["cleanup"] = baseline
        pipeline.close()
        embedder.close()
        pg.close()
        print(
            f"[fixture] 清场：{json.dumps(baseline, ensure_ascii=False)} "
            f"points_left={meta.get('points_left')}"
        )


# ---------------------------------------------------------------- 合成自检


def synthetic_points() -> list[Point]:
    """自检用：两峰分开的合成分布。**这不是语料数据**，只验证波谷算法。."""
    rng = random.Random(20261124)
    points: list[Point] = []
    for index in range(120):
        value = min(1.0, max(0.0, rng.gauss(0.55, 0.08)))
        points.append(
            Point(
                qid=f"A{index:03d}",
                category="basic_table",
                expected="pass",
                verdict="pass",
                top1=value,
                top1_none=False,
            )
        )
    for index in range(120):
        value = min(1.0, max(0.0, rng.gauss(0.28, 0.07)))
        points.append(
            Point(
                qid=f"N{index:03d}",
                category="trap",
                expected="reject_none",
                verdict="reject_none",
                top1=value,
                top1_none=False,
            )
        )
    return points


# --------------------------------------------------------------------------- 报
# 告


def render_report(
    points: Sequence[Point],
    meta: dict[str, Any],
    *,
    out_md: Path,
    out_json: Path,
) -> dict[str, Any]:
    """生成标定报告（Markdown + JSON），返回 payload。."""
    answerable = [p for p in points if p.expected in ANSWERABLE]
    refusable = [p for p in points if p.expected in REFUSABLE]
    clarify = [p for p in points if p.expected == "clarify"]
    stats_answer = group_stats(answerable)
    stats_refuse = group_stats(refusable)
    stats_clarify = group_stats(clarify)
    sweep_rows = sweep(points)
    choice = recommend(points, sweep_rows)
    histogram_rows = histogram(points)

    lines: list[str] = [
        "# 门控阈值标定报告",
        "",
        "- 生成时间："
        f"{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}",
        f"- 数据来源：{meta.get('source')}",
        f"- 样本：应答题 {len(answerable)}，拒答题 {len(refusable)}，"
        f"澄清题 {len(clarify)}",
        "",
        "## 1. 一句话结论",
        "",
    ]
    if not choice.get("available"):
        lines.append(f"**不可标定**：{choice.get('reason')}。")
    else:
        lines.append(
            f"在本样本上，Youden 最优点 **{choice['youden_threshold']:.2f}**"
            f"（TPR {choice['tpr']:.0%} / FPR {choice['fpr']:.0%}）；"
            f"观测波谷 "
            + (
                f"**{choice['valley_low']:.2f}–{choice['valley_high']:.2f}**。"
                if not choice["overlap"]
                else "**不存在**（两组分布重叠，任何阈值都会同时误伤应答题"
                "与放行拒答题）。"
            )
        )
        if not choice["overlap"] and choice["valley_low"] is not None:
            inside = (
                choice["valley_low"] <= 0.38 and choice["valley_high"] >= 0.42
            )
            lines.append(
                "该区间**包含**设计先验 0.38–0.42，可作为本次候选区间。"
                if inside
                else "该区间与设计先验 0.38–0.42 **不重合**——"
                "先别改配置，先看限制一节："
                "语料/样本与生产不一致时，先验仍是默认值。"
            )
    lines += [
        "",
        "## 2. 口径",
        "",
        "- `top1` = 门控看到的精排最高分"
        "（`RetrievalSnapshot.decision.top1`，即 qa_log 的 "
        "`score_snapshot->>'top1'`）。没有候选时记 `None`，"
        "统计时按 0.0 计入并单独报 `n_none`。",
        "- 应答题 = `expected_verdict` 为 `pass`/`partial`；"
        "拒答题 = `reject_none`/`reject_low`；"
        "`clarify` 不参与二分类，单独列。",
        "- 阈值语义：`top1 < t` 预测为 `reject_none`。"
        "扫描区间 0.20–0.60，步长 0.01（覆盖两档门控带）。",
        "",
        "## 3. top1 分布",
        "",
        "| 组 | n | 有分数 | None | min | p10 | 中位 | p90 | max | 均值 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, stat in (
        ("应答题", stats_answer),
        ("拒答题", stats_refuse),
        ("澄清题", stats_clarify),
    ):

        def fmt(key: str, stat: dict[str, Any] = stat) -> str:
            """按 key 取三位小数的统计值，缺失记破折号。."""
            value = stat.get(key)
            return "—" if value is None else f"{value:.3f}"

        lines.append(
            f"| {name} | {stat['n']} | {stat['n_with_score']} | "
            f"{stat['n_none']} | "
            f"{fmt('min')} | {fmt('p10')} | {fmt('median')} | "
            f"{fmt('p90')} | {fmt('max')} | {fmt('mean')} |"
        )

    lines += ["", "### 直方图（0.05 一档）", "", "```"]
    max_count = max(
        [row["answerable"] + row["refusable"] for row in histogram_rows] or [1]
    )
    for row in histogram_rows:
        bar_a = "#" * round(row["answerable"] / max_count * 40)
        bar_r = "+" * round(row["refusable"] / max_count * 40)
        lines.append(
            f"{row['low']:.2f}-{row['high']:.2f}  应 {bar_a:<40} "
            f"{row['answerable']:>2}  拒 {bar_r:<40} {row['refusable']:>2}"
        )
    lines.append("```")
    lines.append(
        "`#` 应答题，`+` 拒答题；观察两峰之间是否有计数接近 0 的连续区间。"
    )

    lines += [
        "",
        "## 4. 阈值扫描与波谷",
        "",
        "| t | TP | FN | FP | TN | TPR | FPR | Youden |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sweep_rows:
        mark = (
            " ←"
            if choice.get("available")
            and row["threshold"] == choice["youden_threshold"]
            else ""
        )
        lines.append(
            f"| {row['threshold']:.2f}{mark} | {row['tp']} | {row['fn']} | "
            f"{row['fp']} | {row['tn']} | "
            f"{row['tpr']:.0%} | {row['fpr']:.0%} | {row['youden']:+.2f} |"
        )
    lines += [
        "",
        "## 5. 建议",
        "",
    ]
    if choice.get("available"):
        lines += [
            f"1. 候选阈值：**{choice['youden_threshold']:.2f}**"
            f"（Youden 最优；TPR {choice['tpr']:.0%}、"
            f"FPR {choice['fpr']:.0%}）。",
            "2. 若观测波谷是一个区间且与 0.38–0.42 有交集，取交集；"
            "否则**保留先验 0.38–0.42**，"
            "先把语料与样本补齐再标定。",
            "3. 每次调阈值都必须重跑 `eval/run_eval.py` 四条红线："
            "阈值下降换来的覆盖率，"
            "不得以忠实度/数值准确率下降为代价（§12.2 第四条）。",
            "4. 阈值、提示词、模型名三者同版本留档；"
            "回滚以版本为单位，不做局部回滚（§12.3）。",
        ]
    else:
        lines.append(
            "1. 先补齐能产生 top1 的数据：语料 active chunks > 0、"
            "生成模型可用，再重跑本脚本。"
        )

    lines += [
        "",
        "## 6. 限制与风险",
        "",
        "- **样本量**：二分类的 TPR/FPR 都是点估计；"
        "n<30 时一个样本就能移动 3–7 个百分点。"
        '本报告不附置信区间，因此只用于"区间候选"，不用于精确验收。',
        "- **语料代表性**：夹具演练只有一篇设计文档，且问题集是为生产语料写的；"
        "分数分布会随语料长度、切片粒度、精排模型变化。**夹具结论不能直接改为生产配置**。",
        "- **top1=None**：没有候选的题按 0.0 计入。"
        "若 `n_none` 占比高，说明问题不是阈值，"
        "而是召回/入库/权限过滤——先查那三处。",
        "- **漂移**：§12.2 明说波谷随语料漂移；"
        "每次语料大版本更新后重新跑，不沿用旧阈值。",
        "",
    ]
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {
        "schema": "eval.calibration.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "meta": meta,
        "groups": {
            "answerable": stats_answer,
            "refusable": stats_refuse,
            "clarify": stats_clarify,
        },
        "histogram": histogram_rows,
        "sweep": sweep_rows,
        "recommendation": choice,
        "points": [point.__dict__ for point in points],
    }
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """入口：按 --report/--dsn/--fixture/--selftest 选数据源出报告。."""
    parser = argparse.ArgumentParser(
        description="门控阈值标定（top1 分布 + 波谷）"
    )
    parser.add_argument("--report", type=Path, help="run_eval 产出的 JSON")
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL", ""),
        help="直接查 qa_log 时用",
    )
    parser.add_argument("--since-minutes", type=int, default=120)
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="夹具演练：设计文档切片入库→检索→清场",
    )
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="合成分布自检（验证波谷算法，不是数据）",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--name", default="")
    args = parser.parse_args(argv)

    meta: dict[str, Any] = {}
    if args.selftest:
        points = synthetic_points()
        meta = {"source": "**合成分布自检**（不是语料数据；仅验证算法）"}
    elif args.fixture:
        try:
            points, meta = run_fixture(args)
        except SystemExit:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                f"[FATAL] 夹具演练失败：{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
    elif args.report:
        points, meta = points_from_report(args.report)
    elif args.dsn:
        golden = load_golden(args.golden)
        points, meta = points_from_qa_log(args.dsn, args.since_minutes, golden)
    else:
        parser.error("需要 --report / --dsn / --fixture / --selftest 之一")

    name = args.name or (
        "calibration_selftest"
        if args.selftest
        else f"calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    )
    out_md = args.out_dir / f"{name}.md"
    out_json = args.out_dir / f"{name}.json"
    payload = render_report(points, meta, out_md=out_md, out_json=out_json)
    choice = payload["recommendation"]
    print(
        f"[calibration] 应答题 {payload['groups']['answerable']['n']} / "
        f"拒答题 {payload['groups']['refusable']['n']}"
    )
    if choice.get("available"):
        print(
            f"[calibration] Youden t={choice['youden_threshold']:.2f} "
            f"J={choice['youden']:+.3f} "
            f"波谷={choice['valley_low']}–{choice['valley_high']} "
            f"overlap={choice['overlap']}"
        )
    else:
        print(f"[calibration] 不可标定：{choice.get('reason')}")
    print(f"[report] {out_md}")
    print(f"[report] {out_json}")

    if args.selftest and choice.get("available"):
        t = choice["youden_threshold"]
        if 0.34 <= t <= 0.45:
            print(
                f"[selftest] PASS：合成的波谷（真值约 0.42）被恢复在 t={t:.2f}"
            )
            return 0
        print(
            f"[selftest] FAIL：合成波谷应在 0.34–0.45，实际 {t:.2f}",
            file=sys.stderr,
        )
        return 2
    if not choice.get("available"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
