"""B4 的变异测试：把每条关键判据故意改坏，确认对应用例会红，再改回。

判据从没红过 = 和被删掉等价。这里每组**只改一处语义、不改数量**——只改数量的变异
会被"条数"类断言一抓一个准，让人误以为判据很严；只改语义的变异才会暴露
"只约束了一边"的盲区。

十组，对应卡片的验收与实现中修掉的四个真缺陷：

1. 证据包不合并同父片（每个子片各占一条证据）→ 合并用例必须红
2. 证据包不删子片重叠（同一句出现两次）→ 重叠用例必须红
3. 封顶时从高分往低分丢（丢错了方向）→ 预算用例必须红
4. 第一道只扫 `citations` 字段、不扫正文 → 正文越界用例必须红
5. 第二道只看 `claims`、不看 answer 的句子 → "模型没报的句子"用例必须红
6. 第三道用**整包**证据而不是被引证据 → "引错编号"用例必须红
7. 第四道通过就 return（第五道永远不执行）→ 五道全跑用例必须红
8. `gate2_verdict` 只看 `judge is None`（把 skipped 读成 not_supported）→ 审计用例必须红
9. 空 claim 被 `continue` 掉（第二道放过空主张）→ 空主张用例必须红
10. `token` 在生成之后就发（不等校验）→ "校验不过无 token"用例必须红

每组都验：改坏后 rc != 0（真的红了），还原后 rc == 0（真的改回来了）。
最后把所有"改坏了却仍然全绿"的变异列出来并以非零退出。

用法：
    docker compose exec -T api python scripts/b4_mutation_check.py
    # 或宿主侧（需 pytest；纯逻辑用例不需要任何外部服务）
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
    "api/tests/test_generate_evidence.py",
    "api/tests/test_verify_checks.py",
    "api/tests/test_verify_numbers.py",
    "api/tests/test_qa_sse.py",
    "api/tests/test_audit_snapshot.py",
]


def run(targets: list[str]) -> tuple[int, str]:
    env = dict(os.environ)
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(var, None)
    proc = subprocess.run(  # noqa: S603
        # 不自己传 `-q`：`api/pytest.ini` 的 addopts 里已经有一个，叠成 `-qq`
        # 会把"29 passed"这行摘要一起抑制掉（实测：stdout 里只剩进度点）。
        [sys.executable, "-m", "pytest", "-c", "api/pytest.ini", "--no-header", "-p", "no:cacheprovider", *targets],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    combined = proc.stdout + proc.stderr
    # 摘要行只认"数字 passed/failed"那几种，且先在 **stdout** 里找：
    # stderr 排在后面，从后往前扫会先撞上 pytest-asyncio 的弃用警告散文
    # （早先的写法报的"摘要"就是那行 warning，看着像没跑测试）。
    summary = ""
    for text in (proc.stdout, combined):
        for line in reversed(text.strip().splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            head = stripped.split()[0]
            if head.isdigit() and ("passed" in stripped or "failed" in stripped or "error" in stripped):
                summary = stripped
                break
        if summary:
            break
    if not summary:
        summary = (combined.strip().splitlines() or ["<no output>"])[-1][:300]
    # 红的时候还要说清是哪条用例红的——否则只看到 rc=1 不知道该看什么。
    failures = [ln for ln in combined.splitlines() if ln.startswith("FAILED") or ln.startswith("ERROR api")]
    if failures:
        summary = summary + " | " + "; ".join(failures[:3])
    return proc.returncode, summary


def mutate(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"变异锚点未找到: {old[:80]!r} in {path.name}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    # 校验文件真的变了：锚点落在 docstring 里的同名文本上时，变异等于没改，
    # 于是报"判据无效"——而真相是变异根本没生效（B3 实测撞过这个坑）。
    assert path.read_text(encoding="utf-8") != text, f"变异没有改变文件内容: {path.name}"


MUTATIONS = [
    {
        "name": "证据包不合并同父片",
        "file": "api/generate/evidence.py",
        # 锚点带上下文：`_group_by_parent` 在 docstring 里也出现过。
        "old": """    groups: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for candidate in candidates:
        key = candidate.parent_id or f"__self__:{candidate.chunk_id}\"""",
        "new": """    groups: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for candidate in candidates:
        key = f"__self__:{candidate.chunk_id}"  # 变异：不按父片分组""",
    },
    {
        "name": "合并时不删子片重叠",
        "file": "api/generate/evidence.py",
        "old": """        overlap = _overlap_length(merged, text)
        merged = merged + text[overlap:]""",
        "new": """        overlap = 0  # 变异：不删重叠
        merged = merged + text[overlap:]""",
    },
    {
        "name": "封顶时从高分往低分丢（方向反了）",
        "file": "api/generate/evidence.py",
        "old": """    kept: list[tuple[Candidate, tuple[str, ...], str, ChunkText]] = []
    dropped: list[str] = []
    used = 0
    for index, entry in enumerate(merged):""",
        "new": """    kept: list[tuple[Candidate, tuple[str, ...], str, ChunkText]] = []
    dropped: list[str] = []
    used = 0
    for index, entry in enumerate(reversed(merged)):  # 变异：从低分开始装""",
    },
    {
        "name": "第一道只扫 citations 字段，不扫正文",
        "file": "api/verify/checks.py",
        "old": """    for number in parse_citations(draft.answer):
        if number not in numbers and f"[{number}]" not in offenders:
            offenders.append(f"[{number}]")""",
        "new": """    for number in ():  # 变异：不扫正文里的角标
        if number not in numbers and f"[{number}]" not in offenders:
            offenders.append(f"[{number}]")""",
    },
    {
        "name": "第二道只看 claims，不看 answer 的句子",
        "file": "api/verify/checks.py",
        "old": """    sentences = _draft_sentences(draft)
    if not sentences:""",
        "new": """    sentences = tuple(
        Sentence(text=c.text, citations=c.citations, bare=c.text) for c in draft.claims
    )  # 变异：丢掉 answer 里模型没报的句子
    if not sentences:""",
    },
    {
        "name": "第三道用整包证据而不是被引证据",
        "file": "api/verify/checks.py",
        "old": """        cited = referable_evidence(pack, sentence.citations)
        if not cited:
            # 没有可查的证据只可能因为编号越界，而第一道已经拦了。
            continue""",
        "new": """        cited = pack.items  # 变异：整包都算"被引"
        if not cited:
            continue""",
    },
    {
        "name": "第四道通过就 return（第五道永远不执行）",
        "file": "api/verify/checks.py",
        "old": """        elif check == CHECK_NLI:
            failure, judge_result = check_nli(judge_client, question, draft, pack)""",
        "new": """        elif check == CHECK_NLI:
            failure, judge_result = check_nli(judge_client, question, draft, pack)
            if failure is None:
                return VerificationReport(
                    passed=True,
                    checks_run=tuple(ran),
                    judge=judge_result,
                    answer=draft.answer,
                    citations=draft.citations,
                    cited_evidence=referable_evidence(pack, draft.citations),
                    elapsed_ms=0.0,
                )  # 变异：提前收尾""",
    },
    {
        "name": "gate2 只看 judge is None（skipped 被读成 not_supported）",
        "file": "api/verify/checks.py",
        "old": """        if self.judge is None:
            return "skipped"
        if self.judge.skipped:
            return "skipped\"""",
        "new": """        if self.judge is None:
            return "skipped"
        if False:  # 变异：不看 judge.skipped
            return "skipped\"""",
    },
    {
        "name": "空 claim 被跳过（第二道放过空主张）",
        "file": "api/verify/sentences.py",
        "old": """        if not bare:
            primary.append(Sentence(text=claim.strip(), citations=parse_citations(claim), bare=""))
            continue""",
        "new": """        if not bare:
            continue  # 变异：空主张直接丢掉""",
    },
    {
        "name": "score_snapshot 重复字典键（分数分布被静默覆盖）",
        "file": "api/retrieval/pipeline.py",
        "old": '            "rerank_scores": [round(s, 6) for s in rerank_scores],',
        "new": '            "rerank": [round(s, 6) for s in rerank_scores],  # 变异：与下面的键重名',
    },
    {
        "name": "token 在校验之前就发（不等校验结论）",
        "file": "api/qa/orchestrator.py",
        "old": """        send(ev.step(ev.PHASE_VERIFY, hop=snapshot.hop_count, elapsed_ms=_ms(started)))
        report = verify_draft(draft, pack, question=question, judge_client=self.judge_client)""",
        "new": """        for _piece in ev.chunk_text(draft.answer, self.token_chunk_chars):
            send(ev.token(_piece))  # 变异：不等校验就发正文
        send(ev.step(ev.PHASE_VERIFY, hop=snapshot.hop_count, elapsed_ms=_ms(started)))
        report = verify_draft(draft, pack, question=question, judge_client=self.judge_client)""",
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
