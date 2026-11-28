#!/usr/bin/env python3
"""F2 演示数据：把 `samples/` 里的制度样本灌成「可检索的生效版本」。.

为什么需要这个脚本：仓库里的 `documents` 只有扫描记录（status=queued），
`chunks` 是空的——解析与写库之间的那一步（离线 worker）还没接上。F2 要验的是
「角标点回去 → PDF 跳页 → bbox 高亮」，这条链需要**真切片、真页码、真 bbox**，
而它们只能来自真 PDF 的解析结果。所以这里手工走一遍 A2 → A3 的既有函数：

    解析（api.parse.pipeline）→ 写 chunks（pending）→ 建向量 +
    翻牌（api.ingest）

不使用测试夹具造数：切片来自 `samples/` 里的真 PDF，bbox 是解析器算出来的。
唯一的合成物是**图表切片**——本机没配 VLM，A2 的图切片产不出来（`build_figure`
需要转译说明），所以演示用的那张 WebP 是直接从 PDF 页面按 bbox 裁的，并在
`notes` 里标了 `f2_demo_fixture`，不冒充转译结果。

用法（容器内，接 compose 网络）：

    docker exec f2-dev2 python scripts/f2_demo_seed.py

退出码：0 = 两个文档都上线且切片数 > 0；1 = 任何一步失败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__:
    from .resources.f2_demo_seed_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/f2_demo_seed.py
    from resources.f2_demo_seed_msgs import MESSAGES as M

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DOC_KEYS = ("policy/travel-cn", "policy/expense-cn")
# 演示身份：财务部 staff。可见性用它验 200，用 eng 部门验 403。
ACL_DEPTS = ("fin", "hr")
ACL_ROLES = ("staff",)


def log(message: str) -> None:
    """Run log."""
    print(message, flush=True)


def main() -> int:
    """Run main."""
    parser = argparse.ArgumentParser(description="seed F2 demo corpus")
    parser.add_argument("--samples", default="samples", help="samples root")
    parser.add_argument("--figures", default="figures", help="figures dir")
    parser.add_argument(
        "--skip-figure", action="store_true", help=M["HELP_01"]
    )
    args = parser.parse_args()

    from api.clients.pg import PgDatabase
    from api.config import Settings
    from api.ingest.fingerprint import file_hash, version_id_for
    from api.ingest.flip import NotPending
    from api.ingest.pipeline import promote
    from api.ingest.vectors import Embedder, QdrantVectorStore
    from api.parse.chunking import Acl
    from api.parse.pipeline import parse_document

    settings = Settings()
    pg = PgDatabase(
        settings.database_url,
        min_size=1,
        max_size=4,
        connect_timeout_s=10,
    )
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
    acl = Acl(is_public=False, allowed_depts=ACL_DEPTS, allowed_roles=ACL_ROLES)
    samples_root = Path(args.samples)
    figures_root = Path(args.figures)

    # 与 scripts/verify_parse_pipeline.py
    # 同一套列（少了任何一列都会被六项校验拦下）。
    insert_sql = (
                     "\n"
                     "    INSERT INTO chunks (\n"
                     "        chunk_id, doc_key, version_id, chunk_type, "
                     "parent_id, page_num, bbox,\n"
                     "        image_path, content_text, table_html, table_md, "
                     "table_cells, notes,\n"
                     "        effective_date, effective_year, "
                     "date_confidence,\n"
                     "        is_public, allowed_depts, allowed_roles, "
                     "token_count, status\n"
                     "    ) VALUES (\n"
                     "        %(chunk_id)s, %(doc_key)s, %(version_id)s, "
                     "%(chunk_type)s, %(parent_id)s,\n"
                     "        %(page_num)s, %(bbox)s::jsonb, %(image_path)s, "
                     "%(content_text)s,\n"
                     "        %(table_html)s, %(table_md)s, "
                     "%(table_cells)s::jsonb, %(notes)s,\n"
                     "        %(effective_date)s, %(effective_year)s, "
                     "%(date_confidence)s,\n"
                     "        %(is_public)s, %(allowed_depts)s, "
                     "%(allowed_roles)s, %(token_count)s, 'pending'\n"
                     "    )\n"
                     "    ON CONFLICT (chunk_id) DO NOTHING\n"
                     "    "
                 )

    summary: list[dict[str, object]] = []
    for doc_key in DOC_KEYS:
        path = samples_root / f"{doc_key}.pdf"
        if not path.is_file():
            log(f"{M['LOG_01']}{path}")
            return 1
        version_id = version_id_for(doc_key, file_hash(path))
        log(f"\n[docs] {doc_key}  version={version_id}  file={path}")

        outcome = parse_document(
            path, doc_key=doc_key, version_id=version_id, acl=acl
        )
        incomplete = [c.chunk_id for c in outcome.chunks if c.missing_fields()]
        if incomplete:
            log(f"{M['LOG_02']}{incomplete[:3]}")
            return 1
        log(f"[parse] {len(outcome.chunks)} chunk(s)  {outcome.counts()}")

        with pg.connection() as conn:
            conn.execute(
                "INSERT INTO documents (doc_key, filename, status) VALUES (%s,"
                " %s, 'queued') "
                "ON CONFLICT (doc_key) DO UPDATE SET filename ="
                " EXCLUDED.filename",
                (doc_key, path.name),
            )
            conn.execute(
                "INSERT INTO doc_versions (version_id, doc_key, file_hash,"
                " status) "
                "VALUES (%s, %s, %s, 'pending') ON CONFLICT (version_id) DO"
                " NOTHING",
                (version_id, doc_key, file_hash(path)),
            )
            for chunk in outcome.chunks:
                row = chunk.as_row()
                conn.execute(
                    insert_sql,
                    {
                        **row,
                        "bbox": json.dumps(row["bbox"], ensure_ascii=False),
                        "table_cells": (
                            json.dumps(row["table_cells"], ensure_ascii=False)
                            if row["table_cells"] is not None
                            else None
                        ),
                    },
                )
            written_cursor = conn.execute(
                "SELECT count(*) AS n FROM chunks WHERE version_id = %s",
                [version_id],
            )
            written = int(written_cursor.fetchone()[0])
        log(f"[pg] chunks for version = {written}")

        try:
            report = promote(pg.pool, store, embedder, version_id)
        except NotPending:
            # 幂等：上一次已经翻过牌了。重新索引一次并把向量库状态对齐即可。
            log(M["LOG_03"])
            from api.ingest.vectors import index_version

            with pg.connection() as conn:
                index_report = index_version(conn, store, embedder, version_id)
            store.set_status_by_version(version_id, "active")
            log(f"[promote] reindex ok={index_report.ok}")
            if not index_report.ok:
                return 1
            summary.append(
                {
                    "doc_key": doc_key,
                    "version_id": version_id,
                    "chunks": written,
                }
            )
            continue
        log(
            f"[promote] ok={report.ok} "
            f"index={report.index and report.index.as_dict()}"
        )
        if not report.ok:
            log(
                f"{M['LOG_04']}"
                f"{json.dumps(report.as_dict(), ensure_ascii=False)[:800]}"
            )
            return 1
        summary.append(
            {"doc_key": doc_key, "version_id": version_id, "chunks": written}
        )

    if not args.skip_figure:
        code = _seed_figure_fixture(
            pg=pg,
            store=store,
            embedder=embedder,
            samples_root=samples_root,
            figures_root=figures_root,
            promote=promote,
            version_id_for=version_id_for,
            file_hash=file_hash,
            acl=acl,
        )
        if code != 0:
            return code

    log("\n=== summary ===")
    log(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _seed_figure_fixture(
    *,
    pg,
    store,
    embedder,
    samples_root: Path,
    figures_root: Path,
    promote,
    version_id_for,
    file_hash,
    acl,
) -> int:
    """造一张**真裁图**的图表切片，用来演示「WebP + 说明」那条展示路径。.

    切割区域取 travel-cn.pdf 第 1 页正文的 bbox（解析器给的归一化坐标），
    这样图与页面对得上，点角标看到的框与切片图是同一块。说明文字标明是演示夹具。
    """
    import pypdfium2 as pdfium

    from api.parse.figures import render_page
    from api.parse.geometry import to_pixels

    doc_key = "policy/travel-cn.pdf"
    path = samples_root / doc_key
    doc_key = "policy/travel-cn"
    version_id = version_id_for(doc_key, file_hash(path))

    with pg.connection() as conn:
        cursor = conn.execute(
            "SELECT page_num, bbox FROM chunks WHERE version_id = %s AND"
            " chunk_type = 'text' "
            "ORDER BY page_num, chunk_id LIMIT 1",
            [version_id],
        )
        row = cursor.fetchone()
    if not row:
        log(M["LOG_05"])
        return 1
    page_num = int(row[0])
    bbox = row[1][0]

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_num - 1]
        width_pt, height_pt = page.get_size()
        # 与 A2 同口径：渲染到"裁片正好 200 DPI"。
        scale = 200 / 72
        rendered = render_page(page, scale=scale)
        x0, y0, x1, y1 = to_pixels(
            bbox, int(width_pt * scale), int(height_pt * scale)
        )
        # 裁片向四周放宽一点，让图表周围的坐标轴/说明也进来。
        pad = 24
        crop = rendered.crop(
            (
                max(0, x0 - pad),
                max(0, y0 - pad),
                min(rendered.width, x1 + pad),
                min(rendered.height, y1 + pad),
            )
        )
        from api.parse.figures import RenderedFigure

        figure = RenderedFigure(
            image=crop,
            width_px=crop.width,
            height_px=crop.height,
            dpi=200.0,
            scale=scale,
        )
    finally:
        pdf.close()

    chunk_id = f"ch_fig_f2_{page_num}"
    image_path = f"{figures_root.name}/{chunk_id}.webp"
    size = figure.save_lossless_webp(figures_root / f"{chunk_id}.webp")
    log(f"\n[figure] {image_path}  {crop.width}x{crop.height}px  {size} bytes")

    with pg.connection() as conn:
        conn.execute(
            (
                "\n"
                "            INSERT INTO chunks (\n"
                "                chunk_id, doc_key, version_id, chunk_type, "
                "parent_id, page_num, bbox,\n"
                "                image_path, content_text, table_html, "
                "table_md, table_cells, notes,\n"
                "                effective_date, effective_year, "
                "date_confidence,\n"
                "                is_public, allowed_depts, allowed_roles, "
                "token_count, status\n"
                "            ) VALUES (\n"
                "                %s, %s, %s, 'figure', NULL, %s, "
                "%s::jsonb, %s, %s, NULL, NULL, NULL,\n"
                "                %s, NULL, NULL, 'inferred', false, %s, %s, "
                "NULL, 'active'\n"
                "            )\n"
                "            ON CONFLICT (chunk_id) DO UPDATE SET image_path = "
                "EXCLUDED.image_path,\n"
                "                bbox = EXCLUDED.bbox, content_text = "
                "EXCLUDED.content_text,\n"
                "                version_id = EXCLUDED.version_id, status = "
                "'active'\n"
                "            "
            ),
            (
                chunk_id,
                doc_key,
                version_id,
                page_num,
                json.dumps([bbox]),
                image_path,
                M["EXECUTE_01"],
                ["f2_demo_fixture"],
                list(ACL_DEPTS),
                list(ACL_ROLES),
            ),
        )
    # 版本已经是 active 了（正文先上线），所以这里不能走 promote（flip 会拒绝非
    # pending）。
    # 正确做法：把这一条切片直接标成
    # active、重新索引这个版本、再把向量库状态对齐。
    from api.ingest.vectors import index_version

    with pg.connection() as conn:
        report = index_version(conn, store, embedder, version_id)
    store.set_status_by_version(version_id, "active")
    log(f"[index/figure] ok={report.ok} {report.as_dict()}")
    if not report.ok:
        log(M["LOG_06"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
