#!/usr/bin/env python3
"""实测 A2 摄取链路：真 PDF -> 解析 -> 切片 -> 写入真 PG（待转正）。

为什么需要一个**真写库**的脚本（而不是只靠 pytest 的集成用例）：

- 集成用例验的是"每层行为"，但"这些东西到底能不能落进 `chunks` 表"是另一件事：
  `bbox` 是 jsonb、`allowed_depts` 是 text[]、`table_cells` 是 jsonb 数组，
  任何一处形状不对都会被 PG 拒——而那正是"字段缺失被校验拦截"这条验收的下游。
- 它跑**真实样本目录**（`samples/` 下的三份制度文件），不给测试专用夹具开后门。
  这样"链在路上能跑"这句话才有依据。

做四件事：

1. 扫 `samples/` 找非 `layout/` 的三份制度样本，按 `version_id_for` 算出
   `version_id`（与 A1 一致，不自己拼）。
2. 解析 + 切片，逐条 `require_complete()`。
3. 写 `chunks`（`status='pending'`，附 `ingest_jobs` 的版本归属），**在同一事务里**，
   失败整体回滚。
4. 读回并逐项断言：条数、六项随行信息、bbox 能算出原页像素坐标、表格三表示一致。

清理：脚本自己写的行在结束时删掉（`--keep` 可保留以便人工检查）。它**不动**
`documents` / `doc_versions` 的既有行，所以不会干扰 A1 的扫描状态。

用法：

    python scripts/verify_parse_pipeline.py                       # 用 .env 的 DATABASE_URL
    python scripts/verify_parse_pipeline.py --dsn postgresql://...
    python scripts/verify_parse_pipeline.py --keep                # 保留写入的行
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.parse.chunking import Acl, IncompleteChunk  # noqa: E402
from api.parse.geometry import to_pixels  # noqa: E402
from api.parse.pipeline import parse_document  # noqa: E402
from api.parse.tables import cells_from_html, parse_md  # noqa: E402
from api.parse.model import expand_to_matrix  # noqa: E402

DOC_PREFIX = "verify-parse/"
SAMPLES = ("policy/travel-cn.pdf", "policy/expense-cn.pdf", "hr/leave-2025.pdf")

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, message: str) -> bool:
    RESULTS.append((ok, message))
    print(("  PASS  " if ok else "  FAIL  ") + message)
    return ok


def resolve_dsn(arg: str | None) -> str:
    if arg:
        return normalize_dsn(arg)
    env = os.getenv("DATABASE_URL") or os.getenv("AI_RAG_TEST_DATABASE_URL")
    if env:
        return normalize_dsn(env)
    raise SystemExit("ERROR: no DSN (pass --dsn or set DATABASE_URL)")


def normalize_dsn(dsn: str) -> str:
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix) :]
    return dsn


def version_id_for(doc_key: str, path: Path) -> str:
    """与 A1 同源：`fingerprint.version_id_for(doc_key, sha256(bytes))`。"""
    from api.ingest.fingerprint import file_hash, version_id_for as build  # noqa: PLC0415

    return build(doc_key, file_hash(path))


INSERT_SQL = """
INSERT INTO chunks (
    chunk_id, doc_key, version_id, chunk_type, parent_id, page_num, bbox,
    image_path, content_text, table_html, table_md, effective_date, effective_year,
    date_confidence, is_public, allowed_depts, allowed_roles, token_count, status
) VALUES (
    %(chunk_id)s, %(doc_key)s, %(version_id)s, %(chunk_type)s, %(parent_id)s,
    %(page_num)s, %(bbox)s::jsonb, %(image_path)s, %(content_text)s,
    %(table_html)s, %(table_md)s, %(effective_date)s, %(effective_year)s,
    %(date_confidence)s, %(is_public)s, %(allowed_depts)s, %(allowed_roles)s,
    %(token_count)s, 'pending'
)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="verify the A2 parse pipeline against real PG")
    ap.add_argument("--dsn", help="postgresql:// DSN (default: $DATABASE_URL)")
    ap.add_argument("--keep", action="store_true", help="keep the rows written by this script")
    ap.add_argument("--samples", default=str(REPO_ROOT / "samples"), help="samples root")
    args = ap.parse_args()

    import psycopg  # noqa: PLC0415

    dsn = resolve_dsn(args.dsn)
    samples_root = Path(args.samples)
    print(f"target : {dsn.split('@')[-1]}")
    print(f"samples: {samples_root}")

    written_ids: list[str] = []
    acl = Acl(is_public=False, allowed_depts=("fin", "hr"), allowed_roles=("staff",))

    with psycopg.connect(dsn, autocommit=False) as conn:
        # --- 前置：三份样本必须存在 ---
        print("\n[1] 样本与版本号")
        prepared: list[tuple[Path, str, str]] = []
        for rel in SAMPLES:
            path = samples_root / rel
            if not check(path.is_file(), f"sample exists: {rel}"):
                continue
            doc_key = DOC_PREFIX + rel.replace("/", "-").removesuffix(".pdf")
            prepared.append((path, doc_key, version_id_for(doc_key, path)))
            print(f"         {doc_key} -> {version_id_for(doc_key, path)}")
        if not prepared:
            return report()

        # --- 解析 + 切片 ---
        print("\n[2] 解析与切片（每片必须带齐六项随行信息）")
        all_chunks = []
        for path, doc_key, version_id in prepared:
            outcome = parse_document(path, doc_key=doc_key, version_id=version_id, acl=acl)
            counts = outcome.counts()
            check(
                len(outcome.chunks) > 0,
                f"{path.name}: {len(outcome.chunks)} chunk(s) {counts}",
            )
            incomplete = [c.chunk_id for c in outcome.chunks if c.missing_fields()]
            check(not incomplete, f"{path.name}: every chunk carries the six fields")
            table_chunks = [c for c in outcome.chunks if c.chunk_type == "table"]
            for chunk in table_chunks:
                check(
                    _table_three_ways_agree(chunk),
                    f"{path.name}: table {chunk.chunk_id} three representations agree",
                )
            all_chunks.extend(outcome.chunks)

            # 写入：先把版本行准备好（chunks 有复合外键到 doc_versions）
            _ensure_version(conn, doc_key=doc_key, version_id=version_id, path=path)
            for chunk in outcome.chunks:
                row = chunk.as_row()
                conn.execute(
                    INSERT_SQL,
                    {**row, "bbox": json.dumps(row["bbox"], ensure_ascii=False)},
                )
                written_ids.append(chunk.chunk_id)
        conn.commit()

        # --- 读回断言 ---
        print("\n[3] 读回校验")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM chunks WHERE chunk_id = ANY(%s)", (written_ids,)
            )
            stored = cur.fetchone()[0]
        check(stored == len(written_ids), f"{stored}/{len(written_ids)} chunk(s) readable")

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM chunks
                WHERE chunk_id = ANY(%s)
                  AND page_num >= 1
                  AND jsonb_typeof(bbox) = 'array'
                  AND btrim(content_text) <> ''
                  AND version_id IS NOT NULL
                  AND (is_public OR cardinality(allowed_depts) > 0
                       OR cardinality(allowed_roles) > 0)
                """,
                (written_ids,),
            )
            complete = cur.fetchone()[0]
        check(complete == len(written_ids), f"{complete}/{len(written_ids)} rows satisfy the six fields in SQL")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM chunks WHERE chunk_id = ANY(%s) AND status <> 'pending'",
                (written_ids,),
            )
            not_pending = cur.fetchone()[0]
        check(not_pending == 0, "all rows are status=pending (翻牌是 A3 的事)")

        # bbox 能算出原页像素坐标（"可映射原页"的数值形式）
        with conn.cursor() as cur:
            cur.execute(
                "SELECT chunk_id, page_num, bbox FROM chunks WHERE chunk_id = ANY(%s) LIMIT 5",
                (written_ids,),
            )
            rows = cur.fetchall()
        pixel_ok = True
        for chunk_id, page_num, bbox in rows:
            rect = to_pixels(bbox[0], 1654, 2339)  # A4 @200 DPI
            if not (0 <= rect[0] < rect[2] <= 1654 and 0 <= rect[1] < rect[3] <= 2339):
                pixel_ok = False
                print(f"         bad pixel rect for {chunk_id}: {rect}")
        check(pixel_ok, "bbox maps to in-page pixel rectangles at 200 DPI")

        # --- 清理 ---
        if args.keep:
            print(f"\n[4] --keep: 保留 {len(written_ids)} 行（记得手动清理）")
        else:
            print("\n[4] 清理")
            conn.execute("DELETE FROM chunks WHERE chunk_id = ANY(%s)", (written_ids,))
            conn.execute(
                "DELETE FROM doc_versions WHERE version_id LIKE %s", (DOC_PREFIX + "%",)
            )
            conn.execute("DELETE FROM documents WHERE doc_key LIKE %s", (DOC_PREFIX + "%",))
            conn.commit()
            check(True, f"cleaned {len(written_ids)} chunk(s) and their version rows")

    return report()


def _ensure_version(conn, *, doc_key: str, version_id: str, path: Path) -> None:
    """chunks 有复合外键到 doc_versions：要先有一行版本。

    脚本**不动**真实的扫描结果（`documents` / `doc_versions` 里的既有行），
    所以用 `verify-parse/` 前缀的 doc_key + 一份 pending 版本。
    """
    from api.ingest.fingerprint import file_hash  # noqa: PLC0415

    conn.execute(
        "INSERT INTO documents (doc_key, filename, status) VALUES (%s, %s, 'queued') "
        "ON CONFLICT (doc_key) DO NOTHING",
        (doc_key, path.name),
    )
    conn.execute(
        "INSERT INTO doc_versions (version_id, doc_key, file_hash, status) "
        "VALUES (%s, %s, %s, 'pending') ON CONFLICT (version_id) DO NOTHING",
        (version_id, doc_key, file_hash(path)),
    )


def _table_three_ways_agree(chunk) -> bool:
    """HTML 反解 == cells 展开 == md 解析。"""
    if not (chunk.table_html and chunk.table_md and chunk.table_cells):
        return False
    from api.parse.model import TableCell  # noqa: PLC0415

    md_matrix = parse_md(chunk.table_md)
    cells = [
        TableCell(
            row=int(raw["row"]),
            col=int(raw["col"]),
            text=str(raw["text"]),
            is_header=bool(raw["is_header"]),
            row_span=int(raw["row_span"]),
            col_span=int(raw["col_span"]),
        )
        for raw in chunk.table_cells
    ]
    from_cells = expand_to_matrix(cells, len(md_matrix), len(md_matrix[0]))
    from_html_cells, rows, cols = cells_from_html(chunk.table_html)
    return expand_to_matrix(from_html_cells, rows, cols) == from_cells == md_matrix


def report() -> int:
    failures = [message for ok, message in RESULTS if not ok]
    total = len(RESULTS)
    print("\n" + "=" * 62)
    print(f"{total - len(failures)}/{total} checks passed")
    if failures:
        print("FAILURES:")
        for message in failures:
            print("  - " + message)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except IncompleteChunk as exc:
        print(f"\nFAILED: {exc}")
        sys.exit(1)
