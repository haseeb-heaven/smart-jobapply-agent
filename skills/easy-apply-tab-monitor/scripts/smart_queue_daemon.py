#!/usr/bin/env python3
"""Existing-session-only persistent Smart Queue daemon.

The daemon deliberately has just one browser seam: a host-provided adapter
which can list tab URLs and open an exact approved listing URL.  It neither
launches a browser nor owns a browser window, application flow, candidate
recommendations, or candidate outcomes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
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


_adapter_module = _load_sibling_module("codex_chrome_extension_adapter.py")
_monitor_module = _load_sibling_module("persistent_smart_queue_monitor.py")
BrowserAdapterError = _adapter_module.BrowserAdapterError
CodexChromeExtensionAdapter = _adapter_module.CodexChromeExtensionAdapter
PersistentSmartQueueMonitor = _monitor_module.PersistentSmartQueueMonitor
# Construct the exact coordinator class whose exception identity the monitor
# catches, including when this daemon is imported directly from its file path.
SmartQueueCoordinator = _monitor_module.SmartQueueCoordinator
DEFAULT_INTERVAL_SECONDS = _monitor_module.DEFAULT_INTERVAL_SECONDS
DEFAULT_MAX_BACKOFF_SECONDS = _monitor_module.DEFAULT_MAX_BACKOFF_SECONDS


class DaemonConfigurationError(ValueError):
    """Raised without reflecting sensitive configuration values."""


class ListingAdapter(Protocol):
    """The two browser operations available to the daemon."""

    def list_tab_urls(self) -> tuple[str, ...]: ...

    def open_listing(self, url: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    """Validated, data-free daemon configuration.

    The production CLI constructs the bounded Codex Chrome stdio adapter. The
    ``host-provided`` marker is a closed configuration value so tests and
    alternative hosts can inject the same two-operation adapter without
    admitting arbitrary commands.
    """

    database_path: Path
    active_intake_path: Path
    bridge_adapter: str

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "DaemonConfig":
        if not isinstance(payload, Mapping) or set(payload) != {
            "database_path", "active_intake_path", "bridge"
        }:
            raise DaemonConfigurationError("daemon configuration is invalid")
        database = payload.get("database_path")
        intake = payload.get("active_intake_path")
        bridge = payload.get("bridge")
        if not isinstance(database, str) or not isinstance(intake, str):
            raise DaemonConfigurationError("daemon configuration is invalid")
        if not isinstance(bridge, Mapping) or set(bridge) != {"adapter"}:
            raise DaemonConfigurationError("daemon configuration is invalid")
        adapter = bridge.get("adapter")
        if adapter != "host-provided":
            raise DaemonConfigurationError("daemon configuration is invalid")
        return cls(Path(database), Path(intake), adapter)

    @classmethod
    def from_json(cls, raw: str) -> "DaemonConfig":
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise DaemonConfigurationError("daemon configuration is invalid") from None
        return cls.from_mapping(parsed)


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

    def __init__(self, monitor: object) -> None:
        if not callable(getattr(monitor, "run", None)):
            raise TypeError("monitor must provide run")
        self._monitor = monitor

    @classmethod
    def from_config(
        cls,
        config: DaemonConfig,
        *,
        adapter: ListingAdapter,
        monitor_factory: Callable[[object], object] = PersistentSmartQueueMonitor,
        queue_factory: Callable[[Path, Path], object] | None = None,
    ) -> "SmartQueueDaemon":
        if not isinstance(config, DaemonConfig) or config.bridge_adapter != "host-provided":
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
        return cls(monitor)

    @staticmethod
    def _status(ticks: Sequence[object]) -> DaemonStatus:
        return DaemonStatus(
            ticks_completed=len(ticks),
            requested_open_count=sum(_tick_count(tick, "requested_open_count", "requested_open_job_ids") for tick in ticks),
            opened_count=sum(_tick_count(tick, "opened_count", "opened_job_ids") for tick in ticks),
            open_failed_count=sum(_tick_count(tick, "open_failed_count", "open_failed_job_ids") for tick in ticks),
            search_needed=(int(getattr(ticks[-1], "search_needed", 0)) if ticks else 0),
            degraded_tick_count=sum(bool(getattr(tick, "degraded", False)) for tick in ticks),
        )

    def run(self, *, max_ticks: int) -> DaemonStatus:
        if isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 0:
            raise ValueError("max_ticks must be a non-negative integer")
        ticks = self._monitor.run(max_ticks=max_ticks)
        if not isinstance(ticks, tuple):
            raise RuntimeError("monitor returned invalid status")
        return self._status(ticks)

    def run_forever(self, cancellation: threading.Event, status_sink: Callable[[object], None]) -> None:
        if not isinstance(cancellation, threading.Event) or not callable(status_sink):
            raise TypeError("invalid daemon lifecycle arguments")
        self._monitor.run(cancellation, max_ticks=None, result_sink=status_sink)


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
        action="store_true",
        required=True,
        help="use the Node-parented strict NDJSON stdin/stderr bridge",
    )
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--max-backoff-seconds", type=float, default=DEFAULT_MAX_BACKOFF_SECONDS)
    parser.add_argument("--max-ticks", type=int)
    return parser


def _shutdown_event() -> tuple[threading.Event, Callable[[], None]]:
    stop = threading.Event()
    if threading.current_thread() is not threading.main_thread():
        return stop, lambda: None
    previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
    for value in previous:
        signal.signal(value, lambda _signal, _frame: stop.set())
    return stop, lambda: [signal.signal(value, handler) for value, handler in previous.items()]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        interval = _positive_duration(arguments.interval_seconds, "interval_seconds")
        maximum = _positive_duration(arguments.max_backoff_seconds, "max_backoff_seconds")
        if maximum < interval or arguments.max_ticks is not None and arguments.max_ticks < 0:
            raise DaemonConfigurationError("daemon configuration is invalid")
        config = DaemonConfig.from_mapping({
            "database_path": str(arguments.database),
            "active_intake_path": str(arguments.candidate_intake),
            "bridge": {"adapter": "host-provided"},
        })
        adapter = CodexChromeExtensionAdapter()
        daemon = SmartQueueDaemon.from_config(
            config,
            adapter=adapter,
            monitor_factory=lambda coordinator: PersistentSmartQueueMonitor(
                coordinator, interval_seconds=interval, max_backoff_seconds=maximum
            ),
        )
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
