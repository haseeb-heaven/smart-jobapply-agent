#!/usr/bin/env python3
"""Browser-neutral, listing-only adapter contract for the bounded tab monitor.

The external adapter deliberately speaks a tiny subprocess protocol instead of
owning a browser automation library.  A caller may supply any bridge for its
chosen browser, operating system, and agent runtime, provided the bridge only
lists tab URLs and opens an exact approved listing URL.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Protocol, Sequence, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_TIMEOUT_SECONDS = 15.0
_LINKEDIN_LISTING_PATH = re.compile(r"^/jobs/view/[A-Za-z0-9_-]+/?$")
_INDEED_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_TRACKING_QUERY_KEYS = frozenset(
    {"campaign", "from", "mcid", "ref", "source", "trackingid", "trk"}
)


class BrowserAdapterError(RuntimeError):
    """A redacted failure reported by a browser tab adapter."""


@runtime_checkable
class BrowserTabAdapter(Protocol):
    """Minimum authority needed by the listing tab monitor."""

    def list_tab_urls(self) -> tuple[str, ...]:
        """Return currently open tab URLs without inspecting page content."""

    def open_listing(self, url: str) -> None:
        """Open one exact, canonical LinkedIn or Indeed listing URL."""


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _is_safe_tracking_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized in _SAFE_TRACKING_QUERY_KEYS or normalized.startswith("utm_")


def canonical_listing_url(value: object) -> str:
    """Return a credential-free canonical listing URL or raise ``ValueError``.

    Tracking keys are accepted only from a small public allowlist and are then
    removed. This keeps the argv bridge bound to a stable public job identity
    rather than forwarding caller-controlled query data.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError("listing URL must be a non-empty string")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("listing URL has an invalid authority") from None
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
    ):
        raise ValueError("listing URL must be credential-free HTTPS without a port or fragment")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if parsed.query and not query:
        raise ValueError("listing URL query is invalid")
    if any(not key or (key.casefold() != "jk" and not _is_safe_tracking_key(key)) for key, _value in query):
        raise ValueError("listing URL contains an unsupported query key")
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        if _LINKEDIN_LISTING_PATH.fullmatch(parsed.path) is None or any(
            key.casefold() == "jk" for key, _value in query
        ):
            raise ValueError("listing URL is not a canonical LinkedIn listing")
        return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))
    if host == "indeed.com" or host.endswith(".indeed.com"):
        if parsed.path.rstrip("/") != "/viewjob":
            raise ValueError("listing URL is not a canonical Indeed listing")
        job_ids = [
            item
            for key, item in query
            if key.casefold() == "jk"
        ]
        if len(job_ids) != 1 or _INDEED_JOB_ID.fullmatch(job_ids[0]) is None:
            raise ValueError("listing URL is not a canonical Indeed listing")
        return urlunsplit(("https", host, "/viewjob", urlencode({"jk": job_ids[0]}), ""))
    raise ValueError("listing URL host is unsupported")


def is_listing_url(value: object) -> bool:
    """Return whether *value* is a supported credential-free listing URL."""

    try:
        canonical_listing_url(value)
    except ValueError:
        return False
    return True


class ExternalCommandAdapter:
    """Delegate the two permitted actions to an explicit argv-based bridge.

    Bridge protocol:

    * ``<command...> list-tabs`` writes a JSON array of URL strings to stdout.
    * ``<command...> open-listing <url>`` opens the exact supplied listing.

    The bridge is never invoked through a shell.  Failure messages intentionally
    omit command arguments, URLs, stdout, and stderr because any of them may
    contain private browser-session data.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        runner: Runner = subprocess.run,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if isinstance(command, (str, bytes)) or not isinstance(command, Sequence):
            raise TypeError("command must be a non-empty sequence of argv strings")
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("command must contain only non-empty argv strings")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout must be a positive finite number")
        self._command = tuple(command)
        self._runner = runner
        self._timeout_seconds = timeout_seconds

    def _invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        try:
            completed = self._runner(
                [*self._command, *arguments],
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise BrowserAdapterError("browser adapter command failed") from None
        if getattr(completed, "returncode", 1) != 0:
            raise BrowserAdapterError("browser adapter command failed")
        return completed

    def list_tab_urls(self) -> tuple[str, ...]:
        completed = self._invoke("list-tabs")
        try:
            payload = json.loads(completed.stdout)
        except (AttributeError, TypeError, json.JSONDecodeError):
            raise BrowserAdapterError("browser adapter returned invalid tab data") from None
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise BrowserAdapterError("browser adapter returned invalid tab data")
        return tuple(payload)

    def open_listing(self, url: str) -> None:
        try:
            canonical = canonical_listing_url(url)
        except ValueError:
            raise ValueError("refusing to open a non-listing LinkedIn/Indeed URL")
        self._invoke("open-listing", canonical)


def _load_chrome_applescript_class() -> type[BrowserTabAdapter]:
    """Load the optional macOS compatibility adapter without a hard dependency."""

    loaded = sys.modules.get("chrome_tab_watcher")
    if loaded is not None and hasattr(loaded, "ChromeAppleScript"):
        return loaded.ChromeAppleScript  # type: ignore[no-any-return]

    try:
        from chrome_tab_watcher import ChromeAppleScript

        return ChromeAppleScript
    except ModuleNotFoundError:
        watcher_path = Path(__file__).with_name("chrome_tab_watcher.py")
        spec = importlib.util.spec_from_file_location("chrome_tab_watcher", watcher_path)
        if spec is None or spec.loader is None:
            raise BrowserAdapterError("Chrome AppleScript adapter is unavailable") from None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module.ChromeAppleScript  # type: ignore[no-any-return]


def create_adapter(
    adapter: str,
    *,
    command: Sequence[str] | None = None,
    runner: Runner = subprocess.run,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> BrowserTabAdapter:
    """Create an adapter for the legacy fixed-round watcher only.

    The Smart Queue coordinator does not call this factory: its host supplies
    an already-bounded adapter directly. The optional AppleScript path is
    therefore unavailable to live Smart Queue execution.
    """

    if adapter == "external":
        if command is None:
            raise ValueError("external adapter requires a command")
        return ExternalCommandAdapter(command, runner=runner, timeout_seconds=timeout_seconds)
    if adapter == "chrome-applescript":
        if command is not None:
            raise ValueError("chrome-applescript adapter does not accept a command")
        chrome_class = _load_chrome_applescript_class()
        return chrome_class(runner=runner)
    raise ValueError("unsupported browser adapter")


__all__ = [
    "BrowserAdapterError",
    "BrowserTabAdapter",
    "ExternalCommandAdapter",
    "canonical_listing_url",
    "create_adapter",
    "is_listing_url",
]
