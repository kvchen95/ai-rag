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

if __package__:
    from .resources.run_eval_msgs import MESSAGES as M
else:  # run as a script: python3 eval/run_eval.py
    from resources.run_eval_msgs import MESSAGES as M

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
    "RL1": M['RL1_01'],
    "RL2": M['RL2_01'],
    "RL3": M['RL3_01'],
    "RL4": M['RL4_01'],
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
        return args.token, M['BUILD_TOKEN_01']
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
            return token, M['BUILD_TOKEN_02']
        raise SystemExit(
            f"{M['BUILD_TOKEN_03']}{type(exc).__name__}: {exc}）。"
            f"{M['BUILD_TOKEN_04']}"
            f"{M['BUILD_TOKEN_05']}"
            "     docker compose exec -T api python eval/run_eval.py\n"
            f"{M['BUILD_TOKEN_06']}"
            f"{M['BUILD_TOKEN_07']}"
            "python3 eval/run_eval.py --dsn <PG DSN>\n"
            f"{M['BUILD_TOKEN_08']}{REPO_ROOT / '.env'}{M['BUILD_TOKEN_09']}"
            f"{M['BUILD_TOKEN_10']}"
        ) from exc


def normalise(text: str) -> str:
    """NFKC 归一并去掉零宽字符，供判分比较用。."""
    return (
        unicodedata.normalize("NFKC", text or "")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )


_NOTICE = re.compile(M['NOTICE_01'])


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
        M['CLEANED_01'], " ", cleaned
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
                    f"{path}:{line_no}{M['LOAD_GOLDEN_01']}{exc}"
                ) from exc
            for key in (
                "id",
                "category",
                "question",
                "expected_verdict",
                "must_not_contain",
            ):
                if key not in row:
                    raise SystemExit(
                        f"{path}:{line_no}{M['LOAD_GOLDEN_02']}{key}"
                    )
            rows.append(row)
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise SystemExit(f"{path}{M['LOAD_GOLDEN_03']}")
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
        raise ValueError(f"{M['POST_SSE_01']}{base_url}")
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
            result.error = M['POST_SSE_02']
    except TimeoutError as exc:
        result.error = f"{M['POST_SSE_03']}{timeout_s}s）: {exc}"
    except (http.client.HTTPException, OSError) as exc:
        result.error = f"{M['POST_SSE_04']}{type(exc).__name__}: {exc}"
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
        return [M['CONTRACT_VIOLATIONS_01']]
    if names.count("done") != 1:
        problems.append(f"{M['CONTRACT_VIOLATIONS_02']}{names.count('done')}{M['CONTRACT_VIOLATIONS_03']}")
    if names[-1] != "done":
        problems.append(f"{M['CONTRACT_VIOLATIONS_04']}{names[-1]}")
    done = stream.first("done") or {}
    verdict = done.get("verdict")
    if "token" in names and verdict not in ANSWERABLE_VERDICTS:
        problems.append(f"verdict={verdict}{M['CONTRACT_VIOLATIONS_05']}")
    if "fallback" in names and verdict in ANSWERABLE_VERDICTS:
        problems.append(f"verdict={verdict}{M['CONTRACT_VIOLATIONS_06']}")
    if "clarify" in names and verdict != "clarify":
        problems.append(f"{M['CONTRACT_VIOLATIONS_07']}{verdict}")
    if "citations" in names and verdict not in ANSWERABLE_VERDICTS:
        problems.append(f"verdict={verdict}{M['CONTRACT_VIOLATIONS_08']}")
    if "fallback" in names and "token" in names:
        problems.append(M['CONTRACT_VIOLATIONS_09'])
    if "citations" in names:
        index_cite = names.index("citations")
        index_done = names.index("done")
        if index_cite > index_done:
            problems.append(M['CONTRACT_VIOLATIONS_10'])
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
        reason = "ok" if ok else M['REASON_01']
        if row is not None:
            if str(item.get("doc_key") or "") != str(row["doc_key"]):
                ok, reason = (
                    False,
                    f"{M['RESOLVE_CITATIONS_01']}"
                    f"{item.get('doc_key')}{M['RESOLVE_CITATIONS_02']}{row['doc_key']}）",
                )
            elif int(item.get("page_num") or 0) != int(row["page_num"]):
                ok, reason = (
                    False,
                    f"{M['RESOLVE_CITATIONS_03']}"
                    f"{item.get('page_num')}{M['RESOLVE_CITATIONS_02']}{row['page_num']}）",
                )
            elif not item.get("bbox"):
                ok, reason = False, M['RESOLVE_CITATIONS_04']
            elif not _bbox_equal(item.get("bbox"), row["bbox"]):
                ok, reason = False, M['RESOLVE_CITATIONS_05']
            elif (
                row["chunk_status"] != "active"
                or row["version_status"] != "active"
            ):
                ok, reason = (
                    False,
                    f"{M['RESOLVE_CITATIONS_06']}{row['chunk_status']}/{row['version_status']}）",
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
        reasons.append(f"{M['SCORE_QUESTION_01']}{must_not}")
    elif stream.status == 403 and expected in ("reject_none", "reject_low"):
        # 闸二/闸三的 403 是"无权查看"：不区分不存在与没权限，也不透露标题。
        score = 1.0
    elif actual is None:
        score = 0.0
        reasons.append(M['SCORE_QUESTION_02'])
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
            reasons.append(M['SCORE_QUESTION_03'])
        else:
            score = 0.0
            reasons.append(f"{M['SCORE_QUESTION_04']}{actual}）")
        if actual in ("internal", "model_timeout", "index_stale"):
            score = 0.0
            reasons.append(f"{M['SCORE_QUESTION_05']}{actual}）")
    else:  # 应答题
        if actual in ANSWERABLE_VERDICTS:
            score = 1.0 if expected == "pass" and actual == "pass" else 0.5
            if expected == "partial" and actual == "pass":
                score = 1.0
            if not citations:
                score = 0.0
                reasons.append(M['SCORE_QUESTION_06'])
            if uncited:
                score = 0.0
                reasons.append(f"{M['SCORE_QUESTION_07']}{len(uncited)}{M['SCORE_QUESTION_08']}")
            if numeric == "bad":
                score = 0.0
                reasons.append(f"{M['SCORE_QUESTION_09']}{numeric_bad[:5]}")
            if must_not:
                score = 0.0
        elif actual in REFUSAL_VERDICTS or actual == "clarify":
            score = 0.0
            reasons.append(f"{M['SCORE_QUESTION_10']}{actual}）")
        else:
            score = 0.0
            reasons.append(f"{M['SCORE_QUESTION_11']}{actual}）")

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
        name=M['RL1_02'],
        status=(
            "PASS"
            if trap_n and trap_ok == trap_n and with_evidence
            else "FAIL"
            if trap_n and trap_ok < trap_n
            else "NOT_MEASURABLE"
        ),
        value=f"{trap_ok}/{trap_n} = {trap_ok / trap_n:.1%}"
        if trap_n
        else M['RL1_03'],
        threshold="= 100%",
        numerator=trap_ok,
        denominator=trap_n,
        note=(
            f"{M['RL1_04']}"
            f"{len(with_evidence)}/{len(answerable)}"
            + (M['RL1_05'] if not with_evidence else "")
            + (
                ""
                if corpus is None
                else (
                    f"{M['RL1_06']}"
                    f"{corpus.get('active_chunks', 0)}{M['RL1_07']}"
                )
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
        name=M['RL2_02'],
        status=rl2_status,
        value="—"
        if numeric_rate is None
        else f"{numeric_ok}/{len(numeric_items)} = {numeric_rate:.1%}",
        threshold="≥ 96%",
        numerator=numeric_ok,
        denominator=len(numeric_items),
        note=(
            M['RL2_03']
            + ("" if db_available else M['RL2_04'])
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
        name=M['RL3_02'],
        status=rl3_status,
        value=f"{cite_ok}/{cite_total} = {cite_ok / cite_total:.1%}"
        if cite_total
        else M['RL3_03'],
        threshold="≥ 98%",
        numerator=cite_ok,
        denominator=cite_total,
        note=(
            M['RL3_04']
            + ("" if db_available else M['RL3_05'])
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
        name=M['RL4_02'],
        status=rl4_status,
        value=f"{len(faithful)}/{len(answered)} = "
        + (f"{len(faithful) / len(answered):.1%}" if answered else "—"),
        threshold=M['RL4_03'],
        numerator=len(faithful),
        denominator=len(answered),
        note=(
            M['RL4_04']
            + (
                f"{len(answered)}/{len(answerable)} = {coverage:.1%}"
                if coverage is not None
                else "—"
            )
            + M['RL4_05']
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
        verdict = M['VERDICT_01']
        conclusion = M['CONCLUSION_01']
    elif "NOT_MEASURABLE" in statuses:
        verdict = M['VERDICT_02']
        conclusion = M['CONCLUSION_02']
    else:
        verdict = M['VERDICT_03']
        conclusion = M['CONCLUSION_03']

    summary = (
        f"{M['SUMMARY_01']}{len(scores)}{M['SUMMARY_02']}{len(answerable)}{M['SUMMARY_03']}{len(trap)}），"
        f"{M['SUMMARY_04']}{len(answered)}{M['SUMMARY_05']}"
        f"{M['SUMMARY_06']}{total_score:.2f}；"
        f"{M['SUMMARY_07']}{sum(1 for i in trap if i.trap_safe)}/{len(trap)}。"
    )
    gen_cell = (
        M['GEN_CELL_01']
        if precondition.get("llm_configured")
        else M['GEN_CELL_02']
    )
    llm_cell = (
        M['LLM_CELL_01']
        if precondition.get("llm_configured")
        else M['LLM_CELL_02']
    )
    lines: list[str] = [
        M['LINES_01'],
        "",
        f"{M['LINES_02']}"
        f"{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}",
        f"{M['LINES_03']}{args.golden}`",
        f"- API：`{args.base_url}`",
        f"{M['LINES_04']}{token_source}"
        f"（user={args.user_id}, depts={list(args.dept)}, "
        f"roles={list(args.role)}）",
        M['LINES_05'],
        "",
        M['LINES_06'],
        "",
        f"**{verdict}**：{conclusion}",
        "",
        f"> {summary}",
        "",
        M['LINES_07'],
        "",
        M['LINES_08'],
        "|---|---|",
        f"{M['LINES_09']}{args.golden}`（{len(scores)}{M['LINES_10']}"
        f"{M['LINES_11']}{precondition.get('tbd_questions', 0)}{M['LINES_12']}",
        M['LINES_13'],
        f"{M['LINES_14']}{counts.get('documents', '—')}，"
        f"active versions={counts.get('active_versions', '—')}，"
        f"active chunks={counts.get('active_chunks', '—')} |",
        f"{M['LINES_15']}{gen_cell} |",
        f"{M['LINES_16']}{M['LINES_17'] if counts else M['LINES_18']} |",
        "",
        M['LINES_19'],
        "",
        M['LINES_20'],
        M['LINES_21'],
        M['LINES_22'],
        M['LINES_23'],
        "",
        M['LINES_24'],
        "",
        M['LINES_25'],
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
        M['LINES_26'],
        "",
    ]
    for key, text in RED_LINE_DEFS.items():
        lines.append(f"- **{key}**：{text}")
    lines += [
        "",
        M['LINES_27'],
        "",
        M['LINES_28'],
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
        M['LINES_29'],
        "",
        M['LINES_30'],
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
        M['LINES_31'],
        "",
        f"{M['LINES_32']}{counts.get('active_chunks', '—')}{M['LINES_33']}"
        f"{M['LINES_34']}",
        f"{M['LINES_35']}"
        f"{precondition.get('tbd_questions', 0)}/{len(scores)}"
        f"{M['LINES_36']}"
        f"{M['LINES_37']}"
        f"{M['LINES_38']}"
        f"{M['LINES_39']}",
        f"{M['LINES_40']}{llm_cell}；"
        f"{M['LINES_41']}"
        f"{M['LINES_42']}",
        M['LINES_43'],
        M['LINES_44'],
        "",
        M['LINES_45'],
        "",
        M['LINES_46'],
        M['LINES_47'],
        M['LINES_48'],
        M['LINES_49'],
        "",
        M['LINES_50'],
        "",
        f"{M['LINES_51']}{path_json}`"
        f"{M['LINES_52']}",
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
        description=M['PARSER_01']
    )
    parser.add_argument(
        "--golden", type=Path, default=DEFAULT_GOLDEN, help=M['BUILD_PARSER_01']
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:8000", help=M['BUILD_PARSER_02']
    )
    parser.add_argument(
        "--token", default="", help=M['BUILD_PARSER_03']
    )
    parser.add_argument(
        "--jwt-secret", default="", help=M['BUILD_PARSER_04']
    )
    parser.add_argument(
        "--user-id", default="eval_runner", help=M['BUILD_PARSER_05']
    )
    parser.add_argument(
        "--dept",
        action="append",
        default=["fin"],
        help=M['BUILD_PARSER_06'],
    )
    parser.add_argument(
        "--role",
        action="append",
        default=["staff"],
        help=M['BUILD_PARSER_07'],
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help=M['BUILD_PARSER_08'],
    )
    parser.add_argument(
        "--dsn",
        default=os.getenv("DATABASE_URL", ""),
        help=M['BUILD_PARSER_09'],
    )
    parser.add_argument(
        "--timeout", type=float, default=45.0, help=M['BUILD_PARSER_10']
    )
    parser.add_argument(
        "--settle", type=float, default=3.0, help=M['BUILD_PARSER_11']
    )
    parser.add_argument(
        "--only", default="", help=M['BUILD_PARSER_12']
    )
    parser.add_argument(
        "--category",
        default="",
        help=M['BUILD_PARSER_13'],
    )
    parser.add_argument(
        "--limit", type=int, default=0, help=M['BUILD_PARSER_14']
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--report-name", default="", help=M['BUILD_PARSER_15']
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
            raise SystemExit(f"{M['MAIN_01']}{sorted(missing)}")
    if args.category:
        rows = [row for row in rows if row["category"] == args.category]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        raise SystemExit(M['MAIN_02'])

    try:
        token, token_source = build_token(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(
            f"{M['MAIN_03']}{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 3

    db: Db | None = None
    db_reason = M['DB_REASON_01']
    if args.dsn:
        try:
            db = Db(args.dsn)
            db.counts()
            db_reason = M['DB_REASON_02']
        except Exception as exc:  # noqa: BLE001
            db = None
            db_reason = f"{M['DB_REASON_03']}{type(exc).__name__}: {exc}"

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
        "rubric": M['RUBRIC_01'],
    }

    missing_chunks = precondition["corpus_empty"]
    print(
        f"{M['MAIN_04']}{len(rows)} TBD doc_key={tbd_questions} "
        f"active_chunks={counts.get('active_chunks', '—')} "
        f"LLM={'on' if llm_configured else 'off'} DB={db_reason}"
    )
    if missing_chunks:
        print(M['MAIN_05'])
    if not llm_configured:
        print(M['MAIN_06'])

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
                audit = {"source": f"{M['SOURCE_01']}{type(exc).__name__}"}
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
    print(M['MAIN_07'])
    for item in red_lines:
        print(
            f"  {item.key} {item.name}: {item.status}  {item.value}  "
            f"{M['MAIN_08']}{item.threshold})"
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
