"""过滤条件的执行计划与耗时（需要真 PG + 种子数据）。

为什么需要这个脚本：功能测试无法区分 `&&` 与 `<@`、`is_public` 与 `is_public = true`
在执行计划上的差别。要证明"确权与时间条件能在预算内完成",必须有足够的行数让
计划器做出真实选择,否则小表一律 Seq Scan,结论没有意义。

判据取自 §7.5 的召回预算:过滤 + 两路 prefetch 的 95% 在 30 毫秒内。这里单独量
**过滤这一半**,所以阈值定得比 30ms 更紧。

关于 Seq Scan 的诚实结论(实测 20000 行):
ACL 是 `is_public OR depts && ... OR roles && ...` 三支 OR。PG 要用 BitmapOr 合并
多个索引,三支必须都能生成 bitmap;而 `is_public` 是低选择性布尔列。实测分别
加了 is_public 索引、把 depts/roles 改成部分索引、两者都加,三种组合的计划与耗时
都没有改善(5.4–6.4ms),说明在 2 万行这个规模上 Seq Scan 是计划器的正确选择,
不是缺索引。所以本脚本不硬断言"必须走索引",而是断言"耗时在预算内",
并把计划树的真实形状打出来供人判断。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from api.clients.pg import PgDatabase  # noqa: E402
from api.filters import PgSource, RetrievalFilter  # noqa: E402
from api.identity import IdentitySnapshot  # noqa: E402
from api.time_range import TimeRange  # noqa: E402

# §7.5 的 30ms 是"召回 + 两路 prefetch"的总预算，过滤只占其中一部分。
FILTER_BUDGET_MS = 15.0

CASES: dict[str, RetrievalFilter] = {
    "public_only": RetrievalFilter(identity=IdentitySnapshot(user_id="u_new")),
    "multi_depts": RetrievalFilter(
        identity=IdentitySnapshot(user_id="u_multi", depts=("fin", "corp"))
    ),
    "dept_role_time": RetrievalFilter(
        identity=IdentitySnapshot(user_id="u_both", depts=("fin",), roles=("staff",)),
        time_range=TimeRange(20260301, 20260331, "month", "三月", "assumed_year"),
    ),
    "admin": RetrievalFilter(identity=IdentitySnapshot(user_id="u_admin", admin=True)),
}


def explain(db: PgDatabase, flt: RetrievalFilter, *, ordered: bool) -> dict:
    where, params = flt.pg_where("c", source=PgSource.CHUNKS)
    order = " ORDER BY chunk_id" if ordered else ""
    sql = (
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
        f"SELECT chunk_id FROM chunks AS c WHERE {where}{order}"
    )
    rows = db.fetch_all(sql, params)
    return rows[0]["QUERY PLAN"][0]


def walk(node: dict, depth: int = 0) -> list[str]:
    bits = [f"{'  ' * depth}{node.get('Node Type')}"]
    if node.get("Index Name"):
        bits.append(f"idx={node['Index Name']}")
    bits.append(f"rows={node.get('Actual Rows')}")
    if node.get("Index Cond"):
        bits.append(f"cond={node['Index Cond']}")
    if node.get("Filter"):
        bits.append(f"filter={str(node['Filter'])[:90]}")
    out = ["  ".join(bits)]
    for child in node.get("Plans", []) or []:
        out.extend(walk(child, depth + 1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL") or os.getenv("AI_RAG_TEST_DATABASE_URL"),
    )
    args = ap.parse_args()
    if not args.dsn:
        print("ERROR: 需要 --dsn 或 DATABASE_URL", file=sys.stderr)
        return 2

    db = PgDatabase(args.dsn, min_size=1, max_size=2, connect_timeout_s=5)
    db.open()
    problems: list[str] = []
    try:
        total = db.fetch_one("SELECT count(*) AS n FROM chunks")["n"]
        print(
            f"chunks 总行数: {total}（过滤预算 {FILTER_BUDGET_MS}ms，§7.5 召回 30ms 的一部分）\n"
        )
        if total < 5000:
            print("警告：行数太少，计划器一律 Seq Scan，结论仅供参考\n")

        for name, flt in CASES.items():
            for ordered in (True, False):
                doc = explain(db, flt, ordered=ordered)
                ms = float(doc["Execution Time"])
                label = f"{name}{' (带 ORDER BY)' if ordered else ' (纯过滤)'}"
                print(f"=== {label} ===")
                print("\n".join(walk(doc["Plan"])))
                print(f"  执行时间: {ms:.2f}ms\n")
                if ms > FILTER_BUDGET_MS:
                    problems.append(f"{label}: {ms:.2f}ms > {FILTER_BUDGET_MS}ms")
    finally:
        db.close()

    if problems:
        print("超出预算:\n  " + "\n  ".join(problems))
        return 1
    print(f"ALL FILTER PLANS WITHIN {FILTER_BUDGET_MS}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
