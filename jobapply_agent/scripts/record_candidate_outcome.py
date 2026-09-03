#!/usr/bin/env python3
"""Record one candidate-confirmed queue outcome without browser authority."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = PROJECT_ROOT / "private"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jobapply_agent.candidate_memory import (  # noqa: E402
    CandidateMemory,
    CandidateMemoryPolicyError,
    CandidateMemoryStorageError,
    private_database_path,
)
from jobapply_agent.smart_queue import QueuePolicyError, QueueStorageError, SmartJobQueue  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Record one explicit candidate outcome in private local queue memory."
    )
    parser.add_argument("--queue-db", type=Path, required=True)
    parser.add_argument("--memory-db", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--outcome", required=True, choices=("submitted", "rejected", "skipped"))
    parser.add_argument(
        "--vacated",
        action="store_true",
        required=True,
        help="Required explicit confirmation that the managed listing tab is vacated.",
    )
    return parser.parse_args()


def _private_database(value: Path, *, must_exist: bool) -> Path:
    if PRIVATE_ROOT.is_symlink():
        raise CandidateMemoryPolicyError("private runtime directory is invalid")
    database = private_database_path(value, private_root=PRIVATE_ROOT)
    if must_exist and not database.is_file():
        raise CandidateMemoryPolicyError("private queue database is unavailable")
    return database


def main() -> int:
    args = parse_args()
    try:
        if args.vacated is not True:
            raise CandidateMemoryPolicyError("explicit vacancy confirmation is required")
        queue_database = _private_database(args.queue_db, must_exist=True)
        memory_database = _private_database(args.memory_db, must_exist=False)
        queue = SmartJobQueue(queue_database)
        memory = CandidateMemory(memory_database, private_root=PRIVATE_ROOT)
        reconciliation = memory.finalize_queue_outcome(
            queue=queue,
            job_id=args.job_id,
            outcome=args.outcome,
            actor="user",
            vacated=True,
        )
    except (
        CandidateMemoryPolicyError,
        CandidateMemoryStorageError,
        KeyError,
        QueuePolicyError,
        QueueStorageError,
        OSError,
    ):
        print(json.dumps({"status": "blocked", "reconciled": 0}, separators=(",", ":")))
        return 2
    print(
        json.dumps(
            {"status": "ok", "reconciled": int(reconciliation.inserted)},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
