"""Chinese message catalog for scripts/make_samples.py.

Keys are generated per call site; each comment gives the source line
the message was lifted from.
"""

from __future__ import annotations

MESSAGES: dict[str, str] = {
    # make_samples.py:130 (BUILD_PDF)
    "BUILD_PDF_01": "font 对象号位移了",
    # make_samples.py:202 (HELP)
    "HELP_01": "samples 根目录",
    # make_samples.py:204 (HELP)
    "HELP_02": "覆盖已存在的样本",
}
