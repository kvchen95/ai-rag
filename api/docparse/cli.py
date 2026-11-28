"""只暴露 single 子命令；all 等用户确认单文件产物后再实现。."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from api.docparse.single import run_single
from api.docparse.stages import run_full_single
from api.resources import messages as msg


def build_parser() -> argparse.ArgumentParser:
    """Run build_parser."""
    parser = argparse.ArgumentParser(prog="parse_doc")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("single", help=msg.CLI_HELP_SINGLE)
    one.add_argument("file", nargs="?", type=Path, default=None)
    one.add_argument(
        "--max-pages", type=int, default=None, help=msg.CLI_HELP_MAX_PAGES
    )
    one.add_argument(
        "--dry-run", action="store_true", help=msg.CLI_HELP_DRY_RUN
    )
    one.add_argument("--out", type=Path, default=Path("data/parse"))
    one.add_argument(
        "--resume",
        type=Path,
        default=None,
        help=msg.CLI_HELP_RESUME,
    )
    one.add_argument(
        "--from-raw", type=Path, default=None, help=msg.CLI_HELP_FROM_RAW
    )
    one.add_argument(
        "--source", type=Path, default=None, help=msg.CLI_HELP_SOURCE
    )
    one.add_argument(
        "--batch-id", default=None, help=msg.CLI_HELP_BATCH_ID
    )
    one.add_argument(
        "--zip-url", default=None, help=msg.CLI_HELP_ZIP_URL
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run main."""
    args = build_parser().parse_args(argv)
    if args.file is None and args.resume is None and args.from_raw is None:
        print(msg.CLI_REFUSE_NO_ARGUMENT)
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
        print(msg.CLI_REFUSE.format(error=exc))
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0
