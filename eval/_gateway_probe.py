"""Read-only probe: measure live SSL-EOF rate of the configured LLM gateway.

Runs inside the api container so it uses exactly the same env as LlmClient.
Does NOT print the API key. 30 tiny calls, non-streaming, temperature=0.
"""
import os
import time
import collections
import httpx

base = os.environ["LLM_BASE_URL"].rstrip("/")
key = os.environ.get("LLM_API_KEY", "")
model = os.environ["LLM_MODEL"]
headers = {"Authorization": f"Bearer {key}"} if key else {}
N = 30
fails = collections.Counter()
lat = []
ok = 0
t0 = time.monotonic()
for i in range(N):
    t = time.monotonic()
    try:
        with httpx.Client(base_url=base, timeout=30.0, headers=headers, trust_env=False) as c:
            r = c.post("/chat/completions", json={"model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "temperature": 0.0, "stream": False})
        if r.status_code == 200 and (r.json().get("choices") or []):
            ok += 1
        else:
            fails[f"HTTP{r.status_code}"] += 1
    except Exception as exc:
        fails[f"{type(exc).__name__}: {str(exc)[:90]}"] += 1
    lat.append(time.monotonic() - t)
print(f"gateway={base}  model={model}")
print(f"calls={N}  ok={ok}  fail={N-ok}  elapsed={time.monotonic()-t0:.1f}s")
print(f"latency min/median/max = {min(lat):.2f}/{sorted(lat)[N//2]:.2f}/{max(lat):.2f}s")
for k, v in fails.most_common():
    print(f"  FAIL x{v}: {k}")
