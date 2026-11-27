"""Generate the devops follow-up report JSON from the real eval reports."""

import json
import os
from datetime import UTC, datetime

ROOT = (
    "/app/eval/reports/"
    if os.path.isdir("/app/eval/reports")
    else "/workspace/ai-rag/eval/reports/"
)
rounds = [
    ("redline_20261125_003237.json", "1410-1459"),
    ("redline_20261125_003715.json", "1460-1509"),
    ("redline_20261125_004135.json", "1510-1559"),
]
v = []
for fn, window in rounds:
    with open(ROOT + fn) as fh:
        d = json.load(fh)
    qs = d["questions"]
    v.append(
        {
            "report": fn,
            "qa_log_window": window,
            "model_timeout": sum(
                1
                for q in qs
                if q.get("reject_reason") == "model_timeout"
                or q.get("actual") == "model_timeout"
            ),
            "RL1": next(
                (
                    f"{r['status']} {r['value']}"
                    for r in d["red_lines"]
                    if r["key"] == "RL1"
                ),
                None,
            ),
            "red_lines_all_pass": all(
                r["status"] == "PASS" for r in d["red_lines"]
            ),
            "RL4_value": next(
                (r["value"] for r in d["red_lines"] if r["key"] == "RL4"), ""
            ),
            "coverage": next(
                (
                    r["note"]
                    .split("覆盖率 = 应答题得到答案/应答题总数 = ", 1)[1]
                    .split("；")[0]
                    for r in d["red_lines"]
                    if r["key"] == "RL4"
                    and "覆盖率 = 应答题得到答案/应答题总数 = " in r["note"]
                ),
                None,
            ),
            "verdict": d["verdict"],
        }
    )

report = {
    "schema": "devops.llm_gateway_resilience/1.0",
    "task_id": "t_962f62ba",
    "generated_at": datetime.now(UTC).isoformat(),
    "endpoint": "https://api-slb.micuapi.ai/v1",
    "model": "deepseek-flash",
    "problem": {
        "symptom_1": (
            "5/35 answerable questions model_timeout (SSL "
            "UNEXPECTED_EOF_WHILE_READING)"
        ),
        "symptom_2": (
            "RL1 trap refusal 13/15 (redline_20261124_232015) vs 15/15 "
            "(redline_20261124_233521) with identical config, no concurrency"
        ),
    },
    "root_cause": {
        "primary": (
            "LlmClient.complete() created a brand-new httpx.Client per call -> "
            "fresh TCP+TLS handshake on every generation AND NLI judge call; "
            "third-party SLB intermittently terminates the handshake with SSL "
            "EOF. No retry existed: every transient EOF became "
            "fallback(model_timeout)."
        ),
        "evidence": {
            "qa_log_ids_1150_1309": {
                "model_timeout_rows": 21,
                "ssl_eof_rows": 20,
                "genuine_30s_timeout_rows": 1,
                "ssl_eof_latency_ms_range": [1290, 12740],
            },
            "live_probe_fresh_connections": {
                "attempt": 1,
                "calls": 30,
                "fail": 7,
                "fail_kind": "ConnectError SSL UNEXPECTED_EOF",
            },
            "live_probe_fresh_connections_repeat": {
                "attempt": 2,
                "calls": 30,
                "fail": 5,
                "fail_kind": "ConnectError SSL UNEXPECTED_EOF",
            },
            "live_probe_pooled_keepalive": {
                "calls": 30,
                "fail": 1,
                "median_latency_s": 2.26,
                "vs_fresh_median_s": 3.10,
            },
            "rl1_flip_detail": {
                "flipped_traps": ["T03", "T05"],
                "t03_top1_identical": 0.424983,
                "t05_top1_identical": 0.832604,
                "change": (
                    "reject_reason model_timeout -> no_evidence; gate1 pass -> "
                    "reject_none; trap_safe False -> True"
                ),
                "conclusion": (
                    "RL1 divergence is 100% driven by gateway "
                    "availability, NOT by gate/threshold/golden changes."
                ),
            },
        },
        "ruled_out": {
            "connection_pool_stale_reuse": (
                "No pool existed — a new client was constructed per call; "
                "pooled keep-alive REDUCED failures 5/30 -> 1/30."
            ),
            "eval_concurrency": (
                "Eval ran serially with no concurrent jobs; qa_budget_s is "
                "stored but not enforced as a hard cutoff."
            ),
            "qa_budget_30s": (
                "20/21 failures completed in 1.3-12.7s (SSL EOF), only 1 was a "
                "real 30s timeout."
            ),
            "thresholds_or_golden": (
                "top1 identical across the two divergent rounds; 0.20/0.40 "
                "unchanged; golden unchanged."
            ),
        },
    },
    "fix": {
        "files": [
            "api/generate/llm.py",
            "api/config.py",
            "api/routers/qa.py",
            "infra/docker-compose.yml",
            ".env.example",
            "api/tests/test_generate_llm_retry.py",
        ],
        "shared_keepalive_pool": (
            "httpx.Client keyed by (base_url, api_key, timeout), "
            "max_keepalive_connections=8, keepalive_expiry=30s; shared by "
            "generation and judge clients."
        ),
        "bounded_retry": {
            "max_attempts": 6,
            "backoff_base_s": 0.3,
            "backoff": "exponential with jitter",
            "total_budget_s": 45,
            "retry_only": (
                "transient GenerationUnavailable (SSL EOF / timeout / 5xx / "
                "empty response)"
            ),
            "no_retry": "GenerationRejected (4xx) and DraftFormatError",
        },
        "tests": {"new_regression_tests": 4, "full_suite_exit_code": 0},
    },
    "verification": {
        "post_fix_e2e_probe": {
            "calls": 30,
            "success": 30,
            "final_fail": 0,
            "retry_events": 1,
        },
        "three_serial_eval_rounds": v,
        "db_independent_check": {
            "qa_log_ids": "1410-1559",
            "calls": 150,
            "model_timeout": 0,
            "ssl_eof": 0,
        },
        "intermediate_finding": (
            "At 4 attempts / 30s budget, one cold first call (B01) failed 4 "
            "consecutive SSL EOFs during a ~20s SLB outage window -> budget "
            "raised to 6 attempts / 45s."
        ),
    },
    "acceptance": {
        "model_timeout_zero_all_three_rounds": True,
        "RL1_stable_15_15": True,
        "no_13_15_flap": True,
    },
    "impact_on_eval_conclusions": {
        "publishability": (
            "STILL NOT_PUBLISHABLE — this task fixes stability only; coverage "
            "remains ~2/35 (5.7%). Over-refusal (20 draft_status) is owned by "
            "backend follow-up t_e7b858e4; threshold recalibration by "
            "data-analyst."
        ),
        "what_changed": (
            "Red-line verdicts are now reproducible: the 13/15 FAIL was an "
            "infrastructure artifact, not a real gate regression. Single-round "
            "PASS can now be trusted as a gate signal for RL1."
        ),
        "what_did_not_change": (
            "Retrieval, gate thresholds 0.20/0.40, golden set, and the 26/35 "
            "generation-layer blocks are untouched."
        ),
        "residual_risk": (
            "If the SLB outage exceeds 45s or 6 attempts, "
            "fallback(model_timeout) still occurs; it is logged via llm_retry "
            "and remains fail-closed (no unverified answer is emitted)."
        ),
    },
    "rollback": {
        "steps": (
            "Set LLM_MAX_ATTEMPTS=1 and remove the shared pool (restore old "
            "`with httpx.Client(...)` block), then `docker compose --env-file "
            ".env -f infra/docker-compose.yml up -d api`."
        ),
        "effect": (
            "Restores the exact pre-fix behavior; eval results become "
            "non-reproducible again (13/15 <-> 15/15)."
        ),
    },
    "boundaries": {
        "golden_changed": False,
        "thresholds_changed": False,
        "db_cleared": False,
        "qa_budget_s_changed": False,
    },
}
path = ROOT + "llm_gateway_resilience_20261125_004135.json"
with open(path, "w") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print("wrote", path)
print(json.dumps(v, ensure_ascii=False, indent=2))
