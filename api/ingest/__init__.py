"""摄取（A1）：样本扫描、判重、ingest_jobs 队列。.

分成三块，各自只管一件事：

- `fingerprint`：文件名 -> `doc_key`，文件字节 -> SHA-256。纯函数，不碰库。
- `jobs`：`ingest_jobs` 的状态机与重试计数。所有状态迁移只在这里写 SQL，
  别处一律用它的函数，否则"哪几个状态之间能跳"就又散开了。
- `scan`：扫 `samples/` 并写 `documents` / `doc_versions` / `ingest_jobs`，
  自带单任务锁（拿不到锁抛 `ScanInProgress`）。

口径来源：详细设计.md §2.1（doc_key 归一化）、§3.1（扫描判定表）、
§4.4（重复入库返回"已存在"）、附录 A.1/A.2/B（字段与索引）。
"""

from __future__ import annotations

from api.ingest.fingerprint import (
    SampleFile,
    doc_key_for,
    file_hash,
    list_samples,
)
from api.ingest.jobs import (
    IllegalTransition,
    JobStatus,
    claim_next_job,
    enqueue_job,
    fail_job,
    terminal_fail_job,
)
from api.ingest.scan import (
    ScanInProgress,
    ScanItem,
    ScanReport,
    scan_samples,
)

__all__ = [
    "IllegalTransition",
    "JobStatus",
    "SampleFile",
    "ScanInProgress",
    "ScanItem",
    "ScanReport",
    "claim_next_job",
    "doc_key_for",
    "enqueue_job",
    "fail_job",
    "file_hash",
    "list_samples",
    "scan_samples",
    "terminal_fail_job",
]
