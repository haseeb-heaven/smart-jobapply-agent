"""Contract tests for the persistent, listing-only Smart Queue monitor.

The monitor is deliberately tested through its public ``tick`` and
``confirm_outcome`` seams.  The coordinator and browser below are fakes: no
browser process, page inspection, application interaction, or live queue data
is involved in these tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from jobapply_agent.smart_queue import QueueCandidate


_SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts"
_MONITOR_PATH = _SCRIPTS_DIR / "persistent_smart_queue_monitor.py"
_MONITOR_SPEC = importlib.util.spec_from_file_location("persistent_smart_queue_monitor", _MONITOR_PATH)
assert _MONITOR_SPEC and _MONITOR_SPEC.loader
_MONITOR_MODULE = importlib.util.module_from_spec(_MONITOR_SPEC)
sys.modules[_MONITOR_SPEC.name] = _MONITOR_MODULE
_MONITOR_SPEC.loader.exec_module(_MONITOR_MODULE)
PersistentSmartQueueMonitor = _MONITOR_MODULE.PersistentSmartQueueMonitor
DatabaseLease = _MONITOR_MODULE.DatabaseLease
MonitorLeaseError = _MONITOR_MODULE.MonitorLeaseError
QueueCoordinatorError = _MONITOR_MODULE.QueueCoordinatorError
MonitorTick = _MONITOR_MODULE.MonitorTick


_PROFILE_REVISION = "persistent-monitor-profile-v1"
_POLICY_REVISION = "persistent-monitor-policy-v1"


def _candidate(number: int) -> QueueCandidate:
    return QueueCandidate(
        job_id=f"persistent-monitor-job-{number}",
        source_url=f"https://www.linkedin.com/jobs/view/{80_000 + number}",
        fit_score=100 - number,
        eligible=True,
        decision="recommended",
        evidence=(f"synthetic evidence {number}",),
        profile_revision=_PROFILE_REVISION,
        matcher_policy_revision=_POLICY_REVISION,
    )


def _indeed_candidate(number: int) -> QueueCandidate:
    return QueueCandidate(
        job_id=f"persistent-monitor-indeed-job-{number}",
        source_url=f"https://www.indeed.com/viewjob?jk={80_000 + number}",
        fit_score=100 - number,
        eligible=True,
        decision="recommended",
        evidence=(f"synthetic Indeed evidence {number}",),
        profile_revision=_PROFILE_REVISION,
        matcher_policy_revision=_POLICY_REVISION,
    )


@dataclass(frozen=True, slots=True)
class FakeCycle:
    requested_open_job_ids: tuple[str, ...]
    opened_job_ids: tuple[str, ...]
    open_failed_job_ids: tuple[str, ...]
    search_needed: int = 0


class FakeListingAdapter:
    """URL-only browser double; it has no page or application-flow operations."""

    def __init__(self, *, fail_opens: bool = False) -> None:
        self.visible_urls: list[str] = []
        self.opened_urls: list[str] = []
        self.fail_opens = fail_opens

    def list_tab_urls(self) -> tuple[str, ...]:
        return tuple(self.visible_urls)

    def open_listing(self, url: str) -> None:
        self.opened_urls.append(url)
        if self.fail_opens:
            raise RuntimeError("synthetic bridge failure")
        self.visible_urls.append(url)


class FakeSmartQueueCoordinator:
    """Small coordinator double that preserves the queue lifecycle boundary.

    It models only the behavior that the monitor must preserve: missing tabs
    release their slots without inferring outcomes, pre-admitted candidates
    refill those slots, and an open failure is not a successful open.
    """

    def __init__(self, browser: FakeListingAdapter, *, target_size: int = 5) -> None:
        self.browser = browser
        self.target_size = target_size
        self.calls: list[tuple[QueueCandidate, ...]] = []
        self.outcome_calls: list[tuple[str, str, str, bool]] = []
        self.fail_cycles = 0
        self.failed_cycle_attempts: set[int] = set()
        self.cycle_attempts = 0
        self._candidates: dict[str, QueueCandidate] = {}
        self._states: dict[str, str] = {}

    def cycle(self, recommendations: tuple[QueueCandidate, ...] = ()) -> FakeCycle:
        self.cycle_attempts += 1
        if self.cycle_attempts in self.failed_cycle_attempts:
            raise QueueCoordinatorError("synthetic bridge follow-up failure")
        if self.fail_cycles:
            self.fail_cycles -= 1
            raise QueueCoordinatorError("synthetic bridge follow-up failure")
        recommendations = tuple(recommendations)
        self.calls.append(recommendations)
        for candidate in recommendations:
            self._candidates.setdefault(candidate.job_id, candidate)
            self._states.setdefault(candidate.job_id, "recommended")

        visible = set(self.browser.list_tab_urls())
        for job_id, candidate in self._candidates.items():
            if self._states[job_id] == "open" and candidate.source_url not in visible:
                self._states[job_id] = "released"

        occupied_states = {"open"}
        vacancies = self.target_size - sum(state in occupied_states for state in self._states.values())
        requested = tuple(
            candidate
            for candidate in self._candidates.values()
            if self._states[candidate.job_id] == "recommended"
        )[: max(0, vacancies)]

        opened: list[str] = []
        failed: list[str] = []
        for candidate in requested:
            try:
                self.browser.open_listing(candidate.source_url)
            except RuntimeError:
                self._states[candidate.job_id] = "open_failed"
                failed.append(candidate.job_id)
            else:
                self._states[candidate.job_id] = "open"
                opened.append(candidate.job_id)

        return FakeCycle(
            requested_open_job_ids=tuple(candidate.job_id for candidate in requested),
            opened_job_ids=tuple(opened),
            open_failed_job_ids=tuple(failed),
            search_needed=max(0, vacancies - len(requested)),
        )

    def confirm_outcome(
        self, job_id: str, outcome: str, *, actor: str, vacated: bool, candidate_memory: object
    ) -> None:
        if actor != "user" or vacated is not True:
            raise PermissionError("only the candidate may confirm an outcome")
        if outcome not in {"submitted", "rejected", "skipped"}:
            raise ValueError("unsupported synthetic outcome")
        self.outcome_calls.append((job_id, outcome, actor, vacated))
        self._states[job_id] = outcome


class FakeCandidateProvider:
    """Host candidate-provider double with deterministic failures and recovery."""

    def __init__(self, *responses: object) -> None:
        self._responses = list(responses)
        self.calls: list[int] = []

    def __call__(self, search_needed: int) -> tuple[QueueCandidate, ...]:
        self.calls.append(search_needed)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return tuple(response)


class BlockingFakeSmartQueueCoordinator:
    """Coordinator double that exposes overlap through events, not a browser."""

    def __init__(self) -> None:
        self.first_operation_entered = threading.Event()
        self.second_operation_entered = threading.Event()
        self.allow_first_operation_to_finish = threading.Event()
        self.operation_calls: list[str] = []
        self.max_active_operations = 0
        self._active_operations = 0
        self._lock = threading.Lock()

    def _enter(self, operation: str, *, block: bool = False) -> None:
        with self._lock:
            self.operation_calls.append(operation)
            self._active_operations += 1
            self.max_active_operations = max(self.max_active_operations, self._active_operations)
            is_first = len(self.operation_calls) == 1
        if is_first:
            self.first_operation_entered.set()
        else:
            self.second_operation_entered.set()
        if block:
            assert self.allow_first_operation_to_finish.wait(timeout=1)
        with self._lock:
            self._active_operations -= 1

    def cycle(self, recommendations: tuple[QueueCandidate, ...] = ()) -> FakeCycle:
        self._enter("cycle", block=True)
        return FakeCycle((), (), ())

    def confirm_outcome(
        self, job_id: str, outcome: str, *, actor: str, vacated: bool, candidate_memory: object
    ) -> None:
        if actor != "user" or vacated is not True:
            raise PermissionError("only the candidate may confirm an outcome")
        self._enter("confirm_outcome")


class FakeLease:
    """Non-blocking lease double for the monitor's single-tick ownership seam."""

    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        return self.available

    def release(self) -> None:
        self.release_calls += 1


def _monitor(
    browser: FakeListingAdapter,
    *,
    lease: FakeLease | None = None,
    target_size: int = 5,
    candidate_provider: object | None = None,
    interval_seconds: float = 60.0,
    max_backoff_seconds: float = 300.0,
) -> tuple[FakeSmartQueueCoordinator, PersistentSmartQueueMonitor]:
    coordinator = FakeSmartQueueCoordinator(browser, target_size=target_size)
    return coordinator, PersistentSmartQueueMonitor(
        coordinator,
        candidate_provider=candidate_provider,
        lease=lease or FakeLease(),
        interval_seconds=interval_seconds,
        max_backoff_seconds=max_backoff_seconds,
    )


def _load_discover_module() -> object:
    """Load the admission seam without invoking its CLI."""

    script_path = Path(__file__).parents[1] / "jobapply_agent" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("discover_for_monitor_lease_test", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _active_admission_intake(runtime_directory: Path) -> Path:
    """Create a complete synthetic active intake for lease-only admission coverage."""

    from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake

    professional = ["Python", "FastAPI", "REST APIs", "PostgreSQL", "unit testing"]
    payload = {
        "schema_version": 1,
        "documents": [],
        "approved_facts": {
            "experience": {"total_years": 3},
            "roles": {"include": ["Python Backend Developer"], "exclude_title_terms": ["senior"]},
            "skills": {
                "professional": professional,
                "personal_open_source": ["OpenAI"],
                "learning_or_exposure": ["Docker"],
                "evidence_by_skill": {skill: "professional" for skill in professional},
            },
        },
        "unknown_fields": [],
        "contradictions": [],
        "pending_facts": [],
    }
    runtime_directory.mkdir(parents=True, exist_ok=True)
    intake_path = runtime_directory / "candidate-intake.json"
    intake_path.write_text(
        json.dumps(activate_candidate_profile(validate_candidate_intake(payload), actor="user")),
        encoding="utf-8",
    )
    return intake_path


def _admission_export(discover: object, intake_path: Path, export_path: Path) -> None:
    profile = discover.active_candidate_profile(intake_path)
    row = {
        "schema_version": 2,
        "record_type": "recommended_job_for_human_review",
        "discovery_mode": "export_only",
        "application_actions": 0,
        "fingerprint": hashlib.sha256(b"synthetic-monitor-lease-admission").hexdigest(),
        "profile_revision": discover.candidate_profile_revision(profile),
        "matcher_policy_revision": discover.matcher_policy_revision(),
        "run_id": "synthetic-monitor-lease-run",
        "discovered_at": "2026-09-03T00:00:00+00:00",
        "search_url": "https://www.linkedin.com/jobs/search/?keywords=python",
        "platform": "linkedin",
        "title": "Python Backend Developer",
        "company": "Synthetic Queue Systems",
        "url": "https://www.linkedin.com/jobs/view/991001",
        "location": "",
        "work_mode": "",
        "posted_at": None,
        "score": 95,
        "decision": "recommended",
        "minimum_profile_fit_score": discover.MINIMUM_RECOMMENDED_SCORE,
        "threshold_met": True,
        "reasons": ["synthetic eligible evidence"],
        "gaps": [],
        "evidence_explanations": ["synthetic candidate-approved evidence"],
        "score_explanation": "synthetic deterministic score explanation",
        "human_action_required": "candidate reviews the listing manually",
    }
    export_path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")


def test_initial_tick_opens_the_candidate_selected_capacity_once() -> None:
    browser = FakeListingAdapter()
    coordinator, monitor = _monitor(browser)
    candidates = tuple(_candidate(number) for number in range(1, 8))

    result = monitor.tick(candidates)

    assert result is not None
    assert result.opened_job_ids == tuple(candidate.job_id for candidate in candidates[:5])
    assert browser.opened_urls == [candidate.source_url for candidate in candidates[:5]]
    assert coordinator.calls == [candidates]


def test_released_tabs_refill_from_pre_admitted_candidates_without_calling_the_provider() -> None:
    browser = FakeListingAdapter()
    provider = FakeCandidateProvider(AssertionError("provider must not be called while admitted inventory remains"))
    coordinator, monitor = _monitor(browser, candidate_provider=provider)
    candidates = tuple(_candidate(number) for number in range(1, 8))
    monitor.tick(candidates)
    for candidate in candidates[:2]:
        browser.visible_urls.remove(candidate.source_url)
    browser.opened_urls.clear()

    result = monitor.tick()
    repeated = monitor.tick()

    assert result is not None
    assert result.opened_job_ids == tuple(candidate.job_id for candidate in candidates[5:])
    assert result.requested_open_job_ids == tuple(candidate.job_id for candidate in candidates[5:])
    assert repeated is not None
    assert repeated.opened_job_ids == ()
    assert repeated.requested_open_job_ids == ()
    assert browser.opened_urls == [candidate.source_url for candidate in candidates[5:]]
    assert provider.calls == []
    assert coordinator.calls == [candidates, (), ()]


def test_delayed_user_outcomes_after_released_refill_do_not_open_duplicate_tabs() -> None:
    browser = FakeListingAdapter()
    _coordinator, monitor = _monitor(browser)
    candidates = tuple(_candidate(number) for number in range(1, 8))
    monitor.tick(candidates)
    for candidate in candidates[:2]:
        browser.visible_urls.remove(candidate.source_url)
    released_refill = monitor.tick()
    browser.opened_urls.clear()

    monitor.confirm_outcome(
        candidates[0].job_id, "submitted", actor="user", vacated=True, candidate_memory=object()
    )
    monitor.confirm_outcome(
        candidates[1].job_id, "skipped", actor="user", vacated=True, candidate_memory=object()
    )
    result = monitor.tick()

    assert result is not None
    assert released_refill is not None
    assert released_refill.opened_job_ids == (candidates[5].job_id, candidates[6].job_id)
    assert result.opened_job_ids == ()
    assert browser.opened_urls == []


def test_repeated_ticks_do_not_duplicate_existing_listing_opens() -> None:
    browser = FakeListingAdapter()
    _coordinator, monitor = _monitor(browser)
    candidates = tuple(_candidate(number) for number in range(1, 6))

    first = monitor.tick(candidates)
    second = monitor.tick()

    assert first is not None and first.opened_job_ids == tuple(candidate.job_id for candidate in candidates)
    assert second is not None and second.opened_job_ids == ()
    assert browser.opened_urls == [candidate.source_url for candidate in candidates]


def test_retry_after_bridge_follow_up_failure_opens_no_duplicate_listing() -> None:
    browser = FakeListingAdapter()
    coordinator, monitor = _monitor(browser)
    candidates = tuple(_candidate(number) for number in range(1, 6))
    monitor.tick(candidates)
    coordinator.fail_cycles = 1

    failed = monitor.tick()
    recovered = monitor.tick()

    assert failed is not None and failed.bridge_error is True
    assert failed.opened_job_ids == ()
    assert recovered is not None and recovered.opened_job_ids == ()
    assert browser.opened_urls == [candidate.source_url for candidate in candidates]


def test_provider_failure_preserves_provider_specific_retry_state_then_a_later_tick_recovers() -> None:
    browser = FakeListingAdapter()
    private_failure = RuntimeError("synthetic provider token=not-for-status")
    candidate = _candidate(1)
    provider = FakeCandidateProvider(private_failure, (candidate,))
    _coordinator, monitor = _monitor(
        browser,
        target_size=1,
        candidate_provider=provider,
        interval_seconds=2,
        max_backoff_seconds=8,
    )

    failed = monitor.tick()
    recovered = monitor.tick()

    assert failed is not None
    assert failed.candidate_provider_called is True
    assert failed.candidate_provider_error is True
    assert failed.bridge_error is False
    assert failed.degraded is True
    assert failed.search_needed == 1
    assert failed.consecutive_failures == 1
    assert failed.consecutive_bridge_errors == 0
    assert failed.next_delay_seconds == 2
    assert failed.requested_open_job_ids == ()
    assert failed.opened_job_ids == ()
    assert failed.open_failed_job_ids == ()
    assert "token=not-for-status" not in repr(asdict(failed))
    assert recovered is not None
    assert recovered.bridge_error is False
    assert recovered.candidate_provider_error is False
    assert recovered.degraded is False
    assert recovered.search_needed == 0
    assert recovered.consecutive_failures == 0
    assert recovered.consecutive_bridge_errors == 0
    assert recovered.next_delay_seconds == 2
    assert recovered.candidate_provider_called is True
    assert recovered.opened_job_ids == (candidate.job_id,)
    assert provider.calls == [1, 1]


def test_provider_success_then_refill_bridge_failure_preserves_initial_cycle_reporting() -> None:
    browser = FakeListingAdapter()
    initial_candidate = _candidate(1)
    refill_candidate = _candidate(2)
    provider = FakeCandidateProvider((refill_candidate,))
    coordinator, monitor = _monitor(
        browser,
        target_size=2,
        candidate_provider=provider,
        interval_seconds=2,
        max_backoff_seconds=8,
    )
    coordinator.failed_cycle_attempts.add(2)

    failed = monitor.tick((initial_candidate,))

    assert failed is not None
    assert failed.candidate_provider_called is True
    assert failed.candidate_provider_error is False
    assert failed.bridge_error is True
    assert failed.degraded is True
    assert failed.search_needed == 1
    assert failed.requested_open_job_ids == (initial_candidate.job_id,)
    assert failed.opened_job_ids == (initial_candidate.job_id,)
    assert failed.open_failed_job_ids == ()
    assert failed.consecutive_failures == 1
    assert failed.consecutive_bridge_errors == 1
    assert failed.next_delay_seconds == 2
    assert provider.calls == [1]
    assert coordinator.calls == [(initial_candidate,)]
    assert browser.opened_urls == [initial_candidate.source_url]


def test_successful_two_cycle_tick_aggregates_initial_and_refill_opens_once() -> None:
    browser = FakeListingAdapter()
    initial_candidate = _candidate(1)
    refill_candidate = _candidate(2)
    provider = FakeCandidateProvider((refill_candidate,))
    coordinator, monitor = _monitor(browser, target_size=2, candidate_provider=provider)

    result = monitor.tick((initial_candidate,))

    assert result is not None
    assert result.candidate_provider_called is True
    assert result.search_needed == 0
    assert result.requested_open_job_ids == (initial_candidate.job_id, refill_candidate.job_id)
    assert result.opened_job_ids == (initial_candidate.job_id, refill_candidate.job_id)
    assert result.open_failed_job_ids == ()
    assert result.requested_open_count == 2
    assert result.opened_count == 2
    assert provider.calls == [1]
    assert coordinator.calls == [(initial_candidate,), (refill_candidate,)]
    assert browser.opened_urls == [initial_candidate.source_url, refill_candidate.source_url]


def test_provider_error_after_bridge_error_resets_bridge_streak_and_increments_generic_failures() -> None:
    browser = FakeListingAdapter()
    provider = FakeCandidateProvider(RuntimeError("synthetic provider failure"))
    coordinator, monitor = _monitor(
        browser,
        target_size=1,
        candidate_provider=provider,
        interval_seconds=2,
        max_backoff_seconds=8,
    )
    coordinator.fail_cycles = 1

    bridge_failed = monitor.tick()
    provider_failed = monitor.tick()

    assert bridge_failed is not None
    assert bridge_failed.bridge_error is True
    assert bridge_failed.candidate_provider_error is False
    assert bridge_failed.consecutive_failures == 1
    assert bridge_failed.consecutive_bridge_errors == 1
    assert provider_failed is not None
    assert provider_failed.bridge_error is False
    assert provider_failed.candidate_provider_error is True
    assert provider_failed.candidate_provider_called is True
    assert provider_failed.search_needed == 1
    assert provider_failed.consecutive_failures == 2
    assert provider_failed.consecutive_bridge_errors == 0
    assert provider_failed.next_delay_seconds == 4
    assert provider.calls == [1]


def test_tick_and_candidate_outcome_confirmation_cannot_overlap() -> None:
    coordinator = BlockingFakeSmartQueueCoordinator()
    monitor = PersistentSmartQueueMonitor(coordinator, lease=FakeLease())
    errors: list[BaseException] = []
    second_operation_attempted = threading.Event()

    def run_tick() -> None:
        try:
            monitor.tick()
        except BaseException as error:  # pragma: no cover - surfaced by the assertion below.
            errors.append(error)

    def confirm_outcome() -> None:
        second_operation_attempted.set()
        try:
            monitor.confirm_outcome(
                "opaque-job-id", "submitted", actor="user", vacated=True, candidate_memory=object()
            )
        except BaseException as error:  # pragma: no cover - surfaced by the assertion below.
            errors.append(error)

    first = threading.Thread(target=run_tick)
    second = threading.Thread(target=confirm_outcome)
    first.start()
    assert coordinator.first_operation_entered.wait(timeout=1)
    second.start()
    assert second_operation_attempted.wait(timeout=1)

    assert not coordinator.second_operation_entered.wait(timeout=0.1)
    coordinator.allow_first_operation_to_finish.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert coordinator.operation_calls == ["cycle", "confirm_outcome"]
    assert coordinator.max_active_operations == 1


def test_transformed_indeed_continuation_with_nonvacating_user_outcome_opens_no_replacement() -> None:
    browser = FakeListingAdapter()
    coordinator, monitor = _monitor(browser, target_size=1)
    indeed_listing = _indeed_candidate(1)
    replacement = _candidate(2)
    monitor.tick((indeed_listing, replacement))
    browser.visible_urls[:] = [
        "https://www.indeed.com/applystart?jk=80001&from=jobsearch&continue=synthetic"
    ]
    monitor.tick()
    browser.opened_urls.clear()

    try:
        outcome = monitor.confirm_outcome(
            indeed_listing.job_id,
            "submitted",
            actor="user",
            vacated=False,
            candidate_memory=object(),
        )
    except PermissionError:
        outcome = None
    result = monitor.tick()

    assert outcome is None
    assert coordinator.outcome_calls == []
    assert result is not None
    assert result.requested_open_job_ids == ()
    assert result.opened_job_ids == ()
    assert browser.opened_urls == []


def test_unrelated_tab_urls_do_not_change_managed_counts_or_leak_from_status() -> None:
    browser = FakeListingAdapter()
    _coordinator, monitor = _monitor(browser)
    candidates = tuple(_candidate(number) for number in range(1, 6))
    monitor.tick(candidates)
    unrelated_url = "https://unrelated.example.test/private-account?token=synthetic-secret"
    browser.visible_urls.append(unrelated_url)

    result = monitor.tick()

    assert result is not None
    assert result.requested_open_count == 0
    assert result.opened_count == 0
    assert unrelated_url not in repr(asdict(result))


def test_lease_contention_fails_closed_without_a_coordinator_cycle_or_open() -> None:
    browser = FakeListingAdapter()
    lease = FakeLease(available=False)
    coordinator, monitor = _monitor(browser, lease=lease)

    result = monitor.tick((_candidate(1),))

    assert result is None
    assert lease.acquire_calls == 1
    assert lease.release_calls == 0
    assert coordinator.calls == []
    assert browser.opened_urls == []


def test_monitor_lease_blocks_admission_without_queue_or_memory_mutation(tmp_path: Path) -> None:
    """Admission shares the monitor's durable sibling-file lease."""

    discover = _load_discover_module()
    runtime_directory = (
        Path(__file__).parents[1]
        / "jobapply_agent"
        / "private"
        / "test-persistent-smart-queue-monitor"
        / f"{tmp_path.parent.name}-{tmp_path.name}"
    )
    intake_path = _active_admission_intake(runtime_directory)
    export_path = runtime_directory / "discovery.jsonl"
    _admission_export(discover, intake_path, export_path)
    queue_path = runtime_directory / "smart-queue.sqlite3"
    memory_path = runtime_directory / "candidate-memory.sqlite3"

    with DatabaseLease(queue_path):
        with pytest.raises(discover.AdmissionError, match="queue admission failed"):
            discover.admit_current_recommendations_for_active_queue(
                intake_path,
                export_path,
                queue_path,
                memory_path,
            )

    assert queue_path.exists() is False
    assert memory_path.exists() is False


def test_database_lease_rejects_symlink_without_modifying_its_external_target(
    tmp_path: Path,
) -> None:
    runtime_directory = tmp_path / "private-runtime"
    runtime_directory.mkdir()
    database_path = runtime_directory / "smart-queue.sqlite3"
    external_target = tmp_path / "external-target"
    external_target.write_bytes(b"")
    lease = DatabaseLease(database_path)
    try:
        lease.path.symlink_to(external_target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"symlink creation is unavailable: {error}")

    with pytest.raises(MonitorLeaseError):
        lease.acquire()

    assert lease.held is False
    assert lease.path.is_symlink()
    assert external_target.read_bytes() == b""


def test_database_lease_rejects_a_live_owner_then_recovers_after_hard_process_death(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "smart-queue.sqlite3"
    holder_script = """
import importlib.util
from pathlib import Path
import sys
import threading

module_path = Path(sys.argv[1])
sys.path.insert(0, sys.argv[3])
spec = importlib.util.spec_from_file_location("lease_holder_monitor", module_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
lease = module.DatabaseLease(Path(sys.argv[2]))
lease.acquire()
print("lease-acquired", flush=True)
threading.Event().wait()
"""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(_MONITOR_PATH),
            str(database_path),
            str(Path(__file__).parents[1] / "jobapply_agent" / "src"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "lease-acquired"
        with pytest.raises(MonitorLeaseError, match="already owns"):
            DatabaseLease(database_path).acquire()

        holder.kill()
        assert holder.wait(timeout=5) != 0

        browser = FakeListingAdapter()
        coordinator = FakeSmartQueueCoordinator(browser, target_size=1)
        monitor = PersistentSmartQueueMonitor(
            coordinator,
            lease=DatabaseLease(database_path),
        )
        candidate = _candidate(1)

        result = monitor.tick((candidate,))

        assert result is not None
        assert result.opened_job_ids == (candidate.job_id,)
        assert coordinator.calls == [(candidate,)]
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)


def test_bridge_open_failure_never_claims_or_creates_an_open_listing() -> None:
    browser = FakeListingAdapter(fail_opens=True)
    _coordinator, monitor = _monitor(browser)
    candidate = _candidate(1)

    result = monitor.tick((candidate,))

    assert result is not None
    assert result.opened_job_ids == ()
    assert result.open_failed_job_ids == (candidate.job_id,)
    assert browser.visible_urls == []
    assert browser.opened_urls == [candidate.source_url]


def test_long_running_monitor_streams_ticks_without_retaining_results() -> None:
    browser = FakeListingAdapter()
    coordinator, monitor = _monitor(browser)
    cancellation = threading.Event()
    delivered: list[MonitorTick] = []

    def receive(result: MonitorTick) -> None:
        delivered.append(result)
        if len(delivered) == 3:
            cancellation.set()

    retained = monitor.run(cancellation, result_sink=receive)

    assert len(delivered) == 3
    assert all(result.opened_count == 0 for result in delivered)
    assert coordinator.calls == [(), (), ()]
    assert retained == ()
