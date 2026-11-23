"""F2 联调用的故障替身：对所有请求返回 503，响应体故意夹带"内部细节"。

用途只有一个：验证前端「503 不泄露内部堆栈」。如果界面把响应体原样渲染出来，
这段 Traceback 与内网路径就会出现在屏幕上。它不参与任何业务，跑完即弃。

    python scripts/f2_stub_503.py --port 8098
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY = {
    "error": {
        "code": "index_stale",
        "message": (
            'Traceback (most recent call last):\n  File "/app/api/retrieval/pipeline.py", '
            "line 88, in retrieve\n    raise IndexStale(dsn=\"postgresql://ai_rag:pw@postgres/ai_rag\")\n"
            "psycopg.OperationalError: server closed the connection unexpectedly"
        ),
        "request_id": "stub-503-request",
    }
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server 约定的方法名
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        data = json.dumps(BODY, ensure_ascii=False).encode("utf-8")
        self.send_response(503)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[stub-503] {format % args}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
