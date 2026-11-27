#!/usr/bin/env python3
"""F2 前端验收的可复现取证：不点浏览器，只用 HTTP 验前端依赖的那几条契约。.

为什么用 stdlib 的 urllib 而不引 httpx/requests：这个脚本要在**宿主**上直接跑
（`python3 scripts/verify_f2_frontend.py`），宿主的 python 未必装了第三方库；
而它要验的东西（状态码、响应头、SSE 帧顺序）用 urllib 全都能拿到。
依赖越少，越可能在别人的机器上一次跑通。

四条判据（任一不成立即非零退出）：

1. **SSE 顺序**：`done` 是最后一帧；拒答路径没有 `token`；`citations` 在 `done`
 之前。
2. **角标可点回**：正文里出现的 `[n]` 都能在 `citations` 里找到，每项带
   doc_key / doc_name / page_num / snippet / chunk_id
   （bbox 可缺，缺了前端降级）。
3. **样本与切片图四态**：200（有权限）/ 401（无凭证）/ 403（无权限）/ 404（不存
在）。
4. **错误体不泄露**：没有 Traceback、DSN、容器内路径、异常类名。

用法（宿主，需要 API 在跑）：

    python3 scripts/verify_f2_frontend.py --api http://127.0.0.1:8000

`--secret` 用来现签开发 JWT（与 AUTH_JWT_SECRET 同值）。宿主没装 pyjwt 时
自动退回用容器里的 python 签（见 `_mint_jwt`）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

# 演示样本：覆盖"有证据作答 / 有权限但库外问题 / 多实体"三种出口。
DEMO_QUESTIONS = ("住宿标准是多少", "报销标准是多少", "差旅和报销的区别")

CITE_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

LEAK_PATTERNS = (
    re.compile(r"Traceback", re.I),
    re.compile(r"postgresql(\+psycopg)?://", re.I),
    re.compile(r"/app/(api|db|scripts)/"),
    re.compile(r"psycopg|sqlalchemy|pdfplumber", re.I),
    re.compile(r'File "', re.I),
)

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(
        f"  [{'PASS' if ok else 'FAIL'}] {name}"
        + (f" — {detail}" if detail else ""),
        flush=True,
    )


def _mint_jwt(
    secret: str, *, sub: str, depts: list[str], roles: list[str]
) -> str:
    """现签一份开发 JWT：宿主有 pyjwt 就用宿主，没有就借容器里的 python。."""
    now = int(time.time())
    payload = {
        "sub": sub,
        "user_depts": depts,
        "user_roles": roles,
        "iat": now,
        "exp": now + 3600,
    }
    try:
        import jwt  # noqa: PLC0415

        return jwt.encode(payload, secret, algorithm="HS256")
    except ModuleNotFoundError:
        pass
    code = (
        "import jwt,json,sys;"
        "p=json.loads(sys.argv[1]);"
        "print(jwt.encode(p, sys.argv[2], algorithm='HS256'))"
    )
    out = subprocess.run(
        [
            "docker",
            "exec",
            "ai-rag-api",
            "python",
            "-c",
            code,
            json.dumps(payload),
            secret,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _request(
    api: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 60.0,
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{api}{path}", data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - 本地地址
            return (
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(),
            )
    except urllib.error.HTTPError as error:
        return (
            error.code,
            {k.lower(): v for k, v in error.headers.items()},
            error.read(),
        )


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if not data_lines:
            continue
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            frames.append((name, {"__unparsed__": True}))
            continue
        frames.append((name, payload))
    return frames


def check_event_order(
    label: str, frames: list[tuple[str, dict[str, Any]]]
) -> None:
    names = [name for name, _ in frames]
    check(f"{label}: 有事件", bool(names), f"{len(names)} frame(s)")
    if not names:
        return
    check(
        f"{label}: done 是最后一帧", names[-1] == "done", f"最后是 {names[-1:]}"
    )
    check(f"{label}: step 打头", names[0] == "step", f"第一帧 {names[:1]}")
    if any(name in {"clarify", "fallback"} for name in names):
        # §10.4：拒答路径只有进度与收尾，没有逐字。
        check(f"{label}: 拒答路径无 token", "token" not in names, f"{names}")
    if "citations" in names:
        check(
            f"{label}: citations 先于 done",
            names.index("citations") < names.index("done"),
        )


def check_citation_fields(
    label: str, frames: list[tuple[str, dict[str, Any]]]
) -> None:
    items: list[dict[str, Any]] = []
    text = ""
    for name, payload in frames:
        if name == "citations":
            items = payload.get("items") or []
        elif name == "token":
            text += str(payload.get("delta") or "")
    if not items:
        check(f"{label}: 本轮无引用（无需检查角标）", True)
        return
    required = ("n", "doc_key", "doc_name", "page_num", "snippet", "chunk_id")
    missing = sorted({k for item in items for k in required if k not in item})
    check(f"{label}: 引用字段齐", not missing, f"缺 {missing}")

    referenced: set[int] = set()
    for match in CITE_RE.finditer(text):
        referenced.update(
            int(p) for p in match.group(1).split(",") if p.strip().isdigit()
        )
    known = {int(item["n"]) for item in items}
    orphans = sorted(referenced - known)
    check(
        f"{label}: 角标全部可点回",
        not orphans,
        f"正文 {sorted(referenced)} / citations {sorted(known)} / "
        f"点不动 {orphans}",
    )
    check(
        f"{label}: bbox 是数组或缺省（缺省即降级展示）",
        all(
            isinstance(item.get("bbox"), list) or item.get("bbox") is None
            for item in items
        ),
        f"{[(item['n'], bool(item.get('bbox'))) for item in items]}",
    )


def check_no_leak(leaks: list[tuple[str, str]]) -> None:
    for label, text in leaks:
        hits = [
            pattern.pattern for pattern in LEAK_PATTERNS if pattern.search(text)
        ]
        check(f"不泄露：{label}", not hits, f"命中 {hits}" if hits else "")


def main() -> int:
    parser = argparse.ArgumentParser(description="F2 frontend acceptance probe")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--secret", default="dev-only-change-me")
    args = parser.parse_args()
    api = args.api.rstrip("/")

    fin = _mint_jwt(args.secret, sub="u_fin", depts=["fin"], roles=["staff"])
    stranger = _mint_jwt(
        args.secret, sub="u_eng", depts=["eng"], roles=["viewer"]
    )

    print("=" * 78)
    print(
        f"F2 验收：SSE 顺序 / 角标可点回 / 样本四态 / 错误体不泄露  (api={api})"
    )
    print("=" * 78)

    leaks: list[tuple[str, str]] = []
    for question in DEMO_QUESTIONS:
        status, _headers, raw = _request(
            api,
            "/api/v1/qa/stream",
            method="POST",
            token=fin,
            body={"question": question},
        )
        body = raw.decode("utf-8", "replace")
        frames = parse_sse(body)
        print(f"\n问：{question}（HTTP {status}）")
        print(f"  事件序列：{[name for name, _ in frames]}")
        for name, payload in frames:
            if name in ("fallback", "done", "citations"):
                print(
                    f"  {name}: {json.dumps(payload, ensure_ascii=False)[:220]}"
                )
        check(f"{question}: 流式响应 200", status == 200, f"{status}")
        check_event_order(question, frames)
        check_citation_fields(question, frames)
        leaks.append((f"问答流「{question}」", body))

    print("\n[接口四态]")
    four = (
        ("/api/v1/samples/policy/travel-cn.pdf", None, 401, "样本：无凭证 401"),
        ("/api/v1/samples/policy/travel-cn.pdf", fin, 200, "样本：有权限 200"),
        (
            "/api/v1/samples/policy/travel-cn.pdf",
            stranger,
            403,
            "样本：无权限 403",
        ),
        (
            "/api/v1/samples/policy/does-not-exist.pdf",
            fin,
            404,
            "样本：不存在 404",
        ),
        (
            "/api/v1/chunks/ch_fig_f2_1/figure",
            stranger,
            403,
            "切片图：无权限 403",
        ),
        ("/api/v1/chunks/no_such_chunk/figure", fin, 404, "切片图：不存在 404"),
    )
    for path, token, expect, label in four:
        status, headers, raw = _request(api, path, token=token)
        check(label, status == expect, f"{status}")
        if expect == 200:
            content_type = headers.get("content-type", "")
            expect_type = (
                "image/webp" if path.endswith("/figure") else "application/pdf"
            )
            check(
                f"{label}：content-type 是 {expect_type}",
                content_type.startswith(expect_type),
                content_type,
            )
        else:
            leaks.append(
                (f"{path} -> {status}", raw.decode("utf-8", "replace"))
            )

    bad_status, _headers, bad_raw = _request(
        api,
        "/api/v1/qa/stream",
        method="POST",
        token="not-a-jwt",
        body={"question": "x"},
    )
    check("问答：坏凭证 401", bad_status == 401, f"{bad_status}")
    leaks.append(("坏凭证 401", bad_raw.decode("utf-8", "replace")))

    print("\n[不泄露]")
    check_no_leak(leaks)

    failed = [name for name, ok, _ in CHECKS if not ok]
    print("\n" + "=" * 78)
    print(f"判据 {len(CHECKS)} 条，失败 {len(failed)} 条")
    for name in failed:
        print(f"  FAIL {name}")
    print("=" * 78)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
