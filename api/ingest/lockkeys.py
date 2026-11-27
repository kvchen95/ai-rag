"""advisory lock 的 key 派生物（纯 stdlib，无第三方 import）。.

单独一个文件是为了避免 `config.py`（几乎每个模块都 import 它）反向依赖
`api.ingest.scan`（会一路拖进 psycopg、连接池）。锁 key 是"两个模块都需要
的常量"，而常量放在一个零依赖的模块里最省事。

### 为什么 key 要算出来而不是写死

PG 的 advisory lock 只收 int4 或 int8。手写一个十六进制字面量时特别容易超出 int8
（`0x61695F7261675F7363616E` 就会被当成 numeric，`pg_try_advisory_lock` 直接报
"function does not exist"——实测踩过）。从固定字符串取 sha256 前 8 字节并显式折成
带符号 int8，既落在范围内，也保证"同一个种子永远是同一个 key"。

### 两把锁的空间不能重叠

- 扫描锁：从字符串派生，占满 int8 全域（含负数段）。
- 文档锁：A3 用 `pg_advisory_xact_lock(hashtext(doc_key))`，而 `hashtext` 只
  返回 int4。

int4 的值一定落在 int8 的范围内，所以"空间不重叠"靠的是**值不相等**而不是区间：
种子固定，实际冲突概率是 2^-32 量级；即使撞上，后果是"某份文档的翻牌会等
扫描结束"，不会出现数据错乱。要做到结构上绝对的隔离需要用双参数形式
`pg_advisory_lock(int4, int4)` 给两类锁分域，但那会让 A3 现有的 `hashtext`
调用全部改一遍——收益不值这个代价，
这里把权衡写下来，免得下次有人以为是漏了。
"""

from __future__ import annotations

import hashlib

# 改这个种子 = 换一把锁：正在跑的旧扫描不会被新代码看见（实际影响只是那一轮
# 不互斥）。
SCAN_LOCK_SEED = b"ai_rag:sample_scan:v1"

SCAN_LOCK_KEY: int = int.from_bytes(
    hashlib.sha256(SCAN_LOCK_SEED).digest()[:8], "big", signed=True
)

# int8 的边界：给测试用，证明派生值确实落在 PG 认的范围内。
INT8_MIN = -(2**63)
INT8_MAX = 2**63 - 1


def in_int8(value: int) -> bool:
    """Run in_int8."""
    return INT8_MIN <= value <= INT8_MAX
