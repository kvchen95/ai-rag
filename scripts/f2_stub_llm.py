#!/usr/bin/env python3
"""F2 联调用的**模型替身**：OpenAI 兼容的 `/chat/completions`。.

为什么需要它：本机没有 LLM 网关（`LLM_BASE_URL` 为空），B4 的生成环节在真实服务
里
走的是"系统档降级"。前端要验的却是 token → citations → done 这条主路径，
不接一个模型就永远看不到 `citations` 帧。

**它的边界要写清楚**：这个替身只保证"格式合法、内容来自证据"，不保证模型质量。
它把证据包第一条的第一句原样抄成答案并标上 [n]，因此必然通过 §10 的数字/专名
校验——这正好让前端拿到一条能点回原文的引用。判别器那一侧按固定 `SUPPORTED`
回答，同样只为了让第五道通过。

用法：

    python scripts/f2_stub_llm.py --port 8099

然后在 api 侧配 `LLM_BASE_URL=http://<host>:8099/v1`、`LLM_MODEL=stub-f2`。
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

EVIDENCE_HEAD = re.compile(r"^\[(\d+)\] 来源：(.+)$", re.MULTILINE)
VERBOSE = False


def evidence_blocks(prompt: str) -> list[tuple[int, str, str]]:
    r"""从提示词里切出 `[n] 来源：…\\n正文` 的证据块。."""
    blocks: list[tuple[int, str, str]] = []
    matches = list(EVIDENCE_HEAD.finditer(prompt))
    for index, match in enumerate(matches):
        n = int(match.group(1))
        source = match.group(2).strip()
        start = match.end()
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(prompt)
        )
        body = prompt[start:end].strip()
        # 证据包后面还有提示词的其他段落（问题、输出要求），
        # 遇到空行后的标题就停。
        body = body.split("\n\n")[0].strip()
        blocks.append((n, source, body))
    return blocks


def first_sentence(text: str) -> str:
    text = text.strip().replace("\n", " ")
    for sep in ("。", ". ", "；", "，"):
        head, found, _ = text.partition(sep)
        if found and head.strip():
            text = (head + sep).strip()
            break
    else:
        text = text[:120]
    # 句内的英文句点会让「切句 + 每句带引用」那道校验把它拆成两句，其中一句没有
    # [n]。
    # 替身不做语言处理，直接把句内句点换成中点：形状变了，数字与专名不变。
    return text.replace(".", "·").strip()


QUESTION_HEAD = re.compile(r"【问题】\s*(.+)$", re.DOTALL)


def extract_question(prompt: str) -> str:
    """从提示词里摘出用户问题，不能整段判。.

    证据包与系统提示词里也有"图"这类字，整段判会误伤。
    """
    match = QUESTION_HEAD.search(prompt)
    if match:
        return match.group(1).strip()
    # 退回最后一节非证据文本：真实提示词的尾部就是问题。
    return prompt.rsplit("\n\n", 1)[-1].strip()


def build_answer(prompt: str) -> dict[str, Any]:
    blocks = evidence_blocks(prompt)
    if not blocks:
        return {
            "status": "unknown",
            "answer": "证据包为空，未找到可验证依据。",
            "citations": [],
            "claims": [],
        }
    # 问题里提到"图/切片"时优先引用图表类证据：这样前端那条「WebP 切片」展示路径
    # 在真链路上也能被演示到（否则永远只引用纯文本切片）。
    question = extract_question(prompt)
    chosen = blocks[0]
    if "图" in question:
        figure_like = [b for b in blocks if "裁图" in b[2] or "图表" in b[2]]
        if figure_like:
            chosen = figure_like[0]
    n, _source, body = chosen
    sentence = first_sentence(body)
    answer = f"{sentence}[{n}]"
    return {
        "status": "answered",
        "answer": answer,
        "citations": [n],
        "claims": [{"text": sentence, "citations": [n]}],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "f2-stub-llm/0.1"

    def do_GET(self) -> None:  # noqa: N802 - http.server 的命名约定
        """健康检查与模型列表。compose healthcheck 与客户端探测都打这里。.

        为什么要有：没有 GET 端点时，healthcheck 只能用 TCP 探测，探不出
        "进程活着但 handler 挂了"这种半死状态；而且 [OI] 客户端偶尔会先
        拉一次 `/v1/models` 做能力探测，没有就会在真正请求前失败。
        """
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [{"id": "stub-f2", "object": "model"}],
                },
            )
            return
        if self.path.rstrip("/") in ("/health", "/healthz"):
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server 的命名约定
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        messages = body.get("messages") or []
        prompt = "\n".join(str(m.get("content") or "") for m in messages)
        if "SUPPORTED" in prompt:
            content = json.dumps(
                {"verdict": "SUPPORTED", "reason": "f2 stub"},
                ensure_ascii=False,
            )
        else:
            content = json.dumps(build_answer(prompt), ensure_ascii=False)
            if VERBOSE:
                print(f"[stub-llm] prompt={prompt[:1200]!r}", flush=True)
                print(f"[stub-llm] answer={content}", flush=True)
        self._json(
            200,
            {
                "id": "stub-f2",
                "object": "chat.completion",
                "model": body.get("model") or "stub-f2",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                    }
                ],
            },
        )

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆盖基类签名
        print(f"[stub-llm] {format % args}", flush=True)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="F2 stub LLM gateway")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument(
        "--verbose", action="store_true", help="打印提示词与生成的答案"
    )
    args = parser.parse_args()
    VERBOSE = args.verbose
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[stub-llm] listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
