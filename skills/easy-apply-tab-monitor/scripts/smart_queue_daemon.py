#!/usr/bin/env python3
"""Existing-session-only persistent Smart Queue daemon.

The daemon deliberately has just one browser seam: a host-provided adapter
which can list tab URLs and open an exact approved listing URL.  It neither
launches a browser nor owns a browser window, application flow, candidate
recommendations, or candidate outcomes.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from dataclasses import dataclass
import importlib.util
import inspect
import json
import math
from pathlib import Path
import signal
import sys
import threading
from typing import Callable, Mapping, Protocol, Sequence


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIRECTORY.parents[2]
PACKAGE_SOURCE = PROJECT_ROOT / "jobapply_agent" / "src"
PRIVATE_RUNTIME_DIRECTORY = PROJECT_ROOT / "jobapply_agent" / "private"
_DAEMON_VALUE_FLAGS = frozenset({
    "--candidate-intake", "--database", "--adapter", "--adapter-command",
    "--interval-seconds", "--max-backoff-seconds", "--max-ticks",
})

if str(PACKAGE_SOURCE) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SOURCE))


def _load_sibling_module(filename: str) -> object:
    """Load a sibling script when this file is imported by path in tests."""

    path = SCRIPT_DIRECTORY / filename
    name = f"smart_queue_daemon_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Smart Queue runtime dependency is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_adapter_module = _load_sibling_module("browser_bridge_adapter.py")
_tab_adapter_module = _load_sibling_module("browser_tab_adapter.py")
_monitor_module = _load_sibling_module("persistent_smart_queue_monitor.py")
BrowserAdapterError = _adapter_module.BrowserAdapterError
StdioBridgeAdapter = _adapter_module.StdioBridgeAdapter
# Historical import path; the generic stdio bridge is the implementation.
CodexChromeExtensionAdapter = _adapter_module.StdioBridgeAdapter
ExternalCommandAdapter = _tab_adapter_module.ExternalCommandAdapter
PersistentSmartQueueMonitor = _monitor_module.PersistentSmartQueueMonitor
# Construct the exact coordinator class whose exception identity the monitor
# catches, including when this daemon is imported directly from its file path.
SmartQueueCoordinator = _monitor_module.SmartQueueCoordinator
DEFAULT_INTERVAL_SECONDS = _monitor_module.DEFAULT_INTERVAL_SECONDS
DEFAULT_MAX_BACKOFF_SECONDS = _monitor_module.DEFAULT_MAX_BACKOFF_SECONDS


class DaemonConfigurationError(ValueError):
    """Raised without reflecting sensitive configuration values."""


def _invalid_configuration() -> None:
    raise DaemonConfigurationError("daemon configuration is invalid")


class ListingAdapter(Protocol):
    """The two browser operations available to the daemon."""

    def list_tab_urls(self) -> tuple[str, ...]: ...

    def open_listing(self, url: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Validated, data-free daemon configuration.

    The production CLI constructs either the Node-parented stdio adapter or
    the explicit argv bridge. The ``host-provided`` and ``external`` markers
    are closed configuration values so tests and alternative hosts can inject
    the same two-operation adapter without admitting arbitrary commands. An
    external ``command`` is validated but never stored: it stays outside the
    redacted config surface.
    """

    database_path: Path
    active_intake_path: Path
    bridge_adapter: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "DaemonConfig":
        if not isinstance(payload, Mapping) or set(payload) != {
            "database_path", "active_intake_path", "bridge"
        }:
            _invalid_configuration()
        database = payload.get("database_path")
        intake = payload.get("active_intake_path")
        bridge = payload.get("bridge")
        if not isinstance(database, str) or not isinstance(intake, str):
            _invalid_configuration()
        return cls(Path(database), Path(intake), _bridge_adapter_name(bridge))

    @classmethod
    def from_json(cls, raw: str) -> "DaemonConfig":
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise DaemonConfigurationError("daemon configuration is invalid") from None
        return cls.from_mapping(parsed)


def _bridge_adapter_name(bridge: object) -> str:
    """Validate the closed bridge configuration without retaining its command."""

    if not isinstance(bridge, Mapping) or "adapter" not in bridge:
        _invalid_configuration()
    adapter = bridge.get("adapter")
    if adapter == "host-provided":
        if set(bridge) != {"adapter"}:
            _invalid_configuration()
        return adapter
    if adapter != "external" or set(bridge) not in ({"adapter"}, {"adapter", "command"}):
        _invalid_configuration()
    if "command" in bridge:
        _require_external_command(bridge["command"])
    return adapter


def _require_external_command(command: object) -> None:
    if (
        isinstance(command, (str, bytes))
        or not isinstance(command, (list, tuple))
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        _invalid_configuration()


@dataclass(frozen=True, slots=True)
class DaemonStatus:
    """Count-only aggregate returned by a finite daemon run."""

    ticks_completed: int
    requested_open_count: int
    opened_count: int
    open_failed_count: int
    search_needed: int
    degraded_tick_count: int


def _private_runtime_root() -> Path:
    if PRIVATE_RUNTIME_DIRECTORY.is_symlink():
        raise DaemonConfigurationError("private runtime configuration is invalid")
    return PRIVATE_RUNTIME_DIRECTORY.resolve()


def _private_path(value: Path, *, must_exist: bool) -> Path:
    if str(value) == ":memory:":
        raise DaemonConfigurationError("private runtime configuration is invalid")
    # The documented command is repository-relative.  Resolve it against this
    # repository, then enforce containment instead of rejecting every relative
    # path.  An absolute path receives the same containment check.
    candidate = value if value.is_absolute() else PROJECT_ROOT / value
    if candidate.is_symlink() or (must_exist and (not candidate.is_file() or candidate.is_symlink())):
        raise DaemonConfigurationError("private runtime configuration is invalid")
    if not must_exist and candidate.exists() and (candidate.is_dir() or candidate.is_symlink()):
        raise DaemonConfigurationError("private runtime configuration is invalid")
    try:
        resolved = candidate.resolve()
        resolved.relative_to(_private_runtime_root())
    except (OSError, RuntimeError, ValueError):
        raise DaemonConfigurationError("private runtime configuration is invalid") from None
    return resolved


def _positive_duration(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DaemonConfigurationError(f"{name} is invalid")
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        raise DaemonConfigurationError(f"{name} is invalid")
    return duration


def _load_queue_factory() -> Callable[[Path, Path], object]:
    path = PROJECT_ROOT / "jobapply_agent" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("smart_queue_daemon_discover", path)
    if spec is None or spec.loader is None:
        raise DaemonConfigurationError("queue factory is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    factory = getattr(module, "smart_queue_for_active_intake", None)
    if not callable(factory):
        raise DaemonConfigurationError("queue factory is unavailable")
    return factory


def _tick_count(tick: object, count_name: str, ids_name: str) -> int:
    value = getattr(tick, count_name, None)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    identifiers = getattr(tick, ids_name, ())
    return len(identifiers) if isinstance(identifiers, tuple) else 0


class SmartQueueDaemon:
    """A durable monitor host with no recommendation or outcome interface."""

    def __init__(self, monitor: object, *, adapter: ListingAdapter | None = None) -> None:
        if not callable(getattr(monitor, "run", None)):
            raise TypeError("monitor must provide run")
        self._monitor = monitor
        self._adapter = adapter

    @classmethod
    def from_config(
        cls,
        config: DaemonConfig,
        *,
        adapter: ListingAdapter,
        monitor_factory: Callable[[object], object] = PersistentSmartQueueMonitor,
        queue_factory: Callable[[Path, Path], object] | None = None,
    ) -> "SmartQueueDaemon":
        if not isinstance(config, DaemonConfig) or config.bridge_adapter not in ("host-provided", "external"):
            raise DaemonConfigurationError("daemon configuration is invalid")
        if (
            not callable(getattr(adapter, "list_tab_urls", None))
            or not callable(getattr(adapter, "open_listing", None))
            or not callable(monitor_factory)
        ):
            raise DaemonConfigurationError("daemon configuration is invalid")
        intake = _private_path(config.active_intake_path, must_exist=True)
        database = _private_path(config.database_path, must_exist=False)
        # This preflight happens before the active-intake factory can create a
        # SQLite database or mutate queue state.
        try:
            adapter.list_tab_urls()
        except Exception:
            raise DaemonConfigurationError("existing browser bridge is unavailable") from None
        factory = queue_factory or _load_queue_factory()
        try:
            queue = factory(intake, database)
            coordinator = SmartQueueCoordinator(queue, adapter)
            monitor = monitor_factory(coordinator)
        except DaemonConfigurationError:
            raise
        except Exception:
            raise DaemonConfigurationError("daemon initialization failed") from None
        return cls(monitor, adapter=adapter)

    @staticmethod
    def _status(ticks: Sequence[object]) -> DaemonStatus:
        return DaemonStatus(
            ticks_completed=len(ticks),
            requested_open_count=sum(
                _tick_count(tick, "requested_open_count", "requested_open_job_ids") for tick in ticks
            ),
            opened_count=sum(_tick_count(tick, "opened_count", "opened_job_ids") for tick in ticks),
            open_failed_count=sum(_tick_count(tick, "open_failed_count", "open_failed_job_ids") for tick in ticks),
            search_needed=(int(getattr(ticks[-1], "search_needed", 0)) if ticks else 0),
            degraded_tick_count=sum(bool(getattr(tick, "degraded", False)) for tick in ticks),
        )

    def run(self, *, max_ticks: int) -> DaemonStatus:
        if isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 0:
            raise ValueError("max_ticks must be a non-negative integer")
        monitor_run = self._monitor.run
        try:
            parameters = inspect.signature(monitor_run).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        supports_result_sink = any(
            parameter.name == "result_sink" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if supports_result_sink:
            cancellation = threading.Event()

            def stop_after_terminal_adapter(_status: object) -> None:
                # A timed-out stdio read makes the response boundary unsafe.
                # Stop a finite monitor at that completed degraded tick so the
                # monitor releases its lease before the child exits.
                if getattr(self._adapter, "terminal", False) is True:
                    cancellation.set()

            ticks = monitor_run(
                cancellation,
                max_ticks=max_ticks,
                result_sink=stop_after_terminal_adapter,
            )
        else:
            # Retain the original narrow test-double contract for monitors
            # that only expose finite tuple runs.
            ticks = monitor_run(max_ticks=max_ticks)
        if not isinstance(ticks, tuple):
            raise RuntimeError("monitor returned invalid status")
        return self._status(ticks)

    def run_forever(self, cancellation: threading.Event, status_sink: Callable[[object], None]) -> None:
        if not isinstance(cancellation, threading.Event) or not callable(status_sink):
            raise TypeError("invalid daemon lifecycle arguments")

        def emit_and_stop_after_terminal_adapter(status: object) -> None:
            status_sink(status)
            # A timed-out stdio read leaves a late response without a safe
            # request boundary. The degraded tick above is the final count-only
            # status; stopping here releases the monitor lease and ends the child.
            if getattr(self._adapter, "terminal", False) is True:
                cancellation.set()

        self._monitor.run(cancellation, max_ticks=None, result_sink=emit_and_stop_after_terminal_adapter)


def _emit_status(status: object) -> None:
    """Emit a status object only as non-sensitive aggregate counts."""

    if isinstance(status, DaemonStatus):
        payload = {
            "ticks_completed": status.ticks_completed,
            "requested_open_count": status.requested_open_count,
            "opened_count": status.opened_count,
            "open_failed_count": status.open_failed_count,
            "search_needed": status.search_needed,
            "degraded_tick_count": status.degraded_tick_count,
        }
    else:
        payload = {
            "requested_open_count": _tick_count(status, "requested_open_count", "requested_open_job_ids"),
            "opened_count": _tick_count(status, "opened_count", "opened_job_ids"),
            "open_failed_count": _tick_count(status, "open_failed_count", "open_failed_job_ids"),
            "search_needed": int(getattr(status, "search_needed", 0)),
            "degraded_count": int(bool(getattr(status, "degraded", False))),
        }
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the existing-session-only Smart Queue daemon.")
    parser.add_argument("--candidate-intake", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument(
        "--bridge-stdio",
        action="count",
        default=0,
        help="use the Node-parented strict NDJSON stdin/stderr bridge",
    )
    parser.add_argument(
        "--adapter",
        choices=("external",),
        default=None,
        help="explicit argv bridge kind; exactly one bridge must be selected",
    )
    parser.add_argument(
        "--adapter-command",
        nargs="+",
        default=None,
        help=(
            "External adapter argv prefix. Unknown option-like bridge tokens are passed "
            "as argv without a shell; recognized daemon flags are still parsed. "
            "Bridge argv must not reuse daemon flag spellings (for example "
            "--bridge-stdio or --max-ticks): argparse consumes recognized flags "
            "anywhere, including after this marker, so a colliding bridge token "
            "is parsed as daemon configuration instead of bridge argv."
        ),
    )
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--max-backoff-seconds", type=float, default=DEFAULT_MAX_BACKOFF_SECONDS)
    parser.add_argument("--max-ticks", type=int)
    return parser


def _resolve_stdio_adapter_class() -> type:
    """Resolve the Node-parented stdio adapter class inside the CLI entry point.

    Module globals are preferred so historical monkeypatch targets keep
    resolving; otherwise the sibling bridge file is loaded by path.
    """

    candidate = globals().get("CodexChromeExtensionAdapter")
    if isinstance(candidate, type):
        return candidate
    candidate = globals().get("StdioBridgeAdapter")
    if isinstance(candidate, type):
        return candidate
    module = _load_sibling_module("browser_bridge_adapter.py")
    return module.StdioBridgeAdapter


def _resolve_external_adapter_class() -> type:
    """Resolve the explicit argv adapter class inside the CLI entry point."""

    candidate = globals().get("ExternalCommandAdapter")
    if isinstance(candidate, type):
        return candidate
    module = _load_sibling_module("browser_tab_adapter.py")
    return module.ExternalCommandAdapter


def _shutdown_event() -> tuple[threading.Event, Callable[[], None]]:
    stop = threading.Event()
    if threading.current_thread() is not threading.main_thread():
        return stop, lambda: None
    previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
    for value in previous:
        signal.signal(value, lambda _signal, _frame: stop.set())
    return stop, lambda: [signal.signal(value, handler) for value, handler in previous.items()]


def _bridge_command_from_extras(
    raw_args: Sequence[str], base_command: Sequence[str] | None, extras: Sequence[str]
) -> list[str] | None:
    """Merge unknown bridge argv tokens positioned after ``--adapter-command``.

    Unknown option-like bridge tokens (for example a wrapped browser's
    ``--browser firefox`` pair) cannot be consumed by ``nargs="+"``, so they
    arrive via ``parse_known_args`` extras. Tokens after the marker belong to
    the explicit argv bridge; anything unknown elsewhere is rejected without
    echoing it. Returns ``None`` when no marker is present.

    Bridge argv must not reuse daemon flag spellings: ``argparse`` consumes
    recognized flags anywhere on the command line, including after the
    marker, so a colliding bridge token (for example ``--bridge-stdio``) is
    parsed as daemon configuration. A collision with a bridge-selection
    flag surfaces as a redacted configuration error through the normal
    exactly-one-bridge selection check, never as silent bridge misrouting.
    """

    marker = _adapter_command_marker(raw_args)
    if marker is None:
        if extras:
            raise DaemonConfigurationError("daemon configuration is invalid")
        return None
    merged = _merge_bridge_extras(raw_args, marker, extras)
    _reject_extras_before_bridge_marker(raw_args, marker, extras)
    return [*(base_command or []), *merged]


def _adapter_command_marker(raw_args: Sequence[str]) -> int | None:
    """Return the first explicit external-bridge marker, if present."""

    return next(
        (
            index
            for index, token in enumerate(raw_args)
            if token == "--adapter-command" or token.startswith("--adapter-command=")
        ),
        None,
    )


def _merge_bridge_extras(raw_args: Sequence[str], marker: int, extras: Sequence[str]) -> list[str]:
    """Return unknown argv tokens only when they occur after the bridge marker."""

    from collections import Counter

    pending = Counter(extras)
    merged: list[str] = []
    # Scan only tokens after the --adapter-command marker. Daemon values
    # before the marker (for example --database sharing a spelling with a
    # bridge token) must never consume a pending extras entry and
    # false-reject the bridge command.
    for index in range(marker + 1, len(raw_args)):
        token = raw_args[index]
        if pending.get(token, 0) <= 0:
            continue
        pending[token] -= 1
        merged.append(token)
    if any(pending.values()):
        raise DaemonConfigurationError("daemon configuration is invalid")
    return merged


def _reject_extras_before_bridge_marker(raw_args: Sequence[str], marker: int, extras: Sequence[str]) -> None:
    """Fail closed when an unknown token is outside the external bridge argv."""

    # Preserve fail-closed rejection of unknown tokens elsewhere: an extras
    # entry occurring before the marker (outside any daemon flag value)
    # means an unknown token was passed outside the bridge command.
    extra_strings = set(extras)
    index = 0
    while index < marker:
        token = raw_args[index]
        if token in _DAEMON_VALUE_FLAGS and not token.startswith("--adapter-command="):
            index += 2
            continue
        if token in extra_strings:
            raise DaemonConfigurationError("daemon configuration is invalid")
        index += 1


def _parse_arguments(parser: argparse.ArgumentParser, argv: Sequence[str] | None) -> tuple[argparse.Namespace, list[str]]:
    """Parse CLI input while keeping argparse's potentially sensitive stderr private."""

    with contextlib.redirect_stderr(io.StringIO()):
        return parser.parse_known_args(argv)


def _monitor_factory(interval: float, maximum: float) -> Callable[[object], object]:
    return lambda coordinator: PersistentSmartQueueMonitor(
        coordinator, interval_seconds=interval, max_backoff_seconds=maximum
    )


def _validate_timing(arguments: argparse.Namespace) -> tuple[float, float]:
    interval = _positive_duration(arguments.interval_seconds, "interval_seconds")
    maximum = _positive_duration(arguments.max_backoff_seconds, "max_backoff_seconds")
    if maximum < interval or arguments.max_ticks is not None and arguments.max_ticks < 0:
        raise DaemonConfigurationError("daemon configuration is invalid")
    return interval, maximum


def _create_adapter_and_config(
    arguments: argparse.Namespace,
    adapter_command: list[str] | None,
) -> tuple[DaemonConfig, ListingAdapter]:
    selections = (arguments.bridge_stdio or 0) + (1 if arguments.adapter == "external" else 0)
    if selections != 1:
        raise DaemonConfigurationError("daemon configuration is invalid")
    if arguments.adapter == "external":
        if not adapter_command or not all(isinstance(part, str) and part for part in adapter_command):
            raise DaemonConfigurationError("daemon configuration is invalid")
        config = DaemonConfig.from_mapping({
            "database_path": str(arguments.database),
            "active_intake_path": str(arguments.candidate_intake),
            "bridge": {"adapter": "external"},
        })
        return config, _resolve_external_adapter_class()(tuple(adapter_command))
    if adapter_command:
        raise DaemonConfigurationError("daemon configuration is invalid")
    config = DaemonConfig.from_mapping({
        "database_path": str(arguments.database),
        "active_intake_path": str(arguments.candidate_intake),
        "bridge": {"adapter": "host-provided"},
    })
    return config, _resolve_stdio_adapter_class()()


def _daemon_from_arguments(arguments: argparse.Namespace, extras: Sequence[str], argv: Sequence[str] | None) -> SmartQueueDaemon:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    adapter_command = _bridge_command_from_extras(raw_args, arguments.adapter_command, extras)
    interval, maximum = _validate_timing(arguments)
    config, adapter = _create_adapter_and_config(arguments, adapter_command)
    return SmartQueueDaemon.from_config(
        config,
        adapter=adapter,
        monitor_factory=_monitor_factory(interval, maximum),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        arguments, extras = _parse_arguments(parser, argv)
    except SystemExit as parse_failed:
        # Argument errors echo untrusted argv; stay redacted with a bare exit code.
        if parse_failed.code == 2:
            return 2
        raise
    try:
        daemon = _daemon_from_arguments(arguments, extras, argv)
    except (DaemonConfigurationError, BrowserAdapterError, OSError, RuntimeError, TypeError, ValueError):
        return 2
    if arguments.max_ticks is not None:
        _emit_status(daemon.run(max_ticks=arguments.max_ticks))
        return 0
    stop, restore = _shutdown_event()
    try:
        daemon.run_forever(stop, _emit_status)
    finally:
        restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
