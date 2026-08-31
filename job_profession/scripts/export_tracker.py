#!/usr/bin/env python3
"""Refresh the local tracker export; this script never performs application actions."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from job_profession.tracker import WorkbookDependencyError, export_tracker  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export the local, auditable job tracker.")
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "data" / "jobs.sqlite3",
        help="SQLite tracker database path (default: %(default)s)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "output" / "Job_Application_Tracker.xlsx",
        help="Output .csv path. The historical .xlsx default reports a clear tooling blocker.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        exported = export_tracker(args.database, args.output)
    except WorkbookDependencyError as error:
        print(f"Export blocked: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1
    print(f"Exported tracker CSV: {exported}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
