"""本地链路追踪文件的行为用例（不碰 PG/Qdrant/数据库）。."""

from __future__ import annotations

import json

from api.observability import request_id_var
from api.trace import reset_trace, trace


def test_trace_writes_one_json_line_with_request_id(monkeypatch, tmp_path):
    """Run test_trace_writes_one_json_line_with_request_id."""
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AI_RAG_TRACE_FILE", str(path))
    monkeypatch.setenv("AI_RAG_TRACE_ENABLED", "1")
    reset_trace()

    token = request_id_var.set("rid-1")
    try:
        trace("unit.event", answer="你好", n=2)
    finally:
        request_id_var.reset(token)

    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["request_id"] == "rid-1"
    assert rows[0]["event"] == "unit.event"
    assert rows[0]["answer"] == "你好"
    assert rows[0]["n"] == 2


def test_trace_disabled_by_env_writes_no_file(monkeypatch, tmp_path):
    """Run test_trace_disabled_by_env_writes_no_file."""
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AI_RAG_TRACE_FILE", str(path))
    monkeypatch.setenv("AI_RAG_TRACE_ENABLED", "0")
    reset_trace()

    trace("unit.disabled", n=1)

    assert not path.exists()


def test_trace_clips_long_text_but_keeps_numbers(monkeypatch, tmp_path):
    """Run test_trace_clips_long_text_but_keeps_numbers."""
    path = tmp_path / "trace.jsonl"
    monkeypatch.setenv("AI_RAG_TRACE_FILE", str(path))
    monkeypatch.delenv("AI_RAG_TRACE_ENABLED", raising=False)
    reset_trace()

    trace("unit.long", text="甲" * 5000, score=0.875)

    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert len(row["text"]) < 5000
    assert row["text"].endswith("…")
    assert row["score"] == 0.875
