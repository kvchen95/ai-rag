"""跨仓库中文源码规则。."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _checker():
    """Run _checker."""
    spec = importlib.util.spec_from_file_location(
        "check_source_language", ROOT / "scripts/check_source_language.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_source_language"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_chinese_literal_is_flagged_but_docstring_is_allowed(
    tmp_path: Path,
) -> None:
    """Run test_chinese_literal_is_flagged_but_docstring_is_allowed."""
    checker = _checker()
    source = tmp_path / "bad.py"
    source.write_text(
        '"""这是文档字符串，允许出现中文。"""\n'
        "def f():\n"
        '    """函数文档字符串也允许。"""\n'
        '    return "这是生产字符串，不允许"\n',
        encoding="utf-8",
    )
    violations = checker.scan_file(source, tmp_path)
    assert len(violations) == 1
    assert violations[0]["line"] == 4


def test_resource_directory_is_allowed(tmp_path: Path) -> None:
    """Run test_resource_directory_is_allowed."""
    checker = _checker()
    source = tmp_path / "prompts" / "answer.py"
    source.parent.mkdir()
    source.write_text('PROMPT = "这是提示词"\n', encoding="utf-8")
    assert checker.scan_file(source, tmp_path) == []
    assert checker._allowed(source, tmp_path) is True
