"""单文件完整链路：MinerU -> 修复 -> Docling 表格 -> 富化 -> 分块。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from api.docparse.chunker import chunk_blocks
from api.docparse.docling_tables import enhance_tables
from api.docparse.repair import repair_blocks
from api.docparse.single import run_single as run_mineru

TableRunner = Callable[[str], list[dict[str, Any]]]


def _default_docling_runner(pdf_path: str) -> list[dict[str, Any]]:
    from api.docparse.docling_tables import _default_runner

    return _default_runner(pdf_path)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _enrich(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    headings: list[tuple[int, str]] = []
    for block in blocks:
        level = block.get("heading_level")
        text = str(block.get("text") or "").strip()
        if level is not None and text:
            headings = [item for item in headings if item[0] < level] + [(level, text)]
        enriched.append(
            {
                **block,
                "page_num": int(block.get("page_idx") or 0) + 1,
                "heading_path": " > ".join(item[1] for item in headings),
            }
        )
    return enriched


def run_full_single(
    path: Path | str = ".",
    *,
    max_pages: int | None = None,
    out_root: Path | str = Path("data/parse"),
    dry_run: bool = False,
    client: Any | None = None,
    docling_runner: TableRunner | None = None,
    chunk_splitter: Any | None = None,
    poll_interval_s: float = 3.0,
    poll_timeout_s: float = 900.0,
    from_raw_dir: Path | str | None = None,
    source_path: Path | str | None = None,
) -> dict[str, Any]:
    if from_raw_dir is not None:
        target = Path(from_raw_dir)
        if not (target / "01_mineru_raw.json").exists():
            raise FileNotFoundError(f"找不到 MinerU 原始 JSON：{target / '01_mineru_raw.json'}")
        raw = _load_json(target / "01_mineru_raw.json")
        manifest = _load_json(target / "manifest.json") if (target / "manifest.json").exists() else {}
        doc_key = str(manifest.get("doc_key") or "")
        if not doc_key:
            try:
                doc_key = target.parent.relative_to(Path(out_root) if not isinstance(out_root, Path) else out_root).as_posix()
            except ValueError:
                doc_key = target.parent.name
        version_id = str(manifest.get("version_id") or f"{doc_key}#{target.name}")
        source = Path(source_path) if source_path is not None else (Path(manifest.get("source_file", ".")) if manifest.get("source_file") else None)
        page_count = int(manifest.get("page_count") or 0)
        summary = {
            "file": str(source) if source else str(target),
            "doc_key": doc_key,
            "version_id": version_id,
            "page_count": page_count,
            "output_dir": str(target),
            "spent_tokens": False,
        }
    else:
        summary = run_mineru(
            path,
            max_pages=max_pages,
            out_root=out_root,
            dry_run=dry_run,
            client=client,
            poll_interval_s=poll_interval_s,
            poll_timeout_s=poll_timeout_s,
        )
        if dry_run:
            return summary
        target = Path(summary["output_dir"])
        raw = _load_json(target / "01_mineru_raw.json")
        doc_key = str(summary["doc_key"])
        version_id = str(summary["version_id"])
        page_count = int(summary["page_count"])
        source = Path(summary["file"])

    repaired = repair_blocks(raw)
    if page_count <= 0:
        page_count = max((int(block.get("page_idx") or 0) + 1 for block in repaired), default=0)
    _write_json(target / "02_repaired.json", repaired)

    manifest = _load_json(target / "manifest.json") if (target / "manifest.json").exists() else {}
    manifest.update({"state": "REPAIRED", "doc_key": doc_key, "page_count": page_count, "stage_states": {"01_mineru": "done", "02_repair": "done"}})
    _write_json(target / "manifest.json", manifest)

    runner = docling_runner or _default_docling_runner
    try:
        blocks, tables = enhance_tables(repaired, str(source if source and source.exists() else target), runner=runner)
    except Exception as exc:  # noqa: BLE001 - 阶段失败要落盘，不能只抛
        manifest.update({"state": "FAILED", "stage": "docling", "errors": [str(exc)]})
        _write_json(target / "manifest.json", manifest)
        raise
    _write_json(target / "03_docling_tables.json", {"tables": tables})

    enriched = _enrich(blocks)
    _write_json(target / "04_enriched.json", enriched)

    summary["page_count"] = page_count
    chunks = chunk_blocks(enriched, version_id, splitter=chunk_splitter)
    _write_json(target / "05_chunks.json", {"chunks": chunks})

    manifest.update(
        {
            "state": "READY_FOR_REVIEW",
            "stage_states": {
                "01_mineru": "done",
                "02_repair": "done",
                "03_docling": "done",
                "04_enrich": "done",
                "05_chunks": "done",
            },
            "chunk_count": len(chunks),
            "table_count": len(tables),
            "warnings": manifest.get("warnings", []),
            "errors": [],
        }
    )
    _write_json(target / "manifest.json", manifest)
    summary.update(
        {
            "state": "READY_FOR_REVIEW",
            "chunk_count": len(chunks),
            "table_count": len(tables),
            "files": [
                "01_mineru_raw.json",
                "02_repaired.json",
                "03_docling_tables.json",
                "04_enriched.json",
                "05_chunks.json",
                "manifest.json",
            ],
        }
    )
    return summary
