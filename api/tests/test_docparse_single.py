"""单文件解析入口：付费闸门与阶段产物（不花真实 API token）。."""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path

import pytest

from api.docparse.cli import build_parser
from api.docparse.single import GateError, run_single


def test_cli_exposes_only_single_subcommand():
    """Run test_cli_exposes_only_single_subcommand."""
    parser = build_parser()
    sub = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert set(sub.choices or {}) == {"single"}


def test_dry_run_never_creates_a_paid_client(monkeypatch, tmp_path):
    """Run test_dry_run_never_creates_a_paid_client."""
    from api.docparse import single

    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.4 dummy")
    monkeypatch.setattr(single, "local_page_count", lambda path: 7)

    def no_client(*_args, **_kwargs):
        """Run no_client."""
        raise AssertionError("dry-run 不允许构造付费客户端")

    monkeypatch.setattr(single, "_default_client", no_client)
    summary = run_single(
        source, max_pages=None, out_root=tmp_path / "out", dry_run=True
    )
    assert summary["page_count"] == 7
    assert summary["spent_tokens"] is False
    preview = Path(summary["preview_file"])
    assert preview.exists()
    assert json.loads(preview.read_text(encoding="utf-8"))["page_count"] == 7
    assert not (preview.parent / "01_mineru_raw.json").exists()


def test_missing_max_pages_refuses_before_network(monkeypatch, tmp_path):
    """Run test_missing_max_pages_refuses_before_network."""
    from api.docparse import single

    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.4 dummy")
    monkeypatch.setattr(single, "local_page_count", lambda path: 7)

    def no_client(*_args, **_kwargs):
        """Run no_client."""
        raise AssertionError("没有 --max-pages 时不允许发请求")

    monkeypatch.setattr(single, "_default_client", no_client)
    with pytest.raises(GateError, match="--max-pages"):
        run_single(
            source, max_pages=None, out_root=tmp_path / "out", dry_run=False
        )


def test_single_writes_mineru_stage_and_manifest(monkeypatch, tmp_path):
    """Run test_single_writes_mineru_stage_and_manifest."""
    from api.docparse import single

    source = tmp_path / "demo.pdf"
    source.write_bytes(b"%PDF-1.4 dummy")
    monkeypatch.setattr(single, "local_page_count", lambda path: 2)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "demo_content_list.json",
            json.dumps([{"page_idx": 0, "text": "甲"}]),
        )
    zip_bytes = buffer.getvalue()

    class FakeClient:
        """FakeClient."""
        def submit_local(self, path, data_id):
            """Run submit_local."""
            assert data_id
            return "batch-1"

        def wait_result(self, batch_id):
            """Run wait_result."""
            return "https://cdn.example/result.zip"

        def download(self, url):
            """Run download."""
            return zip_bytes

    out = tmp_path / "out"
    summary = run_single(
        source, max_pages=10, out_root=out, dry_run=False, client=FakeClient()
    )

    target = Path(summary["output_dir"])
    assert (
        json.loads((target / "01_mineru_raw.json").read_text(encoding="utf-8"))[
            0
        ]["text"]
        == "甲"
    )
    manifest = json.loads(
        (target / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "MINERU_DONE"
    assert manifest["stage_states"]["01_mineru"] == "done"
    assert manifest["batch_id"] == "batch-1"


def test_download_retry_raises_clean_error(monkeypatch):
    """Run test_download_retry_raises_clean_error."""
    from api.docparse import single

    calls = 0

    class Flaky:
        """Flaky."""
        def download(self, url):
            """Run download."""
            nonlocal calls
            calls += 1
            raise OSError("network down")

    monkeypatch.setattr(single.time, "sleep", lambda seconds: None)
    with pytest.raises(single.MineruError, match="已重试 3 次"):
        single._download_with_retry(Flaky(), "https://cdn.example/result.zip")
    assert calls == 3


def test_result_origin_url_maps_cdn_to_oss():
    """Run test_result_origin_url_maps_cdn_to_oss."""
    from api.docparse.mineru_client import result_origin_url

    url = "https://cdn-mineru.openxlab.org.cn/pdf/2026-11-25/result.zip"
    assert (
        result_origin_url(url)
        == "https://mineru.oss-cn-shanghai.aliyuncs.com/pdf/2026-11-25/result.zip"
    )


def test_download_falls_back_to_origin(monkeypatch):
    """Run test_download_falls_back_to_origin."""
    from api.docparse.mineru_client import MineruClient, MineruError

    client = MineruClient("https://mineru.net/api/v4", "token")
    calls: list[str] = []

    def fake_get(url):
        """Run fake_get."""
        calls.append(url)
        if url.startswith("https://cdn-"):
            raise MineruError("ssl eof")
        return b"zip-bytes"

    monkeypatch.setattr(client, "_get_bytes", fake_get)
    assert (
        client.download("https://cdn-mineru.openxlab.org.cn/pdf/result.zip")
        == b"zip-bytes"
    )
    assert calls == [
        "https://cdn-mineru.openxlab.org.cn/pdf/result.zip",
        "https://mineru.oss-cn-shanghai.aliyuncs.com/pdf/result.zip",
    ]


def test_full_single_writes_all_stage_files(tmp_path):
    """Run test_full_single_writes_all_stage_files."""
    from api.docparse import stages

    source = tmp_path / "doc.pdf"
    source.write_bytes(b"%PDF-1.4 dummy")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "demo_content_list.json",
            json.dumps(
                [
                    {
                        "type": "text",
                        "page_idx": 0,
                        "text": "第一段。",
                        "text_level": 1,
                    }
                ]
            ),
        )
    zip_bytes = buffer.getvalue()

    class FakeClient:
        """FakeClient."""
        def submit_local(self, path, data_id):
            """Run submit_local."""
            return "batch-1"

        def wait_result(self, batch_id):
            """Run wait_result."""
            return "https://cdn.example/result.zip"

        def download(self, url):
            """Run download."""
            return zip_bytes

    def fake_runner(pdf_path):
        """Run fake_runner."""
        return [
            {
                "page_num": 1,
                "markdown": "| a | b |\n|---|---|\n| 1 | 2 |",
                "cells": [["a", "b"], ["1", "2"]],
            }
        ]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("api.docparse.single.local_page_count", lambda path: 1)
    summary = stages.run_full_single(
        source,
        max_pages=10,
        out_root=tmp_path / "out",
        client=FakeClient(),
        docling_runner=fake_runner,
        chunk_splitter=type(
            "FakeSplitter", (), {"split_text": lambda self, text: [text]}
        )(),
    )
    target = Path(summary["output_dir"])
    for name in [
        "02_repaired.json",
        "03_docling_tables.json",
        "04_enriched.json",
        "05_chunks.json",
    ]:
        assert (target / name).exists(), name
    manifest = json.loads(
        (target / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["state"] == "READY_FOR_REVIEW"
    assert summary["chunk_count"] >= 1
    chunks = json.loads(
        (target / "05_chunks.json").read_text(encoding="utf-8")
    )["chunks"]
    assert all(item["page_num"] == 1 for item in chunks)
    assert all(item["char_count"] <= 600 for item in chunks)
    monkeypatch.undo()


def test_resume_download_never_resubmits_paid_task(tmp_path):
    """Run test_resume_download_never_resubmits_paid_task."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "demo_content_list.json", json.dumps([{"text": "恢复"}])
        )
    target = tmp_path / "resume"
    target.mkdir()
    (target / "mineru_task.json").write_text(
        json.dumps(
            {
                "batch_id": "batch-old",
                "zip_url": "https://cdn.example/old.zip",
                "state": "ZIP_READY",
            }
        ),
        encoding="utf-8",
    )

    class FakeClient:
        """FakeClient."""
        poll_interval_s = 3.0
        poll_timeout_s = 900.0

        def submit_local(self, path, data_id):
            """Run submit_local."""
            raise AssertionError("恢复下载不允许重新提交任务")

        def wait_result(self, batch_id):
            """Run wait_result."""
            raise AssertionError("已有 zip_url 时不需要再轮询")

        def download(self, url):
            """Run download."""
            return buffer.getvalue()

    summary = run_single(
        tmp_path / "unused.pdf",
        max_pages=None,
        dry_run=False,
        client=FakeClient(),
        resume_from=target,
    )
    assert summary["spent_tokens"] is False
    assert summary["resumed"] is True
    assert (
        json.loads((target / "01_mineru_raw.json").read_text(encoding="utf-8"))[
            0
        ]["text"]
        == "恢复"
    )
