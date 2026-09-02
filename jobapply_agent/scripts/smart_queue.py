#!/usr/bin/env python3
"""Run one URL-only Smart Job Queue cycle through an external tab bridge.

The bridge is restricted to ``list-tabs`` and ``open-listing <exact-url>``.
This command has no form, DOM, credential, upload, or application-action
options. Its JSON output contains counts only, so browser URLs stay internal to
the host and queue reconciliation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from typing import Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
REPOSITORY_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jobapply_agent.smart_queue import QueueCandidate, QueuePolicyError, QueueStorageError, SmartJobQueue  # noqa: E402


DEFAULT_DATABASE_PATH = PROJECT_ROOT / "private" / "smart_queue.sqlite3"
_CANDIDATE_FIELDS = frozenset(QueueCandidate.__dataclass_fields__)


def _load_external_adapter_class() -> type[object]:
    """Load the portable argv/JSON adapter without making it a core dependency."""

    adapter_path = REPOSITORY_ROOT / "skills" / "easy-apply-tab-monitor" / "scripts" / "browser_tab_adapter.py"
    spec = importlib.util.spec_from_file_location("smart_queue_external_adapter", adapter_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("external browser adapter is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    adapter_class = getattr(module, "ExternalCommandAdapter", None)
    if adapter_class is None:
        raise RuntimeError("external browser adapter is unavailable")
    return adapter_class


def _load_coordinator_class() -> type[object]:
    """Load the browser-owning coordinator from the bounded tab-monitor skill."""

    coordinator_path = REPOSITORY_ROOT / "skills" / "easy-apply-tab-monitor" / "scripts" / "smart_queue_coordinator.py"
    spec = importlib.util.spec_from_file_location("smart_queue_skill_coordinator", coordinator_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("smart queue coordinator is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    coordinator_class = getattr(module, "SmartQueueCoordinator", None)
    if coordinator_class is None:
        raise RuntimeError("smart queue coordinator is unavailable")
    return coordinator_class


def _load_recommendations(path: Path) -> tuple[QueueCandidate, ...]:
    """Read explicit deterministic recommendation records, never candidate data."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("recommendation input is invalid") from None
    if not isinstance(payload, list):
        raise ValueError("recommendation input must be a JSON array")

    candidates: list[QueueCandidate] = []
    for value in payload:
        if not isinstance(value, dict) or set(value) != _CANDIDATE_FIELDS:
            raise ValueError("recommendation input has an invalid record shape")
        candidate_values = dict(value)
        evidence = candidate_values.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError("recommendation evidence must be a JSON array")
        candidate_values["evidence"] = tuple(evidence)
        try:
            candidates.append(QueueCandidate(**candidate_values))
        except (QueuePolicyError, TypeError):
            raise ValueError("recommendation input contains an invalid record") from None
    return tuple(candidates)


def _status_payload(result: object) -> dict[str, int]:
    """Emit only counts: URL and browser-session data are never CLI output."""

    return {
        "initial_search_needed": result.initial_action.search_needed,
        "open_failed": len(result.open_failed_job_ids),
        "opened_visible": len(result.opened_job_ids) - len(result.open_failed_job_ids),
        "requested_opens": len(result.requested_action.job_ids),
        "search_needed": result.refill_action.search_needed,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one candidate-controlled, URL-only Smart Job Queue cycle.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    parser.add_argument(
        "--recommendations",
        type=Path,
        help="Explicit JSON recommendation records only; candidate profiles and form data are unsupported.",
    )
    parser.add_argument("--adapter-timeout-seconds", type=float, default=15.0)
    parser.add_argument(
        "--adapter-command",
        nargs=argparse.REMAINDER,
        required=True,
        help="External bridge argv prefix; this must be last and receives only list-tabs/open-listing operations.",
    )
    args = parser.parse_args(argv)
    try:
        if not args.adapter_command:
            raise ValueError("external adapter requires a command")
        recommendations = _load_recommendations(args.recommendations) if args.recommendations else ()
        adapter_class = _load_external_adapter_class()
        browser = adapter_class(args.adapter_command, timeout_seconds=args.adapter_timeout_seconds)
        coordinator_class = _load_coordinator_class()
        result = coordinator_class(SmartJobQueue(args.database), browser).cycle(recommendations)
        print(json.dumps(_status_payload(result), sort_keys=True))
    except (OSError, RuntimeError, TypeError, ValueError, QueuePolicyError, QueueStorageError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
