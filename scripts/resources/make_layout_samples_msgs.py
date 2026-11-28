"""Chinese message catalog for scripts/make_layout_samples.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # make_layout_samples.py:560 (HELP)
    "HELP_01": "输出根目录（建议 api/tests/fixtures/pdf 或 /tmp/...）",
    # make_layout_samples.py:562 (HELP)
    "HELP_02": "覆盖已存在的文件",
}
