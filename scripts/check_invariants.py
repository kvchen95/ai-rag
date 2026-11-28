#!/usr/bin/env python3
"""Architecture fitness function: detect implicit degradation patterns.

Examples:
  python scripts/check_invariants.py --all
  python scripts/check_invariants.py --changed
  python scripts/check_invariants.py --changed --base origin/main
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMPTY_VALUES = (None, [], {}, "", (), 0, False)
BROAD_EXCEPTIONS = {"Exception", "BaseException"}
DETERMINISTIC_PREFIXES = ("api/time_range.py", "api/temporal/", "api/query/")
LLM_IMPORTS = ("api.generate.llm", "api.generate.judge", "api.llm_json")
BANNED_TEMPORAL_NAMES = (
    "_REL_YEAR_MONTH",
    "_REL_YEAR",
    "_PREV_MONTH",
    "_THIS_MONTH",
    "_BARE_MONTH",
    "_ACTIVE_ONLY",
    "_VAGUE",
    "_subject_metric",
    "_with_subject",
    "_rule_split",
    "_split_and",
    "_FORECAST_MARKERS",
    "_DOC_TIME_MARKERS",
    "forecast_markers",
    "doc_markers",
    "REPORT_TYPE_KEYWORDS",
    "_forecast_years",
    "_strip_forecast_years",
    "_extract_report_type",
)


@dataclass(frozen=True, slots=True)
class Violation:
    """Violation."""
    invariant: str
    path: str
    line: int
    message: str

    def as_dict(self) -> dict[str, object]:
        """Run as_dict."""
        return {
            "invariant": self.invariant,
            "path": self.path,
            "line": self.line,
            "message": self.message,
        }


def _is_empty(node: ast.AST | None) -> bool:
    """Run _is_empty."""
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value in (None, "", 0, False)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    return False


def _handler_is_broad(handler: ast.ExceptHandler) -> bool:
    """Run _handler_is_broad."""
    if handler.type is None:
        return True
    names: list[str] = []
    if isinstance(handler.type, ast.Name):
        names = [handler.type.id]
    elif isinstance(handler.type, ast.Tuple):
        names = [
            item.id for item in handler.type.elts if isinstance(item, ast.Name)
        ]
    return any(name in BROAD_EXCEPTIONS for name in names)


def _scan_except(tree: ast.AST, path: str) -> list[Violation]:
    """Run _scan_except."""
    out: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if _handler_is_broad(node):
            returns = [
                item for item in node.body if isinstance(item, ast.Return)
            ]
            if any(_is_empty(item.value) for item in returns):
                out.append(
                    Violation(
                        "INV-001",
                        path,
                        node.lineno,
                        "broad except returns empty; return Fail or Degraded",
                    )
                )
        if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
            if _handler_is_broad(node):
                out.append(
                    Violation(
                        "INV-001",
                        path,
                        node.lineno,
                        "broad try/except/pass silently swallows the exception",
                    )
                )
            continue
    return out


def _scan_or_empty(tree: ast.AST, path: str) -> list[Violation]:
    """Run _scan_or_empty."""
    out: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            left = node.values[0] if node.values else None
            # 只拦“外部数据缺失被当成空集合”的写法；局部变量给空默认不算。
            if not isinstance(left, ast.Call | ast.Attribute | ast.Subscript):
                continue
            if any(_is_empty(value) for value in node.values[1:]):
                out.append(
                    Violation(
                        "INV-002",
                        path,
                        node.lineno,
                        "or [] / {} / '' treats missing data as normal",
                    )
                )
    return out


def _scan_llm_import(tree: ast.AST, path: str) -> list[Violation]:
    """Run _scan_llm_import."""
    if not any(path.startswith(prefix) for prefix in DETERMINISTIC_PREFIXES):
        return []
    out: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            (node.module or "").startswith(module) for module in LLM_IMPORTS
        ):
            out.append(
                Violation(
                    "INV-003",
                    path,
                    node.lineno,
                    "deterministic module imports a generative model",
                )
            )
    return out


def _scan_temporal_names(tree: ast.AST, path: str) -> list[Violation]:
    """Run _scan_temporal_names."""
    if not any(path.startswith(prefix) for prefix in DETERMINISTIC_PREFIXES):
        return []
    out: list[Violation] = []
    for node in ast.walk(tree):
        name = ""
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            name = node.name
        elif isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        if any(banned in name for banned in BANNED_TEMPORAL_NAMES):
            out.append(
                Violation(
                    "INV-006",
                    path,
                    getattr(node, "lineno", 1),
                    f"banned temporal heuristic symbol: {name}",
                )
            )
    return out


def _scan_missing_metadata_open(tree: ast.AST, path: str) -> list[Violation]:
    """Run _scan_missing_metadata_open."""
    out: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        returns_true = any(
            isinstance(item, ast.Return)
            and isinstance(item.value, ast.Constant)
            and item.value.value is True
            for item in node.body
        )
        mentions_none = any(
            isinstance(item, ast.Constant) and item.value is None
            for item in ast.walk(node.test)
        )
        if returns_true and mentions_none:
            out.append(
                Violation(
                    "INV-004",
                    path,
                    node.lineno,
                    "missing metadata returns True; filter may widen",
                )
            )
    return out


def scan_file(path: Path) -> list[Violation]:
    """Run scan_file."""
    try:
        rel = path.relative_to(ROOT).as_posix()
    except ValueError:
        rel = path.name
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except SyntaxError as exc:
        return [
            Violation("PARSE", rel, exc.lineno or 1, f"cannot parse: {exc.msg}")
        ]
    return [
        *_scan_except(tree, rel),
        *_scan_or_empty(tree, rel),
        *_scan_llm_import(tree, rel),
        *_scan_temporal_names(tree, rel),
        *_scan_missing_metadata_open(tree, rel),
    ]


def _changed_lines(base: str) -> dict[str, set[int]]:
    """Run _changed_lines."""
    result = subprocess.run(
        [
            "git",
            "diff",
            "--unified=0",
            "--diff-filter=ACMR",
            base,
            "--",
            "*.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    changed: dict[str, set[int]] = {}
    current: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            changed.setdefault(current, set())
        elif line.startswith("@@") and current:
            plus = line.split("+", 1)[1].split(" ", 1)[0]
            start, _, count = plus.partition(",")
            start_i = int(start)
            count_i = int(count or "1")
            changed[current].update(range(start_i, start_i + count_i))

    # 新增未跟踪文件在 git diff 里看不到，按整文件计入。
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for rel in untracked:
        path = ROOT / rel
        try:
            total = len(path.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
        changed[rel] = set(range(1, total + 1))
    return changed


def main() -> int:
    """Run main."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--all", action="store_true", help="scan all api/**/*.py"
    )
    parser.add_argument(
        "--changed",
        action="store_true",
        help="only report added lines relative to base",
    )
    parser.add_argument(
        "--base", default="HEAD~1", help="git base for --changed"
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.changed:
        parser.error("use --all or --changed")

    files = sorted((ROOT / "api").rglob("*.py"))
    changed = _changed_lines(args.base) if args.changed else None
    violations: list[Violation] = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if "tests/" in rel or "__pycache__" in rel:
            continue
        if changed is not None and rel not in changed:
            continue
        for violation in scan_file(path):
            if changed is not None and violation.line not in changed.get(
                rel, set()
            ):
                continue
            violations.append(violation)

    if args.json:
        print(
            json.dumps(
                [item.as_dict() for item in violations],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        for item in violations:
            print(f"{item.invariant} {item.path}:{item.line} {item.message}")
        print(f"invariants: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
