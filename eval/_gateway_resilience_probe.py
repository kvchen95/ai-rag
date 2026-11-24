"""End-to-end verification: 30 calls through the REAL LlmClient path.

Counts final failures (after keep-alive pool + bounded retry). Prints latency,
retry usage, and failure kinds. Never prints the API key.
"""
import os
import time
import collections

from api.generate.llm import LlmClient, GenerationUnavailable

client = LlmClient(
    base_url=os.environ["LLM_BASE_URL"],
    model=os.environ["LLM_MODEL"],
    api_key=os.environ.get("LLM_API_KEY", ""),
    timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "30")),
    max_attempts=int(os.environ.get("LLM_MAX_ATTEMPTS", "4")),
    retry_backoff_s=float(os.environ.get("LLM_RETRY_BACKOFF_S", "0.3")),
    retry_budget_s=float(os.environ.get("LLM_RETRY_BUDGET_S", "30")),
)
N = 30
fails = collections.Counter()
lat = []
ok = 0
t0 = time.monotonic()
for i in range(N):
    t = time.monotonic()
    try:
        out = client.complete([{"role": "user", "content": "ping"}])
        ok += 1
    except GenerationUnavailable as exc:
        fails[f"{str(exc)[:80]}"] += 1
    lat.append(time.monotonic() - t)
print(f"client=LlmClient(max_attempts={client.max_attempts}) calls={N} ok={ok} final_fail={N-ok} elapsed={time.monotonic()-t0:.1f}s")
print(f"latency min/median/max = {min(lat):.2f}/{sorted(lat)[N//2]:.2f}/{max(lat):.2f}s")
for k, v in fails.most_common():
    print(f"  FINAL-FAIL x{v}: {k}")
