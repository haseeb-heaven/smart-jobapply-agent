"""Contract tests for the bounded live Smart Job Queue coordinator.

The coordinator is an orchestration boundary: it joins the persistent queue to
the two-method browser adapter protocol without gaining application-form or
browser-session authority.  All values below are synthetic listing URLs.
"""

from __future__ import annotations

from dataclasses import asdict, fields
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from jobapply_agent.candidate_memory import CandidateMemory
from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake
from jobapply_agent.smart_queue import QueueAction, QueueCandidate, QueuePolicyError, SmartJobQueue


_SCRIPTS_DIR = Path(__file__).parents[1] / "skills" / "easy-apply-tab-monitor" / "scripts"
_ADAPTER_PATH = _SCRIPTS_DIR / "browser_tab_adapter.py"
_ADAPTER_SPEC = importlib.util.spec_from_file_location("browser_tab_adapter", _ADAPTER_PATH)
assert _ADAPTER_SPEC and _ADAPTER_SPEC.loader
_ADAPTER_MODULE = importlib.util.module_from_spec(_ADAPTER_SPEC)
sys.modules[_ADAPTER_SPEC.name] = _ADAPTER_MODULE
_ADAPTER_SPEC.loader.exec_module(_ADAPTER_MODULE)
BrowserAdapterError = _ADAPTER_MODULE.BrowserAdapterError

_COORDINATOR_PATH = _SCRIPTS_DIR / "smart_queue_coordinator.py"
_COORDINATOR_SPEC = importlib.util.spec_from_file_location("smart_queue_coordinator", _COORDINATOR_PATH)
assert _COORDINATOR_SPEC and _COORDINATOR_SPEC.loader
_COORDINATOR_MODULE = importlib.util.module_from_spec(_COORDINATOR_SPEC)
sys.modules[_COORDINATOR_SPEC.name] = _COORDINATOR_MODULE
_COORDINATOR_SPEC.loader.exec_module(_COORDINATOR_MODULE)
SmartQueueCoordinator = _COORDINATOR_MODULE.SmartQueueCoordinator
QueueCoordinatorError = _COORDINATOR_MODULE.QueueCoordinatorError


_PROFILE_REVISION = "coordinator-profile-v1"
_POLICY_REVISION = "coordinator-policy-v1"


def _candidate(number: int, *, score: int | None = None) -> QueueCandidate:
    return QueueCandidate(
        job_id=f"coordinator-job-{number}",
        source_url=(
            f"https://WWW.LinkedIn.com/jobs/view/{70_000 + number}/?"
            "trk=public_jobs&utm_source=synthetic"
        ),
        fit_score=score if score is not None else 100 - number,
        eligible=True,
        decision="recommended",
        evidence=(f"synthetic verified evidence {number}",),
        profile_revision=_PROFILE_REVISION,
        matcher_policy_revision=_POLICY_REVISION,
    )


class SnapshotBrowser:
    """Structural BrowserTabAdapter fake with URL-only observation and opening."""

    smart_queue_adapter = "codex-chrome-extension"

    def __init__(self, *, visible_urls: tuple[str, ...] = (), failing_urls: set[str] | None = None) -> None:
        self.visible_urls = list(visible_urls)
        self.failing_urls = failing_urls or set()
        self.list_calls = 0
        self.opened_urls: list[str] = []

    def list_tab_urls(self) -> tuple[str, ...]:
        self.list_calls += 1
        return tuple(self.visible_urls)

    def open_listing(self, url: str) -> None:
        self.opened_urls.append(url)
        if url in self.failing_urls:
            raise BrowserAdapterError("synthetic browser bridge failure")
        self.visible_urls.append(url)


class OpenThenRaiseBrowser(SnapshotBrowser):
    """Bridge double for an ambiguous timeout after the browser opened a tab."""

    def open_listing(self, url: str) -> None:
        self.opened_urls.append(url)
        self.visible_urls.append(url)
        raise BrowserAdapterError("synthetic timeout after opening")


class FollowUpUnavailableBrowser(SnapshotBrowser):
    """Bridge double whose post-open snapshot is temporarily unavailable."""

    def list_tab_urls(self) -> tuple[str, ...]:
        self.list_calls += 1
        if self.list_calls == 2:
            raise BrowserAdapterError("synthetic unavailable follow-up snapshot")
        return tuple(self.visible_urls)


def _coordinator(
    tmp_path: Path, browser: SnapshotBrowser, *, target_size: int = 5
) -> tuple[SmartJobQueue, SmartQueueCoordinator]:
    runtime_name = f"{tmp_path.parent.name}-{tmp_path.name}"
    runtime_directory = (
        Path(__file__).parents[1]
        / "jobapply_agent"
        / "private"
        / "test-smart-queue-coordinator"
        / runtime_name
    )
    database = runtime_directory / "smart-queue.sqlite3"
    if target_size == 5:
        queue = SmartJobQueue(database)
    else:
        queue = _load_discover_module().smart_queue_for_active_intake(
            _active_intake_with_capacity(tmp_path, target_size),
            database,
        )
    return queue, SmartQueueCoordinator(queue, browser)


def _private_runtime_database(tmp_path: Path, name: str) -> Path:
    """Return an ignored runtime database path accepted by the live coordinator."""

    runtime_name = f"{tmp_path.parent.name}-{tmp_path.name}-{name}"
    return (
        Path(__file__).parents[1]
        / "jobapply_agent"
        / "private"
        / "test-smart-queue-coordinator"
        / runtime_name
        / "smart-queue.sqlite3"
    )


def _candidate_memory(queue: SmartJobQueue) -> CandidateMemory:
    return CandidateMemory(
        queue.database_path.with_name("candidate-memory.sqlite3"),
        private_root=queue.database_path.parent,
    )


def _load_discover_module():
    """Load the public active-intake queue factory without using a CLI host."""

    script_path = Path(__file__).parents[1] / "jobapply_agent" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("discover_for_live_queue_provenance", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _active_intake_with_capacity(tmp_path: Path, target_size: int) -> Path:
    """Create only synthetic, candidate-confirmed active capacity evidence."""

    payload = {
        "schema_version": 1,
        "documents": [],
        "approved_facts": {"targets.smart_queue_capacity": target_size},
        "unknown_fields": [],
        "contradictions": [],
        "pending_facts": [],
    }
    intake_path = tmp_path / "private" / "candidate_intake.json"
    intake_path.parent.mkdir(parents=True)
    intake_path.write_text(
        json.dumps(activate_candidate_profile(validate_candidate_intake(payload), actor="user")),
        encoding="utf-8",
    )
    return intake_path


def _assert_redacted_public_cycle(result: object, *, search_needed: int) -> None:
    """Assert that the public coordinator result never leaks private URL work."""

    payload = asdict(result)
    assert result.search_needed == search_needed
    assert not {"initial_snapshot", "follow_up_snapshot", "initial_action", "refill_action", "requested_action"} & set(
        payload
    )
    assert not any(isinstance(getattr(result, field.name), QueueAction) for field in fields(result))
    assert "https://" not in repr(payload)
    assert "http://" not in repr(payload)


def _active_admission_intake(runtime_directory: Path) -> Path:
    """Write an active synthetic intake in the admission service's private root."""

    professional = ["Python", "FastAPI", "REST APIs", "PostgreSQL", "unit testing"]
    payload = {
        "schema_version": 1,
        "documents": [],
        "approved_facts": {
            "experience": {"total_years": 3},
            "roles": {
                "include": ["Python Backend Developer"],
                "exclude_title_terms": ["senior"],
            },
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


def _admission_row(discover: object, *, number: int, profile_revision: str, policy_revision: str) -> dict[str, object]:
    """Return one strict, synthetic current-revision discovery export row."""

    return {
        "schema_version": 2,
        "record_type": "recommended_job_for_human_review",
        "discovery_mode": "export_only",
        "application_actions": 0,
        "fingerprint": hashlib.sha256(f"synthetic-admission-{number}".encode()).hexdigest(),
        "profile_revision": profile_revision,
        "matcher_policy_revision": policy_revision,
        "run_id": "synthetic-admission-run",
        "discovered_at": "2026-09-03T00:00:00+00:00",
        "search_url": "https://www.linkedin.com/jobs/search/?keywords=python",
        "platform": "linkedin",
        "title": "Python Backend Developer",
        "company": "Synthetic Queue Systems",
        "url": f"https://www.linkedin.com/jobs/view/{990000 + number}",
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


def test_five_admitted_current_rows_refill_five_url_only_slots(tmp_path: Path) -> None:
    """Admission is the only source of the five canonical URL-only opens."""

    discover = _load_discover_module()
    runtime_directory = _private_runtime_database(tmp_path, "admission-refill").parent
    intake_path = _active_admission_intake(runtime_directory)
    profile = discover.active_candidate_profile(intake_path)
    profile_revision = discover.candidate_profile_revision(profile)
    policy_revision = discover.matcher_policy_revision()
    rows = [
        _admission_row(
            discover,
            number=number,
            profile_revision=profile_revision,
            policy_revision=policy_revision,
        )
        for number in range(1, 6)
    ]
    export_path = runtime_directory / "discovery.jsonl"
    export_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    queue_path = runtime_directory / "smart-queue.sqlite3"
    memory_path = runtime_directory / "candidate-memory.sqlite3"

    status = discover.admit_current_recommendations_for_active_queue(
        intake_path,
        export_path,
        queue_path,
        memory_path,
    )
    browser = SnapshotBrowser()
    queue = discover.smart_queue_for_active_intake(intake_path, queue_path)
    coordinator = SmartQueueCoordinator(queue, browser)
    result = coordinator.cycle()

    expected_urls = [row["url"] for row in rows]
    assert status.validated_count == 5
    assert status.suppressed_count == 0
    assert status.admitted_count == 5
    assert len(result.opened_job_ids) == 5
    assert browser.opened_urls == expected_urls
    assert len(browser.visible_urls) == 5


def test_cycle_opens_at_most_five_exact_canonical_listing_urls_from_caller_recommendations(tmp_path: Path):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidates = [_candidate(number) for number in range(1, 8)]

    coordinator.cycle(candidates)

    expected_urls = [candidate.source_url for candidate in candidates[:5]]
    assert browser.opened_urls == expected_urls
    assert len(browser.opened_urls) == 5
    assert browser.list_calls >= 2  # A post-open snapshot verifies every claimed open.
    assert [queue.get(candidate.job_id).state for candidate in candidates[:5]] == ["open"] * 5
    assert [queue.get(candidate.job_id).state for candidate in candidates[5:]] == ["recommended"] * 2


def test_cycle_rejects_mismatched_revision_recommendations_without_opening(tmp_path: Path):
    """The coordinator inherits the queue's active-pair admission check.

    A bound queue admits only its active pair, so caller-supplied
    recommendations from another revision fail closed atomically: no rows
    are stored, no tabs open, and no snapshot is consumed for them.
    """
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    coordinator.cycle([_candidate(1)])

    mismatched = QueueCandidate(
        job_id="coordinator-job-stale",
        source_url="https://www.linkedin.com/jobs/view/70999",
        fit_score=99,
        eligible=True,
        decision="recommended",
        evidence=("synthetic verified evidence stale",),
        profile_revision="coordinator-profile-stale",
        matcher_policy_revision=_POLICY_REVISION,
    )
    with pytest.raises(QueuePolicyError, match="conflict"):
        coordinator.cycle([mismatched])

    assert queue.active_revisions == (_PROFILE_REVISION, _POLICY_REVISION)
    with pytest.raises(KeyError):
        queue.get(mismatched.job_id)
    assert browser.opened_urls == [queue.get("coordinator-job-1").source_url]


@pytest.mark.parametrize("target_size", (1, 3, 10))
def test_cycle_opens_exactly_the_candidate_selected_number_of_listing_tabs(
    tmp_path: Path, target_size: int
):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser, target_size=target_size)
    candidates = [_candidate(number) for number in range(1, 13)]

    result = coordinator.cycle(candidates)

    expected = candidates[:target_size]
    assert result.requested_open_job_ids == tuple(candidate.job_id for candidate in expected)
    assert result.opened_job_ids == tuple(candidate.job_id for candidate in expected)
    assert browser.opened_urls == [candidate.source_url for candidate in expected]
    assert len(browser.visible_urls) == target_size
    assert {queue.get(candidate.job_id).state for candidate in expected} == {"open"}


def test_manual_close_immediately_refills_from_distinct_pre_admitted_recommendations(
    tmp_path: Path,
):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser, target_size=3)
    candidates = [_candidate(number) for number in range(1, 6)]
    coordinator.cycle(candidates)

    manually_closed = candidates[:2]
    for candidate in manually_closed:
        browser.visible_urls.remove(candidate.source_url)
    browser.opened_urls.clear()
    refilled = coordinator.cycle(())

    expected_replacements = candidates[3:5]
    assert refilled.opened_job_ids == tuple(candidate.job_id for candidate in expected_replacements)
    assert browser.opened_urls == [candidate.source_url for candidate in expected_replacements]
    assert {queue.get(candidate.job_id).state for candidate in manually_closed} == {"released"}
    assert len(browser.visible_urls) == 3

    for candidate in manually_closed:
        coordinator.confirm_outcome(
            candidate.job_id, "submitted", actor="user", vacated=True, candidate_memory=_candidate_memory(queue)
        )
    repeated = coordinator.cycle(())

    assert repeated.opened_job_ids == ()
    assert browser.opened_urls == [candidate.source_url for candidate in expected_replacements]
    assert len(browser.visible_urls) == 3


def test_missing_tab_releases_slot_and_never_reopens_the_original_listing(tmp_path: Path):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidates = [_candidate(number) for number in range(1, 7)]
    coordinator.cycle(candidates)
    missing = candidates[0]
    browser.visible_urls.remove(missing.source_url)
    browser.opened_urls.clear()

    refilled = coordinator.cycle(())

    assert queue.get(missing.job_id).state == "released"
    assert refilled.opened_job_ids == (candidates[5].job_id,)
    assert browser.opened_urls == [candidates[5].source_url]
    assert missing.source_url not in browser.opened_urls


def test_cycle_reports_no_post_attempt_shortage_when_a_failed_open_has_an_admitted_reserve(tmp_path: Path):
    candidates = [_candidate(number) for number in range(1, 7)]
    browser = SnapshotBrowser(failing_urls={candidates[0].source_url})
    queue, coordinator = _coordinator(tmp_path, browser)

    result = coordinator.cycle(candidates)

    assert browser.list_calls >= 2
    assert browser.opened_urls == [candidate.source_url for candidate in candidates[:5]]
    assert queue.get(candidates[0].job_id).state == "open_failed"
    assert [queue.get(candidate.job_id).state for candidate in candidates[1:5]] == ["open"] * 4
    assert queue.get(candidates[5].job_id).state == "recommended"
    assert not any(queue.get(candidate.job_id).state == "waiting" for candidate in candidates)
    _assert_redacted_public_cycle(result, search_needed=0)


def test_cycle_reports_new_post_attempt_shortage_when_a_failed_open_has_no_admitted_reserve(tmp_path: Path):
    candidates = [_candidate(number) for number in range(1, 6)]
    browser = SnapshotBrowser(failing_urls={candidates[0].source_url})
    queue, coordinator = _coordinator(tmp_path, browser)

    result = coordinator.cycle(candidates)

    assert browser.opened_urls == [candidate.source_url for candidate in candidates]
    assert queue.get(candidates[0].job_id).state == "open_failed"
    assert [queue.get(candidate.job_id).state for candidate in candidates[1:]] == ["open"] * 4
    assert not any(queue.get(candidate.job_id).state == "waiting" for candidate in candidates)
    _assert_redacted_public_cycle(result, search_needed=1)


def test_open_then_raise_is_reconciled_as_open_when_follow_up_snapshot_contains_the_url(tmp_path: Path):
    browser = OpenThenRaiseBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidate = _candidate(1)

    result = coordinator.cycle([candidate])

    assert result.opened_job_ids == (candidate.job_id,)
    assert result.open_failed_job_ids == ()
    assert queue.get(candidate.job_id).state == "open"
    assert [event.name for event in queue.history_for(candidate.job_id)] == ["recommended", "waiting", "open"]


def test_unavailable_follow_up_snapshot_preserves_waiting_reservation_until_a_later_snapshot_confirms_it(tmp_path: Path):
    browser = FollowUpUnavailableBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidate = _candidate(1)

    with pytest.raises(QueueCoordinatorError, match="snapshot"):
        coordinator.cycle([candidate])

    assert queue.get(candidate.job_id).state == "waiting"
    assert [event.name for event in queue.history_for(candidate.job_id)] == ["recommended", "waiting"]
    assert queue.confirmed_outcome_events() == ()

    browser.opened_urls.clear()
    resumed = coordinator.cycle(())

    assert resumed.requested_open_job_ids == ()
    assert browser.opened_urls == []
    assert queue.get(candidate.job_id).state == "open"
    assert [event.name for event in queue.history_for(candidate.job_id)] == ["recommended", "waiting", "open"]


def test_restart_releases_stale_waiting_reservations_and_refills_only_with_distinct_admitted_jobs(
    tmp_path: Path,
) -> None:
    browser = SnapshotBrowser()
    queue, _coordinator_before_process_death = _coordinator(tmp_path, browser)
    candidates = [_candidate(number) for number in range(1, 9)]
    queue.add_recommendations(candidates)
    reserved = queue.plan_refill(open_urls=())
    assert reserved.job_ids == tuple(candidate.job_id for candidate in candidates[:5])

    visible_before_process_death = candidates[:2]
    stale_reservations = candidates[2:5]
    admitted_reserves = candidates[5:8]
    browser.visible_urls[:] = [candidate.source_url for candidate in visible_before_process_death]

    restarted_queue = SmartJobQueue(queue.database_path)
    restarted_coordinator = SmartQueueCoordinator(restarted_queue, browser)
    resumed = restarted_coordinator.cycle(())

    assert resumed.requested_open_job_ids == tuple(candidate.job_id for candidate in admitted_reserves)
    assert resumed.opened_job_ids == tuple(candidate.job_id for candidate in admitted_reserves)
    assert browser.opened_urls == [candidate.source_url for candidate in admitted_reserves]
    assert not set(browser.opened_urls) & {candidate.source_url for candidate in candidates[:5]}
    assert {restarted_queue.get(candidate.job_id).state for candidate in stale_reservations} == {"open_failed"}
    assert {restarted_queue.get(candidate.job_id).state for candidate in admitted_reserves} == {"open"}
    assert all(
        [event.name for event in restarted_queue.history_for(candidate.job_id)]
        == ["recommended", "waiting", "open_failed"]
        for candidate in stale_reservations
    )
    assert restarted_queue.confirmed_outcome_events() == ()
    _assert_redacted_public_cycle(resumed, search_needed=0)


def test_failed_follow_up_snapshot_defers_waiting_reconciliation_until_next_reliable_initial_snapshot(
    tmp_path: Path,
) -> None:
    browser = FollowUpUnavailableBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidates = [_candidate(number) for number in range(1, 9)]

    with pytest.raises(QueueCoordinatorError, match="snapshot"):
        coordinator.cycle(candidates)

    assert {queue.get(candidate.job_id).state for candidate in candidates[:5]} == {"waiting"}
    still_visible = candidates[:2]
    no_longer_visible = candidates[2:5]
    admitted_reserves = candidates[5:8]
    browser.visible_urls[:] = [candidate.source_url for candidate in still_visible]
    browser.opened_urls.clear()

    resumed = coordinator.cycle(())

    assert {queue.get(candidate.job_id).state for candidate in still_visible} == {"open"}
    assert {queue.get(candidate.job_id).state for candidate in no_longer_visible} == {"open_failed"}
    assert resumed.requested_open_job_ids == tuple(candidate.job_id for candidate in admitted_reserves)
    assert resumed.opened_job_ids == tuple(candidate.job_id for candidate in admitted_reserves)
    assert browser.opened_urls == [candidate.source_url for candidate in admitted_reserves]
    assert not set(browser.opened_urls) & {candidate.source_url for candidate in candidates[:5]}
    assert queue.confirmed_outcome_events() == ()
    _assert_redacted_public_cycle(resumed, search_needed=0)


def test_failed_initial_open_never_records_an_outcome_and_next_cycle_uses_a_different_candidate(tmp_path: Path):
    candidates = [_candidate(number) for number in range(1, 7)]
    failed_candidate = candidates[0]
    browser = SnapshotBrowser(failing_urls={failed_candidate.source_url})
    queue, coordinator = _coordinator(tmp_path, browser)

    coordinator.cycle(candidates)

    assert queue.get(failed_candidate.job_id).state == "open_failed"
    assert [event.name for event in queue.history_for(failed_candidate.job_id)] == [
        "recommended",
        "waiting",
        "open_failed",
    ]
    assert queue.confirmed_outcome_events() == ()

    browser.opened_urls.clear()
    coordinator.cycle(())

    assert browser.opened_urls == [candidates[5].source_url]
    assert queue.get(candidates[5].job_id).state == "open"
    assert sum(queue.get(candidate.job_id).state == "open" for candidate in candidates) == 5


def test_confirmed_visible_tab_waits_for_later_url_only_snapshot_before_replacement(tmp_path: Path):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidates = [_candidate(number) for number in range(1, 7)]
    coordinator.cycle(candidates)
    confirmed = candidates[0]

    coordinator.confirm_outcome(
        confirmed.job_id, "submitted", actor="user", vacated=True, candidate_memory=_candidate_memory(queue)
    )
    browser.opened_urls.clear()
    coordinator.cycle(())

    assert queue.get(confirmed.job_id).state == "submitted"
    assert browser.opened_urls == []

    browser.visible_urls.remove(confirmed.source_url)
    coordinator.cycle(())

    assert browser.opened_urls == [candidates[5].source_url]
    assert queue.get(candidates[5].job_id).state == "open"


def test_restart_preserves_history_and_opens_one_replacement_for_four_visible_tabs(tmp_path: Path):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidates = [_candidate(number) for number in range(1, 7)]
    coordinator.cycle(candidates)
    confirmed = candidates[0]
    coordinator.confirm_outcome(
        confirmed.job_id, "submitted", actor="user", vacated=True, candidate_memory=_candidate_memory(queue)
    )
    browser.visible_urls.remove(confirmed.source_url)
    browser.opened_urls.clear()

    restarted = SmartJobQueue(queue.database_path)
    restarted_coordinator = SmartQueueCoordinator(restarted, browser)
    restarted_coordinator.cycle(())

    assert restarted.get(confirmed.job_id).state == "submitted"
    assert [event.name for event in restarted.history_for(confirmed.job_id)] == [
        "recommended",
        "waiting",
        "open",
        "submitted",
        "missing",
    ]
    assert browser.opened_urls == [candidates[5].source_url]
    assert sum(restarted.get(candidate.job_id).state == "open" for candidate in candidates) == 5


def test_live_coordinator_requires_a_queue_database_in_repository_private_runtime(tmp_path: Path):
    browser = SnapshotBrowser()

    with pytest.raises(QueueCoordinatorError, match="private runtime"):
        SmartQueueCoordinator(SmartJobQueue(tmp_path / "outside.sqlite3"), browser)
    with pytest.raises(QueueCoordinatorError, match="private runtime"):
        SmartQueueCoordinator(SmartJobQueue(":memory:"), browser)

    queue, coordinator = _coordinator(tmp_path, browser)
    assert coordinator is not None
    assert queue.database_path.resolve().is_relative_to(
        (Path(__file__).parents[1] / "jobapply_agent" / "private").resolve()
    )


def test_live_coordinator_rejects_direct_nondefault_queue_without_verified_intake_provenance(
    tmp_path: Path,
):
    """A host cannot invent a 3-tab live queue by calling SmartJobQueue directly."""

    browser = SnapshotBrowser()
    direct_queue = SmartJobQueue(_private_runtime_database(tmp_path, "direct-nondefault"), target_size=3)

    with pytest.raises(QueueCoordinatorError, match="provenance|active intake|candidate"):
        SmartQueueCoordinator(direct_queue, browser)

    assert browser.list_calls == 0
    assert browser.opened_urls == []


def test_live_coordinator_accepts_default_queue_without_nondefault_intake_provenance(tmp_path: Path):
    """The documented default remains usable when capacity is not explicitly selected."""

    browser = SnapshotBrowser()
    default_queue = SmartJobQueue(_private_runtime_database(tmp_path, "direct-default"))

    coordinator = SmartQueueCoordinator(default_queue, browser)

    assert default_queue.target_size == 5
    assert coordinator is not None
    assert browser.list_calls == 0
    assert browser.opened_urls == []


def test_live_coordinator_accepts_verified_active_intake_capacity_and_opens_exactly_three_tabs(
    tmp_path: Path,
):
    """A candidate-confirmed active profile is the authority for a 3-tab live queue."""

    discover = _load_discover_module()
    browser = SnapshotBrowser()
    queue = discover.smart_queue_for_active_intake(
        _active_intake_with_capacity(tmp_path, 3),
        _private_runtime_database(tmp_path, "verified-three"),
    )
    coordinator = SmartQueueCoordinator(queue, browser)
    candidates = [_candidate(number) for number in range(1, 5)]

    cycle = coordinator.cycle(candidates)

    assert queue.target_size == 3
    assert cycle.opened_job_ids == tuple(candidate.job_id for candidate in candidates[:3])
    assert browser.opened_urls == [candidate.source_url for candidate in candidates[:3]]
    assert len(browser.visible_urls) == 3


def test_verified_capacity_provenance_persists_and_direct_host_cannot_overwrite_it(tmp_path: Path):
    """Restarting may reuse verified policy, but cannot replace it with host configuration."""

    discover = _load_discover_module()
    database = _private_runtime_database(tmp_path, "persisted-provenance")
    discover.smart_queue_for_active_intake(_active_intake_with_capacity(tmp_path, 3), database)

    restarted = SmartJobQueue(database)
    restarted_coordinator = SmartQueueCoordinator(restarted, SnapshotBrowser())

    assert restarted.target_size == 3
    assert restarted_coordinator is not None
    with pytest.raises(QueuePolicyError, match="conflict|target_size|capacity"):
        SmartJobQueue(database, target_size=5)


def test_live_coordinator_rejects_a_database_path_that_escapes_private_runtime_via_symlink(tmp_path: Path):
    runtime_directory = Path(__file__).parents[1] / "jobapply_agent" / "private" / "test-smart-queue-symlink"
    escape = runtime_directory / tmp_path.parent.name / tmp_path.name / "escaped"
    escape.parent.mkdir(parents=True, exist_ok=True)
    escape.symlink_to(tmp_path, target_is_directory=True)
    queue = SmartJobQueue(escape / "outside.sqlite3")

    with pytest.raises(QueueCoordinatorError, match="private runtime"):
        SmartQueueCoordinator(queue, SnapshotBrowser())


def test_live_coordinator_accepts_any_bounded_listing_adapter(tmp_path: Path):
    class GenericListingAdapter:
        def list_tab_urls(self) -> tuple[str, ...]:
            return ()

        def open_listing(self, _url: str) -> None:
            pass

    queue, existing_coordinator = _coordinator(tmp_path, SnapshotBrowser())
    assert existing_coordinator is not None
    assert SmartQueueCoordinator(queue, GenericListingAdapter()) is not None

    watcher_spec = importlib.util.spec_from_file_location("legacy_watcher_for_coordinator_test", _SCRIPTS_DIR / "chrome_tab_watcher.py")
    assert watcher_spec and watcher_spec.loader
    watcher = importlib.util.module_from_spec(watcher_spec)
    sys.modules[watcher_spec.name] = watcher
    watcher_spec.loader.exec_module(watcher)
    assert SmartQueueCoordinator(queue, watcher.ChromeAppleScript(runner=lambda *_args, **_kwargs: None)) is not None


def test_live_coordinator_has_no_structural_dependency_on_legacy_adapter_factory():
    source = _COORDINATOR_PATH.read_text(encoding="utf-8")

    assert "chrome_tab_watcher" not in source
    assert "create_adapter" not in source
    assert "ChromeAppleScript" not in source


def test_invalid_recommendation_generator_cannot_strand_a_waiting_reservation(tmp_path: Path):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    existing = _candidate(1)
    queue.add_recommendations([existing])

    def invalid_recommendations():
        yield _candidate(2)
        yield object()

    with pytest.raises(QueuePolicyError, match="QueueCandidate"):
        coordinator.cycle(invalid_recommendations())

    assert queue.get(existing.job_id).state == "recommended"
    assert browser.list_calls == 0
    assert browser.opened_urls == []


def test_insufficient_pool_returns_only_counts_then_exact_caller_recommendations_restore_capacity(tmp_path: Path):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    initial = [_candidate(number) for number in range(1, 6)]
    replacements = [_candidate(number) for number in range(6, 8)]
    coordinator.cycle(initial)
    for candidate in initial[3:]:
        browser.visible_urls.remove(candidate.source_url)
    coordinator.cycle(())
    for candidate in initial[3:]:
        coordinator.confirm_outcome(
            candidate.job_id, "skipped", actor="user", vacated=True, candidate_memory=_candidate_memory(queue)
        )

    browser.opened_urls.clear()
    insufficient = coordinator.cycle(())

    _assert_redacted_public_cycle(insufficient, search_needed=2)
    assert browser.opened_urls == []

    restored = coordinator.cycle(replacements)

    _assert_redacted_public_cycle(restored, search_needed=0)
    assert browser.opened_urls == [candidate.source_url for candidate in replacements]
    assert sum(queue.get(candidate.job_id).state == "open" for candidate in [*initial, *replacements]) == 5


def test_coordinator_public_result_is_count_only_and_contains_no_browser_or_queue_action_data(tmp_path: Path):
    browser = SnapshotBrowser()
    _queue, coordinator = _coordinator(tmp_path, browser)

    result = coordinator.cycle([_candidate(1)])

    _assert_redacted_public_cycle(result, search_needed=4)


def test_cycle_rejects_duplicate_managed_canonical_listing_urls_in_one_snapshot(tmp_path: Path):
    candidate = _candidate(1)
    browser = SnapshotBrowser(visible_urls=(candidate.source_url, candidate.source_url))
    _queue, coordinator = _coordinator(tmp_path, browser)

    with pytest.raises(QueuePolicyError, match="duplicate"):
        coordinator.cycle([candidate])

    assert browser.opened_urls == []


@pytest.mark.parametrize("state", ("recommended", "waiting", "open_failed"))
def test_direct_outcomes_reject_non_visible_or_non_open_queue_states(tmp_path: Path, state: str):
    candidate = _candidate(1)
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    queue.add_recommendations([candidate])
    if state in {"waiting", "open_failed"}:
        action = queue.plan_refill(open_urls=[])
        assert action.job_ids == (candidate.job_id,)
    if state == "open_failed":
        queue.record_open_failure(candidate.job_id, actor="synthetic-bridge")

    with pytest.raises(QueuePolicyError, match="open|released"):
        coordinator.confirm_outcome(
            candidate.job_id, "skipped", actor="user", vacated=True, candidate_memory=_candidate_memory(queue)
        )

    assert queue.get(candidate.job_id).state == state


@pytest.mark.parametrize("state", ("open", "released"))
def test_direct_outcomes_accept_currently_open_or_released_jobs(tmp_path: Path, state: str):
    candidate = _candidate(1)
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    queue.add_recommendations([candidate])
    action = queue.plan_refill(open_urls=[])
    browser.visible_urls[:] = list(action.urls_to_open)
    queue.record_visible_snapshot(browser.visible_urls, actor="synthetic-bridge")
    if state == "released":
        browser.visible_urls.clear()
        queue.record_visible_snapshot(browser.visible_urls, actor="synthetic-bridge")

    result = coordinator.confirm_outcome(
        candidate.job_id, "skipped", actor="user", vacated=True, candidate_memory=_candidate_memory(queue)
    )

    assert result.state == "skipped"
    assert queue.get(candidate.job_id).state == "skipped"
