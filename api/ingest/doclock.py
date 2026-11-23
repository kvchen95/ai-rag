"""文档级咨询锁：同一份文档的写入必须串行（详细设计.md §4.1）。

三个模块要抢同一把锁：

- `scan`   —— 给文档建新版本（pending）；
- `flip`   —— 翻牌（旧版下线、新版生效、切片状态同步）；
- `reclaim`—— 回收（把过期旧版划 purged、删切片）。

同一个表达式、同一个语义（事务级锁，提交即释放），所以只有一处定义。锁 key 用
`hashtext(doc_key)`：它是 PG 内置的 int4 哈希函数，跨进程/跨语言都能算出来，
运维手敲 SQL 时也能复现——如果改用应用侧自算的 int8，排障时就得先跑一段 Python。

为什么是**事务级**（`pg_advisory_xact_lock`）而不是会话级：

- 这三个动作各自就该是一个事务（翻牌的三处改动要么全成要么全不成），
  提交/回滚自动放锁，没有"忘了 unlock 就留僵尸锁"的可能；
- 会话级锁会被连接池的 reset 保留（实测：psycopg 的 `reset()` 不释放 advisory
  lock），连接还池后锁仍挂在原会话上，后续请求会莫名其妙地被拒——B2 在扫描锁上
  踩过这个坑（`pg_try_advisory_lock` + `commit()` 还池 = 锁悬空）。

扫描用会话级锁是另一件事（整轮扫描跨多个事务），那由 `scan.py` 自己管，key 也
不同（`lockkeys.SCAN_LOCK_KEY`），两类锁互不影响。
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection

# 唯一的锁表达式。改这里 = 改所有调用方，这正是它只写一次的理由。
DOC_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtext(%s))"


def lock_document(conn: Connection[Any], doc_key: str, *, wait: bool = True) -> None:
    """给一份文档上事务级排他锁（必须已经在事务里）。

    `wait=False` 时用 `pg_try_advisory_xact_lock`，拿不到直接返回 False 给调用方——
    给"人工触发的翻牌"用：与其排在队列里等几秒再失败，不如立刻告诉操作者
    "这份文档正在被同步，稍后再试"。
    """
    sql = DOC_LOCK_SQL if wait else "SELECT pg_try_advisory_xact_lock(hashtext(%s))"
    with conn.cursor() as cur:
        cur.execute(sql, (doc_key,))
        row = cur.fetchone()
    if not wait and (row is None or not row[0]):
        raise DocumentLocked(doc_key)


class DocumentLocked(RuntimeError):
    """拿不到文档锁（另一事务正在写这份文档）。"""

    def __init__(self, doc_key: str) -> None:
        super().__init__(f"document {doc_key} is locked by another transaction")
        self.doc_key = doc_key


__all__ = ["DOC_LOCK_SQL", "DocumentLocked", "lock_document"]
