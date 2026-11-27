"""Compare fresh-connection vs pooled keep-alive client against the LLM gateway.

Read-only probe; runs inside the api container; never prints the API key.
"""

import collections
import os
import time

import httpx

base = os.environ["LLM_BASE_URL"].rstrip("/")
key = os.environ.get("LLM_API_KEY", "")
model = os.environ["LLM_MODEL"]
headers = {"Authorization": f"Bearer {key}"} if key else {}
N = 30


def run(mode):
    """跑一轮连接模式探测：统计成功数、失败分类与延迟中位数。."""
    fails = collections.Counter()
    lat = []
    ok = 0
    t0 = time.monotonic()
    if mode == "pooled":
        client = httpx.Client(
            base_url=base,
            timeout=30.0,
            headers=headers,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=4,
                keepalive_expiry=30.0,
            ),
        )
    else:
        client = None
    for _i in range(N):
        t = time.monotonic()
        try:
            c = client or httpx.Client(
                base_url=base, timeout=30.0, headers=headers, trust_env=False
            )
            r = c.post(
                "/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "temperature": 0.0,
                    "stream": False,
                },
            )
            if mode != "pooled":
                c.close()
            if r.status_code == 200 and (r.json().get("choices") or []):
                ok += 1
            else:
                fails[f"HTTP{r.status_code}"] += 1
        except (httpx.HTTPError, ValueError) as exc:
            fails[f"{type(exc).__name__}: {str(exc)[:80]}"] += 1
        lat.append(time.monotonic() - t)
    if client:
        client.close()
    print(
        f"[{mode}] calls={N} ok={ok} fail={N-ok} "
        f"elapsed={time.monotonic()-t0:.1f}s "
        f"median={sorted(lat)[N//2]:.2f}s"
    )
    for k, v in fails.most_common():
        print(f"    FAIL x{v}: {k}")


for m in ("fresh", "pooled"):
    run(m)
