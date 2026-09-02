"""Public contracts for durable, candidate-owned Smart Queue memory.

These tests deliberately use only synthetic listing URLs and the public queue
and memory APIs.  They do not construct a browser adapter or inspect candidate
data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobapply_agent.candidate_memory import CandidateMemory, CandidateMemoryPolicyError
from jobapply_agent.smart_queue import QueueCandidate, SmartJobQueue


_PROFILE_REVISION = "candidate-memory-profile-v1"
_POLICY_REVISION = "candidate-memory-policy-v1"
_LINKEDIN_URL = "https://www.linkedin.com/jobs/view/910001?utm_source=synthetic"
_INDEED_URL = "https://in.indeed.com/viewjob?jk=910001&utm_source=synthetic"


def _candidate(job_id: str, url: str) -> QueueCandidate:
    return QueueCandidate(
        job_id=job_id,
        source_url=url,
        fit_score=95,
        eligible=True,
        decision="recommended",
        evidence=("synthetic candidate-approved evidence",),
        profile_revision=_PROFILE_REVISION,
        matcher_policy_revision=_POLICY_REVISION,
    )


def _queue_with_open_job(tmp_path: Path, *, job_id: str = "queue-job", url: str = _LINKEDIN_URL) -> tuple[SmartJobQueue, QueueCandidate]:
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3", target_size=1)
    candidate = _candidate(job_id, url)
    queue.add_recommendations([candidate])
    action = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(action.urls_to_open, actor="synthetic-browser-bridge")
    assert queue.get(job_id).state == "open"
    return queue, candidate


def test_replays_one_real_candidate_confirmed_queue_outcome_once_across_restart_and_a_fresh_queue(
    tmp_path: Path,
) -> None:
    queue, candidate = _queue_with_open_job(tmp_path)
    queue.confirm_outcome(candidate.job_id, "submitted", actor="user")
    queue_event = queue.confirmed_outcome_events()
    assert len(queue_event) == 1

    private_root = tmp_path / "private"
    memory_path = private_root / "candidate-memory.sqlite3"
    memory = CandidateMemory(memory_path, private_root=private_root)

    first_replay = memory.reconcile_queue_outcome(
        queue=queue,
        event=queue_event[0],
        actor="user",
        vacated=True,
    )

    assert first_replay.inserted is True
    assert first_replay.queue_id == queue.queue_id
    assert first_replay.event_id == queue_event[0].event_id
    assert first_replay.source_url == "https://www.linkedin.com/jobs/view/910001"
    assert first_replay.outcome == "submitted"
    assert memory.is_suppressed("https://WWW.LinkedIn.com/jobs/view/910001/?trk=synthetic") is True

    repeated_replay = memory.reconcile_queue_outcome(
        queue=queue,
        event=queue_event[0],
        actor="user",
        vacated=True,
    )
    assert repeated_replay.inserted is False
    assert repeated_replay.recorded_at == first_replay.recorded_at

    restarted = CandidateMemory(memory_path, private_root=private_root)
    restarted_replay = restarted.reconcile_queue_outcome(
        queue=queue,
        event=queue_event[0],
        actor="user",
        vacated=True,
    )
    assert restarted_replay.inserted is False

    fresh_queue, fresh_candidate = _queue_with_open_job(
        tmp_path / "fresh-queue",
        job_id="fresh-queue-job",
        url=_LINKEDIN_URL,
    )
    fresh_queue.confirm_outcome(fresh_candidate.job_id, "submitted", actor="user")
    fresh_event = fresh_queue.confirmed_outcome_events()[0]
    fresh_replay = restarted.reconcile_queue_outcome(
        queue=fresh_queue,
        event=fresh_event,
        actor="user",
        vacated=True,
    )
    assert fresh_replay.inserted is True
    assert fresh_replay.queue_id != first_replay.queue_id
    assert restarted.filter_unsuppressed_candidates([fresh_candidate]) == ()


def test_memory_keeps_canonical_linkedin_and_indeed_listing_identities_independent(tmp_path: Path) -> None:
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3", target_size=2)
    linkedin = _candidate("linkedin-job", _LINKEDIN_URL)
    indeed = _candidate("indeed-job", _INDEED_URL)
    queue.add_recommendations([linkedin, indeed])
    action = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(action.urls_to_open, actor="synthetic-browser-bridge")
    queue.confirm_outcome(linkedin.job_id, "submitted", actor="user")
    queue.confirm_outcome(indeed.job_id, "rejected", actor="user")

    private_root = tmp_path / "private"
    memory = CandidateMemory(private_root / "candidate-memory.sqlite3", private_root=private_root)
    queue_events = {event.job_id: event for event in queue.confirmed_outcome_events()}
    replayed = (
        memory.reconcile_queue_outcome(
            queue=queue,
            event=queue_events[linkedin.job_id],
            actor="user",
            vacated=True,
        ),
        memory.reconcile_queue_outcome(
            queue=queue,
            event=queue_events[indeed.job_id],
            actor="user",
            vacated=True,
        ),
    )

    assert {(entry.source_url, entry.outcome) for entry in replayed} == {
        ("https://www.linkedin.com/jobs/view/910001", "submitted"),
        ("https://in.indeed.com/viewjob?jk=910001", "rejected"),
    }
    assert memory.is_suppressed(_LINKEDIN_URL) is True
    assert memory.is_suppressed(_INDEED_URL) is True
    assert memory.filter_unsuppressed_candidates([linkedin, indeed]) == ()


@pytest.mark.parametrize(
    ("actor", "vacated"),
    (
        ("agent", True),
        ("", True),
        (None, True),
        ("user", False),
        ("user", None),
    ),
)
def test_candidate_outcome_requires_the_exact_user_actor_and_explicit_vacated_attestation(
    tmp_path: Path, actor: object, vacated: object
) -> None:
    queue, candidate = _queue_with_open_job(tmp_path)
    private_root = tmp_path / "private"
    memory = CandidateMemory(private_root / "candidate-memory.sqlite3", private_root=private_root)
    queue.confirm_outcome(candidate.job_id, "submitted", actor="user")
    event = queue.confirmed_outcome_events()[0]

    with pytest.raises(CandidateMemoryPolicyError):
        memory.reconcile_queue_outcome(
            queue=queue,
            event=event,
            actor=actor,
            vacated=vacated,
        )

    assert queue.get(candidate.job_id).state == "submitted"
    assert memory.is_suppressed(candidate.source_url) is False
