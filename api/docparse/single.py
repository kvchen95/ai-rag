"""单文件解析入口：付费闸门 + MinerU 原始产物落盘。.

本模块只做第一步（MinerU → JSON）。语义修复、Docling 表格增强、分块与并发入口
都要等用户确认这一步产物后再接；这里刻意不提供批量接口，避免误烧 token。
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from api.docparse.mineru_client import MineruError
from api.observability import get_logger, log_event

logger = get_logger("api.docparse.single")

_FINGERPRINT: ModuleType | None = None


def _fingerprint() -> ModuleType:
    """只加载纯 stdlib 的 fingerprint.py，绕开会拉 psycopg 的 api.ingest。.

    doc_key/SHA-256 算法必须只有一份，所以不复制实现，按文件路径加载原模块。
    """
    global _FINGERPRINT
    if _FINGERPRINT is None:
        path = (
            Path(__file__).resolve().parent.parent / "ingest" / "fingerprint.py"
        )
        spec = importlib.util.spec_from_file_location(
            "_ai_rag_fingerprint", path
        )
        if spec is None or spec.loader is None:  # pragma: no cover
            raise RuntimeError(f"无法加载 {path}")
        module = importlib.util.module_from_spec(spec)
        # dataclass 在 exec 时会按 __module__ 回查 sys.modules，必须先登记。
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        # 无 pypinyin 的本机 python3 也要能 dry-run：显式接受中文 doc_key，
        # 本地目录本来就是 UTF-8 路径，不需要为指纹步骤装拼音依赖。
        if importlib.util.find_spec("pypinyin") is None:
            os.environ.setdefault("AI_RAG_ALLOW_CJK_DOC_KEY", "1")
        _FINGERPRINT = module
    return _FINGERPRINT


class GateError(ValueError):
    """付费闸门没过：还没发任何请求就拒绝。."""


class MineruLike(Protocol):
    """MineruLike."""
    def parse_local(self, path: Path, data_id: str) -> dict[str, Any]: ...


def _stdlib_page_count(path: Path) -> int:
    """不装第三方库时数 PDF 的 /Page 对象（与 pypdfium2 结果一致）。."""
    data = path.read_bytes()
    count = len(re.findall(rb"/Type\s*/Page(?!s)\b", data))
    if count == 0:
        counts = [int(value) for value in re.findall(rb"/Count\s+(\d+)", data)]
        count = max(counts) if counts else 0
    if count == 0:
        raise RuntimeError(
            f"无法从 {path} 读出页数；请安装 pypdfium2 后重试"
            "（dry-run 不能瞎猜页数）"
        )
    return count


def local_page_count(path: Path) -> int:
    """Run local_page_count."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return _stdlib_page_count(path)
    doc = pdfium.PdfDocument(str(path))
    try:
        return len(doc)
    finally:
        doc.close()


def _as_doc_key(path: Path) -> str:
    # doc_key_for 的入参必须是相对 samples/ 的路径（见其 docstring）。
    """Run _as_doc_key."""
    samples_root = (Path.cwd() / "samples").resolve()
    try:
        rel = path.resolve().relative_to(samples_root).as_posix()
    except ValueError:
        try:
            rel = path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            rel = path.name
        if rel.startswith("samples/"):
            rel = rel[len("samples/") :]
    return _fingerprint().doc_key_for(rel)


def _data_id(doc_key: str, sha256: str) -> str:
    """Run _data_id."""
    raw = f"{doc_key}_{sha256[:12]}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:128]


def _read_dotenv() -> dict[str, str]:
    """没有 python-dotenv 时也能从仓库根的 .env 读配置（只读 KEY=VALUE）。."""
    result: dict[str, str] = {}
    path = Path.cwd() / ".env"
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _default_client() -> MineruLike:
    """Run _default_client."""
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - 本机 python3 可能没装依赖
        dotenv_values = _read_dotenv  # type: ignore[assignment]
    env = {**dotenv_values(), **os.environ}
    token = env.get("MINERU_API_KEY", "").strip()
    if not token:
        raise GateError("MINERU_API_KEY 未设置：不提交任何付费任务")
    from api.docparse.mineru_client import MineruClient

    return MineruClient(
        base_url=env.get("MINERU_BASE_URL", "https://mineru.net/api/v4"),
        token=token,
        poll_interval_s=float(env.get("MINERU_POLL_INTERVAL_S", "3")),
        poll_timeout_s=float(env.get("MINERU_POLL_TIMEOUT_S", "900")),
    )


def _extract_content_list(zip_bytes: bytes) -> Any:
    """Run _extract_content_list."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.endswith("content_list.json") or name == "content_list.json"
        ]
        if not names:
            raise RuntimeError("MinerU ZIP 里没有 content_list.json")
        return json.loads(archive.read(names[0]).decode("utf-8"))


def _download_with_retry(
    client: MineruLike, url: str, attempts: int = 3
) -> bytes:
    """下载失败只重试下载，绝不重提交任务。."""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.download(url)
        except Exception as exc:  # noqa: BLE001 - 网络抖动/SSL EOF 都算可重试
            last = exc
            if attempt < attempts:
                time.sleep(min(2**attempt, 8))
    assert last is not None
    raise MineruError(f"下载失败（已重试 {attempts} 次）：{last}")


def _write_manifest(target: Path, manifest: dict[str, Any]) -> None:
    """Run _write_manifest."""
    (target / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _resume_single(
    target: Path,
    *,
    client: MineruLike | None,
    resume_batch_id: str | None,
    resume_zip_url: str | None,
    poll_interval_s: float,
    poll_timeout_s: float,
) -> dict[str, Any]:
    """Run _resume_single."""
    if not target.is_dir():
        raise FileNotFoundError(f"输出目录不存在：{target}")
    task_path = target / "mineru_task.json"
    task: dict[str, Any] = {}
    if task_path.exists():
        task = json.loads(task_path.read_text(encoding="utf-8"))
    batch_id = resume_batch_id or task.get("batch_id")
    if not batch_id:
        raise GateError(
            "恢复下载需要 batch_id（--batch-id 或目录内 mineru_task.json）"
        )
    paid = client or _default_client()
    paid.poll_interval_s = poll_interval_s
    paid.poll_timeout_s = poll_timeout_s

    zip_url = resume_zip_url or task.get("zip_url")
    if not zip_url:
        zip_url = paid.wait_result(str(batch_id))
        task.update({"zip_url": zip_url, "state": "ZIP_READY"})
        task_path.write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    try:
        zip_bytes = _download_with_retry(paid, str(zip_url))
    except Exception as exc:
        manifest_path = target / "manifest.json"
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "state": "MINERU_DOWNLOAD_FAILED",
                "batch_id": batch_id,
                "zip_url": zip_url,
                "errors": [str(exc)],
            }
        )
        _write_manifest(target, manifest)
        raise

    (target / "01_mineru_raw.zip").write_bytes(zip_bytes)
    blocks = _extract_content_list(zip_bytes)
    (target / "01_mineru_raw.json").write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "state": "MINERU_DONE",
        "stage_states": {"01_mineru": "done"},
        "batch_id": batch_id,
        "zip_url": zip_url,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "warnings": [],
        "errors": [],
    }
    _write_manifest(target, manifest)
    (target / "mineru_task.json").write_text(
        json.dumps(
            {"batch_id": batch_id, "zip_url": zip_url, "state": "DONE"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "output_dir": str(target),
        "batch_id": batch_id,
        "spent_tokens": False,
        "resumed": True,
    }


def run_single(
    path: Path | str,
    *,
    max_pages: int | None,
    out_root: Path | str = Path("data/parse"),
    dry_run: bool = False,
    client: MineruLike | None = None,
    resume_from: Path | str | None = None,
    resume_batch_id: str | None = None,
    resume_zip_url: str | None = None,
    poll_interval_s: float = 3.0,
    poll_timeout_s: float = 900.0,
) -> dict[str, Any]:
    """Run run_single."""
    if resume_from is not None:
        return _resume_single(
            Path(resume_from),
            client=client,
            resume_batch_id=resume_batch_id,
            resume_zip_url=resume_zip_url,
            poll_interval_s=poll_interval_s,
            poll_timeout_s=poll_timeout_s,
        )

    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"文件不存在：{source}")

    page_count = local_page_count(source)
    sha256 = _fingerprint().file_hash(source)
    doc_key = _as_doc_key(source)
    version_id = _fingerprint().version_id_for(doc_key, sha256)
    # version_id 形如 `doc_key#hash`；目录只用 hash 段，避免重复拼一层 doc_key。
    version_dir = version_id.split("#", 1)[-1]
    target = Path(out_root) / doc_key / version_dir

    summary: dict[str, Any] = {
        "file": str(source),
        "doc_key": doc_key,
        "version_id": version_id,
        "page_count": page_count,
        "output_dir": str(target),
        "spent_tokens": False,
    }
    if dry_run:
        target.mkdir(parents=True, exist_ok=True)
        preview = target / "dry_run.json"
        preview.write_text(
            json.dumps(
                {
                    **summary,
                    "kind": "dry_run",
                    "note": (
                        "预检：未调用 MinerU，未产生解析结果；"
                        "去掉 DRY_RUN=1 才会提交付费任务"
                    ),
                    "planned_files": [
                        "01_mineru_raw.zip",
                        "01_mineru_raw.json",
                        "manifest.json",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log_event(
            logger, "docparse_dry_run", page_count=page_count, doc_key=doc_key
        )
        summary["preview_file"] = str(preview)
        return summary

    # 付费闸门：先看页数上限，再构造客户端/发请求。
    if max_pages is None:
        raise GateError("必须显式给 --max-pages，才允许提交付费解析任务")
    if page_count > max_pages:
        raise GateError(
            f"页数 {page_count} 超过 --max-pages {max_pages}，未提交任务"
        )

    paid = client or _default_client()
    batch_id = paid.submit_local(source, _data_id(doc_key, sha256))

    target.mkdir(parents=True, exist_ok=True)
    (target / "mineru_task.json").write_text(
        json.dumps(
            {"batch_id": batch_id, "zip_url": None, "state": "SUBMITTED"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    zip_url = paid.wait_result(batch_id)
    (target / "mineru_task.json").write_text(
        json.dumps(
            {"batch_id": batch_id, "zip_url": zip_url, "state": "ZIP_READY"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    try:
        zip_bytes = _download_with_retry(paid, zip_url)
    except Exception as exc:  # noqa: BLE001
        _write_manifest(
            target,
            {
                "state": "MINERU_DOWNLOAD_FAILED",
                "batch_id": batch_id,
                "zip_url": zip_url,
                "errors": [str(exc)],
            },
        )
        (target / "mineru_task.json").write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "zip_url": zip_url,
                    "state": "DOWNLOAD_FAILED",
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise

    (target / "01_mineru_raw.zip").write_bytes(zip_bytes)
    blocks = _extract_content_list(zip_bytes)
    (target / "01_mineru_raw.json").write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest = {
        "state": "MINERU_DONE",
        "stage_states": {"01_mineru": "done"},
        "source_sha256": sha256,
        "doc_key": doc_key,
        "version_id": version_id,
        "page_count": page_count,
        "batch_id": batch_id,
        "zip_url": zip_url,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "warnings": [],
        "errors": [],
    }
    _write_manifest(target, manifest)
    (target / "mineru_task.json").write_text(
        json.dumps(
            {"batch_id": batch_id, "zip_url": zip_url, "state": "DONE"},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log_event(logger, "docparse_mineru_done", doc_key=doc_key, pages=page_count)
    summary.update({"spent_tokens": True, "batch_id": batch_id})
    return summary


__all__ = ["GateError", "run_single", "local_page_count"]
