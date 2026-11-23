"""扫描判定（详细设计.md §3.1 的判定表 + §4.4 的重复入库口径）。

把"这轮扫到这个文件该做什么"抽成纯函数，因为它是整张卡里最容易被改错的地方：
判定表有五格，每格对应一种写库动作，混掉一格就会悄悄多出一个版本、或者把
已经生效的版本顶掉。纯函数可以脱离数据库把每一格都测一遍。

判定表（§3.1）：

| 情况 | 判定 | 动作 |
|---|---|---|
| 指纹未变 | 库里同 doc_key 已有同 file_hash 的版本 | 跳过，不产生任务 |
| 指纹变了 | 同 doc_key 出现新指纹 | 建版本，状态待转正 |
| 新文档 | doc_key 不存在 | 建文档 + 建版本 |
| 文件读不到 | 权限或路径问题 | 记失败，不动已有版本 |
| 扫描进行中 | 锁被占用 | 返回 409，不重复启动 |

判定的两个输入都是**库里已有的既成事实**（documents 行是否存在、同指纹版本
的状态），所以这一层不需要连接数据库。

### 同指纹命中时的三种细分

`UNIQUE (doc_key, file_hash)` 让同一内容在一个 doc_key 下只能有一个版本行，
于是"指纹没变"这一格其实是三种完全不同的情况，报告里必须分开，否则运维看到
一个 `skipped` 计数根本不知道是正常还是异常：

- `unchanged`      同指纹版本是 pending/active/skipped：正常跳过。
- `version_failed` 同指纹版本已经是 failed：§2.2 里 failed 是终态，同一份内容
  不自动重解析。静默重试会让"一直失败的文件"每轮扫描都产生新的解析尝试，
  日志刷满还看不出是同一个问题；要重试就人工重置那条 job。
- `content_revert` 同指纹版本是 deprecated/purged：内容回退到了某个旧版本。
  这时**不能**建新版本（唯一约束会拒），也不能当成"没变化"——回滚要走 §4.4 的
  翻牌事务把它重新置 active。扫描只负责把它报出来，不替人做翻牌决定。

### 为什么 added 与 changed 是两个 Outcome

两者写库动作相同（建一个 pending 版本 + 入队），区别只在"documents 行是不是
这次新建的"——而这正好是报告口径 `added` / `changed` 的定义（附录 C.1）。
把它做成判定函数的输出而不是调用方自己记一个布尔，是为了让报告与实际写库
在同一句话里决定，不会出现"建了文档但报的是 changed"。
"""

from __future__ import annotations

from enum import StrEnum


class VersionStatus(StrEnum):
    """`doc_versions.status`（附录 A.2 / `ck_doc_versions_status`）。"""

    PENDING = "pending"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    PURGED = "purged"
    FAILED = "failed"
    SKIPPED = "skipped"


class Outcome(StrEnum):
    """一文件的扫描结论。写库动作与报告口径都由它决定。"""

    # 建文档 + 建 pending 版本 + 入队（documents 行这次新建 -> 报 added）
    ADD = "add"
    # 建 pending 版本 + 入队（documents 已存在 -> 报 changed）
    CHANGE = "change"
    # 不建版本、不入队
    SKIP = "skip"
    # 记一条 failed 的 ingest_jobs（不建版本，不动已有版本）
    FAIL = "fail"


class SkipReason(StrEnum):
    """跳过/失败的原因码。机器可读，直接进响应与日志。"""

    UNCHANGED = "unchanged"
    VERSION_FAILED = "version_failed"
    CONTENT_REVERT = "content_revert"
    # 目录里两个文件归一化到同一个 doc_key（§4.4「两份文档 doc_key 撞车」）
    DOC_KEY_COLLISION = "doc_key_collision"
    # 文件读不到：权限、符号链接断裂、扫描中被删
    UNREADABLE = "unreadable"


# 命中这些状态 = 这份内容已经在库里，且没有"上一次解析失败"或"需要回滚"的语义。
SEEN_SKIP_STATUSES: frozenset[str] = frozenset(
    {VersionStatus.PENDING, VersionStatus.ACTIVE, VersionStatus.SKIPPED}
)

# deprecated / purged：内容回退到旧版，建不了新版本（唯一约束），要人走翻牌。
REVERT_STATUSES: frozenset[str] = frozenset(
    {VersionStatus.DEPRECATED, VersionStatus.PURGED}
)


def classify(
    *, doc_exists: bool, existing_status: str | None
) -> tuple[Outcome, SkipReason | None]:
    """(文档是否已存在, 同指纹版本的状态) -> 该做什么。

    `existing_status` 为 `None` 表示这个 (doc_key, file_hash) 还没有版本行。
    未知状态值会抛 `ValueError`——不替数据库猜枚举，让坏值在最早的地方炸。
    """
    if existing_status is not None:
        status = VersionStatus(existing_status)
        if status is VersionStatus.FAILED:
            return Outcome.SKIP, SkipReason.VERSION_FAILED
        if status in REVERT_STATUSES:
            return Outcome.SKIP, SkipReason.CONTENT_REVERT
        return Outcome.SKIP, SkipReason.UNCHANGED
    return (Outcome.CHANGE if doc_exists else Outcome.ADD), None
