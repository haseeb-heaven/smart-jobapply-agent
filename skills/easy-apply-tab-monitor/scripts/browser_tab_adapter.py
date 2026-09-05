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
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
from typing import Callable, Protocol, Sequence, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_TIMEOUT_SECONDS = 15.0
_MAX_TAB_URLS = 512
_MAX_TAB_URL_LENGTH = 8_192
_MAX_STDOUT_BYTES = 1_048_576
_LINKEDIN_LISTING_PATH = re.compile(r"^/jobs/view/[A-Za-z0-9_-]+/?$")
_INDEED_JOB_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_COMMAND_FAILED_MESSAGE = "browser adapter command failed"
_INVALID_TAB_DATA_MESSAGE = "browser adapter returned invalid tab data"
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


def _kill_bridge_process_tree(
    child: subprocess.Popen[str], *, pgid: int | None = None
) -> None:
    """Kill one bridge child and any helpers sharing its process session.

    ``pgid`` is captured at spawn time because the group leader may exit
    (becoming an unreaped zombie) while a helper still holds the captured
    pipe open; on some platforms ``getpgid`` then fails for the leader's
    pid even though the group survives. Falls back to resolving the group
    from the live child, then to the direct child where process groups are
    unavailable. Never raises and never reports process, command, or URL
    details.
    """

    try:
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        resolved_pgid = pgid
        if (
            resolved_pgid is None
            and os.name != "nt"
            and callable(getpgid)
            and getattr(child, "pid", None) is not None
        ):
            try:
                resolved_pgid = getpgid(child.pid)
            except (OSError, ProcessLookupError):
                resolved_pgid = None
        if os.name != "nt" and callable(killpg) and resolved_pgid is not None:
            try:
                killpg(resolved_pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            child.kill()
        except Exception:
            pass
    except Exception:
        pass


def _read_bounded_bridge_stdout(child: subprocess.Popen[str]) -> tuple[str, bool]:
    """Read bridge stdout without materializing more than the configured cap."""

    chunks: list[str] = []
    total_bytes = 0
    assert child.stdout is not None
    while True:
        try:
            piece = child.stdout.read(65_536)
        except Exception:
            raise BrowserAdapterError(_COMMAND_FAILED_MESSAGE) from None
        if not piece:
            return "".join(chunks), False
        total_bytes += len(piece.encode("utf-8"))
        if total_bytes > _MAX_STDOUT_BYTES:
            return "", True
        chunks.append(piece)


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

    def _invoke_default(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run the bridge with incrementally bounded stdout capture.

        The production path never materializes unbounded bridge output: stdout
        is read in chunks and the child is killed once the byte cap is
        exceeded, before any JSON parsing. ``stderr`` is discarded (failures
        stay redacted) so a voluminous stderr cannot block the child either.
        ``shell`` stays ``False`` and no command, argument, URL, or output
        bytes ever enter the raised error.

        The child starts in its own process session with no inherited file
        descriptors, so a bridge that spawns background helpers cannot hold
        the captured pipe open past the timeout: the timeout kills the whole
        process group (falling back to the direct child where process groups
        are unavailable), which bounds the wall clock even when a helper
        inherits the stdout descriptor. The group id is captured at spawn
        because the leader may exit while a helper survives, after which the
        group can no longer be resolved from the leader's pid.
        """

        try:
            child = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
            raise BrowserAdapterError(_COMMAND_FAILED_MESSAGE) from None
        # With start_new_session=True the child is the deterministic leader
        # of a new process group, so its pgid equals its pid. Capture it
        # directly instead of resolving it via getpgid(): the leader may
        # exit immediately while a pipe-holding helper survives, after which
        # getpgid() can fail for the leader's pid (and it is unavailable on
        # Windows), leaving the timeout with no group to kill and the wait
        # unbounded. No command, argument, URL, or output data is recorded.
        if os.name != "nt":
            spawn_pgid: int | None = child.pid
        else:
            spawn_pgid = None
        timer = threading.Timer(
            self._timeout_seconds,
            _kill_bridge_process_tree,
            args=(child,),
            kwargs={"pgid": spawn_pgid},
        )
        try:
            timer.start()
            stdout, oversize = _read_bounded_bridge_stdout(child)
            _kill_bridge_process_tree(child, pgid=spawn_pgid)
            try:
                child.wait(timeout=5)
            except Exception:
                pass
            if oversize:
                raise BrowserAdapterError(_INVALID_TAB_DATA_MESSAGE)
            try:
                returncode = child.returncode
            except Exception:
                raise BrowserAdapterError(_COMMAND_FAILED_MESSAGE) from None
            if returncode != 0:
                raise BrowserAdapterError(_COMMAND_FAILED_MESSAGE)
            return subprocess.CompletedProcess(argv, returncode, stdout, "")
        except BrowserAdapterError:
            _kill_bridge_process_tree(child, pgid=spawn_pgid)
            raise
        except (OSError, subprocess.SubprocessError):
            _kill_bridge_process_tree(child, pgid=spawn_pgid)
            raise BrowserAdapterError(_COMMAND_FAILED_MESSAGE) from None
        finally:
            timer.cancel()

    def _invoke(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        argv = [*self._command, *arguments]
        if self._runner is not subprocess.run:
            try:
                completed = self._runner(
                    argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    shell=False,
                )
            except (OSError, subprocess.SubprocessError):
                raise BrowserAdapterError(_COMMAND_FAILED_MESSAGE) from None
            if getattr(completed, "returncode", 1) != 0:
                raise BrowserAdapterError(_COMMAND_FAILED_MESSAGE)
            stdout = getattr(completed, "stdout", None)
            if isinstance(stdout, str) and len(stdout.encode("utf-8")) > _MAX_STDOUT_BYTES:
                raise BrowserAdapterError(_INVALID_TAB_DATA_MESSAGE)
            return completed
        return self._invoke_default(argv)

    def list_tab_urls(self) -> tuple[str, ...]:
        """Return canonical supported listing URLs, skipping unmanaged tabs.

        Layered contract: this adapter speaks to raw bridge output, so tabs
        that are not managed listings are skipped as not-observable. The
        strict stdio adapter (``browser_bridge_adapter.StdioBridgeAdapter``)
        instead speaks to an already-filtering host, so a non-canonical URL
        there is a protocol violation and the whole batch fails closed.
        """
        completed = self._invoke("list-tabs")
        stdout = completed.stdout
        if not isinstance(stdout, str) or len(stdout.encode("utf-8")) > _MAX_STDOUT_BYTES:
            raise BrowserAdapterError(_INVALID_TAB_DATA_MESSAGE)
        try:
            payload = json.loads(stdout)
        except (AttributeError, TypeError, json.JSONDecodeError):
            raise BrowserAdapterError(_INVALID_TAB_DATA_MESSAGE) from None
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            raise BrowserAdapterError(_INVALID_TAB_DATA_MESSAGE)
        if len(payload) > _MAX_TAB_URLS:
            raise BrowserAdapterError(_INVALID_TAB_DATA_MESSAGE)
        canonical: list[str] = []
        for item in payload:
            if not 0 < len(item) <= _MAX_TAB_URL_LENGTH:
                raise BrowserAdapterError(_INVALID_TAB_DATA_MESSAGE)
            try:
                canonical.append(canonical_listing_url(item))
            except ValueError:
                # Not a managed listing URL; unsupported tabs are not observable.
                continue
        return tuple(canonical)

    def open_listing(self, url: str) -> None:
        try:
            canonical = canonical_listing_url(url)
        except ValueError:
            raise ValueError("refusing to open a non-listing LinkedIn/Indeed URL") from None
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

    Live Smart Queue does not call this factory. Its host supplies a
    browser-neutral adapter with only ``list_tab_urls`` and ``open_listing``
    directly; the optional Codex Chrome host is one reference integration.
    This generic factory and the AppleScript path remain legacy fixed-round
    watcher compatibility only.
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
