"""B3 的变异测试：把每条关键判据故意改坏，确认对应用例会红，再改回。.

判据从没红过 = 和被删掉等价。这里跑七组，每组**只改一处语义、不改数量**——
只改数量的变异会被"条数"类断言一抓一个准，让人误以为判据很严；只改语义的变异
才会暴露"只约束了一边"的盲区。

七组对应卡片的七条验收：

1. 两路不再共用同一个 filter（BM25 路漏掉 ACL）→ 越权检索用例必须红
2. 去掉版本兜底（§7.4）→ 旧版本剔除用例必须红
3. RRF 常数的 k 从 1 改成 60（"论文里更正规"的那种改法）→ 原生融合等价用例必须红
4. 精排降级时给候选填 0 分（伪造分数）→ "不编造分数"用例必须红
5. 门控把"临界带"当成补充而不是替代 → 0.39 的多实体用例必须红
6. 多跳不检查本跳是否过阈（只看总证据）→ 每跳独立过阈的用例必须红
7. 改写不限次数（允许级联）→ 幂等性用例必须红

每组都验：改坏后 rc != 0（真的红了），还原后 rc == 0（真的改回来了）。
最后把所有"改坏了却仍然全绿"的变异列出来并以非零退出。

用法：
    docker compose exec -T api python scripts/b3_mutation_check.py
    # 或宿主侧（需 pytest + 真 PG + Qdrant + TEI + rerank，env 同 pytest）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 每个变异只跑它该打红的那一小撮用例：跑全量会让一次变异花掉几分钟，
# 而"哪条用例该红"是作者自己该说清的事。
PYTEST_TARGETS = [
    "api/tests/test_retrieval_logic.py",
    "api/tests/test_query_understand.py",
]


def run(targets: list[str]) -> tuple[int, str]:
    env = dict(os.environ)
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
    lines = [
        ln
        for ln in combined.strip().splitlines()
        if "passed" in ln or "failed" in ln
    ]
    return proc.returncode, (lines[-1] if lines else combined.strip()[-300:])


def mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"变异锚点未找到: {old[:80]!r} in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


MUTATIONS = [
    {
        "name": "BM25 路不再带 ACL（两路 filter 分裂）",
        "file": "api/retrieval/hybrid.py",
        # 锚点必须落在**代码**里而不是 docstring 里：`filter_json` 出现多次，
        # 但只有"第二路复用同一个 dict"这一处是判据所在。
        # （早先的锚点改到了 docstring
        # 上的同名文本，变异等于没改，于是"没被检出"。）
        "old": """                "using": SPARSE_VECTOR,
            "filter": filter_json,
            "limit": top_k,""",
        "new": (
            "                \"using\": SPARSE_VECTOR,\n"
            "                \"filter\": {\"must\": [{\"key\": \"status\", "
            "\"match\": {\"value\": \"active\"}}]},\n"
            "                \"limit\": top_k,"
        ),
    },
    {
        "name": "去掉版本兜底（§7.4）",
        "file": "api/version_guard.py",
        "old": (
            "        if expected is not None and item.get(\"version_id\") "
            "== expected:\n"
            "            kept.append(item)"
        ),
        "new": """        if True:  # 变异：不做版本兜底
            kept.append(item)""",
    },
    {
        "name": "RRF 常数改成 60（与 Qdrant 原生不再等价）",
        "file": "api/retrieval/hybrid.py",
        # 锚点带换行与上下文：`RRF_K = 1` 单独出现会在 docstring 里先被匹配到
        # （实测撞过——变异改到了注释，用例当然不红）。
        "old": "\nRRF_K = 1\n",
        "new": "\nRRF_K = 60\n",
    },
    {
        "name": "精排降级时伪造 0 分",
        "file": "api/retrieval/rerank.py",
        "old": """    else:
        # 一条都没打上分：整体按融合名次（§7.3"退回按名次取前 8"）。
        merged = list(ordered)""",
        "new": """    else:
        # 变异：给没打分的候选填 0 分（伪造一个"无相关"的结论）
        merged = [item.with_rerank(0.0) for item in ordered]""",
    },
    {
        "name": "临界带改成补充而不是替代（0.39 仍按 <0.40 拒）",
        "file": "api/retrieval/gate.py",
        "old": "    if not in_borderline and top1 < cfg.reject_low:",
        "new": "    if top1 < cfg.reject_low:  # 变异：临界带没被当成替代",
    },
    {
        "name": "多跳不看本跳阈值（只看已有事实）",
        "file": "api/retrieval/multihop.py",
        "old": """        if not outcome.passed:
            stop = "broken\"""",
        "new": """        if False:  # 变异：不看本跳是否过阈
            stop = "broken\"""",
    },
    {
        "name": "改写允许级联（去掉幂等守卫）",
        "file": "api/query/__init__.py",
        "old": """            if _inside_target(text, pos, key, targets):
                # 命中的原文已经处在某个正式说法内部，替换只会制造重复片段。
                break""",
        "new": "            pass  # 变异：去掉幂等守卫",
    },
]


def main() -> int:
    baseline_rc, baseline_tail = run(PYTEST_TARGETS)
    print(f"baseline rc={baseline_rc} :: {baseline_tail}")
    if baseline_rc != 0:
        print("基线不绿，变异测试无意义。先修基线。")
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
                print(f"[{mut['name']}] STILL GREEN（判据无效）:: {tail}")
            else:
                print(f"[{mut['name']}] RED (expected) :: {tail}")
        finally:
            shutil.copy2(backup, path)
            backup.unlink()

    restored_rc, restored_tail = run(PYTEST_TARGETS)
    print(f"restored rc={restored_rc} :: {restored_tail}")
    if restored_rc != 0:
        print("还原后仍然不绿——变异脚本没把文件改回去，先查这个。")
        return 1
    if undetected:
        print(f"\n以下变异没有被检出（对应判据形同虚设）: {undetected}")
        return 1
    print(f"\nALL {len(MUTATIONS)} MUTATIONS DETECTED AND RESTORED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
