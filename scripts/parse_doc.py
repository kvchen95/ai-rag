#!/usr/bin/env python3
"""单文件解析 CLI：当前只提供 single，不提供批量入口。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.docparse.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
