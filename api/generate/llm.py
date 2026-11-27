"""受限生成的模型调用与输出解析（详细设计.md §9.2 / 附录 D.1 / D.4）。

这一层只做三件事：发请求、宽松解析一次、把"解析不了"变成明确的失败类型。
它**不判断内容对不对**——那是 §10 五道校验的事。边界划在这里的理由是：
把"模型说了但没有引用"当成解析错误处理，会让一个内容问题伪装成一个格式问题，
而两者的处置完全不同（前者要拒答，后者可以重试一次解析）。

三条落地口径：

1. **`Temperature = 0`**（§9.2、附录 D 末段）。不做"低温度"，就是 0：这个系统里
   任何随机性都只可能让答案变得更不可复核。请求体里显式写 `temperature`，不依赖
   网关默认值——默认值可能在网关侧被改成 0.7，而那样只有对比两次输出才看得出来。
2. **宽松解析一次**（§9.2 表格）：模型常把 JSON 包进 ```json 代码块。拨掉围栏再
   解析一次。仍失败就降级，**不重试三次以上**（附录 D 末段"失败路径统一"）。
   代码块里再套一层解释文字（"以下是 JSON：{...}"）也走同一条宽松路径：
   只取第一个 `{` 到最后一个 `}`。
3. **缺字段就算失败，不补默认值**（§9.2 表格："缺 `status` 或 `citations` → 降级，
   不重试"）。补一个 `citations=[]` 会让校验"没有引用"那一句失去对象，
   而模型其实就是没给引用——那是必须被拒答的。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

import httpx

from api.observability import get_logger, log_event

logger = get_logger("api.generate.llm")

Status = Literal["answered", "unknown"]

# 附录 D.1 的提示词逐字落地。三条不可弱化：只用证据、无证据说不知道、每句带引用。
# 改动只能加约束、不能删约束——所以这里的文本是一个常量，不是模板。
# 需要区别的只有证据包与问题，它们在最后两段拼接。
GENERATION_SYSTEM_PROMPT = """你是财报问答助手，只能依据【证据包】回答。
规则：
1) 只使用证据包内容，禁止外部知识、常识补全、推断；允许基于证据做透明的四则计算和对比分析。
2) 每个事实单独作为一个 fact，用 evidence_ids 绑定证据包内真实 chunk_id；不要手写 [n]，系统会按 evidence_ids 渲染角标。分号连接的多个事实必须拆成不同 fact；每个 fact 只引用直接包含该事实的 chunk，不得把叙述性 chunk 用作表格数值的引用。
3) 数字、金额、日期、百分比、名称必须与证据原文一致，不得四舍五入、补单位、换算；计算得到的派生值不受此限，但必须按规则 4 展示。
4) 开放性问题和对比问题可以计算：派生结论必须在同一 fact 写出“输入值 + 运算符 + 结果”，输入值只能来自被引证据，单位与期间必须一致；多步计算拆成多个 fact；四舍五入时用“约/≈”。不得把派生值说成报告直接披露。
5) 表格按“列名 = 语义”理解：`本报告期` 对应用户所问的单个报告期；`年初至报告期末/年初至报告期初` 是累计值，不能替代单期值；表头“（%）”“变动幅度（%）”声明该列为百分比。
6) 用户问句中的公司、年份、季度是检索范围，不是需要从证据重新证明的事实。
7) 证据缺少用户所问事实或计算输入时，status=unknown，不得用相邻期间、其他指标或外部假设替代。
8) answered 时 facts 非空，且每个 fact 至少一个 evidence_id；evidence_id 必须来自证据包的 chunk_id。
8) 只输出 JSON：{"status":"answered|unknown","facts":[{"kind":"fact|analysis|calculation","text":"...","evidence_ids":["chunk_id"],"name":"","expression":"","result":""}]}。calculation 必须填 name/expression/result；expression 只能用被引证据中的数值与四则运算，result 由系统/模型展示，但必须能被 evidence 复算。
9) 引用数字时不要额外补证据里没有的单位或页码；证据已写“单位：元”时，数字可直接按证据原文写出，必要时只写数字。
10) 回答先直接给结论，再给必要的期间/口径限定；可以综合多个证据分析趋势、结构和差异，但要区分“原文事实”与“基于原文的计算/判断”，不写报告未披露的原因，不评价投资价值。
11) 对开放性问题，先拆成可检索的子问题，再综合：每个分析结论必须能回溯到 facts 或 calculations；如果只能部分回答，明确说明未覆盖部分。

好问题与好回答示例（示例 chunk_id 只演示格式，实际必须使用证据包里的真实 chunk_id，不得照抄）：

示例 A：明确单季度与指标口径
问题：海光信息2026年第三季度（单季度）营业收入和归属于上市公司股东的净利润分别是多少？同比增幅是多少？
证据要点：单位：元；本报告期营业收入 5,855,689,454.32，同比 45.46%；本报告期归属于上市公司股东的净利润 1,048,040,894.63，同比 37.90%。
好回答：
{"status":"answered","facts":[{"text":"海光信息2026年第三季度营业收入为5,855,689,454.32元，同比增长45.46%。","evidence_ids":["hg_2026q3_fin_revenue"]},{"text":"海光信息2026年第三季度归属于上市公司股东的净利润为1,048,040,894.63元，同比增长37.90%。","evidence_ids":["hg_2026q3_fin_profit"]}]}

示例 B：区分单季度与累计值
问题：宇树科技2026年第三季度营业收入和年初至报告期末营业收入分别是多少？
证据要点：本报告期营业收入 665,044,497.90；年初至报告期末营业收入 1,817,290,048.48。
好回答：
{"status":"answered","facts":[{"text":"宇树科技2026年第三季度营业收入为665,044,497.90元。","evidence_ids":["unitree_2026q3_fin_q"]},{"text":"宇树科技2026年前三季度营业收入为1,817,290,048.48元。","evidence_ids":["unitree_2026q3_fin_ytd"]}]}

示例 C：开放性问题用透明计算回答
问题：海光信息2026年第三季度营业收入同比增速比归母净利润同比增速高多少个百分点？
证据要点：本报告期营业收入同比45.46%；归属于上市公司股东的净利润同比37.90%。
好回答：
{"status":"answered","facts":[{"text":"营业收入同比增速比归母净利润同比增速高：45.46% - 37.90% = 7.56 个百分点。","evidence_ids":["hg_2026q3_fin_revenue","hg_2026q3_fin_profit"]}]}

示例 D：证据没有直接披露或缺少计算输入时拒答
问题：海光信息2026年第三季度毛利率是多少？
证据要点：仅披露本报告期营业收入5,855,689,454.32元，没有披露营业成本，也没有直接披露毛利率。
好回答：
{"status":"unknown","facts":[]}
"""

# 附录 D.4 忠实性判别。判别器只吃"问题 + 被引证据 + 答案"，不吃整库上下文
# （避免它自己脑补）。这条在 `judge.py` 里靠"只传这三样"保证。
JUDGE_SYSTEM_PROMPT = """判断【答案】是否完全由【证据】支持。

SUPPORTED 必须同时满足：
1. 每个数字、金额、日期、百分比、名称都能在对应 [n] 证据中直接找到，或能由证据中直接可定位的数值按答案展示的公式复算；公式、输入值和结果必须在同一 answer/fact 中可检查；
2. “本报告期”与“年初至报告期末/年初至报告期初”没有被混淆；
3. 表头“（%）”“变动幅度（%）”声明的列按百分比理解；
4. 答案覆盖了问题的每个事实子问题；
5. 没有证据外推断、未经披露的假设、常识补全或因果关系；派生值必须明确标为计算结果。

NOT_SUPPORTED：
任一事实无直接证据、引用不匹配、期间错误、单位错误、漏答子问题、出现证据外数字，或公式/输入值/结果无法核实。

问句中的公司、年份、季度是检索范围，可作为答案范围重复。
答案里的 [n] 与“第 N 页”是引用元数据，不算事实内容。
只输出 JSON：{"verdict":"SUPPORTED|NOT_SUPPORTED","reason":"..."}

判定示例：
SUPPORTED：
问题：海光信息2026年第三季度（单季度）营业收入同比增速比归母净利润同比增速高多少个百分点？
证据：[1] 本报告期营业收入同比45.46%；[2] 归属于上市公司股东的净利润同比37.90%。
答案：营业收入同比增速比归母净利润同比增速高：45.46% - 37.90% = 7.56 个百分点。[1][2]
判定：{"verdict":"SUPPORTED","reason":"输入值和公式可由证据直接核实，运算正确。"}

SUPPORTED：
问题：海光信息2026年第三季度（单季度）营业收入是多少？
证据：[1] 本报告期营业收入 5,855,689,454.32；单位：元。
答案：海光信息2026年第三季度营业收入为5,855,689,454.32元。[1]
判定：{"verdict":"SUPPORTED","reason":"期间、指标和金额均与证据直接一致。"}

NOT_SUPPORTED：
问题：海光信息2026年第三季度（单季度）营业收入是多少？
证据：[1] 本报告期营业收入 5,855,689,454.32；年初至报告期末营业收入 14,954,710,229.13；单位：元。
答案：海光信息2026年第三季度营业收入为14,954,710,229.13元。[1]
判定：{"verdict":"NOT_SUPPORTED","reason":"答案把年初至报告期末累计值当成第三季度单季度值，期间口径错误。"}
"""

_CODE_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*|\s*```\s*$")


class GenerationUnavailable(RuntimeError):
    """生成服务不可用（连接失败 / 5xx / 超时）。处置是 fallback `model_timeout`。"""


class GenerationRejected(GenerationUnavailable):
    """模型返回了 4xx。请求形状有问题，重试无意义，也不是"模型超时"。"""


class DraftFormatError(RuntimeError):
    """模型输出解析不出约定结构（非 JSON / 缺字段 / answered 但答案为空）。

    这一类必须与 `GenerationUnavailable` 分开：前者是"模型说了但不算数"（§9.3 的
    整条拒答），后者是"系统异常"（§10.3 第三档）。混在一起会让一条本可以降到
    "只给原文"的回答变成"稍后重试"。
    """

    def __init__(self, reason: str, raw: str = "") -> None:
        self.reason = reason
        self.raw = raw[:800]
        super().__init__(f"{reason}: {self.raw[:200]}")


@dataclass(frozen=True, slots=True)
class Fact:
    """唯一事实单元：事实文本 + 证据 chunk_id 列表。"""

    text: str
    evidence_ids: tuple[str, ...] = ()
    kind: str = "fact"
    name: str = ""
    expression: str = ""
    result: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "kind": self.kind,
            "name": self.name,
            "expression": self.expression,
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class Draft:
    """一份草稿：facts 是唯一事实源；answer/citations 由渲染层生成，不是持久字段。"""

    status: Status
    facts: tuple[Fact, ...] = ()
    raw: str = ""
    model: str = ""
    temperature: float = 0.0
    # 解析时是否拨过一层代码块围栏。留痕是因为"模型总在包围栏"是一个可观测的
    # 提示词退化信号，直接吞掉就看不出来了。
    unwrapped: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def answer(self) -> str:
        return "\n".join(fact.text for fact in self.facts)

    @property
    def answered(self) -> bool:
        return self.status == "answered"

    def as_audit(self) -> dict[str, Any]:
        """审计口径：草稿 facts 进审计，answer/citations 由渲染层生成。"""
        return {
            "status": self.status,
            "facts": [fact.as_dict() for fact in self.facts],
            "model": self.model,
            "temperature": self.temperature,
            "unwrapped": self.unwrapped,
        }


def strip_code_fence(text: str) -> tuple[str, bool]:
    """拨掉 ```json 围栏，并报告"是否动过这段文本"。

    三个方向都要能处理：
    - 整段被围栏包住：` ```json\\n{...}\\n``` `；
    - 围栏外面还有一句解释：取第一个 `{` 到最后一个 `}`；
    - 没围栏但前后有解释（"结果如下：{...}"）：同样取花括号区间。

    第三种与第二种共用一条规则，因为在**解析之前**我们无法区分"正文里恰好有花括号"
    与"模型把 JSON 埋在句子里"。取花括号是安全的方向：真的是散文时，取出来的片段
    一样解析失败（`DraftFormatError`），与不取的结果相同；而模型埋了 JSON 时，取出来
    能救回一次本来会失败的生成。

    `changed=True` 只是"动过"，不代表围栏一定存在：这个标记进审计用来观察
    "模型是不是总在敷衍格式"，不要拿它当围栏检测器。
    """
    original = text or ""
    stripped = _CODE_FENCE.sub("", original).strip()
    unwrapped = stripped != original.strip()
    if not stripped:
        return "", unwrapped
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped, unwrapped
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        return stripped[start : end + 1], True
    return stripped, unwrapped


def _int_list(value: Any, *, field_name: str) -> tuple[int, ...]:
    """把引用编号数组归一成 tuple[int, ...]。非整数元素直接判格式错误。

    `True` 是 int 的子类，`[true]` 会被当成 `[1]`——所以显式排除 bool。
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise DraftFormatError(f"{field_name} 必须是数组，实际是 {type(value).__name__}")
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            if isinstance(item, str) and item.strip().isdigit():
                out.append(int(item.strip()))
                continue
            raise DraftFormatError(f"{field_name} 里有非整数元素: {item!r}")
        out.append(int(item))
    return tuple(dict.fromkeys(out))


def parse_draft(payload: str, *, model: str = "") -> Draft:
    """把模型输出解析成 `Draft`。任何不符合 §9.2 契约的形状都抛 `DraftFormatError`。

    §9.2 的表格逐行落地：
    - 非 JSON 或带代码块标记 → 宽松解析一次；仍失败则降级（抛）
    - 缺 `status` 或 `citations` → 降级，不重试（抛）
    - `status=answered` 但答案为空 → 视为失败（抛）
    - 引用编号超出证据范围 → **这里不管**，交给 §10.1 第一道（它需要证据编号范围）
    """
    body, unwrapped = strip_code_fence(payload)
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DraftFormatError("模型输出不是 JSON", raw=body) from exc
    if not isinstance(data, dict):
        raise DraftFormatError(f"模型输出不是对象: {type(data).__name__}", raw=body)

    status = data.get("status")
    if status not in ("answered", "unknown"):
        raise DraftFormatError(f"status 缺失或非法: {status!r}", raw=body)
    if "facts" not in data:
        raise DraftFormatError("缺少 facts 字段", raw=body)

    facts: list[Fact] = []
    raw_facts = data.get("facts")
    if raw_facts is None:
        raw_facts = []
    if not isinstance(raw_facts, (list, tuple)):
        raise DraftFormatError(f"facts 必须是数组，实际 {type(raw_facts).__name__}", raw=body)
    for item in raw_facts:
        if not isinstance(item, dict):
            raise DraftFormatError(f"fact 不是对象: {item!r}", raw=body)
        kind = str(item.get("kind") or "fact").strip().lower()
        if kind not in ("fact", "analysis", "calculation"):
            raise DraftFormatError(f"fact.kind 非法: {kind!r}", raw=body)
        text = item.get("text")
        if isinstance(text, str):
            text = text.strip()
        else:
            text = ""
        name = str(item.get("name") or "").strip()
        expression = str(item.get("expression") or "").strip()
        result = str(item.get("result") or "").strip()
        if kind == "calculation":
            if not name or not expression or not result:
                raise DraftFormatError("calculation 缺少 name/expression/result", raw=body)
            if not text:
                text = f"{name}：{expression} = {result}"
        if status == "answered" and not text:
            raise DraftFormatError("status=answered 但 fact.text 为空", raw=body)
        evidence_ids = item.get("evidence_ids")
        if evidence_ids is None:
            evidence_ids = item.get("citations")
        if evidence_ids is None:
            evidence_ids = []
        evidence_ids = _str_list(evidence_ids, field_name="fact.evidence_ids")
        if status == "answered" and not evidence_ids:
            raise DraftFormatError("status=answered 但 fact 缺少 evidence_ids", raw=body)
        facts.append(
            Fact(
                text=text,
                evidence_ids=evidence_ids,
                kind=kind,
                name=name,
                expression=expression,
                result=result,
            )
        )

    if status == "answered" and not facts:
        raise DraftFormatError("status=answered 但 facts 为空", raw=body)

    raw = data.get("raw", body)
    known = {"status", "facts", "citations", "claims", "answer"}
    return Draft(
        status=status,  # type: ignore[arg-type]
        facts=tuple(facts),
        raw=body[:4000],
        model=model,
        temperature=0.0,
        unwrapped=unwrapped,
        extra={key: value for key, value in data.items() if key not in known},
    )


def build_generation_messages(question: str, evidence_block: str) -> list[dict[str, str]]:
    """拼三段：角色与约束（固定）、证据包、输出要求。

    §9.2："三段都固定，不随问题变"。所以问题只在最后一段出现，且不加任何
    "请仔细阅读"之类的祈使句——那些话会随人改动而漂移，而漂移的提示词
    必须重跑评测集（附录 D 末段）。
    """
    return [
        {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"【证据包】\n{evidence_block}\n\n"
                f"【问题】\n{question.strip()}\n\n"
                "只输出 JSON。"
            ),
        },
    ]


# --- 共享 keep-alive 连接池 ---------------------------------------------------
# 根因（2026-11-24 实测）：原先每次 complete() 都 `with httpx.Client(...)`，
# 等于每次问答都做一次全新的 TCP + TLS 握手。第三方 SLB 在握手阶段随机回
# `[SSL: UNEXPECTED_EOF_WHILE_READING]`，30 次冷连接探测失败 5~7 次（~20%）；
# 同一网关换成共享 keep-alive 客户端后降到 1/30，中位延迟 3.1s→2.3s。
# 所以这里持有长连接客户端并被生成/判别两条链路共用（同 endpoint+key+timeout）。
_POOL_LOCK = threading.Lock()
_SHARED_CLIENTS: dict[tuple[str, str, float], httpx.Client] = {}


def _shared_client(base_url: str, api_key: str, timeout_s: float) -> httpx.Client:
    """按 (endpoint, key, timeout) 复用一个线程安全的 keep-alive 客户端。

    生成与判别是同一个 llm_client(settings) 工厂，走同一把池；不同凭证绝不共用，
    避免把 A 租户的 token 发到 B 的 endpoint。
    """
    key = (base_url.rstrip("/"), api_key, timeout_s)
    with _POOL_LOCK:
        client = _SHARED_CLIENTS.get(key)
        if client is None or client.is_closed:
            client = httpx.Client(
                base_url=key[0],
                timeout=timeout_s,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                trust_env=False,
                limits=httpx.Limits(
                    max_connections=8,
                    max_keepalive_connections=8,
                    keepalive_expiry=30.0,
                ),
            )
            _SHARED_CLIENTS[key] = client
        return client


def reset_shared_clients() -> None:
    """关闭并清空连接池（测试 / 配置变更后调用）。"""
    with _POOL_LOCK:
        for client in _SHARED_CLIENTS.values():
            client.close()
        _SHARED_CLIENTS.clear()


@dataclass(slots=True)
class LlmClient:
    """OpenAI 兼容网关的 `/chat/completions` 薄客户端。

    刻意不接 SDK：这里只有一次非流式的严格 JSON 调用，SDK 会带进一整套流式与
    重试逻辑，而重试策略必须是我们的（附录 D："不重试三次以上"）。

    max_attempts 只覆盖**传输层瞬时故障**（握手 SSL EOF / 超时 / 5xx /
    空响应）——这些调用都没有产出内容，重试是幂等的。GenerationRejected（4xx）
    和 DraftFormatError（模型输出格式错）不重试，语义见各自的异常类注释。
    """

    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 30.0
    max_attempts: int = 6
    retry_backoff_s: float = 0.3
    retry_budget_s: float = 45.0

    def complete(
        self, messages: Sequence[dict[str, str]], *, temperature: float = 0.0
    ) -> str:
        body = {
            "model": self.model,
            "messages": list(messages),
            # 显式写 0：不依赖网关默认值（默认值可能被改成 0.7，而那时只有对比
            # 两次输出才看得出来）。
            "temperature": temperature,
            "stream": False,
        }
        # 总预算与 LLM_TIMEOUT_S 对齐：单次真超时（如 30s）用满预算后不再重试，
        # 不会把一次问答放大成 N×30s。瞬时 SSL EOF 通常在 1~7s 失败，预算内可重试。
        deadline = time.monotonic() + self.retry_budget_s
        last: GenerationUnavailable | None = None
        for attempt in range(1, max(1, self.max_attempts) + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                client = _shared_client(self.base_url, self.api_key, self.timeout_s)
                resp = client.post("/chat/completions", json=body, timeout=remaining)
                if resp.status_code >= 500:
                    raise GenerationUnavailable(f"llm -> HTTP {resp.status_code}: {resp.text[:200]}")
                if resp.status_code >= 400:
                    # 4xx 是请求形状错误，重试无意义——立即抛出，不进重试分支。
                    raise GenerationRejected(f"llm rejected: HTTP {resp.status_code} {resp.text[:300]}")
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise GenerationUnavailable(f"llm returned non-JSON: {exc}") from exc
                choices = (data or {}).get("choices") or []
                if not choices:
                    raise GenerationUnavailable("llm returned no choices")
                message = choices[0].get("message") or {}
                content = message.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise GenerationUnavailable("llm returned empty content")
                return content
            except httpx.TimeoutException as exc:
                last = GenerationUnavailable(f"llm timed out after {self.timeout_s}s")
                last.__cause__ = exc
            except httpx.HTTPError as exc:
                # SSL EOF 走这里（httpx.ConnectError ⊂ httpx.HTTPError）。
                last = GenerationUnavailable(f"llm unreachable: {exc}")
                last.__cause__ = exc
            except GenerationRejected:
                raise
            except GenerationUnavailable as exc:
                last = exc

            if attempt >= max(1, self.max_attempts):
                raise last  # type: ignore[misc]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise last  # type: ignore[misc]
            # 指数退避 + 抖动：避免同一批请求在 SLB 恢复瞬间同时重试造成二次抖动。
            backoff = min(self.retry_backoff_s * (2 ** (attempt - 1)), remaining)
            if backoff > 0:
                time.sleep(backoff * (0.5 + random.random() * 0.5))
            log_event(
                logger,
                "llm_retry",
                level=30,
                attempt=attempt,
                model=self.model,
                reason=str(last)[:160],
            )
        raise last  # type: ignore[misc]


def generate_draft(
    client: LlmClient,
    question: str,
    evidence_block: str,
    *,
    model: str | None = None,
) -> Draft:
    """一次生成：发请求 → 解析。解析失败**不重试**（§9.2 表格）。

    "不重试"是有代价的取舍：重试一次能把"模型偶发多说一句解释"救回来，但也会把
    一次真正的提示词退化变成偶发失败而被忽略。§9.2 明确要求不重试，这里照做。
    """
    content = client.complete(
        build_generation_messages(question, evidence_block), temperature=0.0
    )
    draft = parse_draft(content, model=model or client.model)
    if draft.unwrapped:
        log_event(
            logger,
            "draft_unwrapped_code_fence",
            level=30,
            model=draft.model,
        )
    return draft


def _str_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise DraftFormatError(f"{field_name} 必须是数组，实际是 {type(value).__name__}")
    out: list[str] = []
    for item in value:
        if isinstance(item, bool):
            raise DraftFormatError(f"{field_name} 里有非字符串元素: {item!r}")
        if isinstance(item, str):
            out.append(item)
            continue
        if isinstance(item, int):
            out.append(str(item))
            continue
        raise DraftFormatError(f"{field_name} 里有非字符串元素: {item!r}")
    return tuple(dict.fromkeys(out))


__all__ = [
    "Fact",
    "Draft",
    "DraftFormatError",
    "GENERATION_SYSTEM_PROMPT",
    "GenerationRejected",
    "GenerationUnavailable",
    "JUDGE_SYSTEM_PROMPT",
    "LlmClient",
    "build_generation_messages",
    "generate_draft",
    "parse_draft",
    "strip_code_fence",
]
