#!/usr/bin/env python3
"""Persistent, review-only browser tab watcher for a five-job manifest.

The default compatibility adapter uses macOS Chrome Apple Events.  An explicit
external-command adapter permits any browser and operating system while keeping
the same listing-only authority.  The watcher never clicks a page, fills a
field, uploads a file, or submits an application.

In ``--watch`` mode, transient per-cycle adapter failures emit a redacted JSON
failure line and monitoring continues, exiting with an error status after three
consecutive failed cycles.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, Iterable, Sequence
from urllib.parse import parse_qsl, SplitResult, urlsplit


try:
    from browser_tab_adapter import (
        BrowserAdapterError,
        BrowserTabAdapter,
        canonical_listing_url,
        create_adapter,
    )
except ModuleNotFoundError:
    _ADAPTER_PATH = Path(__file__).with_name("browser_tab_adapter.py")
    _ADAPTER_SPEC = importlib.util.spec_from_file_location("browser_tab_adapter", _ADAPTER_PATH)
    if _ADAPTER_SPEC is None or _ADAPTER_SPEC.loader is None:
        raise
    _ADAPTER_MODULE = importlib.util.module_from_spec(_ADAPTER_SPEC)
    sys.modules[_ADAPTER_SPEC.name] = _ADAPTER_MODULE
    _ADAPTER_SPEC.loader.exec_module(_ADAPTER_MODULE)
    BrowserAdapterError = _ADAPTER_MODULE.BrowserAdapterError
    BrowserTabAdapter = _ADAPTER_MODULE.BrowserTabAdapter
    canonical_listing_url = _ADAPTER_MODULE.canonical_listing_url
    create_adapter = _ADAPTER_MODULE.create_adapter


ALLOWED_ROOTS = ("linkedin.com", "indeed.com")
MAX_JOBS = 5
_LINKEDIN_LISTING_PATH = re.compile(r"^/jobs/view/(?P<job_id>[A-Za-z0-9_-]+)/?$")
_LINKEDIN_JOB_ROUTE = re.compile(r"^/jobs/view/(?P<job_id>[A-Za-z0-9_-]+)(?:/.*)?$")
_INDEED_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_TRACKING_QUERY_KEYS = frozenset(
    {"campaign", "from", "mcid", "ref", "source", "trackingid", "trk"}
)

_LIST_TAB_URLS = """
set allUrls to {}
if application "Google Chrome" is not running then error "Chrome is not running"
tell application "Google Chrome"
    if (count of windows) = 0 then error "Chrome has no existing window"
    repeat with aWindow in windows
        repeat with aTab in tabs of aWindow
            set end of allUrls to (URL of aTab)
        end repeat
    end repeat
end tell
set AppleScript's text item delimiters to linefeed
return allUrls as text
"""

_OPEN_LISTING = """
on run argv
    set targetUrl to item 1 of argv
    if application "Google Chrome" is not running then error "Chrome is not running"
    tell application "Google Chrome"
        if (count of windows) = 0 then error "Chrome has no existing window"
        tell window 1 to make new tab at end of tabs with properties {URL:targetUrl}
    end tell
end run
"""


class ChromeAutomationError(BrowserAdapterError):
    """Chrome rejected an Apple Event or the automation permission is absent."""


@dataclass(frozen=True)
class JobTarget:
    listing_url: str
    active_url_prefixes: tuple[str, ...] = ()

    def is_active(self, open_url: str) -> bool:
        if _listing_identity(open_url) == _listing_identity(self.listing_url):
            return True
        return any(_matches_prefix(open_url, prefix) for prefix in self.active_url_prefixes)


@dataclass(frozen=True)
class WatchCycle:
    observed_open_tabs: int
    missing_before_reopen: tuple[str, ...]
    reopened: tuple[str, ...]
    failed: tuple[str, ...]


def status_counts(cycle: WatchCycle) -> dict[str, int]:
    """Return a URL-free status payload for CLI and persistent-watch output."""
    return {
        "failed": len(cycle.failed),
        "missing_before_reopen": len(cycle.missing_before_reopen),
        "observed_open_tabs": cycle.observed_open_tabs,
        "reopened": len(cycle.reopened),
    }


def _board_url(value: object) -> tuple[SplitResult, str] | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        return None
    for root in ALLOWED_ROOTS:
        if host == root or host.endswith("." + root):
            return parsed, root.removesuffix(".com")
    return None


def _is_safe_tracking_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized in _SAFE_TRACKING_QUERY_KEYS or normalized.startswith("utm_")


def _indeed_job_id(parsed: SplitResult) -> str | None:
    if parsed.path.rstrip("/") != "/viewjob":
        return None
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.query and not query:
        return None
    if any(not key or (key.casefold() != "jk" and not _is_safe_tracking_key(key)) for key, _value in query):
        return None
    job_ids = [value for key, value in query if key.casefold() == "jk"]
    if len(job_ids) != 1 or not _INDEED_JOB_ID.fullmatch(job_ids[0]):
        return None
    return job_ids[0]


def _listing_identity(value: object) -> tuple[str, str] | None:
    board_url = _board_url(value)
    if board_url is None:
        return None
    parsed, board = board_url
    if board == "linkedin":
        match = _LINKEDIN_LISTING_PATH.fullmatch(parsed.path)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if parsed.query and not query:
            return None
        query_is_safe = all(key and _is_safe_tracking_key(key) for key, _value in query)
        return (board, match.group("job_id")) if match and query_is_safe else None
    job_id = _indeed_job_id(parsed)
    return (board, job_id) if job_id is not None else None


def _active_prefix_is_safe(listing_url: str, prefix: object) -> bool:
    listing_identity = _listing_identity(listing_url)
    prefix_board_url = _board_url(prefix)
    if listing_identity is None or prefix_board_url is None:
        return False
    listing_board, listing_job_id = listing_identity
    parsed, prefix_board = prefix_board_url
    if prefix_board != listing_board:
        return False
    if listing_board == "linkedin":
        match = _LINKEDIN_JOB_ROUTE.fullmatch(parsed.path)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        return bool(
            match
            and match.group("job_id") == listing_job_id
            and (not parsed.query or query)
            and all(key and _is_safe_tracking_key(key) for key, _value in query)
        )
    prefix_job_id = _indeed_job_id(parsed)
    if prefix_job_id is not None:
        return prefix_job_id == listing_job_id
    query = parse_qsl(parsed.query, keep_blank_values=True)
    return (
        (parsed.hostname or "").casefold().rstrip(".") == "smartapply.indeed.com"
        and parsed.path.startswith("/beta/indeedapply/")
        and (not parsed.query or query)
        and sum(key.casefold() == "jk" and value == listing_job_id for key, value in query) == 1
        and all(key and (key.casefold() == "jk" or _is_safe_tracking_key(key)) for key, _value in query)
    )


def _matches_prefix(open_url: str, prefix: str) -> bool:
    if open_url == prefix:
        return True
    if not open_url.startswith(prefix):
        return False
    if prefix.endswith(("/", "?", "&", "#", "=")):
        return True
    return open_url[len(prefix) : len(prefix) + 1] in {"/", "?", "&", "#"}


def load_targets(path: Path) -> tuple[JobTarget, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_JOBS:
        raise ValueError(f"manifest must contain between one and {MAX_JOBS} jobs")
    targets: list[JobTarget] = []
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("each manifest job must be an object")
        listing_url = job.get("url")
        try:
            canonical_url = canonical_listing_url(listing_url)
        except ValueError:
            raise ValueError("each manifest URL must be a canonical HTTPS LinkedIn or Indeed job listing")
        listing_identity = _listing_identity(canonical_url)
        if listing_identity in seen:
            raise ValueError("manifest URLs must be unique")
        raw_prefixes = job.get("active_url_prefixes", [])
        if not isinstance(raw_prefixes, list) or not all(
            _active_prefix_is_safe(canonical_url, prefix) for prefix in raw_prefixes
        ):
            raise ValueError("active_url_prefixes must be narrow same-board routes for the target job")
        assert listing_identity is not None
        seen.add(listing_identity)
        targets.append(JobTarget(listing_url=canonical_url, active_url_prefixes=tuple(raw_prefixes)))
    return tuple(targets)


def _run_osascript(script: str, args: Sequence[str], runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    try:
        completed = runner(
            ["osascript", "-e", script, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        raise ChromeAutomationError("Chrome tab automation timed out") from None
    if completed.returncode != 0:
        raise ChromeAutomationError("Chrome tab automation is unavailable")
    return completed.stdout


class ChromeAppleScript:
    # This optional adapter is for the legacy fixed-round watcher only. Smart
    # Queue execution receives a host-supplied, browser-neutral two-operation
    # adapter and never selects this compatibility path. The optional Codex
    # Chrome host is one reference integration. The legacy scripts refuse to
    # create a Chrome window, but Apple Events cannot provide the live queue's
    # required atomic session boundary.
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self._runner = runner

    def list_tab_urls(self) -> tuple[str, ...]:
        output = _run_osascript(_LIST_TAB_URLS, (), self._runner)
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    def open_listing(self, url: str) -> None:
        try:
            canonical_url = canonical_listing_url(url)
        except ValueError:
            raise ValueError("refusing to open a non-listing LinkedIn/Indeed URL")
        _run_osascript(_OPEN_LISTING, (canonical_url,), self._runner)


def _validated_targets(targets: Iterable[JobTarget]) -> tuple[JobTarget, ...]:
    try:
        targets = tuple(targets)
    except TypeError:
        raise ValueError("targets must be an iterable of at most five job targets") from None
    if not 1 <= len(targets) <= MAX_JOBS:
        raise ValueError(f"targets must contain between one and {MAX_JOBS} jobs")
    identities: set[tuple[str, str]] = set()
    for target in targets:
        if not isinstance(target, JobTarget):
            raise ValueError("targets must contain only JobTarget values")
        try:
            canonical = canonical_listing_url(target.listing_url)
        except ValueError:
            raise ValueError("targets must contain canonical HTTPS LinkedIn or Indeed listings") from None
        if canonical != target.listing_url:
            raise ValueError("targets must contain canonical listing URLs")
        identity = _listing_identity(canonical)
        assert identity is not None
        if identity in identities:
            raise ValueError("targets must contain unique listing identities")
        identities.add(identity)
    return targets


def reconcile(browser: BrowserTabAdapter, targets: Iterable[JobTarget]) -> WatchCycle:
    targets = _validated_targets(targets)
    open_urls = browser.list_tab_urls()
    missing = tuple(target.listing_url for target in targets if not any(target.is_active(url) for url in open_urls))
    reopened: list[str] = []
    failed: list[str] = []
    for url in missing:
        try:
            browser.open_listing(url)
        except BrowserAdapterError:
            failed.append(url)
        else:
            reopened.append(url)
    return WatchCycle(
        observed_open_tabs=len(open_urls),
        missing_before_reopen=missing,
        reopened=tuple(reopened),
        failed=tuple(failed),
    )


def run_watch(
    browser: BrowserTabAdapter,
    targets: Iterable[JobTarget],
    *,
    interval_seconds: float,
    max_cycles: int | None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> None:
    try:
        targets = tuple(targets)
    except TypeError:
        raise ValueError("targets must be an iterable of at most five job targets") from None
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if max_cycles is not None and max_cycles <= 0:
        raise ValueError("max_cycles must be positive when set")
    targets = _validated_targets(targets)
    cycle = 0
    consecutive_failures = 0
    while max_cycles is None or cycle < max_cycles:
        try:
            cycle_result = reconcile(browser, targets)
        except (BrowserAdapterError, OSError) as error:
            consecutive_failures += 1
            emit(json.dumps({"ok": False, "error": type(error).__name__}, sort_keys=True))
            if consecutive_failures >= 3:
                raise
        else:
            consecutive_failures = 0
            emit(json.dumps(status_counts(cycle_result), sort_keys=True))
        cycle += 1
        if max_cycles is None or cycle < max_cycles:
            sleep(interval_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reopen only missing LinkedIn/Indeed listing tabs from a local manifest."
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to the local five-job monitor manifest.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Reconcile one cycle and exit.")
    mode.add_argument(
        "--watch",
        action="store_true",
        help="Keep monitoring and reopening missing tabs on an interval.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=60.0,
        help="Seconds between watch cycles (default: %(default)s).",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        help="Stop after this many watch cycles; unlimited when omitted.",
    )
    parser.add_argument(
        "--adapter",
        choices=("chrome-applescript", "external"),
        default="chrome-applescript",
        help="Explicit tab adapter; external supports a caller-provided browser bridge.",
    )
    parser.add_argument(
        "--adapter-command",
        nargs=argparse.REMAINDER,
        help=(
            "External adapter argv prefix. This option must be last; every remaining token, "
            "including option-prefixed bridge arguments, is passed as argv without a shell."
        ),
    )
    parser.add_argument(
        "--adapter-timeout-seconds",
        type=float,
        default=15.0,
        help="Adapter command timeout in seconds (default: %(default)s).",
    )
    args = parser.parse_args(argv)
    try:
        targets = load_targets(args.manifest)
        if args.adapter == "chrome-applescript":
            if args.adapter_command is not None:
                raise ValueError("chrome-applescript adapter does not accept --adapter-command")
            browser: BrowserTabAdapter = ChromeAppleScript()
        else:
            browser = create_adapter(
                "external",
                command=args.adapter_command,
                timeout_seconds=args.adapter_timeout_seconds,
            )
        if args.once:
            print(json.dumps(status_counts(reconcile(browser, targets)), sort_keys=True))
        else:
            run_watch(
                browser,
                targets,
                interval_seconds=args.interval_seconds,
                max_cycles=args.max_cycles,
            )
    except (BrowserAdapterError, OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
