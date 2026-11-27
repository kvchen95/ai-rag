#!/usr/bin/env python3
"""黄金 30 题评测：调 QA SSE 接口，判分，算四条红线，写报告。.

口径见详细设计.md §11/§12。这个脚本只做三件事，且刻意只做这三件：

1. **把评测集变成可复核的事实**：每题一条 SSE 流，事件序列、答案、引用、
门控判定全部落进 JSON，报告里每个数字都能指到某一行。
2. **按 §12.1/§12.2 的口径算四条红线**：陷阱拒答率、数值准确率、角标可点回率、
忠实度优先于覆盖率。分母一律写在报告里，不写"约"。
3. **拒绝在前提不成立时给绿灯**：语料 0 切片、生成模型未配置、黄金集 doc_key
是 TBD 占位时，对应红线记 `NOT_MEASURABLE` 而不是"跳过"。
"全拒答"能把陷阱拒答率刷到 100%，所以红线 1 另加一条判别力闸：
应答题里至少要有一题真的拿到了证据，否则记真空（同样 `NOT_MEASURABLE`）。

用法（推荐在 api 容器内跑，省去 token 与 DB 连接配置）：

docker compose exec -T api python eval/run_eval.py docker compose exec
-T api python eval/run_eval.py --only TR01,TR05 --category trap

宿主机跑（需要自己给 token 或 secret）：

    python3 eval/run_eval.py --base-url http://127.0.0.1:8000
        --token *** --dsn postgresql://ai_rag:***@127.0.0.1:5432/ai_rag

退出码：0 四条红线全过；1 任一条不达标；
2 没有不达标但有不可测项（不得据此发布）；3 评测器自身/传输失败。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import sys
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = REPO_ROOT / "eval" / "golden_questions_v1.jsonl"
DEFAULT_OUT_DIR = REPO_ROOT / "eval" / "reports"

# 按脚本路径运行（`python3 eval/run_eval.py`）时，Python 只把 `eval/` 放进
# sys.path，`api` 包不可导入——于是本可以在宿主直接签发 token 的路径永远走不到，
# 只能去容器里跑。这里把仓库根补进去，宿主运行与容器内运行行为一致。
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_dotenv(path: Path) -> dict[str, str]:
    """读仓库根 `.env`（没有就返回空）。只做 `KEY=VALUE`，不引第三方依赖。.

    为什么要读：`.env` 里可能有 `AUTH_JWT_SECRET` 与 `DATABASE_URL`。

    容器内跑时它们由 compose 注入，宿主跑时没人注入——不读 `.env` 的话，
    "已经配好了"和"没配"在脚本看来完全一样，报错还会指向错误的原因。
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


ANSWERABLE_VERDICTS = ("pass", "partial")
REFUSAL_VERDICTS = ("reject_none", "reject_low")
GATE_PASSED_VERDICTS = ("pass", "partial", "unverifiable")
SAFE_TRAP_VERDICTS = ("reject_none", "reject_low", "clarify")

# §12.2 的四个门禁值。分母的定义写在 RED_LINE_DEFS，报告原样带出。
RL_TRAP_RATE = 1.00
RL_NUMERIC_RATE = 0.96
RL_CITATION_RATE = 0.98

RED_LINE_DEFS = {
    "RL1": "陷阱题拒答率 = 安全拒答/澄清的陷阱题数 ÷ 陷阱题总数（要求 100%），"
    '外加判别力闸：应答题中至少 1 题拿到证据，否则本指标是"全拒答"真空态',
    "RL2": (
        "数值准确率 = 数值断言全部能在被引证据中找到的题数 ÷ "
        "含数值断言的已答题数（≥96%）"
    ),
    "RL3": (
        "角标可点回率 = 能在 PG 中定位"
        "（chunk_id + doc_key + page_num + bbox）的引用数 ÷ "
        "总引用数（≥98%）"
    ),
    "RL4": (
        '忠实度优先于覆盖率 = 已答题中"每句有引用、数字有据、无禁用措辞"'
        "的比例（要求 100%）；覆盖率单独报告，不允许用覆盖率换取忠实度"
    ),
}

_CITE_MARK = re.compile(r"\[[0-9]{1,3}\]")
_NUMBER = re.compile(r"[0-9][0-9,，]*(?:\.[0-9]+)?(?:%|％)?")
_SENTENCE = re.compile(r"[^。！？!?\n]+")


# --------------------------------------------------------------------- 基础


def b64url(data: bytes) -> str:
    """base64url 编码（去掉 `=` 填充）。."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def mint_hs256(secret: str, claims: dict[str, Any]) -> str:
    """最小 HS256 签发（不引 pyjwt：评测脚本要能在没有三方库的宿主机跑）。."""
    header = b64url(
        json.dumps(
            {"alg": "HS256", "typ": "JWT"}, separators=(",", ":")
        ).encode()
    )
    payload = b64url(
        json.dumps(claims, ensure_ascii=False, separators=(",", ":")).encode()
    )
    signature = hmac.new(
        secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256
    ).digest()
    return f"{header}.{payload}.{b64url(signature)}"


def _seed_env_from_dotenv() -> list[str]:
    """把 `.env` 里进程还没设的键补进 os.environ。返回补了哪些键（不含值）。.

    只补不覆盖：命令行导出或 compose 注入的值优先，`.env` 是兜底。
    """
    filled: list[str] = []
    for key, value in load_dotenv(REPO_ROOT / ".env").items():
        if key not in os.environ and value:
            os.environ[key] = value
            filled.append(key)
    return filled


def build_token(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (token, 来源说明)。.

    优先用服务自己的 `mint_dev_token`，保证 claim 名与配置一致。.
    """
    if args.token:
        return args.token, "命令行 --token"
    _seed_env_from_dotenv()
    # 容器内、或宿主上 `api` 可导入时走这条：不碰 secret，也不猜 claim 名。
    try:
        from api.config import get_settings
        from api.identity import mint_dev_token

        settings = get_settings()
        token = mint_dev_token(
            settings,
            user_id=args.user_id,
            depts=tuple(args.dept),
            roles=tuple(args.role),
            admin=args.admin,
        )
        return token, f"api.identity.mint_dev_token（{settings.auth_mode}）"
    except Exception as exc:  # noqa: BLE001 - 宿主环境缺依赖或没配 secret 都可能走到这里
        if args.jwt_secret or os.getenv("AUTH_JWT_SECRET"):
            secret = args.jwt_secret or os.getenv("AUTH_JWT_SECRET") or ""
            claim_user = os.getenv("AUTH_JWT_USER_ID_CLAIM", "sub")
            claim_depts = os.getenv("AUTH_JWT_DEPTS_CLAIM", "user_depts")
            claim_roles = os.getenv("AUTH_JWT_ROLES_CLAIM", "user_roles")
            claim_admin = os.getenv("AUTH_JWT_ADMIN_CLAIM", "is_admin")
            now = int(time.time())
            token = mint_hs256(
                secret,
                {
                    claim_user: args.user_id,
                    claim_depts: list(args.dept),
                    claim_roles: list(args.role),
                    claim_admin: bool(args.admin),
                    "iat": now,
                    "exp": now + 3600,
                },
            )
            return token, "本地 HS256（--jwt-secret/AUTH_JWT_SECRET）"
        raise SystemExit(
            f"拿不到身份凭证（{type(exc).__name__}: {exc}）。"
            "三种解法任选其一：\n"
            "  1. 在容器内跑（推荐，secret 与 DSN 由 compose 注入）：\n"
            "     docker compose exec -T api python eval/run_eval.py\n"
            "  2. 宿主跑但给 secret：\n"
            "     AUTH_JWT_SECRET=<与 compose 同值> "
            "python3 eval/run_eval.py --dsn <PG DSN>\n"
            f"     或在 {REPO_ROOT / '.env'} 里加一行 AUTH_JWT_SECRET=...\n"
            "  3. 已有 token：--token <Bearer token>"
        ) from exc


def normalise(text: str) -> str:
    """NFKC 归一并去掉零宽字符，供判分比较用。."""
    return (
        unicodedata.normalize("NFKC", text or "")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )


_NOTICE = re.compile(
    r"（(?:证据超出长度上限|以下问题本次未查到可验证依据|时间口径：|本次仅依据)[^）]*）"
)


def strip_system_notices(text: str) -> str:
    """去掉系统陈述（超长说明/未覆盖/时间口径），它们不是模型事实主张。."""
    return _NOTICE.sub("", text or "")


def attach_trailing_citations(text: str) -> str:
    """把 `。 [3]` 归并为 `[3]。`，避免切句器把角标拆成独立一句。."""
    return re.sub(r"([。！？!?])\s*(\[[0-9]{1,3}\])", r"\2\1", text or "")


def numbers_in(text: str) -> list[tuple[str, str]]:
    """答案里的数值断言（去掉角标/日期；否则每条答案都会多出几个假数字）。."""
    cleaned = _CITE_MARK.sub(" ", normalise(text))
    cleaned = re.sub(
        r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?", " ", cleaned
    )
    out: list[tuple[str, str]] = []
    for match in _NUMBER.finditer(cleaned):
        raw = match.group(0)
        unit = "%" if raw.endswith(("%", "％")) else ""
        body = raw[:-1] if unit else raw
        try:
            value = Decimal(body.replace(",", "").replace("，", "")).normalize()
        except InvalidOperation:
            continue
        # 年份是查询范围/报告期，不是需要回查的数值指标。
        if not unit and 1900 <= value <= 2999 and value == value.to_integral():
            continue
        out.append((format(value, "f"), unit))
    return out


def evidence_numbers(text: str) -> list[tuple[str, str]]:
    """证据原文里出现的数字集合。."""
    return numbers_in(text)


def load_golden(path: Path) -> list[dict[str, Any]]:
    """读 golden JSONL，逐行校验必填字段。."""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(
                    f"{path}:{line_no} 不是合法 JSON: {exc}"
                ) from exc
            for key in (
                "id",
                "category",
                "question",
                "expected_verdict",
                "must_not_contain",
            ):
                if key not in row:
                    raise SystemExit(f"{path}:{line_no} 缺字段 {key}")
            rows.append(row)
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"{path}: 题号重复")
    return rows


# ---------------------------------------------------------------------------
# SSE


@dataclass
class StreamResult:
    """一次 SSE 拉流的完整结果：状态码、事件序列与耗时。."""
    status: int
    content_type: str
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    elapsed_ms: float = 0.0

    @property
    def names(self) -> list[str]:
        """事件名序列（按到达顺序）。."""
        return [str(event.get("name")) for event in self.events]

    def first(self, name: str) -> dict[str, Any] | None:
        """第一条同名事件的数据。."""
        for event in self.events:
            if event.get("name") == name:
                return event.get("data") or {}
        return None

    def all(self, name: str) -> list[dict[str, Any]]:
        """所有同名事件的数据列表。."""
        return [
            event.get("data") or {}
            for event in self.events
            if event.get("name") == name
        ]

    @property
    def tokens(self) -> str:
        """把 token 事件的 delta 拼成完整答案。."""
        return "".join(
            str(item.get("delta") or "") for item in self.all("token")
        )


def post_sse(
    base_url: str,
    token: str,
    question: str,
    *,
    timeout_s: float = 45.0,
    body_extra: dict[str, Any] | None = None,
) -> StreamResult:
    """POST /api/v1/qa/stream 并逐帧解析。.

    状态码在开流前（401/422）时没有 SSE。.
    """
    url = urlsplit(base_url)
    if url.scheme not in ("http", "https"):
        raise ValueError(f"base_url 必须是 http(s)，实际 {base_url}")
    host = url.hostname or "127.0.0.1"
    port = url.port or (443 if url.scheme == "https" else 80)
    prefix = url.path.rstrip("/")
    path = f"{prefix}/api/v1/qa/stream"
    body: dict[str, Any] = {"question": question}
    if body_extra:
        body.update(body_extra)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    started = time.perf_counter()
    if url.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=timeout_s, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    result = StreamResult(status=0, content_type="")
    try:
        conn.request("POST", path, body=payload, headers=headers)
        response = conn.getresponse()
        result.status = response.status
        result.content_type = response.getheader("content-type") or ""
        if response.status != 200:
            raw = response.read().decode("utf-8", "replace")
            result.error = f"HTTP {response.status}: {raw[:500]}"
            return result

        frame_name: str | None = None
        data_lines: list[str] = []
        while True:
            raw_line = response.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if frame_name is not None or data_lines:
                    result.events.append(_make_event(frame_name, data_lines))
                frame_name, data_lines = None, []
                continue
            if line.startswith(":"):  # 注释帧（心跳）忽略
                continue
            field_name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field_name == "event":
                frame_name = value.strip()
            elif field_name == "data":
                data_lines.append(value)
        if frame_name is not None or data_lines:
            result.events.append(_make_event(frame_name, data_lines))
        if not result.events:
            result.error = "流里没有任何 SSE 帧"
    except TimeoutError as exc:
        result.error = f"读取超时（{timeout_s}s）: {exc}"
    except (http.client.HTTPException, OSError) as exc:
        result.error = f"传输失败: {type(exc).__name__}: {exc}"
    finally:
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        with contextlib.suppress(Exception):
            conn.close()
    return result


def _make_event(name: str | None, data_lines: list[str]) -> dict[str, Any]:
    """把一帧 SSE 的 data 解析成事件对象。."""
    raw = "\n".join(data_lines)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {"_raw": raw}
    # data 里的 type 与 event: 行必须一致（两者都带是契约，不是冗余）
    declared = name or str(data.get("type") or "")
    if name and data.get("type") and name != data.get("type"):
        declared = name
    return {"name": declared, "data": data}


# --------------------------------------------------------------------------- 判
# 分


@dataclass
class QuestionScore:
    """一道题的判分结果与失败原因。."""
    qid: str
    category: str
    expected: str
    actual: str | None
    status: int
    verdict_ok: bool
    trap_safe: bool | None
    must_not_violation: list[str]
    evidence_hit: str  # hit / miss / unknown
    numeric: str  # ok / bad / none / unknown
    numeric_bad: list[str]
    citations_total: int
    citations_resolvable: int
    uncited_sentences: list[str]
    contract_violations: list[str]
    score: float
    fail_reasons: list[str]
    top1: float | None
    gate1: str | None
    gate2: str | None
    reject_reason: str | None
    answer: str
    citations: list[dict[str, Any]]
    fallback: dict[str, Any] | None
    clarify: dict[str, Any] | None
    done: dict[str, Any] | None
    events: list[dict[str, Any]]
    latency_ms: float
    top1_source: str

    def as_dict(self) -> dict[str, Any]:
        """题目判分结果的字典形式（落 JSON 用）。."""
        return {
            k: getattr(self, k)
            for k in (
                "qid",
                "category",
                "expected",
                "actual",
                "status",
                "verdict_ok",
                "trap_safe",
                "must_not_violation",
                "evidence_hit",
                "numeric",
                "numeric_bad",
                "citations_total",
                "citations_resolvable",
                "uncited_sentences",
                "contract_violations",
                "score",
                "fail_reasons",
                "top1",
                "gate1",
                "gate2",
                "reject_reason",
                "answer",
                "citations",
                "fallback",
                "clarify",
                "done",
                "latency_ms",
                "top1_source",
            )
        }


def contract_violations(stream: StreamResult) -> list[str]:
    """附录 C.2 的硬约束：违规不阻断判分，但必须进报告。.

    这不是"风格问题"——前端状态机按这些约束写。
    """
    names = stream.names
    problems: list[str] = []
    if not names:
        return ["没有事件"]
    if names.count("done") != 1:
        problems.append(f"done 出现 {names.count('done')} 次（必须恰好 1 次）")
    if names[-1] != "done":
        problems.append(f"最后一条不是 done，而是 {names[-1]}")
    done = stream.first("done") or {}
    verdict = done.get("verdict")
    if "token" in names and verdict not in ANSWERABLE_VERDICTS:
        problems.append(f"verdict={verdict} 却发了 token")
    if "fallback" in names and verdict in ANSWERABLE_VERDICTS:
        problems.append(f"verdict={verdict} 却发了 fallback")
    if "clarify" in names and verdict != "clarify":
        problems.append(f"发了 clarify 但 verdict={verdict}")
    if "citations" in names and verdict not in ANSWERABLE_VERDICTS:
        problems.append(f"verdict={verdict} 却发了 citations")
    if "fallback" in names and "token" in names:
        problems.append("同一条流既有 fallback 又有 token")
    if "citations" in names:
        index_cite = names.index("citations")
        index_done = names.index("done")
        if index_cite > index_done:
            problems.append("citations 在 done 之后")
    return problems


def resolve_citations(
    citations: Sequence[dict[str, Any]], db: Db | None
) -> tuple[int, list[dict[str, Any]]]:
    """逐条到 PG 里回查。返回 (可点回条数, 每条的依据)。."""
    if db is None or not citations:
        return 0, []
    chunk_ids = [str(item.get("chunk_id") or "") for item in citations]
    rows = db.chunks(chunk_ids)
    resolved: list[dict[str, Any]] = []
    count = 0
    for item in citations:
        chunk_id = str(item.get("chunk_id") or "")
        row = rows.get(chunk_id)
        ok = row is not None
        reason = "ok" if ok else "chunk_id 不在 PG"
        if row is not None:
            if str(item.get("doc_key") or "") != str(row["doc_key"]):
                ok, reason = (
                    False,
                    "doc_key 不一致（引用 "
                    f"{item.get('doc_key')}，库 {row['doc_key']}）",
                )
            elif int(item.get("page_num") or 0) != int(row["page_num"]):
                ok, reason = (
                    False,
                    "page_num 不一致（引用 "
                    f"{item.get('page_num')}，库 {row['page_num']}）",
                )
            elif not item.get("bbox"):
                ok, reason = False, "引用没有 bbox"
            elif not _bbox_equal(item.get("bbox"), row["bbox"]):
                ok, reason = False, "bbox 与库里的行不一致"
            elif (
                row["chunk_status"] != "active"
                or row["version_status"] != "active"
            ):
                ok, reason = (
                    False,
                    f"切片/版本未生效（{row['chunk_status']}/{row['version_status']}）",
                )
        count += 1 if ok else 0
        resolved.append({"chunk_id": chunk_id, "ok": ok, "reason": reason})
    return count, resolved


def _bbox_equal(left: Any, right: Any) -> bool:
    """按 1e-6 容差比较两个 bbox（忽略类型差异）。."""
    try:
        left_list = [[float(v) for v in point] for point in left]
        right_list = [[float(v) for v in point] for point in right]
    except (TypeError, ValueError):
        return False
    return len(left_list) == len(right_list) and all(
        len(a) == len(b)
        and all(abs(x - y) < 1e-6 for x, y in zip(a, b, strict=False))
        for a, b in zip(left_list, right_list, strict=False)
    )


def score_question(
    golden: dict[str, Any],
    stream: StreamResult,
    *,
    db: Db | None,
    audit: dict[str, Any] | None,
) -> QuestionScore:
    """按 §12 口径给一道题判分并收集原因。."""
    qid = str(golden["id"])
    category = str(golden["category"])
    expected = str(golden["expected_verdict"])
    actual = (stream.first("done") or {}).get("verdict")
    answer = stream.tokens
    citations = list((stream.first("citations") or {}).get("items") or [])
    fallback = stream.first("fallback")
    clarify = stream.first("clarify")
    done = stream.first("done")
    forbidden = [str(item) for item in golden.get("must_not_contain") or []]
    serialised_events = json.dumps(stream.events, ensure_ascii=False)
    must_not = [
        item
        for item in forbidden
        if _contains(normalise(serialised_events), item)
    ]
    problems = contract_violations(stream)

    # 引用可点回
    resolved, resolved_rows = resolve_citations(citations, db)

    # 数值断言：答案（去掉角标/系统说明/年份）里出现的每个数都必须能在被引证据中
    # 找到。
    checked_answer = strip_system_notices(attach_trailing_citations(answer))
    assertions = numbers_in(checked_answer)
    evidence_text = ""
    if db is not None and citations:
        evidence_text = "\n".join(
            str(row["content_text"])
            for row in db.chunks(
                [str(item.get("chunk_id") or "") for item in citations]
            ).values()
        )
    if not evidence_text:
        evidence_text = "\n".join(
            str(item.get("snippet") or "") for item in citations
        )
    evidence_nums = set(evidence_numbers(evidence_text))
    numeric_bad = [
        f"{value}{unit}"
        for value, unit in assertions
        if (value, unit) not in evidence_nums
        and not (unit and (value, "") in evidence_nums)
    ]
    if not assertions:
        numeric = "none"
    elif db is None and not citations:
        numeric = "unknown"
    else:
        numeric = "ok" if not numeric_bad else "bad"

    # 忠实度：答案切句，非通知句必须有角标。通知是系统陈述（以"（"开头）， §9.3/
    # §8.3/§5.3。
    uncited: list[str] = []
    if actual in ANSWERABLE_VERDICTS:
        for sentence in _SENTENCE.findall(checked_answer):
            text = sentence.strip()
            if len(text) <= 4 or text.startswith("（"):
                continue
            if not _CITE_MARK.search(text):
                uncited.append(text[:80])

    # 证据命中：TBD 占位的期望 doc_key 不参与精确匹配（README §5.3）
    expected_keys = [
        str(key)
        for key in golden.get("expected_doc_keys") or []
        if not str(key).startswith("TBD")
    ]
    actual_keys = [str(item.get("doc_key") or "") for item in citations]
    if not expected_keys:
        evidence_hit = "unknown" if golden.get("expected_doc_keys") else "n/a"
    elif not actual_keys:
        evidence_hit = "miss"
    elif set(expected_keys) & set(actual_keys):
        evidence_hit = "hit"
    else:
        evidence_hit = "miss"

    # 单题得分（§12.1 口径的细化，见 eval/README §5.1）
    reasons: list[str] = []
    if must_not:
        score = 0.0
        reasons.append(f"输出包含禁用措辞：{must_not}")
    elif stream.status == 403 and expected in ("reject_none", "reject_low"):
        # 闸二/闸三的 403 是"无权查看"：不区分不存在与没权限，也不透露标题。
        score = 1.0
    elif actual is None:
        score = 0.0
        reasons.append("没有 done 事件，无法判定")
    elif category == "trap" or expected in (
        "reject_none",
        "reject_low",
        "clarify",
    ):
        safe = actual in SAFE_TRAP_VERDICTS
        if (
            expected == "clarify"
            and actual == "clarify"
            or safe
            and not answer.strip()
        ):
            score = 1.0
        elif safe:
            score = 0.5  # 给了降级输出但仍有内容：按 §5.1 允许半分
            reasons.append("安全降级但输出了内容")
        else:
            score = 0.0
            reasons.append(f"陷阱题未安全拒答（verdict={actual}）")
        if actual in ("internal", "model_timeout", "index_stale"):
            score = 0.0
            reasons.append(f"系统故障，不是安全拒答（{actual}）")
    else:  # 应答题
        if actual in ANSWERABLE_VERDICTS:
            score = 1.0 if expected == "pass" and actual == "pass" else 0.5
            if expected == "partial" and actual == "pass":
                score = 1.0
            if not citations:
                score = 0.0
                reasons.append("应答题没有引用")
            if uncited:
                score = 0.0
                reasons.append(f"有 {len(uncited)} 句没有角标")
            if numeric == "bad":
                score = 0.0
                reasons.append(f"数值无据：{numeric_bad[:5]}")
            if must_not:
                score = 0.0
        elif actual in REFUSAL_VERDICTS or actual == "clarify":
            score = 0.0
            reasons.append(f"应答题拒答（verdict={actual}）")
        else:
            score = 0.0
            reasons.append(f"应答题未得到答案（verdict={actual}）")

    trap_safe: bool | None = None
    if category == "trap" or expected in (
        "reject_none",
        "reject_low",
        "clarify",
    ):
        if stream.status == 403 and expected in ("reject_none", "reject_low"):
            trap_safe = True  # 越权统一 403：不解释、不透露标题，算安全拒答
        else:
            trap_safe = bool(
                actual in SAFE_TRAP_VERDICTS
                and not answer.strip()
                and not must_not
                and actual not in ("internal", "model_timeout", "index_stale")
                and stream.status in (200, 403)
            )

    verdict_ok = bool(actual == expected)

    return QuestionScore(
        qid=qid,
        category=category,
        expected=expected,
        actual=actual,
        status=stream.status,
        verdict_ok=verdict_ok,
        trap_safe=trap_safe,
        must_not_violation=must_not,
        evidence_hit=evidence_hit,
        numeric=numeric,
        numeric_bad=numeric_bad,
        citations_total=len(citations),
        citations_resolvable=resolved,
        uncited_sentences=uncited,
        contract_violations=problems,
        score=score,
        fail_reasons=reasons,
        top1=(audit or {}).get("top1"),
        gate1=(audit or {}).get("gate1"),
        gate2=(audit or {}).get("gate2"),
        reject_reason=(audit or {}).get("reject_reason"),
        answer=answer,
        citations=citations,
        fallback=fallback,
        clarify=clarify,
        done=done,
        events=[
            {"name": e.get("name"), "data": e.get("data")}
            for e in stream.events
        ],
        latency_ms=round(stream.elapsed_ms, 1),
        top1_source=(audit or {}).get("source", "none"),
    )


def _contains(haystack: str, needle: str) -> bool:
    """归一化后做子串判断。."""
    return normalise(needle) in haystack if needle else False


# --------------------------------------------------------------------------- DB


class Db:
    """评测所需的只读回查。没有 psycopg 或没有 DSN 时整体降级为 None。."""

    def __init__(self, dsn: str) -> None:
        """Initialize the instance."""
        import psycopg  # 延迟导入：宿主可能没有

        self._dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
        self._psycopg = psycopg

    def _connect(self):
        """开一个 psycopg 连接（5s 超时）。."""
        return self._psycopg.connect(self._dsn, connect_timeout=5)

    def chunks(self, chunk_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """按 chunk_id 批量查 PG 里的切片与版本状态。."""
        ids = [item for item in chunk_ids if item]
        if not ids:
            return {}
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.chunk_id, c.doc_key, c.page_num, c.bbox::text,
                       c.content_text, c.status, dv.status
                FROM chunks c
                JOIN doc_versions dv ON dv.version_id = c.version_id
                WHERE c.chunk_id = ANY(%s)
                """,
                (list(ids),),
            )
            rows = cur.fetchall()
        out: dict[str, dict[str, Any]] = {}
        for (
            chunk_id,
            doc_key,
            page_num,
            bbox,
            content_text,
            chunk_status,
            version_status,
        ) in rows:
            out[str(chunk_id)] = {
                "chunk_id": chunk_id,
                "doc_key": doc_key,
                "page_num": page_num,
                "bbox": json.loads(bbox),
                "content_text": content_text,
                "chunk_status": chunk_status,
                "version_status": version_status,
            }
        return out

    def counts(self) -> dict[str, int]:
        """库内 documents / versions / chunks 的计数快照。."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM documents")
            docs = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FROM doc_versions WHERE status = 'active'"
            )
            active_versions = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM chunks")
            chunks = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM chunks WHERE status = 'active'")
            active_chunks = int(cur.fetchone()[0])
            cur.execute("SELECT count(*) FROM qa_log")
            qa_rows = int(cur.fetchone()[0])
        return {
            "documents": docs,
            "active_versions": active_versions,
            "chunks": chunks,
            "active_chunks": active_chunks,
            "qa_log": qa_rows,
        }

    def audit_for(
        self, question: str, since: datetime, wait_s: float = 3.0
    ) -> dict[str, Any] | None:
        """审计写入在流结束之后，且失败不报错。.

        这里短轮询几次，拿不到就记 none。
        """
        deadline = time.time() + wait_s
        sql = """
            SELECT gate1_verdict, gate2_verdict, reject_reason,
                   (score_snapshot->>'top1')::float8 AS top1
            FROM qa_log
            WHERE question = %s AND created_at >= %s
            ORDER BY id DESC LIMIT 1
        """
        while True:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, (question, since))
                row = cur.fetchone()
            if row is not None:
                return {
                    "gate1": row[0],
                    "gate2": row[1],
                    "reject_reason": row[2],
                    "top1": row[3],
                    "source": "qa_log",
                }
            if time.time() >= deadline:
                return None
            time.sleep(0.25)


# --------------------------------------------------------------------------- 报
# 告


@dataclass
class RedLine:
    """一条红线（RL1–RL4）的状态、实测值、分母与说明。."""
    key: str
    name: str
    status: str  # PASS / FAIL / NOT_MEASURABLE
    value: str
    threshold: str
    numerator: int | None
    denominator: int | None
    note: str

    def as_dict(self) -> dict[str, Any]:
        """红线结果的字典形式（落 JSON 用）。."""
        return {
            "key": self.key,
            "name": self.name,
            "status": self.status,
            "value": self.value,
            "threshold": self.threshold,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "note": self.note,
        }


def compute_red_lines(
    scores: Sequence[QuestionScore],
    *,
    corpus: dict[str, int] | None,
    db_available: bool,
) -> tuple[list[RedLine], dict[str, Any]]:
    """算四条红线（RL1–RL4）与派生指标。."""
    traps = [item for item in scores if item.category == "trap"]
    answerable = [
        item
        for item in scores
        if item.category != "trap" and item.expected in ANSWERABLE_VERDICTS
    ]
    answered = [
        item for item in answerable if item.actual in ANSWERABLE_VERDICTS
    ]

    # 判别力：应答题里至少一题真的过了门控（有引用，或落到校验层/模型层故障）。
    # 没有它，15/15 的陷阱拒答率只是"语料为空"的另一种说法。
    with_evidence = [
        item
        for item in answerable
        if item.citations_total > 0
        or item.actual in ("unverifiable", "model_timeout")
    ]

    trap_n = len(traps)
    trap_ok = sum(1 for item in traps if item.trap_safe)
    rl1 = RedLine(
        key="RL1",
        name="陷阱题拒答率",
        status=(
            "PASS"
            if trap_n and trap_ok == trap_n and with_evidence
            else "FAIL"
            if trap_n and trap_ok < trap_n
            else "NOT_MEASURABLE"
        ),
        value=f"{trap_ok}/{trap_n} = {trap_ok / trap_n:.1%}"
        if trap_n
        else "无陷阱题",
        threshold="= 100%",
        numerator=trap_ok,
        denominator=trap_n,
        note=(
            "判别力闸：应答题中拿到证据/进入生成层的 "
            f"{len(with_evidence)}/{len(answerable)}"
            + ('；为 0 时本指标是"全拒答"真空态' if not with_evidence else "")
            + (
                ""
                if corpus is None
                else f"；库内 active 切片 {corpus.get('active_chunks', 0)} 条"
            )
        ),
    )

    numeric_items = [item for item in answered if item.numeric in ("ok", "bad")]
    numeric_ok = sum(1 for item in numeric_items if item.numeric == "ok")
    numeric_rate = (numeric_ok / len(numeric_items)) if numeric_items else None
    if not numeric_items:
        rl2_status = "NOT_MEASURABLE"
    elif numeric_rate is not None and numeric_rate + 1e-9 >= RL_NUMERIC_RATE:
        rl2_status = "PASS"
    else:
        rl2_status = "FAIL"
    rl2 = RedLine(
        key="RL2",
        name="数值准确率",
        status=rl2_status,
        value="—"
        if numeric_rate is None
        else f"{numeric_ok}/{len(numeric_items)} = {numeric_rate:.1%}",
        threshold="≥ 96%",
        numerator=numeric_ok,
        denominator=len(numeric_items),
        note=(
            "口径：答案中每个数字（已去掉 [n] 角标）都必须出现在"
            "被引证据原文里；"
            "没有已答题或答案不含数字时不可测。"
            + ("" if db_available else " 本轮无 DB 连接，证据原文取不到。")
        ),
    )

    cite_total = sum(item.citations_total for item in answered)
    cite_ok = sum(item.citations_resolvable for item in answered)
    if not db_available or cite_total == 0:
        rl3_status = "NOT_MEASURABLE"
    elif cite_ok / cite_total + 1e-9 >= RL_CITATION_RATE:
        rl3_status = "PASS"
    else:
        rl3_status = "FAIL"
    rl3 = RedLine(
        key="RL3",
        name="角标可点回率",
        status=rl3_status,
        value=f"{cite_ok}/{cite_total} = {cite_ok / cite_total:.1%}"
        if cite_total
        else "无引用可查",
        threshold="≥ 98%",
        numerator=cite_ok,
        denominator=cite_total,
        note=(
            "逐条回查 PG：chunk_id 存在、doc_key/page_num/bbox 与库一致、"
            "切片与版本均 active。"
            + ("" if db_available else " 本轮无 DB 连接。")
        ),
    )

    faithful = [
        item
        for item in answered
        if not item.uncited_sentences
        and item.numeric != "bad"
        and not item.must_not_violation
        and item.citations_total
    ]
    if not answered:
        rl4_status = "NOT_MEASURABLE"
    elif len(faithful) == len(answered):
        rl4_status = "PASS"
    else:
        rl4_status = "FAIL"
    coverage = (len(answered) / len(answerable)) if answerable else None
    rl4 = RedLine(
        key="RL4",
        name="忠实度优先于覆盖率",
        status=rl4_status,
        value=f"{len(faithful)}/{len(answered)} = "
        + (f"{len(faithful) / len(answered):.1%}" if answered else "—"),
        threshold="= 100%（覆盖率单独报告，不得以覆盖率换忠实度）",
        numerator=len(faithful),
        denominator=len(answered),
        note=(
            "覆盖率 = 应答题得到答案/应答题总数 = "
            + (
                f"{len(answered)}/{len(answerable)} = {coverage:.1%}"
                if coverage is not None
                else "—"
            )
            + "；0 题已答时忠实度不可测（不是通过）。"
        ),
    )
    return [rl1, rl2, rl3, rl4], {
        "answerable": len(answerable),
        "answered": len(answered),
        "answered_with_evidence": len(with_evidence),
        "coverage": coverage,
    }


def write_report(
    path_md: Path,
    path_json: Path,
    *,
    scores: Sequence[QuestionScore],
    red_lines: Sequence[RedLine],
    counts: dict[str, Any],
    precondition: dict[str, Any],
    args: argparse.Namespace,
    token_source: str,
    started_at: datetime,
) -> None:
    """把评测结果写成 Markdown 报告与机器可读 JSON。."""
    trap = [item for item in scores if item.category == "trap"]
    answerable = [
        item
        for item in scores
        if item.category != "trap" and item.expected in ANSWERABLE_VERDICTS
    ]
    answered = [
        item for item in answerable if item.actual in ANSWERABLE_VERDICTS
    ]
    total_score = (
        sum(item.score for item in scores) / len(scores) if scores else 0.0
    )
    statuses = {item.status for item in red_lines}
    if "FAIL" in statuses:
        verdict = "不通过（至少一条红线不达标）"
        conclusion = "四条红线未全过：存在不达标项，按 §12.3 不得发布。"
    elif "NOT_MEASURABLE" in statuses:
        verdict = "不可认证（存在不可测红线）"
        conclusion = (
            "四条红线中有不可测项，本次结果**不能作为发布依据**。"
            '具体见红线表与前提检查；不要把"未测"读成"通过"。'
        )
    else:
        verdict = "通过（四条红线全过）"
        conclusion = (
            "四条红线全部达标，可作为发布依据（需同时留档版本三件套）。"
        )

    summary = (
        f"题目 {len(scores)} 题（应答题 {len(answerable)}，陷阱 {len(trap)}），"
        f"应答题得到答案 {len(answered)} 题，"
        f"单题均分 {total_score:.2f}；"
        f"陷阱安全拒答 {sum(1 for i in trap if i.trap_safe)}/{len(trap)}。"
    )
    gen_cell = (
        "已配置"
        if precondition.get("llm_configured")
        else "**未配置**（应答题会落 model_timeout）"
    )
    llm_cell = (
        "LLM 已配置"
        if precondition.get("llm_configured")
        else "LLM_BASE_URL/LLM_MODEL 为空"
    )
    lines: list[str] = [
        "# 红线验收报告",
        "",
        "- 生成时间："
        f"{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}",
        f"- 评测集：`{args.golden}`",
        f"- API：`{args.base_url}`",
        f"- 身份凭证：{token_source}"
        f"（user={args.user_id}, depts={list(args.dept)}, "
        f"roles={list(args.role)}）",
        "- 评测器：`eval/run_eval.py`"
        "（退出码：0 全过 / 1 不达标 / 2 有不可测项 / 3 评测器失败）",
        "",
        "## 1. 一句话结论",
        "",
        f"**{verdict}**：{conclusion}",
        "",
        f"> {summary}",
        "",
        "## 2. 口径与数据来源",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 题库 | `{args.golden}`（{len(scores)} 题；"
        f"TBD doc_key 占位 {precondition.get('tbd_questions', 0)} 题） |",
        "| 接口 | `POST /api/v1/qa/stream`（SSE，done 必须是最后一条） |",
        f"| 语料快照 | documents={counts.get('documents', '—')}，"
        f"active versions={counts.get('active_versions', '—')}，"
        f"active chunks={counts.get('active_chunks', '—')} |",
        f"| 生成模型 | {gen_cell} |",
        f"| 审计回查 | {'qa_log 可用' if counts else '不可用（无 DB 连接）'} |",
        "",
        "判分口径（与 `eval/README.md` §5 一致，差异处已注明）：",
        "",
        "1. 陷阱题：安全拒答/澄清"
        "（`reject_none`/`reject_low`/`clarify`）、不输出正文、"
        "不含 `must_not_contain` 措辞、不以系统故障收场 → 算拒答成功。",
        "2. 应答题：`pass/partial` 且引用非空、每句有角标、数字有据 → 得分；"
        "拒答记 0（防止靠全拒答刷忠实度）。",
        '3. 数值准确率只统计"已答题且答案含数字"的题。',
        "4. 角标可点回逐条回查 PG（存在性 + doc_key/page/bbox + active）。",
        "",
        "## 3. 四条红线",
        "",
        "| 红线 | 结果 | 实测 | 阈值 | 分母/样本 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    for item in red_lines:
        lines.append(
            f"| {item.key} {item.name} | **{item.status}** | "
            f"{item.value} | {item.threshold} | "
            f"{'—' if item.denominator is None else item.denominator} | "
            f"{item.note} |"
        )
    lines += [
        "",
        "红线定义（原样落盘，避免报告与门禁两份口径）：",
        "",
    ]
    for key, text in RED_LINE_DEFS.items():
        lines.append(f"- **{key}**：{text}")
    lines += [
        "",
        "## 4. 陷阱题逐题（15 题）",
        "",
        "| 题号 | 期望 | 实际 | 安全拒答 | 拒绝码 | fallback docs | 禁用措辞 |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in trap:
        code = (
            (item.fallback or {}).get("code")
            or (item.done or {}).get("code")
            or "—"
        )
        docs = (item.fallback or {}).get("docs") or []
        doc_keys = (
            ", ".join(
                str(doc.get("doc_key")) for doc in docs if isinstance(doc, dict)
            )
            or "—"
        )
        lines.append(
            f"| {item.qid} | {item.expected} | {item.actual or '—'} | "
            f"{'✅' if item.trap_safe else '❌'} | {code} | {doc_keys} | "
            f"{'、'.join(item.must_not_violation) or '—'} |"
        )
    lines += [
        "",
        "## 5. 应答题逐题",
        "",
        "| 题号 | 期望 | 实际 | 引用 | 可点回 | 数值 | "
        "证据命中 | 得分 | 失败原因 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in answerable:
        lines.append(
            f"| {item.qid} | {item.expected} | {item.actual or '—'} | "
            f"{item.citations_total} | "
            f"{item.citations_resolvable} | {item.numeric} | "
            f"{item.evidence_hit} | "
            f"{item.score:.1f} | {'；'.join(item.fail_reasons[:2]) or '—'} |"
        )
    lines += [
        "",
        "## 6. 限制与风险",
        "",
        f"- **语料**：库内 active 切片 {counts.get('active_chunks', '—')} 条。"
        "为 0 时所有题都会走 `no_evidence`，陷阱拒答率再高也没有判别力。",
        f"- **题库**：{precondition.get('tbd_questions', 0)}/{len(scores)} 题"
        "的 `expected_doc_keys` 是 `TBD_*` 占位，"
        "doc_key 精确命中不可评（`evidence_hit=unknown`），"
        "只能评答案内部一致性与行为。",
        f"- **生成**：{llm_cell}；"
        "未配置时 faithful/numeric 两条红线天然不可测——"
        "报告不会把它们记成通过。",
        "- **样本量**：50 题里应答题 31、陷阱 15；"
        "任何按百分比的门禁（96%/98%）在小样本上都是"
        '"错一题就不过"，因此报告同时给出分子/分母，不做四舍五入后的口头判断。',
        "- **单轮**：忠实度优先于覆盖率是跨版本约束；"
        "本报告只给当前版本的覆盖率与忠实度，"
        "不与历史版本比较（没有基线快照）。",
        "",
        "## 7. 建议动作",
        "",
        "1. 先补齐语料：把 golden 的 `TBD_*` 回填成真实 doc_key，"
        "并让对应文档真正入库"
        "（active chunks > 0）；否则四条红线里三条永远不可测。",
        "2. 配好生成网关（LLM_BASE_URL/LLM_MODEL）后再跑本脚本；"
        "只有那时 RL2/RL4 才有意义。",
        "3. 每次发布候选按 `eval/README.md` §5.2 的四条红线过门禁；"
        "参数/提示词/模型名三者同版本留档。",
        "4. 阈值调参后先跑 `eval/calibrate.py` 看 top1 分布是否真有波谷，"
        "再动 `GATE_*`。",
        "",
        "## 8. 附件",
        "",
        f"- 机器可读：`{path_json}`"
        "（含逐题事件序列，可直接用于 `eval/calibrate.py`）",
        "",
    ]
    path_md.parent.mkdir(parents=True, exist_ok=True)
    path_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "schema": "eval.redline.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "started_at": started_at.isoformat(),
        "base_url": args.base_url,
        "golden": str(args.golden),
        "token_source": token_source,
        "args": {
            "user_id": args.user_id,
            "depts": list(args.dept),
            "roles": list(args.role),
            "only": args.only,
            "category": args.category,
        },
        "precondition": precondition,
        "counts": counts,
        "red_lines": [item.as_dict() for item in red_lines],
        "verdict": verdict,
        "summary": summary,
        "questions": [item.as_dict() for item in scores],
    }
    path_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# main


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。."""
    parser = argparse.ArgumentParser(
        description="黄金 50 题评测 + 四条红线报告"
    )
    parser.add_argument(
        "--golden", type=Path, default=DEFAULT_GOLDEN, help="黄金集 JSONL"
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8000", help="API 根地址"
    )
    parser.add_argument(
        "--token", default="", help="已有 Bearer token（不给则本地签发）"
    )
    parser.add_argument(
        "--jwt-secret", default="", help="宿主签发时用的 HS256 secret"
    )
    parser.add_argument(
        "--user-id", default="eval_runner", help="评测身份 user_id"
    )
    parser.add_argument(
        "--dept",
        action="append",
        default=["fin"],
        help="评测身份部门（可多次）",
    )
    parser.add_argument(
        "--role",
        action="append",
        default=["staff"],
        help="评测身份角色（可多次）",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="用管理员身份（审计会带 admin_access）",
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL", ""),
        help="PG DSN，用于引用回查/top1",
    )
    parser.add_argument(
        "--timeout", type=float, default=45.0, help="单题超时秒数"
    )
    parser.add_argument(
        "--settle", type=float, default=3.0, help="审计行等待秒数"
    )
    parser.add_argument(
        "--only", default="", help="只跑这些题号，逗号分隔（如 TR01,TR05）"
    )
    parser.add_argument(
        "--category",
        default="",
        help="只跑这个类别（basic_table/chart/temporal_multihop/trap）",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="最多跑多少题（0=全量）"
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--report-name", default="", help="报告文件名（不含扩展名）"
    )
    parser.add_argument(
        "--allow-empty-corpus", action="store_true", help=argparse.SUPPRESS
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """入口：preflight、逐题评测、算红线、写报告。."""
    args = build_parser().parse_args(argv)
    started_at = datetime.now(UTC)
    rows = load_golden(args.golden)

    if args.only:
        wanted = {
            item.strip().upper()
            for item in args.only.split(",")
            if item.strip()
        }
        rows = [row for row in rows if str(row["id"]).upper() in wanted]
        missing = wanted - {str(row["id"]).upper() for row in rows}
        if missing:
            raise SystemExit(f"--only 里有不存在的题号：{sorted(missing)}")
    if args.category:
        rows = [row for row in rows if row["category"] == args.category]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit("筛选后没有题目可跑")

    try:
        token, token_source = build_token(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(
            f"[FATAL] 签发 token 失败：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3

    db: Db | None = None
    db_reason = "未配置 DATABASE_URL/--dsn"
    if args.dsn:
        try:
            db = Db(args.dsn)
            db.counts()
            db_reason = "psycopg 已连接"
        except Exception as exc:  # noqa: BLE001
            db = None
            db_reason = f"DB 不可用：{type(exc).__name__}: {exc}"

    counts: dict[str, Any] = {}
    if db is not None:
        try:
            counts = db.counts()
        except Exception as exc:  # noqa: BLE001
            counts = {"error": f"{type(exc).__name__}: {exc}"}

    llm_configured = bool(
        os.getenv("LLM_MODEL", "").strip()
        and os.getenv("LLM_BASE_URL", "").strip()
    )
    tbd_questions = sum(
        1
        for row in rows
        if all(
            str(key).startswith("TBD")
            for key in (row.get("expected_doc_keys") or [])
        )
        or not row.get("expected_doc_keys")
    )
    precondition = {
        "llm_configured": llm_configured,
        "llm_model": os.getenv("LLM_MODEL", ""),
        "db": db_reason,
        "tbd_questions": tbd_questions,
        "corpus_empty": int(counts.get("active_chunks", 0) or 0) == 0,
        "rubric": "eval/README.md §5（细化 §12.1）",
    }

    missing_chunks = precondition["corpus_empty"]
    print(
        f"[preflight] 题目={len(rows)} TBD doc_key={tbd_questions} "
        f"active_chunks={counts.get('active_chunks', '—')} "
        f"LLM={'on' if llm_configured else 'off'} DB={db_reason}"
    )
    if missing_chunks:
        print(
            "[preflight][WARN] 语料为空：所有题目都会走 no_evidence，"
            "陷阱拒答率是真空态，报告会记 NOT_MEASURABLE（不是通过）。"
        )
    if not llm_configured:
        print(
            "[preflight][WARN] 生成模型未配置：应答题会落 model_timeout，"
            "RL2/RL4 将不可测。"
        )

    scores: list[QuestionScore] = []
    for index, row in enumerate(rows, 1):
        qid = str(row["id"])
        stream = post_sse(
            args.base_url,
            token,
            str(row["question"]),
            timeout_s=args.timeout,
        )
        audit: dict[str, Any] | None = None
        if db is not None:
            try:
                audit = db.audit_for(
                    str(row["question"]), started_at, wait_s=args.settle
                )
            except Exception as exc:  # noqa: BLE001
                audit = {"source": f"qa_log 查询失败: {type(exc).__name__}"}
        score = score_question(row, stream, db=db, audit=audit)
        scores.append(score)
        mark = "✓" if score.score >= 1 else ("~" if score.score > 0 else "✗")
        print(
            f"[{index:02d}/{len(rows)}] {qid} {mark} status={stream.status} "
            f"actual={score.actual} score={score.score:.1f} "
            f"cites={score.citations_total}/{score.citations_resolvable} "
            f"top1={score.top1} {stream.elapsed_ms:.0f}ms"
        )

    red_lines, derived = compute_red_lines(
        scores, corpus=counts or None, db_available=db is not None
    )
    print("\n=== 四条红线 ===")
    for item in red_lines:
        print(
            f"  {item.key} {item.name}: {item.status}  {item.value}  "
            f"(阈值 {item.threshold})"
        )
    print(f"  coverage={derived.get('coverage')}")

    stamp = args.report_name or datetime.now().strftime("redline_%Y%m%d_%H%M%S")
    md_path = args.out_dir / f"{stamp}.md"
    json_path = args.out_dir / f"{stamp}.json"
    write_report(
        md_path,
        json_path,
        scores=scores,
        red_lines=red_lines,
        counts=counts,
        precondition=precondition,
        args=args,
        token_source=token_source,
        started_at=started_at,
    )
    print(f"[report] {md_path}")
    print(f"[report] {json_path}")

    statuses = {item.status for item in red_lines}
    if "FAIL" in statuses:
        return 1
    if "NOT_MEASURABLE" in statuses:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
