"""只暴露 single 子命令；all 等用户确认单文件产物后再实现。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from api.docparse.single import run_single as run_mineru
from api.docparse.stages import run_full_single


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="parse_doc")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("single", help="解析单个 PDF，先落本地阶段产物")
    one.add_argument("file", nargs="?", type=Path, default=None)
    one.add_argument("--max-pages", type=int, default=None, help="付费闸门：必须显式给")
    one.add_argument("--dry-run", action="store_true", help="只报页数与产出路径，不发请求")
    one.add_argument("--out", type=Path, default=Path("data/parse"))
    one.add_argument("--resume", type=Path, default=None, help="从已有输出目录恢复下载，不重提交")
    one.add_argument("--from-raw", type=Path, default=None, help="只跑后续阶段，不调 MinerU")
    one.add_argument("--source", type=Path, default=None, help="from-raw 时的原始 PDF 路径")
    one.add_argument("--batch-id", default=None, help="恢复下载时的 MinerU batch_id")
    one.add_argument("--zip-url", default=None, help="恢复下载时的 full_zip_url")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.file is None and args.resume is None and args.from_raw is None:
        print("拒绝执行：必须给 FILE 或 --resume/--from-raw")
        return 2
    try:
        if args.resume is not None:
            run_single(
                args.file or Path("."),
                max_pages=args.max_pages,
                out_root=args.out,
                dry_run=args.dry_run,
                resume_from=args.resume,
                resume_batch_id=args.batch_id,
                resume_zip_url=args.zip_url,
            )
            from_raw_dir = args.resume
        else:
            from_raw_dir = args.from_raw
        summary = run_full_single(
            args.file or Path("."),
            max_pages=args.max_pages,
            out_root=args.out,
            dry_run=args.dry_run,
            from_raw_dir=from_raw_dir,
            source_path=args.source,
        )
    except ValueError as exc:
        print(f"拒绝执行：{exc}")
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0
