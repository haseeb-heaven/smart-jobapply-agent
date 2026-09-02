"""Contract tests for the bounded live Smart Job Queue coordinator.

The coordinator is an orchestration boundary: it joins the persistent queue to
the two-method browser adapter protocol without gaining application-form or
browser-session authority.  All values below are synthetic listing URLs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from jobapply_agent.smart_queue import QueueCandidate, QueuePolicyError, SmartJobQueue


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


def _coordinator(tmp_path: Path, browser: SnapshotBrowser) -> tuple[SmartJobQueue, SmartQueueCoordinator]:
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3")
    return queue, SmartQueueCoordinator(queue, browser)


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


def test_missing_tab_reserves_slot_as_awaiting_outcome_and_is_not_reopened(tmp_path: Path):
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    candidates = [_candidate(number) for number in range(1, 7)]
    coordinator.cycle(candidates)
    missing = candidates[0]
    browser.visible_urls.remove(missing.source_url)
    browser.opened_urls.clear()

    coordinator.cycle(())

    assert queue.get(missing.job_id).state == "awaiting_outcome"
    assert browser.opened_urls == []
    assert queue.plan_refill(open_urls=browser.visible_urls).urls_to_open == ()

    coordinator.confirm_outcome(missing.job_id, "skipped", actor="user")
    coordinator.cycle(())

    assert browser.opened_urls == [candidates[5].source_url]


def test_cycle_uses_follow_up_snapshot_to_record_open_failures_without_waiting_reservations(tmp_path: Path):
    candidates = [_candidate(number) for number in range(1, 7)]
    browser = SnapshotBrowser(failing_urls={candidates[0].source_url})
    queue, coordinator = _coordinator(tmp_path, browser)

    coordinator.cycle(candidates)

    assert browser.list_calls >= 2
    assert browser.opened_urls == [candidate.source_url for candidate in candidates[:5]]
    assert queue.get(candidates[0].job_id).state == "open_failed"
    assert [queue.get(candidate.job_id).state for candidate in candidates[1:5]] == ["open"] * 4
    assert not any(queue.get(candidate.job_id).state == "waiting" for candidate in candidates)


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

    with pytest.raises(QueuePolicyError, match="open|awaiting"):
        coordinator.confirm_outcome(candidate.job_id, "skipped", actor="user")

    assert queue.get(candidate.job_id).state == state


@pytest.mark.parametrize("state", ("open", "awaiting_outcome"))
def test_direct_outcomes_accept_currently_open_or_awaiting_outcome_jobs(tmp_path: Path, state: str):
    candidate = _candidate(1)
    browser = SnapshotBrowser()
    queue, coordinator = _coordinator(tmp_path, browser)
    queue.add_recommendations([candidate])
    action = queue.plan_refill(open_urls=[])
    browser.visible_urls[:] = list(action.urls_to_open)
    queue.record_visible_snapshot(browser.visible_urls, actor="synthetic-bridge")
    if state == "awaiting_outcome":
        browser.visible_urls.clear()
        queue.record_visible_snapshot(browser.visible_urls, actor="synthetic-bridge")

    result = coordinator.confirm_outcome(candidate.job_id, "skipped", actor="user")

    assert result.state == "skipped"
    assert queue.get(candidate.job_id).state == "skipped"
