"""失败契约类型与不变量检查器的回归。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from api.common.result import Degraded, Fail, Ok, degrade

ROOT = Path(__file__).resolve().parents[2]


def test_result_variants_are_explicit() -> None:
    assert Ok(1).ok is True
    assert Fail("NO_EVIDENCE", "missing").ok is False
    assert Degraded(1, "RERANK_DOWN", "timeout").degraded is True


def test_degrade_requires_reason_and_code() -> None:
    with pytest.raises(ValueError):
        degrade(1, code="", reason="x")
    with pytest.raises(ValueError):
        degrade(1, code="X", reason="")


def _checker():
    spec = importlib.util.spec_from_file_location(
        "check_invariants", ROOT / "scripts/check_invariants.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_invariants"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_checker_flags_broad_except_empty_return(tmp_path: Path) -> None:
    checker = _checker()
    source = tmp_path / "bad.py"
    source.write_text(
        "def f():\n"
        "    try:\n"
        "        return 1\n"
        "    except Exception:\n"
        "        return []\n",
        encoding="utf-8",
    )
    violations = checker.scan_file(source)
    assert any(item.invariant == "INV-001" for item in violations)


def test_checker_flags_or_empty_and_silent_pass(tmp_path: Path) -> None:
    checker = _checker()
    source = tmp_path / "bad.py"
    source.write_text(
        "def f(response):\n"
        "    data = response.json() or []\n"
        "    try:\n"
        "        return data\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    ids = {item.invariant for item in checker.scan_file(source)}
    assert "INV-001" in ids
    assert "INV-002" in ids
