#!/usr/bin/env python3
"""Persistent, review-only Chrome tab watcher for a five-job manifest.

The watcher uses macOS Chrome Apple Events only to list tab URLs and open a
missing *listing* URL. It never clicks a page, fills a field, uploads a file,
or submits an application.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Iterable, Sequence
from urllib.parse import urlsplit


ALLOWED_ROOTS = ("linkedin.com", "indeed.com")
MAX_JOBS = 5

_LIST_TAB_URLS = """
set allUrls to {}
tell application "Google Chrome"
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
    tell application "Google Chrome"
        if (count of windows) = 0 then make new window
        tell front window to make new tab at end of tabs with properties {URL:targetUrl}
        activate
    end tell
end run
"""


class ChromeAutomationError(RuntimeError):
    """Chrome rejected an Apple Event or the automation permission is absent."""


@dataclass(frozen=True)
class JobTarget:
    listing_url: str
    active_url_prefixes: tuple[str, ...] = ()

    def is_active(self, open_url: str) -> bool:
        return open_url == self.listing_url or any(open_url.startswith(prefix) for prefix in self.active_url_prefixes)


@dataclass(frozen=True)
class WatchCycle:
    observed_open_tabs: int
    missing_before_reopen: tuple[str, ...]
    reopened: tuple[str, ...]
    failed: tuple[str, ...]


def _is_allowed_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    return parsed.scheme == "https" and any(host == root or host.endswith("." + root) for root in ALLOWED_ROOTS)


def load_targets(path: Path) -> tuple[JobTarget, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_JOBS:
        raise ValueError(f"manifest must contain between one and {MAX_JOBS} jobs")
    targets: list[JobTarget] = []
    seen: set[str] = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("each manifest job must be an object")
        listing_url = job.get("url")
        if not _is_allowed_url(listing_url):
            raise ValueError("each manifest URL must be HTTPS LinkedIn or Indeed")
        if listing_url in seen:
            raise ValueError("manifest URLs must be unique")
        raw_prefixes = job.get("active_url_prefixes", [])
        if not isinstance(raw_prefixes, list) or not all(_is_allowed_url(prefix) for prefix in raw_prefixes):
            raise ValueError("active_url_prefixes must contain HTTPS LinkedIn or Indeed URLs")
        seen.add(listing_url)
        targets.append(JobTarget(listing_url=listing_url, active_url_prefixes=tuple(raw_prefixes)))
    return tuple(targets)


def _run_osascript(script: str, args: Sequence[str], runner: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    completed = runner(
        ["osascript", "-e", script, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode != 0:
        raise ChromeAutomationError("Chrome tab automation is unavailable")
    return completed.stdout


class ChromeAppleScript:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> None:
        self._runner = runner

    def list_tab_urls(self) -> tuple[str, ...]:
        output = _run_osascript(_LIST_TAB_URLS, (), self._runner)
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    def open_listing(self, url: str) -> None:
        if not _is_allowed_url(url):
            raise ValueError("refusing to open a non-LinkedIn/Indeed URL")
        _run_osascript(_OPEN_LISTING, (url,), self._runner)


def reconcile(chrome: ChromeAppleScript, targets: Iterable[JobTarget]) -> WatchCycle:
    open_urls = chrome.list_tab_urls()
    missing = tuple(target.listing_url for target in targets if not any(target.is_active(url) for url in open_urls))
    reopened: list[str] = []
    failed: list[str] = []
    for url in missing:
        try:
            chrome.open_listing(url)
        except ChromeAutomationError:
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
    chrome: ChromeAppleScript,
    targets: Iterable[JobTarget],
    *,
    interval_seconds: float,
    max_cycles: int | None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
) -> None:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    if max_cycles is not None and max_cycles <= 0:
        raise ValueError("max_cycles must be positive when set")
    cycle = 0
    while max_cycles is None or cycle < max_cycles:
        emit(json.dumps(asdict(reconcile(chrome, targets)), sort_keys=True))
        cycle += 1
        if max_cycles is None or cycle < max_cycles:
            sleep(interval_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reopen only missing LinkedIn/Indeed listing tabs from a local manifest.")
    parser.add_argument("--manifest", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--max-cycles", type=int)
    args = parser.parse_args(argv)
    try:
        targets = load_targets(args.manifest)
        chrome = ChromeAppleScript()
        if args.once:
            print(json.dumps(asdict(reconcile(chrome, targets)), sort_keys=True))
        else:
            run_watch(
                chrome,
                targets,
                interval_seconds=args.interval_seconds,
                max_cycles=args.max_cycles,
            )
    except (ChromeAutomationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
