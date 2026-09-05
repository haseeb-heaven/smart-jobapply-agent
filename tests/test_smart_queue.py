from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from jobapply_agent.candidate_memory import CandidateMemory, CandidateMemoryPolicyError
from jobapply_agent.smart_queue import (
    QueueAction,
    QueueCandidate,
    QueuePolicyError,
    SmartJobQueue,
)


_SYNTHETIC_PROFILE_REVISION = "synthetic-profile-v1"
_SYNTHETIC_POLICY_REVISION = "synthetic-policy-v1"


def _candidate(
    number: int,
    score: int,
    *,
    eligible: bool = True,
    decision: str = "recommended",
    url: str | None = None,
    profile_revision: str | None = _SYNTHETIC_PROFILE_REVISION,
    matcher_policy_revision: str | None = _SYNTHETIC_POLICY_REVISION,
) -> QueueCandidate:
    return QueueCandidate(
        job_id=f"job-{number}",
        source_url=url or f"https://www.linkedin.com/jobs/view/{10_000 + number}",
        fit_score=score,
        eligible=eligible,
        decision=decision,
        evidence=(f"verified professional evidence {number}",),
        profile_revision=profile_revision,
        matcher_policy_revision=matcher_policy_revision,
    )


def _urls(*candidates: QueueCandidate) -> list[str]:
    return [candidate.source_url for candidate in candidates]


def _confirm_outcome(queue: SmartJobQueue, job_id: str, outcome: str) -> None:
    memory = CandidateMemory(
        queue.database_path.with_name("candidate-memory.sqlite3"),
        private_root=queue.database_path.parent,
    )
    queue.confirm_outcome(
        job_id,
        outcome,
        actor="user",
        vacated=True,
        candidate_memory=memory,
    )


@pytest.mark.parametrize("target_size", (1, 3, 10))
def test_queue_policy_accepts_candidate_selected_review_capacity(
    tmp_path: Path, target_size: int
):
    queue = SmartJobQueue(tmp_path / f"queue-{target_size}.sqlite3", target_size=target_size)

    assert queue.target_size == target_size


def test_queue_policy_defaults_to_five_review_tabs(tmp_path: Path):
    assert SmartJobQueue(tmp_path / "queue.sqlite3").target_size == 5


@pytest.mark.parametrize(
    "target_size",
    (0, -1, 11, True, False, 3.0, "3", None),
    ids=("zero", "negative", "above-maximum", "true", "false", "float", "string", "null"),
)
def test_queue_policy_rejects_invalid_candidate_selected_capacity(tmp_path: Path, target_size: object):
    with pytest.raises(QueuePolicyError, match="target_size|capacity"):
        SmartJobQueue(tmp_path / "invalid-size.sqlite3", target_size=target_size)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("profile_revision", "matcher_policy_revision"),
    [
        ("", "policy-v1"),
        ("profile-v1", ""),
        ("profile-v1", None),
        (None, "policy-v1"),
        (None, None),
        ("   ", "policy-v1"),
        ("profile-v1", "\n"),
    ],
)
def test_queue_candidates_require_two_non_empty_revision_ids(
    profile_revision: str | None, matcher_policy_revision: str | None
):
    with pytest.raises(QueuePolicyError, match="revision"):
        _candidate(
            1,
            99,
            profile_revision=profile_revision,
            matcher_policy_revision=matcher_policy_revision,
        )


def test_queue_candidate_revision_fields_are_required_for_new_values():
    with pytest.raises(TypeError):
        QueueCandidate(
            job_id="job-missing-revisions",
            source_url="https://www.linkedin.com/jobs/view/10001",
            fit_score=99,
            eligible=True,
            decision="recommended",
            evidence=("verified professional evidence",),
        )


@pytest.mark.parametrize(
    "job_id",
    (
        "https://www.linkedin.com/jobs/view/123",
        "//www.linkedin.com/jobs/view/123",
        "www.linkedin.com/jobs/view/123",
        "/jobs/view/123",
        r"\\private\\candidate-data",
        "job id",
        " job-1",
        "job-1 ",
        "job\t1",
        "job\n1",
        "candidate@example.test",
        "+15551234567",
        "job.id",
    ),
)
def test_queue_candidate_job_id_is_a_bounded_opaque_identifier(job_id: str):
    with pytest.raises(QueuePolicyError, match="opaque"):
        QueueCandidate(
            job_id=job_id,
            source_url="https://www.linkedin.com/jobs/view/10001",
            fit_score=99,
            eligible=True,
            decision="recommended",
            evidence=("verified professional evidence",),
            profile_revision="profile-v1",
            matcher_policy_revision="policy-v1",
        )


def test_queue_candidate_job_id_has_a_strict_bounded_length():
    with pytest.raises(QueuePolicyError, match="opaque"):
        QueueCandidate(
            job_id="a" * 129,
            source_url="https://www.linkedin.com/jobs/view/10001",
            fit_score=99,
            eligible=True,
            decision="recommended",
            evidence=("verified professional evidence",),
            profile_revision="profile-v1",
            matcher_policy_revision="policy-v1",
        )


def test_versioned_recommendation_revisions_are_durable_and_initialize_active_pair(tmp_path: Path):
    database = tmp_path / "queue.sqlite3"
    candidate = _candidate(1, 99, profile_revision="profile-v1", matcher_policy_revision="policy-v1")
    queue = SmartJobQueue(database)

    queue.add_recommendations([candidate])

    assert queue.active_revisions == ("profile-v1", "policy-v1")
    assert queue.get(candidate.job_id).profile_revision == "profile-v1"
    assert queue.get(candidate.job_id).matcher_policy_revision == "policy-v1"

    restarted = SmartJobQueue(database)
    assert restarted.active_revisions == ("profile-v1", "policy-v1")
    assert restarted.get(candidate.job_id).profile_revision == "profile-v1"
    assert restarted.get(candidate.job_id).matcher_policy_revision == "policy-v1"


def test_bind_empty_queue_revisions_sets_and_durably_reuses_exact_pair(tmp_path: Path):
    database = tmp_path / "queue.sqlite3"
    queue = SmartJobQueue(database)

    queue.bind_empty_queue_revisions("profile-r1", "policy-r1")
    queue.bind_empty_queue_revisions("profile-r1", "policy-r1")

    assert queue.active_revisions == ("profile-r1", "policy-r1")
    assert SmartJobQueue(database).active_revisions == ("profile-r1", "policy-r1")


@pytest.mark.parametrize(
    ("profile_revision", "matcher_policy_revision"),
    (("", "policy-r1"), ("profile-r1", ""), (None, "policy-r1"), ("profile-r1", None)),
)
def test_bind_empty_queue_revisions_rejects_incomplete_pair_without_mutation(
    tmp_path: Path, profile_revision: str | None, matcher_policy_revision: str | None
):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")

    with pytest.raises(QueuePolicyError, match="revision"):
        queue.bind_empty_queue_revisions(profile_revision, matcher_policy_revision)  # type: ignore[arg-type]

    assert queue.active_revisions == (None, None)


def test_bind_empty_queue_revisions_rejects_conflicting_pair_without_mutation(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    queue.bind_empty_queue_revisions("profile-r1", "policy-r1")

    with pytest.raises(QueuePolicyError, match="conflict"):
        queue.bind_empty_queue_revisions("profile-r2", "policy-r1")

    assert queue.active_revisions == ("profile-r1", "policy-r1")


def test_bind_empty_queue_revisions_rejects_versioned_or_unversioned_stored_jobs(
    tmp_path: Path,
):
    versioned = SmartJobQueue(tmp_path / "versioned.sqlite3")
    versioned.add_recommendations([_candidate(1, 99)])

    with pytest.raises(QueuePolicyError, match="empty"):
        versioned.bind_empty_queue_revisions(
            _SYNTHETIC_PROFILE_REVISION, _SYNTHETIC_POLICY_REVISION
        )

    unversioned_database = tmp_path / "unversioned.sqlite3"
    unversioned = SmartJobQueue(unversioned_database)
    with sqlite3.connect(unversioned_database) as connection:
        connection.execute(
            """
            INSERT INTO smart_queue_jobs
                (job_id, source_url, fit_score, eligible, decision, evidence_json,
                 profile_revision, matcher_policy_revision, created_at)
            VALUES (?, ?, 99, 1, 'recommended', '[]', NULL, NULL, ?)
            """,
            (
                "legacy-job",
                "https://www.linkedin.com/jobs/view/legacy-queue-job",
                "2026-09-03T00:00:00+00:00",
            ),
        )

    with pytest.raises(QueuePolicyError, match="empty"):
        unversioned.bind_empty_queue_revisions("profile-r1", "policy-r1")

    assert unversioned.active_revisions == (None, None)


def test_reset_empty_queue_revisions_restores_unbound_pair_idempotently(tmp_path: Path):
    database = tmp_path / "queue.sqlite3"
    queue = SmartJobQueue(database)
    queue.bind_empty_queue_revisions("profile-r1", "policy-r1")

    queue.reset_empty_queue_revisions()

    assert queue.active_revisions == (None, None)
    assert SmartJobQueue(database).active_revisions == (None, None)

    queue.reset_empty_queue_revisions()

    assert queue.active_revisions == (None, None)

    queue.bind_empty_queue_revisions("profile-r2", "policy-r2")

    assert queue.active_revisions == ("profile-r2", "policy-r2")


def test_reset_empty_queue_revisions_is_a_noop_before_any_binding(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")

    queue.reset_empty_queue_revisions()

    assert queue.active_revisions == (None, None)
    queue.bind_empty_queue_revisions("profile-r1", "policy-r1")
    assert queue.active_revisions == ("profile-r1", "policy-r1")


def test_reset_empty_queue_revisions_rejects_stored_job_without_mutation(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidate = _candidate(1, 99)
    queue.add_recommendations([candidate])
    bound_pair = queue.active_revisions
    assert bound_pair != (None, None)

    with pytest.raises(QueuePolicyError, match="empty"):
        queue.reset_empty_queue_revisions()

    assert queue.active_revisions == bound_pair
    assert queue.get(candidate.job_id).source_url == candidate.source_url


def test_bound_queue_rejects_mismatched_revision_batch_atomically(tmp_path: Path):
    """A bound queue admits only its active pair; mismatches fail closed.

    Inserting a mismatched batch would create dead rows that refill
    selection can never reach, so the whole batch is rejected before any
    recommendation is stored.
    """
    database = tmp_path / "queue.sqlite3"
    queue = SmartJobQueue(database)
    queue.set_active_revisions("profile-v1", "policy-v1", actor="user")
    queue.add_recommendations(
        [_candidate(number, 100 - number, profile_revision="profile-v1", matcher_policy_revision="policy-v1") for number in range(1, 4)]
    )
    with sqlite3.connect(database) as connection:
        before = (
            connection.execute("SELECT COUNT(*) FROM smart_queue_jobs").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM smart_queue_events").fetchone()[0],
        )

    mismatched = _candidate(4, 99, profile_revision="profile-v2", matcher_policy_revision="policy-v2")
    matching = _candidate(5, 98, profile_revision="profile-v1", matcher_policy_revision="policy-v1")
    with pytest.raises(QueuePolicyError, match="conflict"):
        queue.add_recommendations([matching, mismatched])

    assert queue.active_revisions == ("profile-v1", "policy-v1")
    with sqlite3.connect(database) as connection:
        after = (
            connection.execute("SELECT COUNT(*) FROM smart_queue_jobs").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM smart_queue_events").fetchone()[0],
        )
    assert after == before
    with pytest.raises(KeyError):
        queue.get(mismatched.job_id)


def test_bound_queue_rejects_multi_pair_batch_even_without_prior_rows(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    queue.set_active_revisions("profile-v1", "policy-v1", actor="user")

    with pytest.raises(QueuePolicyError, match="conflict"):
        queue.add_recommendations(
            [
                _candidate(1, 99, profile_revision="profile-v1", matcher_policy_revision="policy-v1"),
                _candidate(2, 98, profile_revision="profile-v1", matcher_policy_revision="policy-v2"),
            ]
        )

    assert queue.active_revisions == ("profile-v1", "policy-v1")


def test_set_active_revisions_advances_nonempty_queue_without_rewriting_historical_rows(
    tmp_path: Path,
):
    """A durable queue advances future-refill policy while preserving history."""
    database = tmp_path / "queue.sqlite3"
    queue = SmartJobQueue(database)
    queue.set_active_revisions("profile-v1", "policy-v1", actor="user")
    queue.add_recommendations([_candidate(1, 99, profile_revision="profile-v1", matcher_policy_revision="policy-v1")])

    assert queue.set_active_revisions("profile-v2", "policy-v2", actor="host") == (
        "profile-v2",
        "policy-v2",
    )

    assert queue.active_revisions == ("profile-v2", "policy-v2")
    binding_events = queue.revision_history()
    assert len(binding_events) == 2
    assert binding_events[0].prior_profile_revision is None
    assert binding_events[0].profile_revision == "profile-v1"
    assert binding_events[1].prior_profile_revision == "profile-v1"
    assert binding_events[1].prior_matcher_policy_revision == "policy-v1"
    assert binding_events[1].profile_revision == "profile-v2"
    assert binding_events[1].matcher_policy_revision == "policy-v2"
    assert queue.get("job-1").profile_revision == "profile-v1"
    assert queue.get("job-1").matcher_policy_revision == "policy-v1"


def test_set_active_revisions_accepts_same_pair_idempotently_without_event(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    queue.set_active_revisions("profile-v1", "policy-v1", actor="user")
    queue.add_recommendations([_candidate(1, 99, profile_revision="profile-v1", matcher_policy_revision="policy-v1")])
    before = queue.revision_history()

    assert queue.set_active_revisions("profile-v1", "policy-v1", actor="host") == ("profile-v1", "policy-v1")

    assert queue.active_revisions == ("profile-v1", "policy-v1")
    assert queue.revision_history() == before


def test_set_active_revisions_on_empty_queue_changes_pair_with_history_event(tmp_path: Path):
    database = tmp_path / "queue.sqlite3"
    queue = SmartJobQueue(database)
    queue.set_active_revisions("profile-v1", "policy-v1", actor="user")
    first_events = queue.revision_history()
    assert len(first_events) == 1
    assert first_events[0].prior_profile_revision is None
    assert first_events[0].prior_matcher_policy_revision is None

    assert queue.set_active_revisions("profile-v2", "policy-v2", actor="host") == ("profile-v2", "policy-v2")

    events = queue.revision_history()
    assert len(events) == 2
    change = events[-1]
    assert change.prior_profile_revision == "profile-v1"
    assert change.prior_matcher_policy_revision == "policy-v1"
    assert change.profile_revision == "profile-v2"
    assert change.matcher_policy_revision == "policy-v2"
    assert change.actor == "host"
    assert change.queue_id == queue.queue_id
    assert change.occurred_at.endswith("+00:00")

    restarted = SmartJobQueue(database)
    assert restarted.active_revisions == ("profile-v2", "policy-v2")
    assert restarted.revision_history() == events

    assert queue.set_active_revisions("profile-v2", "policy-v2", actor="host") == ("profile-v2", "policy-v2")
    assert queue.revision_history() == events


def test_set_active_revisions_on_unbound_empty_queue_is_accepted_with_event(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    assert queue.active_revisions == (None, None)

    assert queue.set_active_revisions("profile-v1", "policy-v1") == ("profile-v1", "policy-v1")

    assert queue.active_revisions == ("profile-v1", "policy-v1")
    assert len(queue.revision_history()) == 1


def test_legacy_queue_schema_migrates_without_rewriting_history_or_losing_unversioned_behavior(tmp_path: Path):
    database = tmp_path / "legacy-queue.sqlite3"
    source_url = "https://www.linkedin.com/jobs/view/legacy-queue-job"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE smart_queue_jobs (
                job_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE,
                fit_score INTEGER NOT NULL CHECK(fit_score BETWEEN 0 AND 100),
                eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
                decision TEXT NOT NULL CHECK(decision IN ('recommended', 'review', 'reject')),
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE smart_queue_metadata (
                metadata_id INTEGER PRIMARY KEY CHECK(metadata_id = 1),
                queue_id TEXT NOT NULL UNIQUE CHECK(length(queue_id) = 32)
            );
            CREATE TABLE smart_queue_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES smart_queue_jobs(job_id),
                name TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            INSERT INTO smart_queue_metadata (metadata_id, queue_id) VALUES (1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
            """
        )
        connection.execute(
            """
            INSERT INTO smart_queue_jobs
                (job_id, source_url, fit_score, eligible, decision, evidence_json, created_at)
            VALUES (?, ?, 88, 1, 'recommended', '["legacy evidence"]', '2026-09-01T00:00:00+00:00')
            """,
            ("legacy-job", source_url),
        )
        connection.execute(
            """
            INSERT INTO smart_queue_events (job_id, name, actor, occurred_at)
            VALUES ('legacy-job', 'recommended', 'agent', '2026-09-01T00:00:00+00:00')
            """
        )

    queue = SmartJobQueue(database)
    migrated = queue.get("legacy-job")
    action = queue.plan_refill(open_urls=[])

    assert queue.active_revisions == (None, None)
    assert queue.target_size == 5
    assert migrated.profile_revision is None
    assert migrated.matcher_policy_revision is None
    assert action.job_ids == ("legacy-job",)
    assert [event.name for event in queue.history_for("legacy-job")] == ["recommended", "waiting"]

    with sqlite3.connect(database) as connection:
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(smart_queue_jobs)")}
        metadata_columns = {row[1] for row in connection.execute("PRAGMA table_info(smart_queue_metadata)")}
    assert {"profile_revision", "matcher_policy_revision"}.issubset(job_columns)
    assert {
        "active_profile_revision",
        "active_matcher_policy_revision",
        "target_size",
    }.issubset(metadata_columns)


def test_candidate_selected_capacity_persists_and_conflicting_host_configuration_fails_closed(tmp_path: Path):
    database = tmp_path / "queue.sqlite3"
    created = SmartJobQueue(database, target_size=3)

    reopened = SmartJobQueue(database, target_size=3)

    assert created.target_size == 3
    assert reopened.target_size == 3
    with pytest.raises(QueuePolicyError, match="target_size|capacity|conflict"):
        SmartJobQueue(database, target_size=5)


def test_versioned_candidate_cannot_rewrite_a_migrated_unversioned_row(tmp_path: Path):
    database = tmp_path / "legacy-queue.sqlite3"
    source_url = "https://www.linkedin.com/jobs/view/legacy-queue-job"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE smart_queue_jobs (
                job_id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL UNIQUE,
                fit_score INTEGER NOT NULL CHECK(fit_score BETWEEN 0 AND 100),
                eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
                decision TEXT NOT NULL CHECK(decision IN ('recommended', 'review', 'reject')),
                evidence_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE smart_queue_metadata (
                metadata_id INTEGER PRIMARY KEY CHECK(metadata_id = 1),
                queue_id TEXT NOT NULL UNIQUE CHECK(length(queue_id) = 32)
            );
            CREATE TABLE smart_queue_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES smart_queue_jobs(job_id),
                name TEXT NOT NULL,
                actor TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            INSERT INTO smart_queue_metadata (metadata_id, queue_id) VALUES (1, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb');
            INSERT INTO smart_queue_jobs
                (job_id, source_url, fit_score, eligible, decision, evidence_json, created_at)
            VALUES ('legacy-job', 'https://www.linkedin.com/jobs/view/legacy-queue-job', 88, 1,
                    'recommended', '["legacy evidence"]', '2026-09-01T00:00:00+00:00');
            INSERT INTO smart_queue_events (job_id, name, actor, occurred_at)
            VALUES ('legacy-job', 'recommended', 'agent', '2026-09-01T00:00:00+00:00');
            """
        )

    queue = SmartJobQueue(database)
    candidate = QueueCandidate(
        job_id="legacy-job",
        source_url=source_url,
        fit_score=88,
        eligible=True,
        decision="recommended",
        evidence=("legacy evidence",),
        profile_revision=_SYNTHETIC_PROFILE_REVISION,
        matcher_policy_revision=_SYNTHETIC_POLICY_REVISION,
    )

    with pytest.raises(QueuePolicyError, match="conflicting recommendation data"):
        queue.add_recommendations([candidate])

    assert queue.active_revisions == (None, None)
    assert queue.get("legacy-job").profile_revision is None
    assert [event.name for event in queue.history_for("legacy-job")] == ["recommended"]


def test_refill_selects_five_eligible_recommendations_best_first_and_stably(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [
        _candidate(1, 91),
        _candidate(2, 97),
        _candidate(3, 94),
        _candidate(4, 91),
        _candidate(5, 88),
        _candidate(6, 84),
    ]
    queue.add_recommendations(candidates)

    action = queue.plan_refill(open_urls=[])

    assert isinstance(action, QueueAction)
    assert action.job_ids == ("job-2", "job-3", "job-1", "job-4", "job-5")
    assert action.urls_to_open == tuple(
        candidate.source_url for candidate in (candidates[1], candidates[2], candidates[0], candidates[3], candidates[4])
    )
    assert action.search_needed == 0
    assert len(action.urls_to_open) == 5


@pytest.mark.parametrize("target_size", (1, 3, 10))
def test_refill_selects_exactly_the_candidate_selected_capacity(tmp_path: Path, target_size: int):
    queue = SmartJobQueue(tmp_path / f"queue-{target_size}.sqlite3", target_size=target_size)
    candidates = [_candidate(number, 100 - number) for number in range(1, 13)]
    queue.add_recommendations(candidates)

    action = queue.plan_refill(open_urls=[])

    expected = candidates[:target_size]
    assert action.job_ids == tuple(candidate.job_id for candidate in expected)
    assert action.urls_to_open == tuple(candidate.source_url for candidate in expected)
    assert action.search_needed == 0


def test_increasing_candidate_capacity_opens_only_the_new_vacancies(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3", target_size=3)
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    queue.add_recommendations(candidates)
    initial = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(initial.urls_to_open, actor="browser-bridge")

    queue.set_target_size(5, actor="user")
    increased = queue.plan_refill(open_urls=initial.urls_to_open)

    assert queue.target_size == 5
    assert increased.job_ids == ("job-4", "job-5")
    assert increased.urls_to_open == tuple(_urls(candidates[3], candidates[4]))
    assert queue.confirmed_outcome_events() == ()


def test_capacity_changes_have_append_only_user_attributed_history_without_touching_jobs_or_tabs(
    tmp_path: Path,
):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3", target_size=3)
    candidates = [_candidate(number, 100 - number) for number in range(1, 5)]
    queue.add_recommendations(candidates)
    initial = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(initial.urls_to_open, actor="browser-bridge")
    before_jobs = {candidate.job_id: queue.get(candidate.job_id) for candidate in candidates}
    before_job_history = {
        candidate.job_id: queue.history_for(candidate.job_id) for candidate in candidates
    }
    before_outcomes = queue.confirmed_outcome_events()
    before_capacity_history = queue.capacity_history()

    assert queue.set_target_size(5, actor="user") == 5

    capacity_history = queue.capacity_history()
    assert len(capacity_history) == len(before_capacity_history) + 1
    change = capacity_history[-1]
    assert change.prior_target_size == 3
    assert change.target_size == 5
    assert change.actor == "user"
    assert change.queue_id == queue.queue_id
    assert change.occurred_at.endswith("+00:00")
    assert {candidate.job_id: queue.get(candidate.job_id) for candidate in candidates} == before_jobs
    assert {
        candidate.job_id: queue.history_for(candidate.job_id) for candidate in candidates
    } == before_job_history
    assert queue.confirmed_outcome_events() == before_outcomes


def test_decreasing_candidate_capacity_never_closes_tabs_or_infers_outcomes(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    queue.add_recommendations(candidates)
    initial = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(initial.urls_to_open, actor="browser-bridge")
    before = {candidate.job_id: queue.history_for(candidate.job_id) for candidate in candidates[:5]}

    queue.set_target_size(3, actor="user")
    over_capacity = queue.plan_refill(open_urls=initial.urls_to_open)

    assert queue.target_size == 3
    assert over_capacity.job_ids == ()
    assert over_capacity.urls_to_open == ()
    assert over_capacity.search_needed == 0
    assert {candidate.job_id: queue.history_for(candidate.job_id) for candidate in candidates[:5]} == before
    assert {queue.get(candidate.job_id).state for candidate in candidates[:5]} == {"open"}
    assert queue.confirmed_outcome_events() == ()


_INTAKE_HASH = "ab" * 32


def test_set_target_size_nondefault_without_proof_records_host_configured(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")

    assert queue.set_target_size(3, actor="user") == 3

    assert queue.target_size == 3
    assert queue.capacity_provenance == "host-configured"
    assert queue.has_active_intake_capacity_provenance is False


def test_set_target_size_with_active_intake_proof_keeps_live_authorization(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")

    assert (
        queue.set_target_size(
            3,
            actor="user",
            capacity_provenance="active-candidate-intake",
            intake_revision_hash=_INTAKE_HASH,
        )
        == 3
    )

    assert queue.target_size == 3
    assert queue.capacity_provenance == "active-candidate-intake"
    assert queue.has_active_intake_capacity_provenance is True


def test_set_target_size_clears_stale_intake_proof_instead_of_reproving(tmp_path: Path):
    queue = SmartJobQueue.for_active_candidate_intake(
        tmp_path / "queue.sqlite3",
        target_size=3,
        intake_revision_hash=_INTAKE_HASH,
    )
    assert queue.has_active_intake_capacity_provenance is True

    assert queue.set_target_size(4, actor="user") == 4

    assert queue.capacity_provenance == "host-configured"
    assert queue.has_active_intake_capacity_provenance is False

    assert queue.set_target_size(5, actor="user") == 5
    assert queue.capacity_provenance == "default"
    assert queue.has_active_intake_capacity_provenance is False


@pytest.mark.parametrize(
    ("target_size", "provenance_kwargs"),
    (
        (3, {"capacity_provenance": "default"}),
        (3, {"capacity_provenance": "legacy-unverified"}),
        (3, {"capacity_provenance": "active-candidate-intake"}),
        (
            3,
            {
                "capacity_provenance": "host-configured",
                "intake_revision_hash": _INTAKE_HASH,
            },
        ),
        (5, {"capacity_provenance": "host-configured"}),
        (5, {"intake_revision_hash": _INTAKE_HASH}),
        (3, {"capacity_provenance": "unsupported"}),
    ),
    ids=(
        "default-label-with-nondefault-size",
        "legacy-label-with-nondefault-size",
        "intake-proof-without-hash",
        "hash-without-intake-provenance",
        "nondefault-label-with-default-size",
        "hash-with-default-size",
        "unknown-provenance",
    ),
)
def test_set_target_size_rejects_inconsistent_provenance_without_mutation(
    tmp_path: Path, target_size: int, provenance_kwargs: dict[str, str]
):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    before_history = queue.capacity_history()

    with pytest.raises(QueuePolicyError, match="provenance|revision|default"):
        queue.set_target_size(target_size, actor="user", **provenance_kwargs)

    assert queue.target_size == 5
    assert queue.capacity_provenance == "default"
    assert queue.capacity_history() == before_history


def test_rejected_ineligible_duplicate_and_historic_jobs_are_never_selected(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    submitted = _candidate(1, 99)
    rejected = _candidate(2, 98)
    skipped = _candidate(3, 97, url="https://www.indeed.com/viewjob?jk=synthetic-3")
    queue.add_recommendations([submitted, rejected, skipped])
    queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(_urls(submitted, rejected, skipped), actor="agent")
    _confirm_outcome(queue, submitted.job_id, "submitted")
    _confirm_outcome(queue, rejected.job_id, "rejected")
    _confirm_outcome(queue, skipped.job_id, "skipped")

    fresh = [_candidate(number, 90 - number) for number in range(4, 9)]
    duplicate_url = _candidate(9, 100, url=fresh[0].source_url)
    queue.add_recommendations(fresh)
    queue.add_recommendations(
        [
            submitted,
            rejected,
            skipped,
            _candidate(10, 100, eligible=False),
            _candidate(11, 100, decision="reject"),
        ]
    )
    with pytest.raises(QueuePolicyError, match="source URL"):
        queue.add_recommendations([duplicate_url])

    action = queue.plan_refill(open_urls=[])

    assert action.job_ids == tuple(candidate.job_id for candidate in fresh)
    assert not {submitted.job_id, rejected.job_id, skipped.job_id, duplicate_url.job_id} & set(action.job_ids)
    assert queue.confirmed_submitted_count() == 1


def test_planned_jobs_wait_without_being_selected_twice(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 6)]
    queue.add_recommendations(candidates)

    first = queue.plan_refill(open_urls=[])
    second = queue.plan_refill(open_urls=[])

    assert first.job_ids == tuple(candidate.job_id for candidate in candidates)
    assert second.urls_to_open == ()
    assert second.job_ids == ()
    assert second.search_needed == 0
    assert {queue.get(candidate.job_id).state for candidate in candidates} == {"waiting"}


def test_missing_visible_urls_release_jobs_and_immediately_refill_only_from_admitted_recommendations(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 8)]
    queue.add_recommendations(candidates)
    initial = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(initial.urls_to_open, actor="agent")

    remaining_urls = initial.urls_to_open[2:]
    queue.record_visible_snapshot(remaining_urls, actor="agent")
    replacement = queue.plan_refill(open_urls=remaining_urls)

    assert queue.get("job-1").state == "released"
    assert queue.get("job-2").state == "released"
    assert replacement.job_ids == ("job-6", "job-7")
    assert replacement.urls_to_open == tuple(candidate.source_url for candidate in candidates[5:])
    assert replacement.search_needed == 0
    assert [event.name for event in queue.history_for("job-1")] == ["recommended", "waiting", "open", "missing"]

    queue.record_visible_snapshot(remaining_urls, actor="agent")
    repeated = queue.plan_refill(open_urls=remaining_urls)
    assert repeated.job_ids == ()
    assert repeated.urls_to_open == ()
    assert [event.name for event in queue.history_for("job-1")] == ["recommended", "waiting", "open", "missing"]


def test_released_vacancies_without_admitted_inventory_report_search_needed(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    initial = [_candidate(number, 100 - number) for number in range(1, 6)]
    queue.add_recommendations(initial)
    first = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(first.urls_to_open, actor="agent")
    three_still_open = first.urls_to_open[:3]
    queue.record_visible_snapshot(three_still_open, actor="agent")

    short = queue.plan_refill(open_urls=three_still_open)

    assert {queue.get(job_id).state for job_id in ("job-4", "job-5")} == {"released"}
    assert short.urls_to_open == ()
    assert short.search_needed == 2

    replacements = [_candidate(6, 93), _candidate(7, 92)]
    queue.add_recommendations(replacements)
    refill = queue.plan_refill(open_urls=three_still_open)
    queue.record_visible_snapshot([*three_still_open, *refill.urls_to_open], actor="agent")

    assert refill.urls_to_open == tuple(candidate.source_url for candidate in replacements)
    assert refill.search_needed == 0
    assert sum(queue.get(candidate.job_id).state == "open" for candidate in [*initial, *replacements]) == 5


def test_queue_rejects_more_than_ten_known_visible_listings(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3", target_size=10)
    candidates = [_candidate(number, 100 - number) for number in range(1, 12)]
    queue.add_recommendations(candidates)
    visible = [candidate.source_url for candidate in candidates]

    with pytest.raises(QueuePolicyError, match="10|capacity"):
        queue.plan_refill(open_urls=visible)
    with pytest.raises(QueuePolicyError, match="10|capacity"):
        queue.record_visible_snapshot(visible, actor="agent")


def test_queue_conflicting_identity_duplicates_fail_closed(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    original = _candidate(1, 99)
    queue.add_recommendations([original])

    queue.add_recommendations([original])
    with pytest.raises(QueuePolicyError, match="different source URL"):
        queue.add_recommendations([_candidate(1, 98, url="https://www.linkedin.com/jobs/view/99999")])
    with pytest.raises(QueuePolicyError, match="different job_id"):
        queue.add_recommendations([_candidate(2, 98, url=original.source_url)])


@pytest.mark.parametrize("outcome", ["submitted", "rejected", "skipped"])
def test_only_user_can_confirm_an_application_outcome(tmp_path: Path, outcome: str):
    queue = SmartJobQueue(tmp_path / f"{outcome}.sqlite3")
    candidate = _candidate(1, 99)
    queue.add_recommendations([candidate])
    action = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(action.urls_to_open, actor="agent")

    memory = CandidateMemory(
        queue.database_path.with_name("candidate-memory.sqlite3"),
        private_root=queue.database_path.parent,
    )
    with pytest.raises(CandidateMemoryPolicyError, match="user"):
        queue.confirm_outcome(candidate.job_id, outcome, actor="agent", vacated=True, candidate_memory=memory)

    _confirm_outcome(queue, candidate.job_id, outcome)
    assert queue.get(candidate.job_id).state == outcome
    assert queue.confirmed_submitted_count() == (1 if outcome == "submitted" else 0)


def test_confirmed_job_still_occupies_a_physical_slot_while_its_tab_is_visible(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    queue.add_recommendations(candidates)
    first = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(first.urls_to_open, actor="agent")
    _confirm_outcome(queue, "job-1", "submitted")

    while_visible = queue.plan_refill(open_urls=first.urls_to_open)
    assert while_visible.urls_to_open == ()
    assert while_visible.search_needed == 0

    after_user_closed_it = first.urls_to_open[1:]
    queue.record_visible_snapshot(after_user_closed_it, actor="agent")
    refill = queue.plan_refill(open_urls=after_user_closed_it)
    assert refill.job_ids == ("job-6",)
    assert refill.urls_to_open == (candidates[5].source_url,)


def test_restart_preserves_queue_state_history_and_submission_count(tmp_path: Path):
    database = tmp_path / "queue.sqlite3"
    original = SmartJobQueue(database)
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    original.add_recommendations(candidates)
    first = original.plan_refill(open_urls=[])
    original.record_visible_snapshot(first.urls_to_open, actor="agent")
    _confirm_outcome(original, "job-1", "submitted")
    original.record_visible_snapshot(first.urls_to_open[1:], actor="agent")

    restarted = SmartJobQueue(database)
    refill = restarted.plan_refill(open_urls=first.urls_to_open[1:])

    assert restarted.get("job-1").state == "submitted"
    assert restarted.confirmed_submitted_count() == 1
    assert [event.name for event in restarted.history_for("job-1")] == [
        "recommended",
        "waiting",
        "open",
        "submitted",
        "missing",
    ]
    assert refill.job_ids == ("job-6",)


def test_planning_returns_data_only_and_never_exposes_form_actions(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidate = _candidate(1, 99)
    queue.add_recommendations([candidate])

    action = queue.plan_refill(open_urls=[])

    assert action == QueueAction(
        job_ids=(candidate.job_id,),
        urls_to_open=(candidate.source_url,),
        search_needed=4,
    )
    assert not hasattr(action, "clicks")
    assert not hasattr(action, "fields")
    assert not hasattr(action, "uploads")
    assert not hasattr(action, "submissions")


def test_browser_snapshot_safely_ignores_unrelated_and_non_listing_tabs(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    queue.add_recommendations(candidates)
    first = queue.plan_refill(open_urls=[])

    unrelated = [
        "https://mail.example.test/inbox",
        "chrome://settings/",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://user:secret@www.linkedin.com/jobs/view/private",
    ]
    queue.record_visible_snapshot([*unrelated, *first.urls_to_open], actor="agent")
    refill = queue.plan_refill(open_urls=[*unrelated, *first.urls_to_open])

    assert refill.urls_to_open == ()
    assert refill.search_needed == 0
    assert {queue.get(candidate.job_id).state for candidate in candidates[:5]} == {"open"}


def test_attributed_open_failure_releases_waiting_slot_without_inferring_an_outcome(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    queue.add_recommendations(candidates)
    first = queue.plan_refill(open_urls=[])

    failed = queue.record_open_failure(first.job_ids[0], actor="browser-bridge")
    replacement = queue.plan_refill(open_urls=[])

    assert failed.state == "open_failed"
    assert replacement.job_ids == (candidates[5].job_id,)
    assert replacement.urls_to_open == (candidates[5].source_url,)
    assert queue.confirmed_submitted_count() == 0
    assert [(event.name, event.actor) for event in queue.history_for(first.job_ids[0])] == [
        ("recommended", "agent"),
        ("waiting", "agent"),
        ("open_failed", "browser-bridge"),
    ]
    with pytest.raises(QueuePolicyError, match="waiting"):
        queue.record_open_failure(first.job_ids[0], actor="browser-bridge")


@pytest.mark.parametrize(("candidate_count", "expected_shortage"), ((6, 0), (5, 1)))
def test_post_attempt_refill_shortage_is_read_only_for_present_and_absent_reserves(
    tmp_path: Path,
    candidate_count: int,
    expected_shortage: int,
):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, candidate_count + 1)]
    queue.add_recommendations(candidates)
    first = queue.plan_refill(open_urls=[])
    visible_urls = first.urls_to_open[1:]
    queue.record_visible_snapshot(visible_urls, actor="browser-bridge")
    queue.record_open_failure(first.job_ids[0], actor="browser-bridge")

    before = {candidate.job_id: queue.get(candidate.job_id).state for candidate in candidates}
    shortage = queue.refill_search_needed(open_urls=visible_urls)

    assert shortage == expected_shortage
    assert {candidate.job_id: queue.get(candidate.job_id).state for candidate in candidates} == before
    assert not any(queue.get(candidate.job_id).state == "waiting" for candidate in candidates)


def test_confirmed_outcome_events_are_replayable_user_owned_records(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(1, 99), _candidate(2, 98)]
    queue.add_recommendations(candidates)
    memory = CandidateMemory(
        queue.database_path.with_name("candidate-memory.sqlite3"),
        private_root=queue.database_path.parent,
    )
    with pytest.raises(QueuePolicyError, match="open or released"):
        queue.confirm_outcome("job-1", "submitted", actor="user", vacated=True, candidate_memory=memory)

    waiting = queue.plan_refill(open_urls=[])
    with pytest.raises(QueuePolicyError, match="open or released"):
        queue.confirm_outcome("job-1", "submitted", actor="user", vacated=True, candidate_memory=memory)

    queue.record_visible_snapshot(waiting.urls_to_open, actor="agent")
    _confirm_outcome(queue, "job-1", "submitted")
    _confirm_outcome(queue, "job-2", "skipped")

    events = queue.confirmed_outcome_events()

    assert [(event.job_id, event.name, event.actor) for event in events] == [
        ("job-1", "submitted", "user"),
        ("job-2", "skipped", "user"),
    ]
    assert queue.confirmed_outcome_events(after_event_id=events[0].event_id) == (events[1],)
