#!/usr/bin/env python3
"""Reject Chinese string literals in Python source outside resource directories.

Usage:
  python scripts/check_source_language.py --all
  python scripts/check_source_language.py --changed --base HEAD
  python scripts/check_source_language.py path/to/repo --all
  python scripts/check_source_language.py --files a.py b.py
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "site-packages",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "tests",
    "test",
}
ALLOW_DIRS = {"prompts", "resources", "locales", "i18n"}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
        ):
            body = getattr(node, "body", ())
            if body and isinstance(body[0], ast.Expr):
                value = body[0].value
                if isinstance(value, ast.Constant) and isinstance(
                    value.value, str
                ):
                    ids.add(id(value))
    return ids


def _skip_path(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return True
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def _allowed(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part in ALLOW_DIRS for part in parts[:-1])


def scan_file(path: Path, root: Path) -> list[dict[str, object]]:
    if _allowed(path, root) or _skip_path(path, root):
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [
            {
                "line": exc.lineno or 1,
                "text": f"SYNTAX: {exc.msg}",
                "kind": "syntax",
            }
        ]
    docstrings = _docstring_ids(tree)
    out: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(
            node.value, str
        ):
            continue
        if id(node) in docstrings:
            continue
        if not CJK_RE.search(node.value):
            continue
        out.append(
            {
                "line": node.lineno,
                "text": node.value.replace("\n", " ")[:80],
                "kind": "cjk",
            }
        )
    return out


def iter_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.py")
        if not _skip_path(path, root) and not _allowed(path, root)
    )


def _git_root(path: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"not a git repository: {path}")
    return Path(result.stdout.strip())


def _changed_lines(root: Path, base: str) -> dict[str, set[int]]:
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
        cwd=root,
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
            changed[current].update(
                range(int(start), int(start) + int(count or "1"))
            )
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "*.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    for rel in untracked:
        path = root / rel
        try:
            total = len(path.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
        changed[rel] = set(range(1, total + 1))
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--changed", action="store_true")
    parser.add_argument("--base", default="HEAD")
    parser.add_argument("--files", nargs="*", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    if not root.exists():
        raise SystemExit(f"root does not exist: {root}")
    if not args.all and not args.changed and not args.files:
        parser.error("use --all, --changed or --files")

    if args.files:
        files = [Path(item).resolve() for item in args.files]
    else:
        files = iter_files(root)

    changed = (
        _changed_lines(_git_root(root), args.base) if args.changed else None
    )
    violations: list[dict[str, object]] = []
    for path in files:
        if _skip_path(path, root) or _allowed(path, root):
            continue
        rel = (
            path.relative_to(root).as_posix()
            if path.is_relative_to(root)
            else str(path)
        )
        for item in scan_file(path, root):
            line = int(item["line"])
            if changed is not None and line not in changed.get(rel, set()):
                continue
            violations.append({"path": rel, **item})

    if args.json:
        print(json.dumps(violations, ensure_ascii=False, indent=2))
    else:
        for item in violations:
            print(
                f"{item['path']}:{item['line']} {item['kind']} {item['text']}"
            )
        print(f"source-language: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
