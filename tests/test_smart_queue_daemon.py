"""Boundary contracts for the validated Smart Queue daemon host.

The daemon is intentionally tested through its public configuration and
``run`` seams.  The injected adapter and monitor are inert fakes: these tests
never start a browser, inspect page content, or perform an application action.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import importlib.util
import json
from pathlib import Path
import sys
import threading
from typing import Callable

import pytest

from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake
from jobapply_agent.smart_queue import SmartJobQueue


_SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts"
_DAEMON_PATH = _SCRIPTS_DIR / "smart_queue_daemon.py"
_DAEMON_SPEC = importlib.util.spec_from_file_location("smart_queue_daemon", _DAEMON_PATH)
assert _DAEMON_SPEC and _DAEMON_SPEC.loader
_DAEMON_MODULE = importlib.util.module_from_spec(_DAEMON_SPEC)
sys.modules[_DAEMON_SPEC.name] = _DAEMON_MODULE
_DAEMON_SPEC.loader.exec_module(_DAEMON_MODULE)

DaemonConfig = _DAEMON_MODULE.DaemonConfig
DaemonConfigurationError = _DAEMON_MODULE.DaemonConfigurationError
SmartQueueDaemon = _DAEMON_MODULE.SmartQueueDaemon


@dataclass(frozen=True, slots=True)
class FakeMonitorTick:
    """Already-redacted monitor data; queue IDs are opaque test tokens."""

    requested_open_job_ids: tuple[str, ...]
    opened_job_ids: tuple[str, ...]
    open_failed_job_ids: tuple[str, ...]
    search_needed: int
    degraded: bool = False


class FakeListingAdapter:
    """The only browser seam the daemon may receive, with calls observable."""

    def __init__(self) -> None:
        self.list_calls = 0
        self.open_calls: list[str] = []

    def list_tab_urls(self) -> tuple[str, ...]:
        self.list_calls += 1
        return ()

    def open_listing(self, url: str) -> None:
        self.open_calls.append(url)


class UnavailableListingAdapter(FakeListingAdapter):
    """A bridge failure that proves queue setup is never reached."""

    def list_tab_urls(self) -> tuple[str, ...]:
        self.list_calls += 1
        raise RuntimeError("private bridge detail must not escape")


class FailingLiveSnapshotAdapter(FakeListingAdapter):
    """Pass daemon preflight, then fail the first live URL snapshot."""

    def list_tab_urls(self) -> tuple[str, ...]:
        self.list_calls += 1
        if self.list_calls > 1:
            raise RuntimeError("private live snapshot detail must not escape")
        return ()


class TimedOutPreflightAdapter(FakeListingAdapter):
    """A terminal bridge deadline before any queue state may be created."""

    def list_tab_urls(self) -> tuple[str, ...]:
        self.list_calls += 1
        raise _DAEMON_MODULE.BrowserAdapterError("private deadline diagnostic")


class TimedOutLiveSnapshotAdapter(FakeListingAdapter):
    """Preflight succeeds, but the next live bridge snapshot times out."""

    def list_tab_urls(self) -> tuple[str, ...]:
        self.list_calls += 1
        if self.list_calls > 1:
            raise _DAEMON_MODULE.BrowserAdapterError("private deadline diagnostic")
        return ()

    @property
    def terminal(self) -> bool:
        return self.list_calls > 1


class FakeMonitor:
    """Host-owned monitor fake that does not call its coordinator or adapter."""

    def __init__(self, ticks: tuple[FakeMonitorTick, ...]) -> None:
        self.ticks = ticks
        self.run_calls: list[int | None] = []

    def run(self, *, max_ticks: int | None = None) -> tuple[FakeMonitorTick, ...]:
        self.run_calls.append(max_ticks)
        return self.ticks[:max_ticks]


def _private_database(tmp_path: Path, name: str = "queue.sqlite3") -> Path:
    """Return a disposable database path beneath the required private root."""

    runtime = (
        Path(__file__).parents[1]
        / "jobapply_agent"
        / "private"
        / "test-smart-queue-daemon"
        / tmp_path.name
    )
    runtime.mkdir(parents=True, exist_ok=True)
    return runtime / name


def _active_intake(tmp_path: Path) -> Path:
    """Create only synthetic active-intake proof, with no candidate data."""

    intake_path = _private_database(tmp_path, "candidate-intake.json")
    active = activate_candidate_profile(
        validate_candidate_intake(
            {
                "schema_version": 1,
                "documents": [],
                "approved_facts": {"targets.smart_queue_capacity": 5},
                "unknown_fields": [],
                "contradictions": [],
                "pending_facts": [],
            }
        ),
        actor="user",
    )
    intake_path.write_text(json.dumps(active), encoding="utf-8")
    return intake_path


def _valid_config(tmp_path: Path, **overrides: object) -> DaemonConfig:
    """Build the public host-only configuration accepted by the daemon."""

    payload: dict[str, object] = {
        "database_path": str(_private_database(tmp_path)),
        "active_intake_path": str(_active_intake(tmp_path)),
        "bridge": {"adapter": "host-provided"},
    }
    payload.update(overrides)
    return DaemonConfig.from_mapping(payload)


def _factory_for(
    monitor: FakeMonitor,
    calls: list[object],
) -> Callable[[object], FakeMonitor]:
    def factory(coordinator: object) -> FakeMonitor:
        calls.append(coordinator)
        return monitor

    return factory


@pytest.mark.parametrize(
    ("config_factory", "private_value"),
    (
        (
            lambda tmp_path: DaemonConfig.from_mapping(
                {
                    "database_path": str(tmp_path / "outside-private.sqlite3"),
                    "active_intake_path": str(_active_intake(tmp_path)),
                    "bridge": {"adapter": "host-provided"},
                }
            ),
            "outside-private.sqlite3",
        ),
        (
            lambda tmp_path: DaemonConfig.from_mapping(
                {
                    "database_path": str(_private_database(tmp_path)),
                    "active_intake_path": str(_private_database(tmp_path, "missing-active-intake.json")),
                    "bridge": {"adapter": "host-provided"},
                }
            ),
            "missing-active-intake.json",
        ),
        (
            lambda tmp_path: DaemonConfig.from_mapping(
                {
                    "database_path": str(_private_database(tmp_path)),
                    "active_intake_path": str(_active_intake(tmp_path)),
                    "bridge": {"adapter": "untrusted-command", "command": ["browser", "--debug"]},
                }
            ),
            "--debug",
        ),
    ),
    ids=("non-private-database", "missing-active-intake", "malformed-bridge-config"),
)
def test_invalid_configuration_fails_closed_before_monitor_or_adapter_use(
    tmp_path: Path,
    config_factory: Callable[[Path], DaemonConfig],
    private_value: str,
) -> None:
    adapter = FakeListingAdapter()
    monitor = FakeMonitor(())
    factory_calls: list[object] = []

    with pytest.raises(DaemonConfigurationError) as raised:
        SmartQueueDaemon.from_config(
            config_factory(tmp_path),
            adapter=adapter,
            monitor_factory=_factory_for(monitor, factory_calls),
        )

    assert private_value not in str(raised.value)
    assert factory_calls == []
    assert monitor.run_calls == []
    assert adapter.list_calls == 0
    assert adapter.open_calls == []


def test_daemon_accepts_a_finite_tick_limit_and_returns_count_only_redacted_status(tmp_path: Path) -> None:
    private_url = "https://www.linkedin.com/jobs/view/private-listing-123"
    monitor = FakeMonitor(
        (
            FakeMonitorTick(("opaque-1",), ("opaque-1",), (), search_needed=2),
            FakeMonitorTick(("opaque-2",), (), ("opaque-2",), search_needed=1, degraded=True),
            FakeMonitorTick(("opaque-3",), ("opaque-3",), (), search_needed=0),
        )
    )
    adapter = FakeListingAdapter()
    factory_calls: list[object] = []
    daemon = SmartQueueDaemon.from_config(
        _valid_config(tmp_path),
        adapter=adapter,
        monitor_factory=_factory_for(monitor, factory_calls),
    )

    status = daemon.run(max_ticks=2)

    assert monitor.run_calls == [2]
    assert len(factory_calls) == 1
    assert asdict(status) == {
        "ticks_completed": 2,
        "requested_open_count": 2,
        "opened_count": 1,
        "open_failed_count": 1,
        "search_needed": 1,
        "degraded_tick_count": 1,
    }
    assert {field.name for field in fields(status)} == set(asdict(status))
    assert "opaque-" not in repr(status)
    assert private_url not in repr(status)
    assert adapter.list_calls == 1
    assert adapter.open_calls == []


def test_daemon_reports_search_needed_when_a_released_vacancy_has_no_admitted_inventory(tmp_path: Path) -> None:
    monitor = FakeMonitor((FakeMonitorTick((), (), (), search_needed=1),))
    adapter = FakeListingAdapter()
    factory_calls: list[object] = []
    daemon = SmartQueueDaemon.from_config(
        _valid_config(tmp_path),
        adapter=adapter,
        monitor_factory=_factory_for(monitor, factory_calls),
    )

    status = daemon.run(max_ticks=1)

    assert len(factory_calls) == 1
    assert status.requested_open_count == 0
    assert status.opened_count == 0
    assert status.search_needed == 1
    assert adapter.open_calls == []


def test_finite_run_stops_after_terminal_bridge_tick_and_releases_the_monitor_lease(tmp_path: Path) -> None:
    database = _private_database(tmp_path)
    timed_out_adapter = TimedOutLiveSnapshotAdapter()
    daemon = SmartQueueDaemon.from_config(
        _valid_config(tmp_path, database_path=str(database)),
        adapter=timed_out_adapter,
        queue_factory=lambda _intake, target_database: SmartJobQueue(target_database),
    )

    status = daemon.run(max_ticks=5)

    assert asdict(status) == {
        "ticks_completed": 1,
        "requested_open_count": 0,
        "opened_count": 0,
        "open_failed_count": 0,
        "search_needed": 0,
        "degraded_tick_count": 1,
    }
    assert timed_out_adapter.list_calls == 2
    # A second finite monitor can acquire the same queue lease after the
    # terminal adapter tick has stopped and released the first monitor.
    replacement = SmartQueueDaemon.from_config(
        _valid_config(tmp_path, database_path=str(database)),
        adapter=FakeListingAdapter(),
        queue_factory=lambda _intake, target_database: SmartJobQueue(target_database),
    )
    assert replacement.run(max_ticks=1).ticks_completed == 1


def test_config_parser_rejects_candidate_list_json_input_without_leaking_it() -> None:
    private_candidate_url = "https://www.indeed.com/viewjob?jk=private-candidate"
    candidate_list_json = json.dumps(
        {
            "database_path": "jobapply_agent/private/live.sqlite3",
            "active_intake_path": "jobapply_agent/private/candidate_intake.json",
            "bridge": {"adapter": "host-provided"},
            "candidates": [{"source_url": private_candidate_url}],
        }
    )

    with pytest.raises(DaemonConfigurationError) as raised:
        DaemonConfig.from_json(candidate_list_json)

    assert private_candidate_url not in str(raised.value)


def test_bridge_preflight_fails_before_queue_factory_or_monitor_creation(tmp_path: Path) -> None:
    adapter = UnavailableListingAdapter()
    monitor = FakeMonitor(())
    queue_factory_calls: list[tuple[Path, Path]] = []
    monitor_factory_calls: list[object] = []

    def queue_factory(intake: Path, database: Path) -> object:
        queue_factory_calls.append((intake, database))
        raise AssertionError("queue factory must not run after bridge preflight failure")

    with pytest.raises(DaemonConfigurationError) as raised:
        SmartQueueDaemon.from_config(
            _valid_config(tmp_path),
            adapter=adapter,
            monitor_factory=_factory_for(monitor, monitor_factory_calls),
            queue_factory=queue_factory,
        )

    assert "private bridge detail" not in str(raised.value)
    assert adapter.list_calls == 1
    assert adapter.open_calls == []
    assert queue_factory_calls == []
    assert monitor_factory_calls == []


def test_bridge_deadline_during_preflight_prevents_queue_creation_or_mutation(tmp_path: Path) -> None:
    adapter = TimedOutPreflightAdapter()
    database = _private_database(tmp_path)
    queue_factory_calls: list[tuple[Path, Path]] = []

    def queue_factory(intake: Path, target_database: Path) -> object:
        queue_factory_calls.append((intake, target_database))
        raise AssertionError("a bridge deadline must stop before queue creation")

    with pytest.raises(DaemonConfigurationError) as raised:
        SmartQueueDaemon.from_config(
            _valid_config(tmp_path, database_path=str(database)),
            adapter=adapter,
            queue_factory=queue_factory,
        )

    assert "private deadline diagnostic" not in str(raised.value)
    assert adapter.list_calls == 1
    assert adapter.open_calls == []
    assert queue_factory_calls == []
    assert not database.exists()


def test_live_snapshot_failure_emits_degraded_bridge_tick_without_exiting(tmp_path: Path) -> None:
    adapter = FailingLiveSnapshotAdapter()
    cancellation = threading.Event()
    emitted: list[object] = []

    def queue_factory(_intake: Path, database: Path) -> SmartJobQueue:
        return SmartJobQueue(database)

    daemon = SmartQueueDaemon.from_config(
        _valid_config(tmp_path),
        adapter=adapter,
        queue_factory=queue_factory,
    )

    def capture_status(status: object) -> None:
        emitted.append(status)
        cancellation.set()

    daemon.run_forever(cancellation, capture_status)

    assert (
        _DAEMON_MODULE.SmartQueueCoordinator
        is _DAEMON_MODULE._monitor_module.SmartQueueCoordinator
    )
    assert len(emitted) == 1
    assert emitted[0].degraded is True
    assert emitted[0].bridge_error is True
    assert emitted[0].candidate_provider_error is False
    assert "private live snapshot detail" not in repr(emitted[0])
    assert adapter.list_calls == 2
    assert adapter.open_calls == []


def test_live_bridge_deadline_emits_only_count_level_degraded_status_and_finishes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    adapter = TimedOutLiveSnapshotAdapter()
    cancellation = threading.Event()
    emitted: list[object] = []

    daemon = SmartQueueDaemon.from_config(
        _valid_config(tmp_path),
        adapter=adapter,
        queue_factory=lambda _intake, database: SmartJobQueue(database),
    )

    daemon.run_forever(cancellation, emitted.append)
    _DAEMON_MODULE._emit_status(emitted[0])

    assert json.loads(capsys.readouterr().out) == {
        "degraded_count": 1,
        "open_failed_count": 0,
        "opened_count": 0,
        "requested_open_count": 0,
        "search_needed": 0,
    }
    assert cancellation.is_set()
    assert len(emitted) == 1
    assert emitted[0].degraded is True
    assert emitted[0].bridge_error is True
    assert "private deadline diagnostic" not in repr(emitted[0])
    assert adapter.list_calls == 2
    assert adapter.open_calls == []


def test_cli_accepts_documented_relative_private_paths_with_stdio_bridge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeCodexAdapter(FakeListingAdapter):
        def __init__(self) -> None:
            super().__init__()

    relative_runtime = Path("jobapply_agent/private/test-smart-queue-daemon") / tmp_path.name
    intake = _active_intake(tmp_path)
    database = _private_database(tmp_path)
    assert intake.is_relative_to(Path(__file__).parents[1] / "jobapply_agent" / "private")
    assert database.is_relative_to(Path(__file__).parents[1] / "jobapply_agent" / "private")
    monkeypatch.setattr(_DAEMON_MODULE, "CodexChromeExtensionAdapter", FakeCodexAdapter)

    exit_code = _DAEMON_MODULE.main(
        [
            "--candidate-intake", str(relative_runtime / "candidate-intake.json"),
            "--database", str(relative_runtime / "queue.sqlite3"),
            "--bridge-stdio",
            "--max-ticks", "0",
        ]
    )

    captured = capsys.readouterr().out.strip()
    assert exit_code == 0
    assert json.loads(captured) == {
        "degraded_tick_count": 0,
        "open_failed_count": 0,
        "opened_count": 0,
        "requested_open_count": 0,
        "search_needed": 0,
        "ticks_completed": 0,
    }


def test_cli_initialization_failure_emits_no_ready_capable_status_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnavailableCodexAdapter(UnavailableListingAdapter):
        pass

    intake = _active_intake(tmp_path)
    database = _private_database(tmp_path)
    monkeypatch.setattr(_DAEMON_MODULE, "CodexChromeExtensionAdapter", UnavailableCodexAdapter)

    exit_code = _DAEMON_MODULE.main(
        [
            "--candidate-intake", str(intake),
            "--database", str(database),
            "--bridge-stdio",
            "--max-ticks", "0",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "private bridge detail" not in captured.err


@pytest.mark.parametrize(
    "legacy_argument",
    (
        ("--endpoint", "http://127.0.0.1:9876"),
        ("--token", "private-token-value-that-must-not-print"),
        ("--adapter-timeout-seconds", "10"),
    ),
)
def test_cli_rejects_legacy_http_bridge_arguments(legacy_argument: tuple[str, str]) -> None:
    with pytest.raises(SystemExit) as raised:
        _DAEMON_MODULE._parser().parse_args(
            [
                "--candidate-intake", "jobapply_agent/private/candidate-intake.json",
                "--database", "jobapply_agent/private/queue.sqlite3",
                "--bridge-stdio",
                *legacy_argument,
            ]
        )

    assert raised.value.code == 2
