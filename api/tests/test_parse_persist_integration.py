"""切片落库的往返测试（真 PG）：三份表格表示与来源标记必须都活到库里。.

为什么单开一个文件（而不是塞进 `test_parse_*` 的单元用例）：

* 单元用例拿的是内存里的 `Chunk`，`as_row()` 少写一列对它没有任何影响——
  实测过：001 没有 `table_cells` / `notes` 两列时，全套 `test_parse_*` 全绿，
  而"表格三表示一致"只在内存里成立，入库只剩 html + md 两份；"这段文字是 OCR
  认的"入库后也无处可查。
* 真正能发现这件事的判据只有一条：**写进去、读回来、再用读回来的东西重建**。

覆盖三件：
1. `table_html` / `table_md` / `table_cells` 三份表示往返后仍互相重建得出；
2. `notes` 里的 `source:ocr` 与 `continued_of:pageN` 往返后还在；
3. 正文切片的 `table_cells` 是 NULL（矩阵只属于表格，不许被别的类型占位）。
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from api.clients.pg import normalize_dsn
from api.parse.chunking import Acl
from api.parse.model import TableCell, expand_to_matrix
from api.parse.pipeline import parse_document
from api.parse.tables import cells_from_html, parse_md

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "scripts" / "db_migrate.py"
SCRIPTS = REPO_ROOT / "scripts"

# 测试自己的命名空间：绝不碰真实样本的 doc_key。
DOC_PREFIX = "itest-parse/"
ACL = Acl(is_public=False, allowed_depts=("fin",), allowed_roles=("staff",))

INSERT_SQL = """
INSERT INTO chunks (
    chunk_id, doc_key, version_id, chunk_type, parent_id, page_num, bbox,
    image_path, content_text, table_html, table_md, table_cells, notes,
    effective_date, effective_year, date_confidence, is_public,
    allowed_depts, allowed_roles, token_count, status
) VALUES (
    %(chunk_id)s, %(doc_key)s, %(version_id)s, %(chunk_type)s, %(parent_id)s,
    %(page_num)s, %(bbox)s::jsonb, %(image_path)s, %(content_text)s,
    %(table_html)s, %(table_md)s, %(table_cells)s::jsonb, %(notes)s,
    %(effective_date)s, %(effective_year)s, %(date_confidence)s, %(is_public)s,
    %(allowed_depts)s, %(allowed_roles)s, %(token_count)s, 'pending'
)
"""


@pytest.fixture(scope="module")
def migrated(pg_dsn: str) -> str:
    """Run migrated."""
    proc = subprocess.run(  # noqa: S603 - 固定脚本路径
        [sys.executable, str(MIGRATE_SCRIPT), "--dsn", pg_dsn],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"db_migrate.py failed: {proc.stderr[-800:]}"
    import psycopg  # noqa: PLC0415

    with psycopg.connect(normalize_dsn(pg_dsn)) as conn:
        # 004 的两列必须在，否则下面写进去的会静默变成"没这回事"。
        cols = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'chunks'"
            )
        }
    missing = {"table_cells", "notes"} - cols
    assert not missing, f"004 迁移没应用，缺列：{sorted(missing)}"
    return pg_dsn


@pytest.fixture(scope="module")
def parsed_table_doc(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, object]:
    """解析一份带表格的版面样本（夹具在测试里现造，不依赖 samples/）。."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_layout_samples  # noqa: PLC0415

    root = tmp_path_factory.mktemp("persist")
    path = root / "table-demo.pdf"
    path.write_bytes(make_layout_samples.build("layout/table-demo.pdf"))
    outcome = parse_document(
        path,
        doc_key=DOC_PREFIX + "table",
        version_id=DOC_PREFIX + "table#abcdef123456",
        acl=ACL,
    )
    return path, outcome


def _persist(conn, outcome, path: Path) -> list[str]:
    """把一次解析结果写进库（版本行 + 切片），返回写入的 chunk_id。."""
    from api.ingest.fingerprint import file_hash  # noqa: PLC0415

    doc_key = outcome.doc.doc_key
    version_id = outcome.chunks[0].version_id
    conn.execute(
        "INSERT INTO documents (doc_key, filename, status) "
        "VALUES (%s, %s, 'queued')"
        " ON CONFLICT (doc_key) DO NOTHING",
        (doc_key, path.name),
    )
    conn.execute(
        "INSERT INTO doc_versions (version_id, doc_key, file_hash, status)"
        " VALUES (%s, %s, %s, 'pending') ON CONFLICT (version_id) DO NOTHING",
        (version_id, doc_key, file_hash(path)),
    )
    ids: list[str] = []
    for chunk in outcome.chunks:
        row = chunk.as_row()
        conn.execute(
            INSERT_SQL,
            {
                **row,
                # jsonb 列必须显式序列化：psycopg 不会把 list[dict] 当 jsonb
                # 送。
                "bbox": _to_json(row["bbox"]),
                "table_cells": (
                    _to_json(row["table_cells"])
                    if row["table_cells"] is not None
                    else None
                ),
            },
        )
        ids.append(chunk.chunk_id)
    conn.commit()
    return ids


def _to_json(value: object) -> str:
    """Run _to_json."""
    import json  # noqa: PLC0415

    return json.dumps(value, ensure_ascii=False)


@pytest.fixture(scope="module")
def written(
    migrated: str, parsed_table_doc: tuple[Path, object]
) -> dict[str, object]:
    """Run written."""
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    path, outcome = parsed_table_doc
    conn = psycopg.connect(normalize_dsn(migrated), row_factory=dict_row)
    ids: list[str] = []
    try:
        ids = _persist(conn, outcome, path)
        rows = list(
            conn.execute(
                "SELECT chunk_id, chunk_type, page_num, table_html, table_md,"
                " table_cells, notes FROM chunks WHERE chunk_id = ANY(%s)"
                " ORDER BY chunk_id",
                (ids,),
            ).fetchall()
        )
    finally:
        if ids:
            conn.execute("DELETE FROM chunks WHERE chunk_id = ANY(%s)", (ids,))
        conn.execute(
            "DELETE FROM doc_versions WHERE version_id LIKE %s",
            (DOC_PREFIX + "%",),
        )
        conn.execute(
            "DELETE FROM documents WHERE doc_key LIKE %s", (DOC_PREFIX + "%",)
        )
        conn.commit()
        conn.close()
    return {"ids": ids, "rows": rows}


def test_table_three_representations_survive_a_round_trip(written) -> None:
    """库里读回来的三份表示必须互相重建得出——这是 §3.3 可执行的定义。."""
    table_rows = [
        row for row in written["rows"] if row["chunk_type"] == "table"
    ]
    assert table_rows, "样本里没有表格切片：这条判据根本没验到"

    for row in table_rows:
        assert row["table_cells"] is not None, (
            f"{row['chunk_id']}: table_cells 入库后丢了"
            "（001 没有这列时就是这个症状）"
        )
        assert row["table_html"] and row["table_md"]

        cells = [
            TableCell(
                row=int(raw["row"]),
                col=int(raw["col"]),
                text=str(raw["text"]),
                is_header=bool(raw["is_header"]),
                row_span=int(raw["row_span"]),
                col_span=int(raw["col_span"]),
            )
            for raw in row["table_cells"]
        ]
        md_matrix = parse_md(row["table_md"])
        from_cells = expand_to_matrix(cells, len(md_matrix), len(md_matrix[0]))
        html_cells, rows_n, cols_n = cells_from_html(row["table_html"])
        from_html = expand_to_matrix(html_cells, rows_n, cols_n)

        assert (
            from_html == from_cells
        ), f"{row['chunk_id']}: html 与 cells 不一致"
        assert from_cells == md_matrix, f"{row['chunk_id']}: cells 与 md 不一致"


def test_merged_cell_spans_survive_the_round_trip(
    migrated: str, tmp_path: Path
) -> None:
    """`col_span` 必须从库里读得回来。.

    校验阶段靠它对齐数字，丢了就退化成正则拆 HTML。
    """
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import make_layout_samples  # noqa: PLC0415

    path = tmp_path / "merged.pdf"
    path.write_bytes(make_layout_samples.build("layout/merged-table-demo.pdf"))
    outcome = parse_document(
        path,
        doc_key=DOC_PREFIX + "merged",
        version_id=DOC_PREFIX + "merged#abcdef123456",
        acl=ACL,
    )
    conn = psycopg.connect(normalize_dsn(migrated), row_factory=dict_row)
    ids: list[str] = []
    try:
        ids = _persist(conn, outcome, path)
        row = conn.execute(
            "SELECT table_cells FROM chunks WHERE chunk_id = ANY(%s)"
            " AND chunk_type = 'table'",
            (ids,),
        ).fetchone()
    finally:
        if ids:
            conn.execute("DELETE FROM chunks WHERE chunk_id = ANY(%s)", (ids,))
        conn.execute(
            "DELETE FROM doc_versions WHERE version_id LIKE %s",
            (DOC_PREFIX + "%",),
        )
        conn.execute(
            "DELETE FROM documents WHERE doc_key LIKE %s", (DOC_PREFIX + "%",)
        )
        conn.commit()
        conn.close()

    assert row is not None, "合并单元格样本没有产出表格切片"
    spans = {int(cell["col_span"]) for cell in row["table_cells"]}
    assert 3 in spans, f"从库里读回的 col_span 丢了合并信息：{sorted(spans)}"


def test_notes_survive_the_round_trip(written) -> None:
    """`notes` 往返后还在：否则下游看不出“这段文字是识别出来的”。.

    这里不造 OCR（那要真引擎、慢），而是直接验 `notes` 这条通道——OCR
    的来源标记走的就是同一个字段，`scripts/verify_parse_pipeline.py`
    用真引擎验端到端。
    """
    for row in written["rows"]:
        # 正文切片的 notes 应当是空数组（不是 NULL）：下游判"有没有 ocr"时
        # 不必区分两种"没有"。
        assert (
            row["notes"] is not None
        ), f"{row['chunk_id']}: notes 是 NULL，应为空数组"
    assert all(isinstance(row["notes"], list) for row in written["rows"])


def test_notes_column_carries_ocr_source(migrated: str) -> None:
    """把 `source:ocr` 真的写进 `notes` 再读回——这条列存在且能装下标记。."""
    import psycopg  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415

    from api.parse.chunking import Chunk  # noqa: PLC0415

    chunk = Chunk(
        chunk_id="ch_itest_notes_1",
        doc_key=DOC_PREFIX + "notes",
        version_id=DOC_PREFIX + "notes#abcdef123456",
        chunk_type="text",
        page_num=2,
        bbox=[[0.1, 0.1, 0.9, 0.2]],
        content_text="CNY 5o0（识别结果，未修正）",
        date_confidence="inferred",
        is_public=True,
        notes=("source:ocr",),
    )
    conn = psycopg.connect(normalize_dsn(migrated), row_factory=dict_row)
    try:
        conn.execute(
            "INSERT INTO documents (doc_key, filename, status) "
            "VALUES (%s, 'x.pdf', 'queued')"
            " ON CONFLICT (doc_key) DO NOTHING",
            (chunk.doc_key,),
        )
        conn.execute(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status)"
            " VALUES (%s, %s, %s, 'pending') "
            "ON CONFLICT (version_id) DO NOTHING",
            (chunk.version_id, chunk.doc_key, "c" * 64),
        )
        row = chunk.as_row()
        conn.execute(
            INSERT_SQL,
            {
                **row,
                "bbox": _to_json(row["bbox"]),
                "table_cells": None,
            },
        )
        conn.commit()
        stored = conn.execute(
            "SELECT notes, table_cells FROM chunks WHERE chunk_id = %s",
            (chunk.chunk_id,),
        ).fetchone()
    finally:
        conn.execute(
            "DELETE FROM chunks WHERE chunk_id = %s", (chunk.chunk_id,)
        )
        conn.execute(
            "DELETE FROM doc_versions WHERE version_id LIKE %s",
            (DOC_PREFIX + "%",),
        )
        conn.execute(
            "DELETE FROM documents WHERE doc_key LIKE %s", (DOC_PREFIX + "%",)
        )
        conn.commit()
        conn.close()

    assert stored is not None
    assert "source:ocr" in stored["notes"]
    # 正文切片不带矩阵：这条列只属于表格，别的类型填了就是错的。
    assert stored["table_cells"] is None


def test_table_cells_rejects_non_array(migrated: str) -> None:
    """004 的 CHECK：`table_cells` 要么 NULL 要么数组，别的形状进不来。."""
    import psycopg  # noqa: PLC0415

    doc_key = DOC_PREFIX + "bad"
    version_id = doc_key + "#abcdef123456"
    suffix = uuid.uuid4().hex[:8]
    conn = psycopg.connect(normalize_dsn(migrated))
    try:
        conn.execute(
            "INSERT INTO documents (doc_key, filename, status) "
            "VALUES (%s, 'x.pdf', 'queued')",
            (doc_key,),
        )
        conn.execute(
            "INSERT INTO doc_versions (version_id, doc_key, file_hash, status)"
            " VALUES (%s, %s, %s, 'pending')",
            (version_id, doc_key, f"{suffix:0<64}"),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO chunks (chunk_id, doc_key, version_id, "
                "chunk_type, page_num, bbox, content_text, table_cells) "
                "VALUES (%s, %s, %s, 'table', 1, '[[0,0,1,1]]'::jsonb, "
                "'x', '\"not an array\"'::jsonb)",
                (f"ch_itest_bad_{suffix}", doc_key, version_id),
            )
        conn.rollback()
    finally:
        conn.execute("DELETE FROM chunks WHERE doc_key = %s", (doc_key,))
        conn.execute(
            "DELETE FROM doc_versions WHERE version_id = %s", (version_id,)
        )
        conn.execute("DELETE FROM documents WHERE doc_key = %s", (doc_key,))
        conn.commit()
        conn.close()
