#!/usr/bin/env python3
"""变异测试：把 A2 的关键判据逐个改坏，确认对应用例真的会红。

**为什么必须做**：判据从没红过等于没有判据。这一卡里每一个判据都对应一条验收
（bbox 可映射原页 / 三表示一致 / 数值对账失败不入库 / 字段缺失被拦），
如果改坏代码测试还全绿，那就说明测试在测别的东西。

做法：复制一份仓库到临时目录 → 改一个判据 → 跑指定用例 → 断言 rc != 0 → 还原。
不复制的话，中途失败会留下一个被改坏的仓库。

用法：

    python scripts/a2_mutation_check.py            # 全部变异
    python scripts/a2_mutation_check.py --list     # 只列出
    python scripts/a2_mutation_check.py --only rotation

依赖：pdfplumber / pypdfium2 / Pillow / pytest / psycopg 装在当前解释器里
（`api/tests/test_parse_*.py` 的 import 需要它们）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Mutation:
    """一条变异：改哪个文件、改什么、跑哪些用例、期望红的原因。

    `needs_db` 的变异跑的是集成用例：没有真 PG 时它们会 **skip**，而 skip 的 rc
    也是 0。把 skip 当"检出"会让脚本在最需要它的时候（CI 没库）谎报全绿，所以
    这类变异在缺 DSN 时直接判失败并说明原因。
    """

    key: str
    path: str
    old: str
    new: str
    tests: tuple[str, ...]
    why: str
    needs_db: bool = False


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        key="rotation",
        path="api/parse/geometry.py",
        old="        if self.rotation == 90:\n            return (y, self.user_height - x)",
        new="        if self.rotation == 90:\n            return (x, y)",
        tests=("api/tests/test_parse_geometry.py", "api/tests/test_parse_pipeline_e2e.py"),
        why="旋转 90 度映射写错 → 旋转页 bbox 与未旋转页对不上",
    ),
    Mutation(
        key="rotation-inverse",
        path="api/parse/geometry.py",
        old="        if self.rotation == 90:\n            return (self.user_height - y, x)",
        new="        if self.rotation == 90:\n            return (y, x)",
        tests=("api/tests/test_parse_geometry.py",),
        why="逆映射写错 → 显示/阅读空间往返不闭合",
    ),
    Mutation(
        key="header-footer",
        path="api/parse/layout.py",
        old="            if require_margin and not in_margin_by_original.get(original, False):",
        new="            if False:",
        tests=("api/tests/test_parse_layout.py", "api/tests/test_parse_pipeline_e2e.py"),
        why="去掉页边带判据 → 每页的章节标题与条款被当页眉删掉",
    ),
    Mutation(
        key="digits-flatten",
        path="api/parse/layout.py",
        old='    return _DIGITS.sub("#", collapsed)',
        new="    return collapsed",
        tests=("api/tests/test_parse_layout.py",),
        why="不抹平数字 → 带页码的页眉一个都认不出来",
    ),
    Mutation(
        key="gutter-guard",
        path="api/parse/layout.py",
        old="        if rect.x0 < gap_start - 0.5 and rect.x1 > gap_end + 0.5:",
        new="        if False:",
        tests=("api/tests/test_parse_layout.py",),
        why="去掉「跨栏缝即非表格」→ 双栏正文被当成表格",
    ),
    Mutation(
        key="prose-guard",
        path="api/parse/layout.py",
        old="    long_cells = sum(1 for text in texts if len(text) > TABLE_CELL_MAX_CHARS)",
        new="    long_cells = 0",
        tests=("api/tests/test_parse_layout.py",),
        why="去掉「长单元格即非表格」→ 一段话被当成表格的一格",
    ),
    Mutation(
        key="table-span",
        path="api/parse/layout.py",
        old="                    col_span=max(1, col_end - col),",
        new="                    col_span=1,",
        tests=("api/tests/test_parse_merged_table.py",),
        why="丢掉合并列信息 → 三表示的一致性被破坏",
    ),
    Mutation(
        key="widen-expand",
        path="api/parse/model.py",
        old="                if grid[r][c] is None:\n                    grid[r][c] = text",
        new="                grid[r][c] = text",
        tests=("api/tests/test_parse_tables.py",),
        why="合并格展开改成「后到先得」→ 左上角的值被覆盖",
    ),
    Mutation(
        key="md-pipe-escape",
        path="api/parse/tables.py",
        old='    return value.replace("\\\\", "\\\\\\\\").replace("|", "\\\\|")',
        new="    return value",
        tests=("api/tests/test_parse_tables.py",),
        why="md 不转义竖线 → 一行被切成更多列",
    ),
    Mutation(
        key="percent-sum",
        path="api/parse/figures_transcribe.py",
        old="        if not (PERCENT_SUM_MIN <= total <= PERCENT_SUM_MAX):",
        new="        if False:",
        tests=("api/tests/test_parse_figures.py", "api/tests/test_parse_pipeline_e2e.py"),
        why="去掉占比求和检查 → 32 被读成 74 也能入库",
    ),
    Mutation(
        key="value-in-figure",
        path="api/parse/figures_transcribe.py",
        old="    unmatched = [\n        value for value in values if not _value_supported(value, evidence, axis)\n    ]",
        new="    unmatched: list[float] = []",
        tests=("api/tests/test_parse_figures.py",),
        why="去掉「数值能在图上找到」→ 模型编的数字也能入库",
    ),
    Mutation(
        key="label-in-figure",
        path="api/parse/figures_transcribe.py",
        old="    missing_labels = [label for label in labels if not _label_supported(label, evidence)]",
        new="    missing_labels: list[str] = []",
        tests=("api/tests/test_parse_figures.py",),
        why="去掉标签检查 → 编造的类别名也能入库",
    ),
    Mutation(
        key="acl-denied",
        path="api/parse/chunking.py",
        old='        if not self.is_public and not self.allowed_depts and not self.allowed_roles:\n            missing.append("acl(everyone denied)")',
        new="        pass",
        tests=("api/tests/test_parse_chunking.py",),
        why="放行「谁都看不到」的切片 → 六项校验少一项",
    ),
    Mutation(
        key="page-num",
        path="api/parse/chunking.py",
        old="        if not self.page_num or self.page_num < 1:\n            missing.append(\"page_num\")",
        new="        pass",
        tests=("api/tests/test_parse_chunking.py",),
        why="不校验页码 → 引用不能跳页的切片也能入库",
    ),
    Mutation(
        key="year-matches-date",
        path="api/parse/chunking.py",
        old="        if self.effective_date is not None and self.effective_year != self.effective_date // 10000:",
        new="        if False:",
        tests=("api/tests/test_parse_chunking.py",),
        why="派生列不自洽 → 时间过滤按错误的年去筛",
    ),
    Mutation(
        key="overlap",
        path="api/parse/chunking.py",
        old="    effective_overlap = max(0, min(overlap, max_chars // 2))",
        new="    effective_overlap = 0",
        tests=("api/tests/test_parse_chunking.py",),
        why="去掉重叠 → 一句话被切断后两边都检索不到",
    ),
    Mutation(
        key="dates-explicit",
        path="api/parse/dates.py",
        old='        return BusinessDate(value, year, "explicit")',
        new='        return BusinessDate(value, year, "inferred")',
        tests=("api/tests/test_parse_dates.py",),
        why="显式日期被降级成推断 → 时间过滤永远不认它",
    ),
    Mutation(
        key="zero-width",
        path="api/parse/layout.py",
        old="    chars = fill_missing_widths(page.chars)",
        new="    chars = list(page.chars)",
        tests=("api/tests/test_parse_zero_width.py",),
        why="不补零宽字宽 → 真实样本的行宽为 0，整份文档一条切片都出不来",
    ),
    Mutation(
        key="figure-dpi",
        path="api/parse/figure_pipeline.py",
        old="        fig.assert_min_dpi(rendered)",
        new="        pass",
        tests=("api/tests/test_parse_figure_render.py",),
        why="不查 DPI → 糊图也能入库（刻度读出来是错的）",
    ),
    Mutation(
        key="figure-reject",
        path="api/parse/figure_pipeline.py",
        old="    if not result.ok:",
        new="    if False:",
        tests=("api/tests/test_parse_pipeline_e2e.py",),
        why="对账失败仍然入库 → 验收第二条破了",
    ),
    Mutation(
        key="scan-image-ratio",
        path="api/parse/ocr.py",
        old="        if area / page_area >= CONTENT_IMAGE_MIN_RATIO:",
        new="        if False:",
        tests=("api/tests/test_parse_ocr.py",),
        why="要求位图覆盖半页 → 23% 的真实扫描页被判成非扫描件",
    ),
    Mutation(
        key="ocr-render-page",
        path="api/parse/ocr.py",
        old="    image = render() if render is not None else _render_page(page, render_scale)",
        new="    image = _render_page(page, render_scale)",
        tests=("api/tests/test_parse_ocr.py",),
        why="不让调用方提供渲染器 → 拿 pdfplumber 的 Page 去 render，运行时崩",
    ),
    Mutation(
        key="ocr-blocked",
        path="api/parse/reader.py",
        old="    blocked = [] if draft.ocr_applied else list(draft.blocked_rects)",
        new="    blocked = list(draft.blocked_rects)",
        tests=("api/tests/test_parse_ocr.py",),
        why="OCR 页仍按图区域遮挡 → 识别出的文字全被扣掉，一片都不入库",
    ),
    Mutation(
        key="ocr-notes",
        path="api/parse/pipeline.py",
        old='    if sources == {"ocr"}:\n        return ("source:ocr",)',
        new='    if sources == {"ocr"}:\n        return ()',
        tests=("api/tests/test_parse_ocr.py",),
        why="不把来源传下去 → 下游分不清文字层与识别结果",
    ),
    Mutation(
        key="as-row-table-cells",
        path="api/parse/chunking.py",
        old='            "table_cells": self.table_cells,',
        new='            "table_cells": None,',
        tests=("api/tests/test_parse_persist_integration.py",),
        why="as_row 丢掉矩阵 → §3.3 的第三份表示入库即丢，校验只能正则拆 HTML",
        needs_db=True,
    ),
    Mutation(
        key="as-row-notes",
        path="api/parse/chunking.py",
        old='            "notes": list(self.notes),',
        new='            "notes": [],',
        tests=("api/tests/test_parse_persist_integration.py",),
        why="as_row 丢掉来源标记 → 下游看不出这段文字是 OCR 认的",
        needs_db=True,
    ),
    Mutation(
        key="chunk-id-safe",
        path="api/parse/chunking.py",
        old='    return f"ch_{digest[:12]}"',
        new='    return f"ch_{version_id}_{page_num}_{kind}_{index}_{part}"',
        tests=("api/tests/test_parse_chunking.py",),
        why="编号带上 version_id → 文件路径里有 / 与 #，图必定 404",
    ),
)


def run_mutation(mutation: Mutation, workdir: Path) -> tuple[bool, str]:
    """返回 (是否如期变红, 说明)。

    **两种"绿"要分清楚**：一是用例真的通过（判据没被覆盖），二是用例被 **skip**
    （集成用例没有 DSN 就跳过）。两者 rc 都是 0，只看 rc 会把"没验到"当成
    "验过了"。所以这里要求 stdout 里出现 `failed`，并且显式禁止 skip ——
    `--no-header` 之外再加 `-p no:cacheprovider` 无所谓，关键是 skip 会被
    `--strict-markers` 之外的检查拦下：见下面的 `skipped` 判定。
    """
    target = workdir / mutation.path
    original = target.read_text(encoding="utf-8")
    if mutation.old not in original:
        return False, f"锚点未找到（{mutation.path}）：{mutation.old[:60]!r}"
    if mutation.needs_db and not _db_dsn():
        return False, (
            "这条变异需要真 PG（集成用例会 skip），但没设 AI_RAG_TEST_DATABASE_URL/"
            "DATABASE_URL —— 不把 skip 当检出"
        )
    try:
        target.write_text(original.replace(mutation.old, mutation.new, 1), encoding="utf-8")
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "-c", "api/pytest.ini", "-x", "-q", *mutation.tests],
            cwd=workdir,
            capture_output=True,
            text=True,
            env=_pytest_env(mutation),
        )
        if proc.returncode == 0:
            skipped = "skipped" in proc.stdout
            return False, (
                "变异后只用例被 skip 了（没有真 PG）—— 不算检出"
                if skipped
                else "变异后测试仍然全绿 —— 这条判据没有被任何用例覆盖"
            )
        tail = [line for line in proc.stdout.splitlines() if "FAILED" in line or "passed" in line]
        return True, tail[-1][:120] if tail else f"rc={proc.returncode}"
    finally:
        target.write_text(original, encoding="utf-8")


def _db_dsn() -> str | None:
    """集成用例认的那个 DSN（与 api/tests/conftest.py 的 `pg_dsn` 口径一致）。"""
    return os.getenv("AI_RAG_TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


def _pytest_env(mutation: Mutation) -> dict[str, str]:
    """子进程环境：需要真库的变异把 DSN 传下去，其余沿用当前环境。

    不传 DSN 的话集成用例会 skip，rc 仍然是 0 —— 脚本会把"没验到"读成"没红"，
    进而报 `MUTATIONS NOT DETECTED`，看起来像判据失效，实际是环境没接上。
    """
    env = dict(os.environ)
    if mutation.needs_db:
        dsn = _db_dsn()
        if dsn:
            env["AI_RAG_TEST_DATABASE_URL"] = dsn
    return env


def main() -> int:
    ap = argparse.ArgumentParser(description="A2 判据变异测试")
    ap.add_argument("--list", action="store_true", help="只列出变异，不执行")
    ap.add_argument("--only", help="只跑指定 key")
    ap.add_argument("--keep", action="store_true", help="保留临时副本（调试用）")
    args = ap.parse_args()

    selected = [m for m in MUTATIONS if args.only is None or m.key == args.only]
    if args.list:
        for mutation in MUTATIONS:
            print(f"{mutation.key:22s} {mutation.path:34s} {mutation.why}")
        return 0
    if not selected:
        print(f"unknown key: {args.only}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="a2-mutation-"))
    repo = workdir / "repo"
    print(f"copying repo -> {repo}")
    shutil.copytree(
        REPO_ROOT,
        repo,
        ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "web"),
    )

    failures: list[str] = []
    try:
        for mutation in selected:
            print(f"\n[{mutation.key}] {mutation.why}")
            ok, note = run_mutation(mutation, repo)
            print(("  OK    " if ok else "  BROKEN") + f" {note}")
            if not ok:
                failures.append(f"{mutation.key}: {note}")
    finally:
        if args.keep:
            print(f"\nkept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 66)
    print(f"{len(selected) - len(failures)}/{len(selected)} mutations detected")
    if failures:
        print("MUTATIONS NOT DETECTED:")
        for failure in failures:
            print("  - " + failure)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
