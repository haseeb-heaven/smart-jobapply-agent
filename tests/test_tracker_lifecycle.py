from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from job_profession.tracker_lifecycle import (
    InvalidLifecycleTransition,
    LifecycleTracker,
    RoundLimitExceeded,
)
from job_profession.smart_queue import QueueCandidate, SmartJobQueue


def _advance_to_opened(tracker: LifecycleTracker, job_id: str) -> None:
    tracker.shortlist(job_id, f"https://www.linkedin.com/jobs/view/{job_id}", actor="agent")
    tracker.record_tab_event(job_id, "opened", actor="agent")


def _advance_to_submitted(tracker: LifecycleTracker, job_id: str) -> None:
    _advance_to_opened(tracker, job_id)
    tracker.transition(job_id, "manual_applying", actor="user")
    tracker.transition(job_id, "submitted", actor="user")


def _advance_to_state_before(tracker: LifecycleTracker, job_id: str, target: str) -> None:
    _advance_to_opened(tracker, job_id)
    if target != "manual_applying":
        tracker.transition(job_id, "manual_applying", actor="user")
    if target in {"interview", "rejected", "offer", "withdrawn"}:
        tracker.transition(job_id, "submitted", actor="user")
    if target == "offer":
        tracker.transition(job_id, "interview", actor="user")


def _confirmed_queue_outcome(
    tmp_path: Path,
    *,
    job_id: str,
    source_url: str,
    outcome: str,
) -> tuple[SmartJobQueue, int]:
    queue = SmartJobQueue(tmp_path / f"{job_id}-queue.sqlite3")
    queue.add_recommendations(
        [
            QueueCandidate(
                job_id=job_id,
                source_url=source_url,
                fit_score=99,
                eligible=True,
                decision="recommended",
                evidence=("synthetic verified professional evidence",),
                profile_revision="lifecycle-profile-v1",
                matcher_policy_revision="lifecycle-policy-v1",
            )
        ]
    )
    queue.plan_refill(open_urls=[])
    queue.confirm_outcome(job_id, outcome, actor="user")
    return queue, queue.confirmed_outcome_events()[0].event_id


def test_lifecycle_supports_candidate_recorded_submission_and_outcomes(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")

    _advance_to_submitted(tracker, "job-offer")
    tracker.transition("job-offer", "interview", actor="user")
    offered = tracker.transition("job-offer", "offer", actor="user")

    _advance_to_submitted(tracker, "job-rejected")
    rejected = tracker.transition("job-rejected", "rejected", actor="user")

    _advance_to_submitted(tracker, "job-withdrawn")
    withdrawn = tracker.transition("job-withdrawn", "withdrawn", actor="user")

    assert offered.state == "offer"
    assert rejected.state == "rejected"
    assert withdrawn.state == "withdrawn"
    assert tracker.get_job("job-offer").state == "offer"


@pytest.mark.parametrize("target", ["submitted", "interview", "rejected", "offer", "withdrawn"])
def test_agent_cannot_record_submission_or_any_post_submission_outcome(tmp_path: Path, target: str):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    _advance_to_submitted(tracker, "job-1")

    with pytest.raises(InvalidLifecycleTransition, match="user"):
        tracker.transition("job-1", target, actor="agent")


@pytest.mark.parametrize("actor", ["USER", " user "])
@pytest.mark.parametrize("target", ["manual_applying", "submitted", "interview", "rejected", "offer", "withdrawn"])
def test_manual_lifecycle_transitions_require_literal_user_actor(
    tmp_path: Path, actor: str, target: str
):
    tracker = LifecycleTracker(tmp_path / f"lifecycle-{target}-{actor!r}.sqlite3")
    _advance_to_state_before(tracker, "job-1", target)
    submitted_count = tracker.unique_manual_submitted_count()

    with pytest.raises(InvalidLifecycleTransition, match="user"):
        tracker.transition("job-1", target, actor=actor)

    assert tracker.unique_manual_submitted_count() == submitted_count


@pytest.mark.parametrize("actor", ["USER", " user "])
def test_submitted_count_requires_literal_user_actor_in_stored_events(tmp_path: Path, actor: str):
    database = tmp_path / f"lifecycle-{actor!r}.sqlite3"
    tracker = LifecycleTracker(database)
    tracker.shortlist("job-1", "https://www.linkedin.com/jobs/view/job-1", actor="agent")

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO lifecycle_events (
                job_id, category, name, actor, payload_json, occurred_at
            ) VALUES (?, 'lifecycle', 'submitted', ?, '{}', '2026-09-01T00:00:00+00:00')
            """,
            ("job-1", actor),
        )

    assert tracker.unique_manual_submitted_count() == 0


@pytest.mark.parametrize("target", ["interview", "rejected", "offer", "withdrawn"])
def test_outcomes_cannot_be_recorded_before_manual_submission(tmp_path: Path, target: str):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    _advance_to_opened(tracker, "job-1")

    with pytest.raises(InvalidLifecycleTransition):
        tracker.transition("job-1", target, actor="user")


def test_tab_events_are_append_only_observations_and_never_imply_submission(tmp_path: Path):
    database = tmp_path / "lifecycle.sqlite3"
    tracker = LifecycleTracker(database)
    tracker.shortlist("job-1", "https://www.indeed.com/viewjob?jk=synthetic", actor="agent")

    opened = tracker.record_tab_event("job-1", "opened", actor="agent")
    reopened = tracker.record_tab_event("job-1", "reopened", actor="agent")
    closed = tracker.record_tab_event("job-1", "closed", actor="agent")

    assert tracker.get_job("job-1").state == "opened"
    assert [event.name for event in tracker.events_for("job-1") if event.category == "tab"] == [
        "opened",
        "reopened",
        "closed",
    ]
    assert opened.event_id < reopened.event_id < closed.event_id
    assert tracker.unique_manual_submitted_count() == 0

    reloaded = LifecycleTracker(database)
    assert [event.event_id for event in reloaded.events_for("job-1")] == [
        event.event_id for event in tracker.events_for("job-1")
    ]
    assert reloaded.get_job("job-1").state == "opened"


def test_agent_transition_and_tab_activity_cannot_imply_submission(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    _advance_to_opened(tracker, "job-1")
    tracker.record_tab_event("job-1", "closed", actor="agent")
    tracker.record_tab_event("job-1", "reopened", actor="agent")

    with pytest.raises(InvalidLifecycleTransition, match="user"):
        tracker.transition("job-1", "submitted", actor="agent")

    assert tracker.get_job("job-1").state == "opened"
    assert tracker.unique_manual_submitted_count() == 0


def test_review_round_accepts_at_most_five_unique_jobs_and_preserves_order(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    job_ids = ["job-1", "job-2", "job-3", "job-4", "job-5"]
    for job_id in job_ids:
        tracker.shortlist(job_id, f"https://www.linkedin.com/jobs/view/{job_id}", actor="agent")

    review_round = tracker.create_round("round-1", [*job_ids, "job-3"], actor="agent")

    assert review_round.round_id == "round-1"
    assert review_round.job_ids == tuple(job_ids)

    tracker.shortlist("job-6", "https://www.linkedin.com/jobs/view/job-6", actor="agent")
    with pytest.raises(RoundLimitExceeded, match="five"):
        tracker.create_round("round-2", [*job_ids, "job-6"], actor="agent")


def test_attention_blockers_and_follow_ups_are_attributed_append_only_records(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    tracker.shortlist("job-1", "https://www.linkedin.com/jobs/view/job-1", actor="agent")

    blocker = tracker.record_attention(
        "job-1",
        kind="blocker",
        message="Candidate must confirm work authorization.",
        actor="agent",
    )
    follow_up = tracker.record_follow_up(
        "job-1",
        due_at="2026-09-08T09:00:00+00:00",
        note="Candidate may check the employer portal manually.",
        actor="user",
    )

    events = tracker.events_for("job-1")
    assert blocker.category == "attention"
    assert blocker.name == "blocker"
    assert blocker.actor == "agent"
    assert blocker.payload == {"message": "Candidate must confirm work authorization."}
    assert follow_up.category == "follow_up"
    assert follow_up.actor == "user"
    assert follow_up.payload == {
        "due_at": "2026-09-08T09:00:00+00:00",
        "note": "Candidate may check the employer portal manually.",
    }
    assert [event.event_id for event in events] == sorted(event.event_id for event in events)


def test_unique_manual_submission_count_is_derived_from_first_user_submission_per_job(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    _advance_to_submitted(tracker, "job-1")
    _advance_to_submitted(tracker, "job-2")

    assert tracker.unique_manual_submitted_count() == 2
    with pytest.raises(InvalidLifecycleTransition):
        tracker.transition("job-1", "submitted", actor="user")

    assert tracker.unique_manual_submitted_count() == 2
    assert sum(
        event.category == "lifecycle" and event.name == "submitted" and event.actor == "user"
        for event in tracker.events_for("job-1")
    ) == 1


@pytest.mark.parametrize(
    "url",
    (
        "https://user@www.linkedin.com/jobs/view/job-1",
        "https://www.linkedin.com:443/jobs/view/job-1",
        "https://www.linkedin.com/jobs/view/job-1?session=secret",
        "https://in.indeed.com/viewjob?jk=job-1&password=secret",
    ),
)
def test_shortlist_rejects_credentials_ports_and_noncanonical_listing_urls(tmp_path: Path, url: str):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    with pytest.raises(ValueError, match="canonical"):
        tracker.shortlist("job-1", url, actor="agent")


def test_shortlist_canonicalizes_and_deduplicates_listing_identity(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    first = tracker.shortlist(
        "job-1",
        "https://WWW.LinkedIn.com/jobs/view/job-1/?trk=public_jobs&utm_source=agent",
        actor="agent",
    )

    assert first.source_url == "https://www.linkedin.com/jobs/view/job-1"
    with pytest.raises(InvalidLifecycleTransition, match="listing"):
        tracker.shortlist(
            "job-2",
            "https://www.linkedin.com/jobs/view/job-1?trk=another_public_source",
            actor="agent",
        )


def test_queue_outcome_reconciliation_is_transactional_idempotent_and_user_owned(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    source_url = "https://www.linkedin.com/jobs/view/job-1"
    queue, queue_event_id = _confirmed_queue_outcome(
        tmp_path,
        job_id="job-1",
        source_url=source_url,
        outcome="submitted",
    )

    first = tracker.reconcile_queue_outcome(
        queue=queue,
        queue_event_id=queue_event_id,
        job_id="job-1",
        source_url=source_url,
        outcome="submitted",
        actor="user",
    )
    replay = tracker.reconcile_queue_outcome(
        queue=queue,
        queue_event_id=queue_event_id,
        job_id="job-1",
        source_url=source_url + "?trk=public_jobs",
        outcome="submitted",
        actor="user",
    )

    assert first.state == replay.state == "submitted"
    assert tracker.unique_manual_submitted_count() == 1
    assert [event.name for event in tracker.events_for("job-1") if event.category == "lifecycle"] == [
        "shortlisted",
        "manual_applying",
        "submitted",
    ]
    assert [event.name for event in tracker.events_for("job-1") if event.category == "queue_outcome"] == [
        "submitted"
    ]

    with pytest.raises(InvalidLifecycleTransition, match="user"):
        tracker.reconcile_queue_outcome(
            queue=queue,
            queue_event_id=queue_event_id + 1,
            job_id="job-1",
            source_url=source_url,
            outcome="skipped",
            actor="agent",
        )
    with pytest.raises(InvalidLifecycleTransition, match="facts do not match"):
        tracker.reconcile_queue_outcome(
            queue=queue,
            queue_event_id=queue_event_id,
            job_id="job-1",
            source_url=source_url,
            outcome="skipped",
            actor="user",
        )


def test_queue_outcome_reconciliation_namespaces_colliding_event_ids(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    source_url_a = "https://www.linkedin.com/jobs/view/job-queue-a"
    source_url_b = "https://www.linkedin.com/jobs/view/job-queue-b"
    queue_a, queue_event_id_a = _confirmed_queue_outcome(
        tmp_path,
        job_id="job-queue-a",
        source_url=source_url_a,
        outcome="submitted",
    )
    queue_b, queue_event_id_b = _confirmed_queue_outcome(
        tmp_path,
        job_id="job-queue-b",
        source_url=source_url_b,
        outcome="submitted",
    )

    assert queue_event_id_a == queue_event_id_b
    assert queue_a.queue_id != queue_b.queue_id
    assert queue_a.confirmed_outcome_events()[0].queue_id == queue_a.queue_id
    assert queue_b.confirmed_outcome_events()[0].queue_id == queue_b.queue_id

    job_a = tracker.reconcile_queue_outcome(
        queue=queue_a,
        queue_event_id=queue_event_id_a,
        job_id="job-queue-a",
        source_url=source_url_a,
        outcome="submitted",
        actor="user",
    )
    job_b = tracker.reconcile_queue_outcome(
        queue=queue_b,
        queue_event_id=queue_event_id_b,
        job_id="job-queue-b",
        source_url=source_url_b,
        outcome="submitted",
        actor="user",
    )

    assert job_a.state == job_b.state == "submitted"
    assert tracker.unique_manual_submitted_count() == 2
    assert [
        event.payload
        for event in tracker.events_for("job-queue-b")
        if event.category == "queue_outcome"
    ] == [{"queue_event_id": queue_event_id_b, "queue_id": queue_b.queue_id}]


def test_queue_and_reconciliation_identities_survive_restart(tmp_path: Path):
    queue_database = tmp_path / "durable-queue.sqlite3"
    queue = SmartJobQueue(queue_database)
    queue.add_recommendations(
        [
            QueueCandidate(
                job_id="job-durable",
                source_url="https://www.linkedin.com/jobs/view/job-durable",
                fit_score=99,
                eligible=True,
                decision="recommended",
                evidence=("synthetic verified professional evidence",),
                profile_revision="lifecycle-profile-v1",
                matcher_policy_revision="lifecycle-policy-v1",
            )
        ]
    )
    queue_id = queue.queue_id
    queue.plan_refill(open_urls=[])
    queue.confirm_outcome("job-durable", "skipped", actor="user")

    reloaded = SmartJobQueue(queue_database)

    assert reloaded.queue_id == queue_id
    assert reloaded.confirmed_outcome_events()[0].queue_id == queue_id


def test_legacy_reconciliation_rows_are_migrated_and_replay_is_fail_closed(tmp_path: Path):
    database = tmp_path / "legacy-lifecycle.sqlite3"
    source_url = "https://www.linkedin.com/jobs/view/job-legacy"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            f"""
            CREATE TABLE lifecycle_jobs (
                job_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE,
                shortlisted_at TEXT NOT NULL
            );
            CREATE TABLE lifecycle_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES lifecycle_jobs(job_id),
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE lifecycle_rounds (
                round_id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE lifecycle_round_jobs (
                round_id TEXT NOT NULL REFERENCES lifecycle_rounds(round_id),
                job_id TEXT NOT NULL REFERENCES lifecycle_jobs(job_id),
                position INTEGER NOT NULL,
                PRIMARY KEY (round_id, job_id),
                UNIQUE (round_id, position)
            );
            CREATE TABLE lifecycle_queue_reconciliations (
                queue_event_id INTEGER PRIMARY KEY CHECK(queue_event_id > 0),
                job_id TEXT NOT NULL REFERENCES lifecycle_jobs(job_id),
                source_url TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                actor TEXT NOT NULL CHECK(actor = 'user'),
                reconciled_at TEXT NOT NULL
            );
            INSERT INTO lifecycle_jobs VALUES ('job-legacy', '{source_url}', '2026-09-01T00:00:00+00:00');
            INSERT INTO lifecycle_events (
                job_id, category, name, actor, payload_json, occurred_at
            ) VALUES (
                'job-legacy', 'lifecycle', 'shortlisted', 'agent', '{{}}', '2026-09-01T00:00:00+00:00'
            );
            INSERT INTO lifecycle_queue_reconciliations VALUES (
                3, 'job-legacy', '{source_url}', 'skipped', 'user', '2026-09-01T00:00:01+00:00'
            );
            """
        )

    tracker = LifecycleTracker(database)

    with sqlite3.connect(database) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(lifecycle_queue_reconciliations)")]
        migrated = connection.execute(
            """
            SELECT queue_id, queue_event_id, job_id, outcome
            FROM lifecycle_queue_reconciliations
            """
        ).fetchone()

    assert columns[:2] == ["queue_id", "queue_event_id"]
    assert migrated == ("legacy-unscoped", 3, "job-legacy", "skipped")
    queue, queue_event_id = _confirmed_queue_outcome(
        tmp_path,
        job_id="job-legacy",
        source_url=source_url,
        outcome="skipped",
    )
    assert queue_event_id == 3
    with pytest.raises(InvalidLifecycleTransition, match="unscoped legacy"):
        tracker.reconcile_queue_outcome(
            queue=queue,
            queue_event_id=queue_event_id,
            job_id="job-legacy",
            source_url=source_url,
            outcome="skipped",
            actor="user",
        )


def test_non_submission_queue_outcomes_are_audited_without_inventing_application_state(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    source_url = "https://www.indeed.com/viewjob?jk=job-skip"
    queue, queue_event_id = _confirmed_queue_outcome(
        tmp_path,
        job_id="job-skip",
        source_url=source_url,
        outcome="skipped",
    )

    job = tracker.reconcile_queue_outcome(
        queue=queue,
        queue_event_id=queue_event_id,
        job_id="job-skip",
        source_url=source_url,
        outcome="skipped",
        actor="user",
    )

    assert job.state == "shortlisted"
    assert tracker.unique_manual_submitted_count() == 0
    assert [(event.category, event.name) for event in tracker.events_for("job-skip")] == [
        ("lifecycle", "shortlisted"),
        ("queue_outcome", "skipped"),
    ]


def test_queue_reconciliation_rejects_forged_or_mismatched_queue_events(tmp_path: Path):
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    source_url = "https://www.linkedin.com/jobs/view/job-auth"
    queue, queue_event_id = _confirmed_queue_outcome(
        tmp_path,
        job_id="job-auth",
        source_url=source_url,
        outcome="submitted",
    )

    with pytest.raises(InvalidLifecycleTransition, match="authenticated"):
        tracker.reconcile_queue_outcome(
            queue=queue,
            queue_event_id=queue_event_id + 100,
            job_id="job-auth",
            source_url=source_url,
            outcome="submitted",
            actor="user",
        )
    with pytest.raises(InvalidLifecycleTransition, match="facts do not match"):
        tracker.reconcile_queue_outcome(
            queue=queue,
            queue_event_id=queue_event_id,
            job_id="job-auth",
            source_url=source_url,
            outcome="skipped",
            actor="user",
        )
    with pytest.raises(InvalidLifecycleTransition, match="authenticated"):
        tracker.reconcile_queue_outcome(
            queue=queue,
            queue_event_id=queue_event_id,
            job_id="job-other",
            source_url="https://www.linkedin.com/jobs/view/job-other",
            outcome="submitted",
            actor="user",
        )


def test_confirmed_queue_submission_replays_into_lifecycle_after_crash_without_double_counting(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidate = QueueCandidate(
        job_id="job-replay",
        source_url="https://www.linkedin.com/jobs/view/job-replay",
        fit_score=94,
        eligible=True,
        decision="recommended",
        evidence=("synthetic verified professional evidence",),
        profile_revision="lifecycle-profile-v1",
        matcher_policy_revision="lifecycle-policy-v1",
    )
    queue.add_recommendations([candidate])
    queue.plan_refill(open_urls=[])
    queue.confirm_outcome(candidate.job_id, "submitted", actor="user")

    # Simulate a restart after the queue commit but before lifecycle received it.
    replayed_queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    replayed_tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    for event in replayed_queue.confirmed_outcome_events():
        tracked_job = replayed_queue.get(event.job_id)
        for _attempt in range(2):
            replayed_tracker.reconcile_queue_outcome(
                queue=replayed_queue,
                queue_event_id=event.event_id,
                job_id=event.job_id,
                source_url=tracked_job.source_url,
                outcome=event.name,
                actor=event.actor,
            )

    assert replayed_queue.confirmed_submitted_count() == 1
    assert replayed_tracker.unique_manual_submitted_count() == 1
    assert replayed_tracker.get_job(candidate.job_id).state == "submitted"
