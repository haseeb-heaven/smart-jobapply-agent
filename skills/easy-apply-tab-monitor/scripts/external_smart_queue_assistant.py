#!/usr/bin/env python3
"""Finite, redacted host loop for externally supplied public job facts.

This is deliberately a host-side orchestration boundary.  It runs the existing
queue daemon with one tick, calls a provider only with public generic search
terms, then delegates eligibility, scoring, revision validation, suppression,
and queue admission to ``discover.py``.  It has no browser, form, or outcome
authority of its own.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
PRIVATE_ROOT = PROJECT_ROOT / "jobapply_agent" / "private"
DAEMON = SCRIPT_DIR / "smart_queue_daemon.py"
DISCOVER = PROJECT_ROOT / "jobapply_agent" / "scripts" / "discover.py"
SEARCH_PROFILES = PROJECT_ROOT / "jobapply_agent" / "config" / "search_profiles.yaml"
MAX_PROVIDER_BYTES = 256 * 1024
MAX_LISTINGS = 20
_DAEMON_KEYS = frozenset({"ticks_completed", "requested_open_count", "opened_count", "open_failed_count", "search_needed", "degraded_tick_count"})
_ADMISSION_KEYS = frozenset({"validated_count", "suppressed_count", "admitted_count"})
_SHUTDOWN = False


class HostFailure(RuntimeError):
    """A redacted expected host failure."""


def _on_shutdown(_signal: int, _frame: object) -> None:
    global _SHUTDOWN
    _SHUTDOWN = True


def _emit(**payload: object) -> None:
    """Emit only bounded aggregate values; never let subprocess output escape."""

    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


def _command(value: Sequence[str] | None, name: str) -> tuple[str, ...]:
    if not value or not all(isinstance(part, str) and part for part in value):
        raise HostFailure(f"invalid_{name}")
    return tuple(value)


def _positive(value: object, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise HostFailure(f"invalid_{name}")
    return float(value)


def _strict_object(raw: str, *, keys: frozenset[str]) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HostFailure("invalid_child_status") from exc
    if not isinstance(value, dict) or set(value) != keys:
        raise HostFailure("invalid_child_status")
    if any(type(item) is not int or item < 0 for item in value.values()):
        raise HostFailure("invalid_child_status")
    return value


def _run(command: Sequence[str], *, stdin: bytes | None, timeout: float, maximum_output: int) -> str:
    """Bound all pipe IO, including input, and reap the isolated child group."""

    deadline = time.monotonic() + timeout
    if stdin is not None and len(stdin) > MAX_PROVIDER_BYTES:
        raise HostFailure("child_input_oversize")
    process = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            list(command), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False, start_new_session=True,
        )
        for stream in (process.stdout, process.stderr):
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        pending = memoryview(stdin or b"")
        if process.stdin is not None:
            if pending:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE)
            else:
                process.stdin.close()
        chunks: list[bytes] = []
        totals = {process.stdout: 0, process.stderr: 0}
        while selector.get_map() or process.poll() is None:
            if _SHUTDOWN:
                raise HostFailure("shutdown")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HostFailure("child_timeout")
            for key, events in selector.select(min(0.05, remaining)):
                stream = key.fileobj
                if events & selectors.EVENT_WRITE:
                    try:
                        pending = pending[os.write(stream.fileno(), pending[:8192]):]
                    except BrokenPipeError:
                        pending = pending[len(pending):]
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                else:
                    piece = os.read(stream.fileno(), 8192)
                    if not piece:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    totals[stream] += len(piece)
                    if totals[stream] > maximum_output:
                        raise HostFailure("child_oversize")
                    if stream is process.stdout:
                        chunks.append(piece)
        if process.returncode != 0:
            raise HostFailure("child_failed")
        return b"".join(chunks).decode("utf-8", errors="strict")
    except (HostFailure, UnicodeDecodeError, OSError) as exc:
        if isinstance(exc, HostFailure):
            raise
        raise HostFailure("child_failed") from None
    finally:
        selector.close()
        if process is not None:
            # Give a supervised provider time to stop its own worker session.
            # Kill the group even if its leader exits during that grace period.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for stream in (process.stdout, process.stderr, process.stdin):
                if stream is not None:
                    stream.close()


def _private_file(path: Path, *, existing: bool) -> Path:
    """Require regular private runtime paths without resolving through symlinks."""

    try:
        root = Path(os.path.abspath(PRIVATE_ROOT))
        resolved = Path(os.path.abspath(path))
        relative = resolved.relative_to(root)
        os.lstat(root)
        if os.path.islink(root) or not os.path.isdir(root):
            raise OSError
        current = root
        parts = relative.parts
        for index, part in enumerate(parts):
            current = current / part
            is_leaf = index == len(parts) - 1
            if not current.exists():
                if is_leaf and not existing:
                    break
                raise OSError
            os.lstat(current)
            if os.path.islink(current):
                raise OSError
            if is_leaf:
                if existing and not os.path.isfile(current):
                    raise OSError
            elif not os.path.isdir(current):
                raise OSError
        if existing:
            os.lstat(resolved)
        return resolved
    except (OSError, ValueError):
        raise HostFailure("invalid_private_runtime") from None


def _acquire_lock(queue_db: Path) -> int:
    """Hold a POSIX host lock whose ownership the OS releases after a crash.

    Keep the lock inode in place: unlinking it would let simultaneous callers
    acquire different inodes for the same queue. No platform API enters core.
    """
    import fcntl

    lock = queue_db.with_name(f".{queue_db.name}.external-assistant.lock")
    fd = None
    try:
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        raise HostFailure("busy") from None
    except OSError:
        if fd is not None:
            os.close(fd)
        raise HostFailure("invalid_private_runtime") from None
    return fd


def _queries(public_location: str) -> tuple[list[dict[str, str]], dict[str, tuple[str, str]]]:
    """Read committed public generic search profiles; never inspect intake data."""

    try:
        package_source = str(PROJECT_ROOT / "jobapply_agent" / "src")
        if package_source not in sys.path:
            sys.path.insert(0, package_source)
        from jobapply_agent.sources import load_search_profiles
        profiles = load_search_profiles(SEARCH_PROFILES)
    except Exception:
        raise HostFailure("search_profiles_invalid") from None
    criteria: list[dict[str, str]] = []
    urls: dict[str, tuple[str, str]] = {}
    for index, profile in enumerate(profiles):
        query_id = f"q{index}"
        criteria.append({"query_id": query_id, "platform": profile.platform, "keywords": profile.keywords, "location": profile.location or public_location})
        urls[query_id] = (profile.search_url, profile.platform)
    return criteria, urls


def _provider_listings(raw: str, query_urls: Mapping[str, tuple[str, str]], managed_urls: set[str]) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Validate provider raw public facts and bind them to host-owned profiles."""

    try:
        batch = json.loads(raw)
    except (ValueError, RecursionError):
        raise HostFailure("provider_schema_invalid") from None
    if not isinstance(batch, dict) or set(batch) != {"schema_version", "listings"} or type(batch.get("schema_version")) is not int or batch.get("schema_version") != 1:
        raise HostFailure("provider_schema_invalid")
    listings = batch.get("listings")
    if not isinstance(listings, list) or len(listings) > MAX_LISTINGS:
        raise HostFailure("provider_schema_invalid")
    allowed = {"query_id", "platform", "url", "title", "company", "description", "location", "work_mode", "employment_type", "posted_at", "source_job_id", "discovered_at"}
    required = {"query_id", "platform", "url", "title", "company", "description"}
    limits = {"query_id": 32, "platform": 8, "url": 4096, "title": 512,
              "company": 512, "description": 12000, "location": 512,
              "work_mode": 128, "employment_type": 128, "posted_at": 128,
              "source_job_id": 256, "discovered_at": 128}
    pages: dict[str, list[dict[str, Any]]] = {url: [] for url, _platform in query_urls.values()}
    seen: set[str] = set()
    accepted = 0
    package_source = str(PROJECT_ROOT / "jobapply_agent" / "src")
    if package_source not in sys.path:
        sys.path.insert(0, package_source)
    from jobapply_agent.sources import canonical_listing_url
    for listing in listings:
        if not isinstance(listing, dict) or set(listing) - allowed or not required <= set(listing):
            raise HostFailure("provider_schema_invalid")
        query_id, platform = listing.get("query_id"), listing.get("platform")
        if not isinstance(query_id, str) or not re.fullmatch(r"q[0-9]+", query_id) or query_id not in query_urls or not isinstance(platform, str) or platform not in {"linkedin", "indeed"} or platform != query_urls[query_id][1]:
            raise HostFailure("provider_schema_invalid")
        for field, value in listing.items():
            if field in {"posted_at", "source_job_id", "discovered_at"} and value is None:
                continue
            if not isinstance(value, str) or len(value) > limits[field] or field in {"url", "title"} and not value:
                raise HostFailure("provider_schema_invalid")
        try:
            canonical = canonical_listing_url(listing["url"], platform)
        except ValueError:
            raise HostFailure("provider_schema_invalid") from None
        if canonical in seen:
            raise HostFailure("provider_duplicate")
        seen.add(canonical)
        if canonical in managed_urls:
            continue
        payload = {key: listing.get(key, "") for key in ("title", "company", "description", "location", "work_mode", "employment_type", "posted_at", "source_job_id", "discovered_at")}
        payload["url"] = canonical
        pages[query_urls[query_id][0]].append(payload)
        accepted += 1
    return pages, accepted


def _managed_urls(queue_db: Path) -> set[str]:
    """Read active managed identities only; never mutate queue history."""

    if not queue_db.exists():
        return set()
    try:
        connection = sqlite3.connect(f"file:{queue_db}?mode=ro", uri=True)
        try:
            return {str(row[0]) for row in connection.execute("SELECT source_url FROM smart_queue_jobs")}
        finally:
            connection.close()
    except sqlite3.Error:
        raise HostFailure("queue_read_failed") from None


def _backoff_sleep(seconds: float) -> None:
    """Wait in short intervals so a host shutdown remains prompt."""

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _SHUTDOWN:
            raise HostFailure("shutdown")
        time.sleep(min(0.05, deadline - time.monotonic()))


def _cycle_sleep(seconds: float) -> None:
    """Keep a persistent host cancellable between one-tick daemon cycles."""

    _backoff_sleep(seconds)


def _daemon(command: Sequence[str], intake: Path, queue: Path, bridge: Sequence[str], timeout: float) -> Mapping[str, Any]:
    output = _run([*command, "--candidate-intake", str(intake), "--database", str(queue), "--adapter", "external", "--adapter-command", *bridge, "--max-ticks", "1"], stdin=None, timeout=timeout, maximum_output=8192)
    lines = [line for line in output.splitlines() if line]
    if len(lines) != 1:
        raise HostFailure("invalid_child_status")
    return _strict_object(lines[0], keys=_DAEMON_KEYS)


def _admit(intake: Path, queue: Path, memory: Path, payloads: Mapping[str, list[dict[str, Any]]], timeout: float) -> Mapping[str, Any]:
    with tempfile.TemporaryDirectory(dir=PRIVATE_ROOT, prefix="external-smart-queue-") as directory:
        root = Path(directory)
        visible = root / "visible.json"
        visible.write_text(json.dumps({"pages": payloads}, separators=(",", ":")), encoding="utf-8")
        output_dir = root / "discovery"
        discovery = _run([sys.executable, str(DISCOVER), "--candidate-intake", str(intake), "--visible-payloads", str(visible), "--output-dir", str(output_dir)], stdin=None, timeout=timeout, maximum_output=8192)
        # Discovery's status is untrusted process output; its success alone is insufficient.
        try:
            parsed = json.loads(discovery)
            counts = {"payloads_seen", "new_listings", "duplicate_listings", "recommended_exports",
                      "below_threshold", "malformed_payloads", "application_actions", "minimum_profile_fit_score"}
            strings = {"run_id", "started_at", "finished_at", "profile_revision", "matcher_policy_revision"}
            if (not isinstance(parsed, dict)
                    or set(parsed) != counts | strings | {"search_urls", "errors", "mode", "network_access"}
                    or any(type(parsed[key]) is not int or parsed[key] < 0 for key in counts)
                    or any(not isinstance(parsed[key], str) or not parsed[key] for key in strings)
                    or parsed["mode"] != "discovery_export_only"
                    or parsed["network_access"] != "none_by_scheduler"
                    or parsed["application_actions"] != 0 or parsed["errors"] != []
                    or parsed["malformed_payloads"] != 0
                    or not isinstance(parsed["search_urls"], list)
                    or any(not isinstance(url, str) for url in parsed["search_urls"])):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            raise HostFailure("discovery_failed") from None
        export = output_dir / "recommended_jobs.jsonl"
        if parsed["recommended_exports"] == 0:
            # A clean discovery run creates no export when every listing is
            # ineligible or below threshold. This is a shortage, not a failure.
            if export.exists() and (not export.is_file() or export.stat().st_size):
                raise HostFailure("discovery_failed")
            return {"validated_count": 0, "suppressed_count": 0, "admitted_count": 0}
        if not export.is_file():
            raise HostFailure("discovery_failed")
        admission = _run([sys.executable, str(DISCOVER), "admit-queue", "--candidate-intake", str(intake), "--discovery-export", str(export), "--queue-db", str(queue), "--memory-db", str(memory)], stdin=None, timeout=timeout, maximum_output=8192)
        return _strict_object(admission.strip(), keys=_ADMISSION_KEYS)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one bounded external Smart Queue replenishment host loop.")
    parser.add_argument("--candidate-intake", type=Path, required=True)
    parser.add_argument("--queue-db", type=Path, required=True)
    parser.add_argument("--memory-db", type=Path, required=True)
    parser.add_argument("--bridge-command", nargs="+", required=True)
    parser.add_argument("--provider-command", nargs="+", required=True)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--provider-timeout-seconds", type=float, default=310)
    parser.add_argument("--child-timeout-seconds", type=float, default=15)
    parser.add_argument("--provider-max-output-bytes", type=int, default=MAX_PROVIDER_BYTES)
    parser.add_argument("--backoff-seconds", type=float, default=60)
    parser.add_argument("--interval-seconds", type=float, default=15)
    parser.add_argument("--max-cycles", type=int, help="finite host-cycle bound for tests or a host-controlled run")
    parser.add_argument("--search-location", default="India, United Arab Emirates, or worldwide remote", help="public generic provider criterion; never read from candidate intake")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    global _SHUTDOWN
    _SHUTDOWN = False
    arguments = _parser().parse_args(argv)
    try:
        if (arguments.max_rounds < 1 or arguments.max_rounds > 3
                or arguments.provider_max_output_bytes < 1 or arguments.provider_max_output_bytes > MAX_PROVIDER_BYTES
                or arguments.max_cycles is not None and arguments.max_cycles < 1):
            raise HostFailure("invalid_bounds")
        provider_timeout = _positive(arguments.provider_timeout_seconds, "provider_timeout")
        child_timeout = _positive(arguments.child_timeout_seconds, "child_timeout")
        backoff = _positive(arguments.backoff_seconds, "backoff", allow_zero=True)
        interval = _positive(arguments.interval_seconds, "interval")
        if backoff > 60:
            raise HostFailure("invalid_bounds")
        if not isinstance(arguments.search_location, str) or not arguments.search_location.strip() or len(arguments.search_location) > 256:
            raise HostFailure("invalid_search_location")
        intake = _private_file(arguments.candidate_intake, existing=True)
        queue, memory = _private_file(arguments.queue_db, existing=False), _private_file(arguments.memory_db, existing=False)
        bridge, provider = _command(arguments.bridge_command, "bridge_command"), _command(arguments.provider_command, "provider_command")
        lock = _acquire_lock(queue)
    except HostFailure as exc:
        _emit(state="busy" if str(exc) == "busy" else "failed", reason=str(exc))
        return 2
    previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
    for value in previous:
        signal.signal(value, _on_shutdown)
    try:
        queries, query_urls = _queries(arguments.search_location.strip())
        cycle = 0
        while True:
            if _SHUTDOWN:
                raise HostFailure("shutdown")
            cycle += 1
            initial = _daemon((sys.executable, str(DAEMON)), intake, queue, bridge, child_timeout)
            final: Mapping[str, Any] = initial
            provider_count = admitted_count = suppressed_count = 0
            state = "cycle"
            if initial["search_needed"]:
                for round_number in range(1, arguments.max_rounds + 1):
                    criteria = json.dumps({"schema_version": 1, "limit": MAX_LISTINGS, "queries": queries}, separators=(",", ":")).encode("utf-8")
                    managed_urls = _managed_urls(queue)
                    try:
                        raw = _run(provider, stdin=criteria, timeout=provider_timeout, maximum_output=arguments.provider_max_output_bytes)
                        pages, provider_count = _provider_listings(raw, query_urls, managed_urls)
                    except HostFailure as exc:
                        if str(exc) not in {"child_timeout", "child_failed", "child_oversize",
                                            "provider_schema_invalid", "provider_duplicate"}:
                            raise
                        # Reject the entire batch before admission. Retain this
                        # host's lock and reconcile again before the next search.
                        state = "degraded"
                        break
                    if provider_count:
                        admitted = _admit(intake, queue, memory, pages, child_timeout)
                        admitted_count, suppressed_count = admitted["admitted_count"], admitted["suppressed_count"]
                        if admitted_count:
                            # This immediate tick is the only action that can open a newly admitted URL.
                            final = _daemon((sys.executable, str(DAEMON)), intake, queue, bridge, child_timeout)
                            break
                    if round_number < arguments.max_rounds and backoff:
                        _backoff_sleep(backoff)
                else:
                    state = "no_progress"
            _emit(state=state, cycle=cycle, rounds_attempted=(round_number if initial["search_needed"] else 0), provider_listing_count=provider_count, admitted_count=admitted_count, suppressed_count=suppressed_count, opened_count=final["opened_count"], search_needed=final["search_needed"])
            if arguments.max_cycles is not None and cycle >= arguments.max_cycles:
                _emit(state="complete", cycle=cycle, rounds_attempted=0, provider_listing_count=0, admitted_count=0, suppressed_count=0, opened_count=0, search_needed=final["search_needed"])
                return 0
            _cycle_sleep((backoff or min(interval, 60)) if state in {"no_progress", "degraded"} else interval)
    except HostFailure as exc:
        _emit(state="shutdown" if str(exc) == "shutdown" else "failed", reason=str(exc))
        return 2
    finally:
        for value, handler in previous.items():
            signal.signal(value, handler)
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
