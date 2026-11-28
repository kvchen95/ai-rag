"""Chinese message catalog for scripts/b4_mutation_check.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # b4_mutation_check.py:117 (MUTATE)
    "MUTATE_01": "变异锚点未找到: ",
    # b4_mutation_check.py:123 (MUTATE)
    "MUTATE_02": "变异没有改变文件内容: ",
    # b4_mutation_check.py:128 (NAME)
    "NAME_01": "证据包不合并同父片",
    # b4_mutation_check.py:135 (NEW)
    "NEW_01": (
        "    groups: dict[str, list[Candidate]] = {}\n"
        "    order: list[str] = []\n"
        "    for candidate in candidates:\n"
        "        key = f\"__self__:{candidate.chunk_id}\"  # "
        "变异：不按父片分组"
    ),
    # b4_mutation_check.py:141 (NAME)
    "NAME_02": "合并时不删子片重叠",
    # b4_mutation_check.py:145 (NEW)
    "NEW_02": (
        "        overlap = 0  # 变异：不删重叠\n"
        "        merged = merged + text[overlap:]"
    ),
    # b4_mutation_check.py:149 (NAME)
    "NAME_03": "封顶时从高分往低分丢（方向反了）",
    # b4_mutation_check.py:159 (NEW)
    "NEW_03": (
        "    kept: list[tuple[Candidate, tuple[str, ...], str, ChunkText]] "
        "= []\n"
        "    dropped: list[str] = []\n"
        "    used = 0\n"
        "    for index, entry in enumerate(reversed(merged)):  # "
        "变异：从低分开始装"
    ),
    # b4_mutation_check.py:168 (NAME)
    "NAME_04": "第一道只扫 citations 字段，不扫正文",
    # b4_mutation_check.py:173 (NEW)
    "NEW_04": (
        "    for number in ():  # 变异：不扫正文里的角标\n"
        "        if number not in numbers and f\"[{number}]\" not in "
        "offenders:\n"
        "            offenders.append(f\"[{number}]\")"
    ),
    # b4_mutation_check.py:178 (NAME)
    "NAME_05": "第二道只看 claims，不看 answer 的句子",
    # b4_mutation_check.py:183 (NEW)
    "NEW_05": (
        "    sentences = tuple(\n"
        "        Sentence(text=c.text, citations=c.citations, bare=c.text) "
        "for c in draft.claims\n"
        "    )  # 变异：丢掉 answer 里模型没报的句子\n"
        "    if not sentences:"
    ),
    # b4_mutation_check.py:192 (NAME)
    "NAME_06": "第三道用整包证据而不是被引证据",
    # b4_mutation_check.py:194 (OLD)
    "OLD_01": (
        "        cited = referable_evidence(pack, sentence.citations)\n"
        "        if not cited:\n"
        "            # "
        "没有可查的证据只可能因为编号越界，而第一道已经拦了。\n"
        "            continue"
    ),
    # b4_mutation_check.py:198 (NEW)
    "NEW_06": (
        "        cited = pack.items  # 变异：整包都算\"被引\"\n"
        "        if not cited:\n"
        "            continue"
    ),
    # b4_mutation_check.py:203 (NAME)
    "NAME_07": "第四道通过就 return（第五道永远不执行）",
    # b4_mutation_check.py:211 (NEW)
    "NEW_07": (
        "        elif check == CHECK_NLI:\n"
        "            failure, judge_result = check_nli(judge_client, "
        "question, draft, pack)\n"
        "            if failure is None:\n"
        "                return VerificationReport(\n"
        "                    passed=True,\n"
        "                    checks_run=tuple(ran),\n"
        "                    judge=judge_result,\n"
        "                    answer=draft.answer,\n"
        "                    citations=draft.citations,\n"
        "                    cited_evidence=referable_evidence(pack, "
        "draft.citations),\n"
        "                    elapsed_ms=0.0,\n"
        "                )  # 变异：提前收尾"
    ),
    # b4_mutation_check.py:229 (NAME)
    "NAME_08": "gate2 只看 judge is None（skipped 被读成 not_supported）",
    # b4_mutation_check.py:235 (NEW)
    "NEW_08": (
        "        if self.judge is None:\n"
        "            return \"skipped\"\n"
        "        if False:  # 变异：不看 judge.skipped\n"
        "            return \"skipped\""
    ),
    # b4_mutation_check.py:241 (NAME)
    "NAME_09": "空 claim 被跳过（第二道放过空主张）",
    # b4_mutation_check.py:249 (NEW)
    "NEW_09": (
        "        if not bare:\n"
        "            continue  # 变异：空主张直接丢掉"
    ),
    # b4_mutation_check.py:253 (NAME)
    "NAME_10": "score_snapshot 重复字典键（分数分布被静默覆盖）",
    # b4_mutation_check.py:257 (NEW)
    "NEW_10": (
        "            \"rerank\": [round(s, 6) for s in rerank_scores],  # "
        "变异：与下面的键重名"
    ),
    # b4_mutation_check.py:261 (NAME)
    "NAME_11": "token 在校验之前就发（不等校验结论）",
    # b4_mutation_check.py:270 (NEW)
    "NEW_11": (
        "        for _piece in ev.chunk_text(draft.answer, "
        "self.token_chunk_chars):\n"
        "            send(ev.token(_piece))  # 变异：不等校验就发正文\n"
        "        send(ev.step(ev.PHASE_VERIFY, hop=snapshot.hop_count, "
        "elapsed_ms=_ms(started)))\n"
        "        report = verify_draft(draft, pack, question=question, "
        "judge_client=self.judge_client)"
    ),
    # b4_mutation_check.py:287 (PRINT)
    "PRINT_01": "基线不绿，变异测试无意义。先修基线。",
    # b4_mutation_check.py:302 (PRINT)
    "PRINT_02": "] STILL GREEN（判据无效）:: ",
    # b4_mutation_check.py:312 (PRINT)
    "PRINT_03": "还原后仍然不绿——变异脚本没把文件改回去，先查这个。",
    # b4_mutation_check.py:315 (PRINT)
    "PRINT_04": "\n以下变异没有被检出（对应判据形同虚设）: ",
}
