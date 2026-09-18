#!/usr/bin/env python3
"""Host-side Smart Queue CLI for candidate-controlled job listing tabs.

The command-line host orchestrator. It composes only the repository's existing
bounded components and adds no authority beyond them:

    jobapply_queue.py doctor            prerequisite and bridge diagnostics
    jobapply_queue.py status            queue counts, capacity, open tab count
    jobapply_queue.py tabs N            set managed capacity (1-10)
    jobapply_queue.py open              run bounded reconciliation cycles
    jobapply_queue.py admit FILE        admit a validated discovery export
    jobapply_queue.py search            run the candidate-approved search handoff
    jobapply_queue.py watch             persistent host loop with refill
    jobapply_queue.py outcome           record a candidate-confirmed outcome
    jobapply_queue.py log               tail the watch status log

Boundaries preserved:

* No browser authority beyond the bounded bridge. This tool never launches a
  browser, creates a window, closes a tab, or inspects page content. Its only
  browser operations are the bridge's two permitted ones: list tab URLs and
  open one exact approved listing URL.
* Searching is an explicit, candidate-supplied, read-only command run by this
  host loop. The daemon itself must never search, rank, or invent candidates.
* No form filling, no uploads, no submissions. Only an explicit candidate
  outcome confirmation records an application result.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR / "src" / "jobapply_agent" / "smart_queue.py").exists():
    _DEFAULT_ROOT = SCRIPT_DIR
elif (SCRIPT_DIR.parent / "src" / "jobapply_agent" / "smart_queue.py").exists():
    _DEFAULT_ROOT = SCRIPT_DIR.parent
else:
    _DEFAULT_ROOT = SCRIPT_DIR.parent

PRIVATE_DIRECTORY = "jobapply_agent/private"
DEFAULT_QUEUE_DB = "smart-queue.sqlite3"
DEFAULT_MEMORY_DB = "candidate-memory.sqlite3"
DEFAULT_INTAKE = "candidate_intake.json"
DEFAULT_BRIDGE = "system_chrome_bridge.mjs"
DEFAULT_STATUS_LOG = "smart_queue_watch.out"
MINIMUM_CAPACITY = 1
MAXIMUM_CAPACITY = 10
SUPPORTED_OUTCOMES = ("submitted", "rejected", "skipped")

_DURATION = re.compile(r"(?P<value>[0-9]+)(?P<unit>s|m|h)?\Z")
_LINKEDIN_LISTING = re.compile(r"^https://[a-z0-9.-]*linkedin\.com/jobs/view/[A-Za-z0-9_-]+/?$")
_INDEED_LISTING = re.compile(r"^https://[a-z0-9.-]*indeed\.com/viewjob\?jk=[A-Za-z0-9_-]+$")


class CliUsageError(RuntimeError):
    """Raised for an invalid invocation or an unavailable dependency."""


def parse_duration(raw: str) -> float:
    """Return seconds for ``90``, ``30s``, ``10m``, or ``2h``.

    A bare number is seconds. Only non-negative integers are accepted so a
    mistyped duration cannot silently mean "run forever".
    """

    if not isinstance(raw, str):
        raise CliUsageError("duration must be a string such as 30m")
    match = _DURATION.match(raw.strip())
    if match is None:
        raise CliUsageError("duration must look like 90, 30s, 10m, or 2h")
    multiplier = {"s": 1, "m": 60, "h": 3600}[match.group("unit") or "s"]
    return float(int(match.group("value")) * multiplier)


def require_capacity(raw: object) -> int:
    """Return a validated managed-tab capacity inside the documented bound."""

    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CliUsageError("capacity must be an integer between 1 and 10")
    if not MINIMUM_CAPACITY <= raw <= MAXIMUM_CAPACITY:
        raise CliUsageError("capacity must be an integer between 1 and 10")
    return raw


def _has_repo_marker(candidate: Path) -> bool:
    """Return True when the candidate directory is a repository checkout."""

    try:
        return (candidate / "jobapply_agent" / "src" / "jobapply_agent" / "smart_queue.py").is_file()
    except OSError:
        return False


def resolve_repository_root(explicit: object = None) -> Path:
    """Locate the repository root: --repo, else the current directory.

    The current working directory is the default: run the tool from the
    repository root and nothing needs to be configured. The script's own
    checkout is the last fallback.
    """

    candidates: list[Path] = []
    if isinstance(explicit, str) and explicit.strip():
        candidates.append(Path(explicit).expanduser())
    elif isinstance(explicit, Path):
        candidates.append(explicit)
    else:
        candidates.append(Path.cwd())
    candidates.append(SCRIPT_DIR.parent.parent)
    for candidate in candidates:
        if _has_repo_marker(candidate):
            return candidate.resolve()
    raise CliUsageError("run from the repository root or pass --repo <path>")


def private_paths(root: Path) -> dict[str, Path]:
    """Return the git-ignored private runtime paths this tool reads and writes."""

    private = root / PRIVATE_DIRECTORY
    return {
        "private": private,
        "queue": private / DEFAULT_QUEUE_DB,
        "memory": private / DEFAULT_MEMORY_DB,
        "intake": private / DEFAULT_INTAKE,
        "bridge": private / DEFAULT_BRIDGE,
        "status_log": private / DEFAULT_STATUS_LOG,
    }


def _load_queue_module(root: Path):
    """Load the internal queue package from a repository checkout."""

    source_root = root / "jobapply_agent" / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    from jobapply_agent.smart_queue import (  # noqa: PLC0415 - resolved at runtime
        QueueCandidate,
        QueuePolicyError,
        QueueStorageError,
        SmartJobQueue,
    )

    return SmartJobQueue, QueueCandidate, QueuePolicyError, QueueStorageError


def queue_state_summary(states: Mapping[str, str]) -> dict[str, int]:
    """Return a deterministic count of derived queue states."""

    counts = Counter(str(state) for state in states.values())
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def collect_job_states(queue: Any) -> dict[str, str]:
    """Map each queue job id to its derived state using only public reads.

    Job ids come from a read-only inspection of the private queue database; the
    state is always derived by the queue's own ``get`` API so this tool never
    re-implements queue lifecycle rules.
    """

    import sqlite3  # noqa: PLC0415 - keeps module import free of side effects

    states: dict[str, str] = {}
    database = Path(str(queue.database_path))
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT job_id FROM smart_queue_jobs ORDER BY created_at, job_id").fetchall()
    finally:
        connection.close()
    for (raw_job_id,) in rows:
        try:
            states[str(raw_job_id)] = str(queue.get(str(raw_job_id)).state)
        except Exception:  # noqa: BLE001 - one unreadable row must not hide the rest
            continue
    return states


def parse_bridge_urls(stdout: str, *, maximum: int = 512) -> tuple[str, ...]:
    """Parse the bounded bridge's ``list-tabs`` JSON array output.

    A malformed, oversized, or non-array payload fails closed so an unexpected
    bridge response is never treated as a valid tab snapshot.
    """

    if len(stdout.encode("utf-8", "ignore")) > 4_194_304:
        raise CliUsageError("bridge snapshot is too large")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise CliUsageError("bridge returned an invalid snapshot") from error
    if not isinstance(payload, list) or len(payload) > maximum:
        raise CliUsageError("bridge returned an invalid snapshot")
    urls: list[str] = []
    for item in payload:
        if not isinstance(item, str) or not item or len(item) > 8192:
            raise CliUsageError("bridge returned an invalid snapshot")
        urls.append(item)
    return tuple(urls)


def listing_tab_urls(bridge_command: Sequence[str], *, timeout: float = 20.0) -> tuple[str, ...]:
    """List the host session's tab URLs through the bounded bridge only."""

    if not bridge_command:
        raise CliUsageError("a bridge command is required")
    try:
        completed = subprocess.run(
            [*bridge_command, "list-tabs"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as error:
        raise CliUsageError("bridge command is not available") from error
    except subprocess.TimeoutExpired as error:
        raise CliUsageError("bridge command timed out") from error
    if completed.returncode != 0:
        raise CliUsageError("bridge could not list the existing session's tabs")
    return parse_bridge_urls(completed.stdout)


def count_listing_tabs(urls: Iterable[str]) -> int:
    """Count only recognized LinkedIn/Indeed listing URLs, never unrelated tabs."""

    return sum(
        1
        for url in urls
        if isinstance(url, str) and (_LINKEDIN_LISTING.match(url) or _INDEED_LISTING.match(url))
    )


def default_bridge_command(root: Path, node: str = "node") -> list[str]:
    """Return the bounded Chrome bridge argv for this checkout."""

    bridge = private_paths(root)["bridge"]
    if not bridge.is_file():
        raise CliUsageError(f"bounded bridge is missing at {bridge}")
    return [node, str(bridge)]


def status_payload(queue: Any, bridge_command: Sequence[str] | None = None) -> dict[str, Any]:
    """Build the redacted status document the ``status`` command prints.

    Counts and opaque IDs only: no listing URL, candidate fact, or page content.
    """

    states = collect_job_states(queue)
    payload: dict[str, Any] = {
        "queue_id": str(queue.queue_id),
        "capacity": int(queue.target_size),
        "capacity_provenance": str(queue.capacity_provenance),
        "jobs_total": len(states),
        "states": queue_state_summary(states),
        "confirmed_submitted": int(queue.confirmed_submitted_count()),
    }
    profile_revision, matcher_revision = queue.active_revisions
    payload["profile_revision_bound"] = bool(profile_revision)
    payload["matcher_revision_bound"] = bool(matcher_revision)
    if bridge_command:
        try:
            payload["listing_tabs_open"] = count_listing_tabs(listing_tab_urls(bridge_command))
            payload["bridge_error"] = None
        except CliUsageError as error:
            payload["listing_tabs_open"] = None
            payload["bridge_error"] = str(error)
    return payload


def daemon_cycle_command(
    root: Path,
    *,
    intake: Path,
    database: Path,
    bridge_command: Sequence[str],
    ticks: int = 1,
    interval_seconds: float | None = None,
) -> list[str]:
    """Return the bounded daemon argv for finite, host-controlled cycles."""

    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 1:
        raise CliUsageError("ticks must be a positive integer")
    daemon_script = root / "skills" / "easy-apply-tab-monitor" / "scripts" / "smart_queue_daemon.py"
    if not daemon_script.is_file():
        raise CliUsageError("the bounded Smart Queue daemon is unavailable in this checkout")
    if not bridge_command:
        raise CliUsageError("a bridge command is required")
    command = [
        sys.executable,
        str(daemon_script),
        "--candidate-intake", str(intake),
        "--database", str(database),
        "--adapter", "external",
        "--adapter-command", *bridge_command,
        "--max-ticks", str(ticks),
    ]
    if interval_seconds is not None:
        command.extend(["--interval-seconds", str(interval_seconds)])
    return command


def parse_cycle_status(stdout: str) -> dict[str, Any]:
    """Parse the daemon's redacted count-only JSON status frame."""

    text = stdout.strip()
    if not text:
        raise CliUsageError("the daemon returned an unreadable status frame")
    try:
        payload = json.loads(text.splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as error:
        raise CliUsageError("the daemon returned an unreadable status frame") from error
    if not isinstance(payload, dict):
        raise CliUsageError("the daemon returned an unreadable status frame")
    return payload


def run_cycle(command: Sequence[str], *, timeout: float = 420.0) -> dict[str, Any]:
    """Run bounded reconciliation cycles and return the final status frame."""

    try:
        completed = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise CliUsageError("the reconciliation cycle timed out") from error
    if completed.returncode != 0:
        raise CliUsageError(
            "the reconciliation cycle failed closed (exit "
            f"{completed.returncode}); verify the session bridge and configuration"
        )
    return parse_cycle_status(completed.stdout)


def admit_export(
    root: Path,
    *,
    export: Path,
    intake: Path,
    queue_db: Path,
    memory_db: Path,
) -> dict[str, Any]:
    """Admit one validated discovery export through the existing admit path."""

    script = root / "jobapply_agent" / "scripts" / "discover.py"
    if not script.is_file():
        raise CliUsageError("the admission entry point is unavailable in this checkout")
    if not export.is_file():
        raise CliUsageError(f"the discovery export is missing at {export}")
    try:
        completed = subprocess.run(
            [
                sys.executable, str(script), "admit-queue",
                "--candidate-intake", str(intake),
                "--discovery-export", str(export),
                "--queue-db", str(queue_db),
                "--memory-db", str(memory_db),
            ],
            capture_output=True, text=True, timeout=900, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CliUsageError("admission timed out") from error
    if completed.returncode != 0:
        raise CliUsageError("admission failed closed; the export was not admitted")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as error:
        raise CliUsageError("admission returned an unreadable status") from error
    return payload if isinstance(payload, dict) else {}


def run_search_command(command: Sequence[str], *, timeout: float = 420.0) -> str:
    """Run the candidate-supplied, read-only search command.

    The argv sequence runs without a shell and must print one admission-format
    JSON object per line on stdout. The daemon must never search, so this host
    step is the only place a search happens.
    """

    if not command:
        raise CliUsageError("a search command is required for the search handoff")
    try:
        completed = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise CliUsageError("the search command is not available") from error
    except subprocess.TimeoutExpired as error:
        raise CliUsageError("the search command timed out") from error
    if completed.returncode != 0:
        raise CliUsageError("the search command failed")
    if not completed.stdout.strip():
        raise CliUsageError("the search command produced no listings")
    return completed.stdout


def write_export(path: Path, text: str) -> Path:
    """Write an admission export atomically beneath the private directory."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return path


def search_and_admit(
    root: Path, *, search_command: Sequence[str], paths: Mapping[str, Path]
) -> dict[str, Any]:
    """Run the search handoff, stage its export privately, then admit it."""

    text = run_search_command(search_command)
    export = write_export(paths["private"] / "search_handoff_export.jsonl", text)
    return admit_export(
        root,
        export=export,
        intake=paths["intake"],
        queue_db=paths["queue"],
        memory_db=paths["memory"],
    )


def format_status_line(cycle: int, status: Mapping[str, Any]) -> str:
    """Render one compact human status line for the watch loop."""

    return (
        f"cycle={cycle} opened={status.get('opened_count', 0)} "
        f"requested={status.get('requested_open_count', 0)} "
        f"open_failed={status.get('open_failed_count', 0)} "
        f"search_needed={status.get('search_needed', 0)}"
    )


def _one_cycle(
    root: Path, paths: Mapping[str, Path], bridge_command: Sequence[str], interval: float
) -> dict[str, Any]:
    """Run exactly one bounded daemon cycle for the watch loop."""

    return run_cycle(
        daemon_cycle_command(
            root,
            intake=paths["intake"],
            database=paths["queue"],
            bridge_command=bridge_command,
            ticks=1,
            interval_seconds=interval,
        )
    )


def watch(
    root: Path,
    *,
    paths: Mapping[str, Path],
    bridge_command: Sequence[str],
    interval_seconds: float,
    duration_seconds: float | None,
    search_command: Sequence[str] | None,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Run the host reconciliation loop, refilling from search when needed.

    Each iteration is one bounded daemon cycle. When the queue reports
    ``search_needed`` and a search command is configured, the host performs the
    candidate-approved read-only search, admits the validated results, then
    reconciles again so the freed slots refill.
    """

    if isinstance(interval_seconds, bool):
        raise CliUsageError("interval must be a positive number of seconds")
    if not isinstance(interval_seconds, (int, float)) or interval_seconds <= 0:
        raise CliUsageError("interval must be a positive number of seconds")

    started = clock()
    cycle = 0
    while True:
        cycle += 1
        try:
            status = _one_cycle(root, paths, bridge_command, interval_seconds)
        except CliUsageError as error:
            emit(f"cycle={cycle} stopped: {error}")
            return 2
        emit(format_status_line(cycle, status))

        if int(status.get("search_needed", 0) or 0) > 0 and search_command:
            try:
                admitted = search_and_admit(root, search_command=search_command, paths=paths)
            except CliUsageError as error:
                emit(f"cycle={cycle} search handoff skipped: {error}")
            else:
                emit(
                    f"cycle={cycle} search handoff admitted={admitted.get('admitted_count', 0)} "
                    f"suppressed={admitted.get('suppressed_count', 0)}"
                )
                try:
                    refill = _one_cycle(root, paths, bridge_command, interval_seconds)
                except CliUsageError as error:
                    emit(f"cycle={cycle} refill stopped: {error}")
                else:
                    emit("refill " + format_status_line(cycle, refill))

        elapsed = clock() - started
        if duration_seconds is not None and elapsed >= duration_seconds:
            emit("watch window elapsed; stopping cleanly")
            return 0
        if duration_seconds is None:
            delay = interval_seconds
        else:
            delay = min(interval_seconds, max(0.0, duration_seconds - elapsed))
        sleep(delay)


def outcome_recorded(root: Path, *, paths: Mapping[str, Path], job_id: str, outcome: str) -> dict[str, Any]:
    """Record one explicit candidate-confirmed outcome through the existing path."""

    if outcome not in SUPPORTED_OUTCOMES:
        raise CliUsageError(f"outcome must be one of {', '.join(SUPPORTED_OUTCOMES)}")
    if not isinstance(job_id, str) or not job_id:
        raise CliUsageError("a managed queue job id is required")
    script = root / "jobapply_agent" / "scripts" / "record_candidate_outcome.py"
    if not script.is_file():
        raise CliUsageError("the outcome recorder is unavailable in this checkout")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--queue-db", str(paths["queue"]),
                "--memory-db", str(paths["memory"]),
                "--job-id", job_id,
                "--outcome", outcome,
                "--vacated",
            ],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CliUsageError("recording the outcome timed out") from error
    if completed.returncode != 0:
        raise CliUsageError("the outcome was not recorded; confirmation was rejected")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1]) if completed.stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the host Smart Queue tool."""

    parser = argparse.ArgumentParser(
        prog="jobapply_queue.py",
        description=(
            "Host-side Smart Queue CLI: inspect state, set capacity, run cycles, "
            "hand off candidate-approved searches, and record candidate-confirmed outcomes."
        ),
    )
    parser.add_argument("--repo", default=None, help="repository root (defaults to the current directory)")
    parser.add_argument("--queue-db", default=None, help="private queue database path")
    parser.add_argument("--memory-db", default=None, help="private candidate-memory database path")
    parser.add_argument("--intake", default=None, help="active candidate intake path")
    parser.add_argument("--bridge-command", default=None, help="bounded bridge argv prefix, space separated")
    parser.add_argument("--node", default="node", help="node executable used by the default bridge")

    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="check prerequisites without mutating anything")
    doctor.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="print queue counts, capacity, and open listing tabs")
    status.add_argument("--json", action="store_true")

    tabs = commands.add_parser("tabs", help="set the managed listing tab capacity (1-10)")
    tabs.add_argument("capacity", type=int)

    open_command = commands.add_parser("open", help="run bounded reconciliation cycles")
    open_command.add_argument("--ticks", type=int, default=1)
    open_command.add_argument("--interval-seconds", type=float, default=60.0)

    admit = commands.add_parser("admit", help="admit a validated discovery export")
    admit.add_argument("export", help="path to an admission-format JSONL export")

    search = commands.add_parser("search", help="run the candidate-approved read-only search handoff")
    search.add_argument("--search-command", required=True, help="argv prefix printing export JSONL on stdout")
    search.add_argument("--admit", action="store_true", help="admit the produced export immediately")

    watch_command = commands.add_parser("watch", help="run the persistent host reconciliation loop")
    watch_command.add_argument("--interval-seconds", type=float, default=60.0)
    watch_command.add_argument("--duration", default=None, help="stop after e.g. 30m; omit for Ctrl+C")
    watch_command.add_argument("--search-command", default=None, help="argv prefix to refill empty slots")
    watch_command.add_argument("--tabs", type=int, default=None, help="set capacity before watching")

    outcome = commands.add_parser("outcome", help="record a candidate-confirmed outcome")
    outcome.add_argument("job_id")
    outcome.add_argument("--outcome", required=True, choices=list(SUPPORTED_OUTCOMES))

    log = commands.add_parser("log", help="tail the watch status log")
    log.add_argument("--lines", type=int, default=20)

    return parser


def _bridge_override(arguments: argparse.Namespace) -> list[str] | None:
    if not arguments.bridge_command:
        return None
    parts = arguments.bridge_command.split()
    if not parts or any(not part for part in parts):
        raise CliUsageError("--bridge-command must be a non-empty argv prefix")
    return parts


def _resolved_paths(arguments: argparse.Namespace) -> tuple[Path, dict[str, Path]]:
    root = resolve_repository_root(arguments.repo)
    paths = private_paths(root)
    if arguments.queue_db:
        paths["queue"] = Path(arguments.queue_db).expanduser()
    if arguments.memory_db:
        paths["memory"] = Path(arguments.memory_db).expanduser()
    if arguments.intake:
        paths["intake"] = Path(arguments.intake).expanduser()
    return root, paths


def emit_json(payload: Any) -> None:
    """Print one deterministic JSON document."""

    print(json.dumps(payload, indent=2, sort_keys=True))


def doctor_report(
    root: Path, paths: Mapping[str, Path], bridge_command: Sequence[str]
) -> dict[str, Any]:
    """Report prerequisite availability without mutating any state.

    Browser-session health is checked only through the bounded bridge's
    permitted ``list-tabs`` operation, so this tool gains no extra authority.
    """

    checks: dict[str, Any] = {
        "repository_root": str(root),
        "queue_database": paths["queue"].is_file(),
        "candidate_memory": paths["memory"].is_file(),
        "candidate_intake": paths["intake"].is_file(),
        "bounded_bridge": paths["bridge"].is_file(),
        "daemon_present": (
            root / "skills" / "easy-apply-tab-monitor" / "scripts" / "smart_queue_daemon.py"
        ).is_file(),
        "admission_entry_point": (root / "jobapply_agent" / "scripts" / "discover.py").is_file(),
        "outcome_recorder": (root / "jobapply_agent" / "scripts" / "record_candidate_outcome.py").is_file(),
        "python": sys.version.split()[0],
    }
    try:
        checks["listing_tabs_open"] = count_listing_tabs(listing_tab_urls(bridge_command))
        checks["existing_session"] = True
        checks["session_detail"] = None
    except CliUsageError as error:
        checks["existing_session"] = False
        checks["session_detail"] = str(error)
        checks["listing_tabs_open"] = None
    required = (
        "queue_database",
        "bounded_bridge",
        "daemon_present",
        "admission_entry_point",
        "outcome_recorder",
        "existing_session",
    )
    checks["ready"] = all(bool(checks[key]) for key in required)
    return checks


def dispatch(arguments: argparse.Namespace) -> int:
    """Execute one parsed command and return its process exit status."""

    root, paths = _resolved_paths(arguments)
    bridge_command = _bridge_override(arguments) or default_bridge_command(root, arguments.node)

    if arguments.command == "doctor":
        report = doctor_report(root, paths, bridge_command)
        if arguments.json:
            emit_json(report)
        else:
            for key in sorted(report):
                print(f"{key}: {report[key]}")
        return 0 if report["ready"] else 1

    if arguments.command == "search":
        command = arguments.search_command.split()
        if arguments.admit:
            emit_json(search_and_admit(root, search_command=command, paths=paths))
            return 0
        text = run_search_command(command)
        export = write_export(paths["private"] / "search_handoff_export.jsonl", text)
        print(f"wrote {len(text.strip().splitlines())} listing rows to {export}")
        return 0

    if arguments.command == "admit":
        emit_json(
            admit_export(
                root,
                export=Path(arguments.export).expanduser(),
                intake=paths["intake"],
                queue_db=paths["queue"],
                memory_db=paths["memory"],
            )
        )
        return 0

    if arguments.command == "watch":
        if arguments.tabs is not None:
            queue_class, _, _, _ = _load_queue_module(root)
            queue_class(paths["queue"]).set_target_size(require_capacity(arguments.tabs), actor="user")
        duration = parse_duration(arguments.duration) if arguments.duration else None
        if duration is not None and duration <= 0:
            raise CliUsageError("--duration must be greater than zero")
        search_command = arguments.search_command.split() if arguments.search_command else None
        return watch(
            root,
            paths=paths,
            bridge_command=bridge_command,
            interval_seconds=arguments.interval_seconds,
            duration_seconds=duration,
            search_command=search_command,
        )

    if arguments.command == "log":
        log_path = paths["status_log"]
        if not log_path.is_file():
            raise CliUsageError(f"no status log yet at {log_path}")
        for line in log_path.read_text(encoding="utf-8").splitlines()[-max(0, arguments.lines):]:
            print(line)
        return 0

    if arguments.command == "outcome":
        emit_json(outcome_recorded(root, paths=paths, job_id=arguments.job_id, outcome=arguments.outcome))
        return 0

    queue_class, _, _, _ = _load_queue_module(root)
    queue = queue_class(paths["queue"])

    if arguments.command == "tabs":
        capacity = require_capacity(arguments.capacity)
        previous = queue.set_target_size(capacity, actor="user")
        print(f"capacity {previous} -> {capacity}")
        return 0

    if arguments.command == "open":
        emit_json(
            run_cycle(
                daemon_cycle_command(
                    root,
                    intake=paths["intake"],
                    database=paths["queue"],
                    bridge_command=bridge_command,
                    ticks=arguments.ticks,
                    interval_seconds=arguments.interval_seconds,
                )
            )
        )
        return 0

    if arguments.command == "status":
        payload = status_payload(queue, bridge_command)
        if arguments.json:
            emit_json(payload)
        else:
            print(f"queue_id: {payload['queue_id']}")
            print(f"capacity: {payload['capacity']} ({payload['capacity_provenance']})")
            print(f"jobs_total: {payload['jobs_total']}")
            print(f"listing_tabs_open: {payload['listing_tabs_open']}")
            print(f"confirmed_submitted: {payload['confirmed_submitted']}")
            for state, count in payload["states"].items():
                print(f"  {state}: {count}")
        return 0

    raise CliUsageError(f"unknown command: {arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, dispatch, and translate errors into exit codes."""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return dispatch(arguments)
    except CliUsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
