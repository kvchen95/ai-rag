"""变异测试：把每个关键判据故意改坏，确认对应用例变红，再改回。.

判据从没红过 = 和被删掉等价。这里跑四组，每组改一处语义、不改数量：

1. 把 depts 的"有交集"改成"被包含"（PG 侧 `&&` → `<@`）
2. 去掉 Qdrant 侧的时间 date_confidence 条件
3. 把 admin 的"去掉整条 ACL"改成"仍带 public 分支"（语义错但结果同）
4. 去掉版本兜底（§7.4）

每组都验：改坏后 rc != 0（用例真的红了），还原后 rc == 0（改回真的改回来了）。
最后把所有"改坏了却仍然全绿"的变异列出来并以非零退出——
那种情况说明对应用例形同虚设。

用法：
    docker compose exec -T api python scripts/b2_mutation_check.py
    # 或宿主侧（需 pytest + psycopg 与真 PG+Qdrant，env 同 pytest）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .resources.b2_mutation_check_msgs import MESSAGES as M
else:  # run as a script: python3 scripts/b2_mutation_check.py
    from resources.b2_mutation_check_msgs import MESSAGES as M

REPO = Path(__file__).resolve().parent.parent
PYTEST_TARGETS = [
    "api/tests/test_filter_parity.py",
    "api/tests/test_filters.py",
]


def run(targets: list[str]) -> tuple[int, str]:
    """Run run."""
    env = dict(os.environ)
    # 本机 ALL_PROXY 会把 127.0.0.1 也代理走，Qdrant 直连会失败。
    for var in ("ALL_PROXY", "all_proxy"):
        env.pop(var, None)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-c", "api/pytest.ini", *targets],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    # 只留结论行，避免把 pytest-asyncio 的弃用警告当结果。
    lines = [
        ln
        for ln in combined.strip().splitlines()
        if "passed" in ln or "failed" in ln
    ]
    return proc.returncode, (lines[-1] if lines else combined.strip()[-200:])


def mutate(path: Path, old: str, new: str) -> None:
    """Run mutate."""
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{M['MUTATE_01']}{old[:60]!r} in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


MUTATIONS = [
    {
        "name": M["NAME_01"],
        "file": "api/filters.py",
        "old": 'or_parts.append(f"{prefix}allowed_depts && %s::text[]")',
        "new": 'or_parts.append(f"{prefix}allowed_depts <@ %s::text[]")',
    },
    {
        "name": M["NAME_02"],
        "file": "api/filters.py",
        "old": """            must.append(
                FieldCondition(
                    key=P_DATE_CONFIDENCE,
                    match=MatchValue(value=EXPLICIT_CONFIDENCE),
        )
            )""",
        "new": M["NEW_01"],
    },
    {
        "name": M["NAME_03"],
        "file": "api/filters.py",
        "old": """        if self.is_admin:
            return ()""",
        "new": M["NEW_02"],
    },
    {
        "name": M["NAME_04"],
        "file": "api/version_guard.py",
        "old": (
            "        if expected is not None and item.get(\"version_id\") "
            "== expected:\n"
            "            kept.append(item)"
        ),
        "new": M["NEW_03"],
    },
]


def main() -> int:
    """Run main."""
    baseline_rc, baseline_tail = run(PYTEST_TARGETS)
    print(f"baseline rc={baseline_rc} :: {baseline_tail}")
    if baseline_rc != 0:
        print(M["PRINT_01"])
        return 1

    undetected: list[str] = []
    for mut in MUTATIONS:
        path = REPO / mut["file"]
        fd, backup_name = tempfile.mkstemp(suffix=".bak")
        os.close(fd)
        backup = Path(backup_name)
        shutil.copy2(path, backup)
        try:
            mutate(path, mut["old"], mut["new"])
            rc, tail = run(PYTEST_TARGETS)
            if rc == 0:
                undetected.append(mut["name"])
                print(f"[{mut['name']}{M['PRINT_02']}{tail}")
            else:
                print(f"[{mut['name']}] RED (expected) :: {tail}")
        finally:
            shutil.copy2(backup, path)
            backup.unlink()

    restored_rc, restored_tail = run(PYTEST_TARGETS)
    print(f"restored rc={restored_rc} :: {restored_tail}")
    if restored_rc != 0:
        print(M["PRINT_03"])
        return 1
    if undetected:
        print(M["PRINT_04"])
        for name in undetected:
            print(f"  - {name}")
        return 1
    print("\nALL MUTATIONS DETECTED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
