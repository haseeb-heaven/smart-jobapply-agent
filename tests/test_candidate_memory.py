"""Public contracts for durable, candidate-owned Smart Queue memory.

These tests deliberately use only synthetic listing URLs and the public queue
and memory APIs.  They do not construct a browser adapter or inspect candidate
data.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from jobapply_agent.candidate_memory import (
    CandidateMemory,
    CandidateMemoryPolicyError,
    CandidateMemoryStorageError,
)
from jobapply_agent.smart_queue import QueueCandidate, QueuePolicyError, SmartJobQueue


_PROFILE_REVISION = "candidate-memory-profile-v1"
_POLICY_REVISION = "candidate-memory-policy-v1"
_LINKEDIN_URL = "https://www.linkedin.com/jobs/view/910001?utm_source=synthetic"
_INDEED_URL = "https://in.indeed.com/viewjob?jk=910001&utm_source=synthetic"


def _candidate(
    job_id: str,
    url: str,
    *,
    profile_revision: str = _PROFILE_REVISION,
    matcher_policy_revision: str = _POLICY_REVISION,
) -> QueueCandidate:
    return QueueCandidate(
        job_id=job_id,
        source_url=url,
        fit_score=95,
        eligible=True,
        decision="recommended",
        evidence=("synthetic candidate-approved evidence",),
        profile_revision=profile_revision,
        matcher_policy_revision=matcher_policy_revision,
    )
def _scope_queue_ids(memory: CandidateMemory) -> list[str]:
    with sqlite3.connect(memory.database_path) as connection:
        return [
            row[0]
            for row in connection.execute(
                "SELECT queue_id FROM candidate_memory_queue_scope ORDER BY scope_id"
            ).fetchall()
        ]


def _bind_empty_memory_scope(
    tmp_path: Path,
) -> tuple[SmartJobQueue, QueueCandidate, CandidateMemory]:
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3", target_size=1)
    candidate = _candidate("scope-job", _LINKEDIN_URL)
    queue.add_recommendations([candidate])
    memory = CandidateMemory(
        tmp_path / "private" / "candidate-memory.sqlite3",
        private_root=tmp_path / "private",
    )
    assert memory.filter_unsuppressed_candidates([candidate], queue=queue) == (candidate,)
    assert _scope_queue_ids(memory) == [queue.queue_id]
    return queue, candidate, memory


def test_discard_outcome_empty_queue_scope_discards_newly_bound_scope_once(tmp_path: Path) -> None:
    queue, candidate, memory = _bind_empty_memory_scope(tmp_path)

    memory.discard_outcome_empty_queue_scope()

    assert _scope_queue_ids(memory) == []

    memory.discard_outcome_empty_queue_scope()

    assert _scope_queue_ids(memory) == []
    assert memory.is_suppressed(candidate.source_url) is False

    assert memory.filter_unsuppressed_candidates([candidate], queue=queue) == (candidate,)
    assert _scope_queue_ids(memory) == [queue.queue_id]


def test_discard_outcome_empty_queue_scope_fails_closed_with_recorded_outcomes(
    tmp_path: Path,
) -> None:
    queue, candidate, memory = _bind_empty_memory_scope(tmp_path)
    with sqlite3.connect(memory.database_path) as connection:
        connection.execute(
            """
            INSERT INTO candidate_memory_outcomes
                (queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at)
            VALUES (?, 1, ?, ?, 'submitted', 'user', 1, '2026-09-03T00:00:00+00:00')
            """,
            (queue.queue_id, candidate.job_id, candidate.source_url),
        )

    with pytest.raises(CandidateMemoryPolicyError, match="outcomes are empty"):
        memory.discard_outcome_empty_queue_scope()

    assert _scope_queue_ids(memory) == [queue.queue_id]


def test_discard_outcome_empty_queue_scope_is_a_noop_without_a_scope_row(tmp_path: Path) -> None:
    memory = CandidateMemory(
        tmp_path / "private" / "candidate-memory.sqlite3",
        private_root=tmp_path / "private",
    )

    memory.discard_outcome_empty_queue_scope()

    assert _scope_queue_ids(memory) == []


def _queue_with_open_job(tmp_path: Path, *, job_id: str = "queue-job", url: str = _LINKEDIN_URL) -> tuple[SmartJobQueue, QueueCandidate]:
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3", target_size=1)
    candidate = _candidate(job_id, url)
    queue.add_recommendations([candidate])
    action = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(action.urls_to_open, actor="synthetic-browser-bridge")
    assert queue.get(job_id).state == "open"
    return queue, candidate


def test_queue_public_outcome_api_rejects_a_non_memory_finalizer_without_persisting_an_outcome(
    tmp_path: Path,
) -> None:
    queue, candidate = _queue_with_open_job(tmp_path)

    private_root = tmp_path / "private"
    memory_path = private_root / "candidate-memory.sqlite3"
    memory = CandidateMemory(memory_path, private_root=private_root)

    with pytest.raises(QueuePolicyError, match="CandidateMemory"):
        queue.confirm_outcome(
            candidate.job_id,
            "submitted",
            actor="user",
            vacated=True,
            candidate_memory=object(),
        )

    assert queue.get(candidate.job_id).state == "open"
    assert queue.confirmed_outcome_events() == ()
    assert memory.is_suppressed(candidate.source_url) is False


def test_memory_keeps_canonical_linkedin_and_indeed_listing_identities_independent(tmp_path: Path) -> None:
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3", target_size=2)
    linkedin = _candidate("linkedin-job", _LINKEDIN_URL)
    indeed = _candidate("indeed-job", _INDEED_URL)
    queue.add_recommendations([linkedin, indeed])
    action = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(action.urls_to_open, actor="synthetic-browser-bridge")
    private_root = tmp_path / "private"
    memory = CandidateMemory(private_root / "candidate-memory.sqlite3", private_root=private_root)
    finalized = (
        memory.finalize_queue_outcome(
            queue=queue,
            job_id=linkedin.job_id,
            outcome="submitted",
            actor="user",
            vacated=True,
        ),
        memory.finalize_queue_outcome(
            queue=queue,
            job_id=indeed.job_id,
            outcome="rejected",
            actor="user",
            vacated=True,
        ),
    )

    assert {(entry.source_url, entry.outcome) for entry in finalized} == {
        ("https://www.linkedin.com/jobs/view/910001", "submitted"),
        ("https://in.indeed.com/viewjob?jk=910001", "rejected"),
    }
    assert memory.is_suppressed(_LINKEDIN_URL) is True
    assert memory.is_suppressed(_INDEED_URL) is True
    assert memory.filter_unsuppressed_candidates([linkedin, indeed], queue=queue) == ()


def test_memory_filters_suppressed_candidates_before_queue_admission_without_mutating_the_queue(
    tmp_path: Path,
) -> None:
    """Suppression returns only candidates safe for a later admission step."""

    queue, suppressed = _queue_with_open_job(tmp_path)
    private_root = tmp_path / "private"
    memory = CandidateMemory(private_root / "candidate-memory.sqlite3", private_root=private_root)
    memory.finalize_queue_outcome(
        queue=queue,
        job_id=suppressed.job_id,
        outcome="skipped",
        actor="user",
        vacated=True,
    )
    unsuppressed = _candidate(
        "not-yet-admitted-job",
        "https://www.linkedin.com/jobs/view/910002?utm_source=synthetic",
    )

    admitted = memory.filter_unsuppressed_candidates((suppressed, unsuppressed), queue=queue)

    assert admitted == (unsuppressed,)
    with pytest.raises(KeyError):
        queue.get(unsuppressed.job_id)
    with sqlite3.connect(memory.database_path) as connection:
        assert connection.execute(
            "SELECT queue_id FROM candidate_memory_queue_scope WHERE scope_id = 1"
        ).fetchone() == (queue.queue_id,)


def test_finalization_atomically_records_a_released_outcome_and_its_suppression_row(tmp_path: Path) -> None:
    queue, candidate = _queue_with_open_job(tmp_path)
    queue.record_visible_snapshot((), actor="synthetic-browser-bridge")
    assert queue.get(candidate.job_id).state == "released"

    private_root = tmp_path / "private"
    memory = CandidateMemory(private_root / "candidate-memory.sqlite3", private_root=private_root)
    assert queue.confirmed_outcome_events() == ()
    assert memory.is_suppressed(candidate.source_url) is False
    finalized = memory.finalize_queue_outcome(
        queue=queue,
        job_id=candidate.job_id,
        outcome="submitted",
        actor="user",
        vacated=True,
    )

    assert finalized.inserted is True
    assert finalized.outcome == "submitted"
    assert queue.get(candidate.job_id).state == "submitted"
    assert memory.is_suppressed(candidate.source_url) is True
    assert queue.confirmed_outcome_events()[0].event_id == finalized.event_id

    retried = memory.finalize_queue_outcome(
        queue=queue,
        job_id=candidate.job_id,
        outcome="submitted",
        actor="user",
        vacated=True,
    )

    assert retried.inserted is False
    assert retried.event_id == finalized.event_id
    assert len(queue.confirmed_outcome_events()) == 1


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

    with pytest.raises(CandidateMemoryPolicyError):
        memory.finalize_queue_outcome(
            queue=queue,
            job_id=candidate.job_id,
            outcome="submitted",
            actor=actor,
            vacated=vacated,
        )

    assert queue.get(candidate.job_id).state == "open"
    assert memory.is_suppressed(candidate.source_url) is False


def test_first_nonempty_filter_batch_binds_memory_to_one_queue_and_mixed_revision_batch_does_not_bind(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    profile_one = _candidate("profile-one-job", _LINKEDIN_URL, profile_revision="profile-one-v1")
    profile_two = _candidate("profile-two-job", _INDEED_URL, profile_revision="profile-two-v1")
    queue_one = SmartJobQueue(tmp_path / "profile-one-queue.sqlite3")
    queue_one.add_recommendations([profile_one])
    queue_two = SmartJobQueue(tmp_path / "profile-two-queue.sqlite3")
    queue_two.add_recommendations([profile_two])

    bound = CandidateMemory(private_root / "bound.sqlite3", private_root=private_root)
    assert bound.filter_unsuppressed_candidates((), queue=queue_two) == ()
    assert bound.filter_unsuppressed_candidates([profile_one], queue=queue_one) == (profile_one,)
    with pytest.raises(CandidateMemoryPolicyError, match="queue|scope"):
        bound.filter_unsuppressed_candidates([profile_two], queue=queue_two)
    assert bound.filter_unsuppressed_candidates([profile_one], queue=queue_one) == (profile_one,)

    not_partially_bound = CandidateMemory(private_root / "mixed.sqlite3", private_root=private_root)
    with pytest.raises(CandidateMemoryPolicyError, match="revision|queue"):
        not_partially_bound.filter_unsuppressed_candidates(
            [profile_one, profile_two], queue=queue_one
        )
    assert not_partially_bound.filter_unsuppressed_candidates(
        [profile_two], queue=queue_two
    ) == (profile_two,)


def test_same_queue_profile_revision_update_retains_exact_url_suppression(
    tmp_path: Path,
) -> None:
    """Exact-URL suppression survives a revision update without rewriting rows.

    Active revisions cannot change underneath stored recommendations, so a
    revision switch on a non-empty queue fails closed and the submitted
    row keeps its original pair. Suppression stays keyed on the exact
    canonical URL: a same-pair resubmission of the known URL is filtered
    out, while a     cross-pair batch fails closed instead of stranding rows.
    """
    queue, profile_one = _queue_with_open_job(tmp_path)
    private_root = tmp_path / "private"
    memory = CandidateMemory(private_root / "candidate-memory.sqlite3", private_root=private_root)
    assert memory.filter_unsuppressed_candidates([profile_one], queue=queue) == (profile_one,)
    memory.finalize_queue_outcome(
        queue=queue,
        job_id=profile_one.job_id,
        outcome="submitted",
        actor="user",
        vacated=True,
    )
    with pytest.raises(QueuePolicyError, match="empty"):
        queue.set_active_revisions(
            "candidate-memory-profile-v2",
            "candidate-memory-policy-v2",
        )
    assert queue.active_revisions == (
        profile_one.profile_revision,
        profile_one.matcher_policy_revision,
    )
    refreshed_candidate = _candidate(
        "refreshed-job",
        _LINKEDIN_URL,
        profile_revision="candidate-memory-profile-v2",
        matcher_policy_revision="candidate-memory-policy-v2",
    )

    with sqlite3.connect(memory.database_path) as connection:
        before = connection.execute(
            "SELECT queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at "
            "FROM candidate_memory_outcomes ORDER BY queue_id, event_id"
        ).fetchall()
    with pytest.raises(CandidateMemoryPolicyError, match="revision"):
        memory.filter_unsuppressed_candidates(
            [refreshed_candidate], queue=queue
        )
    assert memory.is_suppressed(profile_one.source_url) is True
    same_pair_resubmission = _candidate(
        "resubmitted-job",
        _LINKEDIN_URL,
        profile_revision=profile_one.profile_revision,
        matcher_policy_revision=profile_one.matcher_policy_revision,
    )
    assert memory.filter_unsuppressed_candidates(
        [same_pair_resubmission], queue=queue
    ) == ()
    with sqlite3.connect(memory.database_path) as connection:
        after = connection.execute(
            "SELECT queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at "
            "FROM candidate_memory_outcomes ORDER BY queue_id, event_id"
        ).fetchall()

    assert after == before
    assert len(after) == 1
    assert memory.is_suppressed(profile_one.source_url) is True


def test_existing_outcome_rows_without_queue_scope_fail_closed_without_migration_mutation(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    database = private_root / "legacy-unscoped.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE candidate_memory_schema_versions (
                version INTEGER PRIMARY KEY CHECK(version > 0),
                applied_at TEXT NOT NULL
            );
            CREATE TABLE candidate_memory_outcomes (
                queue_id TEXT NOT NULL CHECK(length(queue_id) = 32),
                event_id INTEGER NOT NULL CHECK(event_id > 0),
                job_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                actor TEXT NOT NULL CHECK(actor = 'user'),
                vacated INTEGER NOT NULL CHECK(vacated = 1),
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (queue_id, event_id)
            );
            INSERT INTO candidate_memory_schema_versions (version, applied_at)
            VALUES (1, '2026-09-01T00:00:00+00:00');
            INSERT INTO candidate_memory_outcomes (
                queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at
            ) VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1, 'legacy-job',
                'https://www.linkedin.com/jobs/view/910099', 'submitted', 'user', 1,
                '2026-09-01T00:00:00+00:00'
            );
            """
        )

    with sqlite3.connect(database) as connection:
        sqlite_master_before = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        versions_before = connection.execute(
            "SELECT version, applied_at FROM candidate_memory_schema_versions ORDER BY version"
        ).fetchall()
        outcomes_before = connection.execute(
            "SELECT queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at "
            "FROM candidate_memory_outcomes ORDER BY queue_id, event_id"
        ).fetchall()

    with pytest.raises(CandidateMemoryStorageError, match="storage|initialization"):
        CandidateMemory(database, private_root=private_root)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == sqlite_master_before
        assert connection.execute(
            "SELECT version, applied_at FROM candidate_memory_schema_versions ORDER BY version"
        ).fetchall() == versions_before
        assert connection.execute(
            "SELECT queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at "
            "FROM candidate_memory_outcomes ORDER BY queue_id, event_id"
        ).fetchall() == outcomes_before


@pytest.mark.parametrize(
    "scope_rows",
    (
        pytest.param((), id="empty-scope-table"),
        pytest.param(((1, "not-a-durable-queue-id"),), id="malformed-queue-id"),
        pytest.param(
            (
                (1, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                (1, "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            ),
            id="multiple-scope-rows",
        ),
    ),
)
def test_populated_legacy_memory_requires_exactly_one_valid_scope_before_migration_mutation(
    tmp_path: Path,
    scope_rows: tuple[tuple[object, object], ...],
) -> None:
    private_root = tmp_path / "private"
    database = private_root / "legacy-invalid-scope.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE candidate_memory_schema_versions (
                version INTEGER PRIMARY KEY CHECK(version > 0),
                applied_at TEXT NOT NULL
            );
            CREATE TABLE candidate_memory_outcomes (
                queue_id TEXT NOT NULL CHECK(length(queue_id) = 32),
                event_id INTEGER NOT NULL CHECK(event_id > 0),
                job_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                actor TEXT NOT NULL CHECK(actor = 'user'),
                vacated INTEGER NOT NULL CHECK(vacated = 1),
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (queue_id, event_id)
            );
            CREATE TABLE candidate_memory_queue_scope (
                scope_id INTEGER,
                queue_id TEXT
            );
            INSERT INTO candidate_memory_schema_versions (version, applied_at)
            VALUES (1, '2026-09-01T00:00:00+00:00');
            INSERT INTO candidate_memory_outcomes (
                queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at
            ) VALUES (
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 1, 'legacy-job',
                'https://www.linkedin.com/jobs/view/910099', 'submitted', 'user', 1,
                '2026-09-01T00:00:00+00:00'
            );
            """
        )
        connection.executemany(
            "INSERT INTO candidate_memory_queue_scope (scope_id, queue_id) VALUES (?, ?)",
            scope_rows,
        )

    with sqlite3.connect(database) as connection:
        sqlite_master_before = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        versions_before = connection.execute(
            "SELECT version, applied_at FROM candidate_memory_schema_versions ORDER BY version"
        ).fetchall()
        outcomes_before = connection.execute(
            "SELECT queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at "
            "FROM candidate_memory_outcomes ORDER BY queue_id, event_id"
        ).fetchall()
        scope_before = connection.execute(
            "SELECT scope_id, queue_id FROM candidate_memory_queue_scope ORDER BY rowid"
        ).fetchall()

    with pytest.raises(CandidateMemoryStorageError, match="storage|initialization"):
        CandidateMemory(database, private_root=private_root)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == sqlite_master_before
        assert connection.execute(
            "SELECT version, applied_at FROM candidate_memory_schema_versions ORDER BY version"
        ).fetchall() == versions_before
        assert connection.execute(
            "SELECT queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at "
            "FROM candidate_memory_outcomes ORDER BY queue_id, event_id"
        ).fetchall() == outcomes_before
        assert connection.execute(
            "SELECT scope_id, queue_id FROM candidate_memory_queue_scope ORDER BY rowid"
        ).fetchall() == scope_before


@pytest.mark.parametrize(
    "outcome_queue_ids",
    (
        pytest.param(
            ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
            id="outcome-scope-mismatch",
        ),
        pytest.param(
            (
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            ),
            id="multiple-outcome-queues",
        ),
    ),
)
def test_populated_memory_requires_all_outcomes_to_match_its_sole_scope_without_any_mutation(
    tmp_path: Path,
    outcome_queue_ids: tuple[str, ...],
) -> None:
    private_root = tmp_path / "private"
    database = private_root / "invalid-outcome-scope.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE candidate_memory_schema_versions (
                version INTEGER PRIMARY KEY CHECK(version > 0),
                applied_at TEXT NOT NULL
            );
            CREATE TABLE candidate_memory_outcomes (
                queue_id TEXT NOT NULL CHECK(length(queue_id) = 32),
                event_id INTEGER NOT NULL CHECK(event_id > 0),
                job_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                actor TEXT NOT NULL CHECK(actor = 'user'),
                vacated INTEGER NOT NULL CHECK(vacated = 1),
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (queue_id, event_id)
            );
            CREATE TABLE candidate_memory_queue_scope (
                scope_id INTEGER PRIMARY KEY CHECK(scope_id = 1),
                queue_id TEXT NOT NULL CHECK(length(queue_id) = 32)
            );
            INSERT INTO candidate_memory_schema_versions (version, applied_at)
            VALUES (1, '2026-09-01T00:00:00+00:00');
            INSERT INTO candidate_memory_queue_scope (scope_id, queue_id)
            VALUES (1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
            """
        )
        connection.executemany(
            """
            INSERT INTO candidate_memory_outcomes (
                queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at
            ) VALUES (?, ?, ?, ?, 'submitted', 'user', 1, '2026-09-01T00:00:00+00:00')
            """,
            (
                (
                    queue_id,
                    event_id,
                    f"legacy-job-{event_id}",
                    f"https://www.linkedin.com/jobs/view/9101{event_id:02d}",
                )
                for event_id, queue_id in enumerate(outcome_queue_ids, start=1)
            ),
        )

    before_files = {
        path.name: path.read_bytes()
        for path in database.parent.iterdir()
        if path.is_file()
    }
    with sqlite3.connect(database) as connection:
        before_dump = tuple(connection.iterdump())

    with pytest.raises(CandidateMemoryStorageError, match="storage|initialization"):
        CandidateMemory(database, private_root=private_root)

    assert {
        path.name: path.read_bytes()
        for path in database.parent.iterdir()
        if path.is_file()
    } == before_files
    with sqlite3.connect(database) as connection:
        assert tuple(connection.iterdump()) == before_dump


def test_populated_memory_with_a_legacy_outcome_shape_missing_queue_id_fails_without_mutation(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    database = private_root / "legacy-outcome-shape.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE candidate_memory_schema_versions (
                version INTEGER PRIMARY KEY CHECK(version > 0),
                applied_at TEXT NOT NULL
            );
            CREATE TABLE candidate_memory_outcomes (
                event_id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL,
                source_url TEXT NOT NULL,
                outcome TEXT NOT NULL,
                actor TEXT NOT NULL,
                vacated INTEGER NOT NULL,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE candidate_memory_queue_scope (
                scope_id INTEGER PRIMARY KEY,
                queue_id TEXT NOT NULL
            );
            INSERT INTO candidate_memory_schema_versions (version, applied_at)
            VALUES (1, '2026-09-01T00:00:00+00:00');
            INSERT INTO candidate_memory_queue_scope (scope_id, queue_id)
            VALUES (1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
            INSERT INTO candidate_memory_outcomes (
                event_id, job_id, source_url, outcome, actor, vacated, recorded_at
            ) VALUES (
                1, 'legacy-job', 'https://www.linkedin.com/jobs/view/910199',
                'submitted', 'user', 1, '2026-09-01T00:00:00+00:00'
            );
            """
        )

    before_bytes = database.read_bytes()
    with sqlite3.connect(database) as connection:
        before_dump = tuple(connection.iterdump())

    with pytest.raises(CandidateMemoryStorageError, match="storage|initialization"):
        CandidateMemory(database, private_root=private_root)

    assert database.read_bytes() == before_bytes
    with sqlite3.connect(database) as connection:
        assert tuple(connection.iterdump()) == before_dump


def test_empty_legacy_memory_migrates_transactionally_and_binds_on_first_nonempty_batch(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    database = private_root / "legacy-empty.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE candidate_memory_schema_versions (
                version INTEGER PRIMARY KEY CHECK(version > 0), applied_at TEXT NOT NULL
            );
            CREATE TABLE candidate_memory_outcomes (
                queue_id TEXT NOT NULL CHECK(length(queue_id) = 32),
                event_id INTEGER NOT NULL CHECK(event_id > 0),
                job_id TEXT NOT NULL, source_url TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                actor TEXT NOT NULL CHECK(actor = 'user'),
                vacated INTEGER NOT NULL CHECK(vacated = 1), recorded_at TEXT NOT NULL,
                PRIMARY KEY (queue_id, event_id)
            );
            INSERT INTO candidate_memory_schema_versions (version, applied_at)
            VALUES (1, '2026-09-01T00:00:00+00:00');
            """
        )
    candidate = _candidate("new-job", _LINKEDIN_URL)
    queue = SmartJobQueue(tmp_path / "new-queue.sqlite3")
    queue.add_recommendations([candidate])

    memory = CandidateMemory(database, private_root=private_root)
    assert memory.filter_unsuppressed_candidates((), queue=queue) == ()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT queue_id FROM candidate_memory_queue_scope"
        ).fetchall() == []
        assert connection.execute(
            "SELECT version FROM candidate_memory_schema_versions ORDER BY version"
        ).fetchall() == [(1,), (2,)]
    assert memory.filter_unsuppressed_candidates([candidate], queue=queue) == (candidate,)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT queue_id FROM candidate_memory_queue_scope WHERE scope_id = 1"
        ).fetchone() == (queue.queue_id,)


def test_cross_queue_memory_pairing_rejects_atomically_without_exposing_queue_ids(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    profile_one = _candidate("profile-one-job", _LINKEDIN_URL)
    profile_two = _candidate("profile-two-job", _INDEED_URL)
    queue_one = SmartJobQueue(tmp_path / "profile-one-queue.sqlite3", target_size=1)
    queue_one.add_recommendations([profile_one])
    queue_two = SmartJobQueue(tmp_path / "profile-two-queue.sqlite3", target_size=1)
    queue_two.add_recommendations([profile_two])
    memory_one = CandidateMemory(private_root / "profile-one.sqlite3", private_root=private_root)
    memory_two = CandidateMemory(private_root / "profile-two.sqlite3", private_root=private_root)
    assert memory_one.filter_unsuppressed_candidates([profile_one], queue=queue_one) == (profile_one,)
    assert memory_two.filter_unsuppressed_candidates([profile_two], queue=queue_two) == (profile_two,)

    action = queue_two.plan_refill(open_urls=())
    queue_two.record_visible_snapshot(action.urls_to_open, actor="synthetic-browser-bridge")
    with sqlite3.connect(memory_one.database_path) as connection:
        scope_before = connection.execute(
            "SELECT scope_id, queue_id FROM candidate_memory_queue_scope"
        ).fetchall()

    with pytest.raises(
        (CandidateMemoryPolicyError, QueuePolicyError),
        match="queue|scope|candidate",
    ) as raised:
        memory_one.finalize_queue_outcome(
            queue=queue_two,
            job_id=profile_two.job_id,
            outcome="submitted",
            actor="user",
            vacated=True,
        )

    assert queue_one.queue_id not in str(raised.value)
    assert queue_two.queue_id not in str(raised.value)
    assert queue_two.get(profile_two.job_id).state == "open"
    assert queue_two.confirmed_outcome_events() == ()
    assert memory_one.is_suppressed(profile_two.source_url) is False
    assert memory_two.is_suppressed(profile_two.source_url) is False
    with sqlite3.connect(memory_one.database_path) as connection:
        assert connection.execute(
            "SELECT scope_id, queue_id FROM candidate_memory_queue_scope"
        ).fetchall() == scope_before
