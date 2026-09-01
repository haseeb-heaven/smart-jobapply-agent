from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

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


def test_queue_policy_is_fixed_at_five_review_tabs(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")

    assert queue.target_size == 5
    with pytest.raises(QueuePolicyError, match="five"):
        SmartJobQueue(tmp_path / "wrong-size.sqlite3", target_size=6)


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


def test_active_revision_switch_excludes_stale_profile_or_policy_recommendations(tmp_path: Path):
    database = tmp_path / "queue.sqlite3"
    queue = SmartJobQueue(database)
    queue.set_active_revisions("profile-v1", "policy-v1", actor="user")
    stale = [
        _candidate(
            number,
            100 - number,
            profile_revision="profile-v1",
            matcher_policy_revision="policy-v1",
        )
        for number in range(1, 6)
    ]
    stale_policy = _candidate(
        6,
        100,
        profile_revision="profile-v1",
        matcher_policy_revision="policy-v2",
    )
    fresh = [
        _candidate(
            number,
            90 - number,
            profile_revision="profile-v2",
            matcher_policy_revision="policy-v2",
        )
        for number in range(7, 12)
    ]
    queue.add_recommendations([*stale, stale_policy, *fresh])

    queue.set_active_revisions("profile-v2", "policy-v2", actor="host")
    restarted = SmartJobQueue(database)
    action = restarted.plan_refill(open_urls=[])

    assert restarted.active_revisions == ("profile-v2", "policy-v2")
    assert action.job_ids == tuple(candidate.job_id for candidate in fresh)
    assert {restarted.get(candidate.job_id).state for candidate in [*stale, stale_policy]} == {"recommended"}


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
    assert migrated.profile_revision is None
    assert migrated.matcher_policy_revision is None
    assert action.job_ids == ("legacy-job",)
    assert [event.name for event in queue.history_for("legacy-job")] == ["recommended", "waiting"]

    with sqlite3.connect(database) as connection:
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(smart_queue_jobs)")}
        metadata_columns = {row[1] for row in connection.execute("PRAGMA table_info(smart_queue_metadata)")}
    assert {"profile_revision", "matcher_policy_revision"}.issubset(job_columns)
    assert {"active_profile_revision", "active_matcher_policy_revision"}.issubset(metadata_columns)


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


def test_rejected_ineligible_duplicate_and_historic_jobs_are_never_selected(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    submitted = _candidate(1, 99)
    rejected = _candidate(2, 98)
    skipped = _candidate(3, 97, url="https://www.indeed.com/viewjob?jk=synthetic-3")
    queue.add_recommendations([submitted, rejected, skipped])
    queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(_urls(submitted, rejected, skipped), actor="agent")
    queue.confirm_outcome(submitted.job_id, "submitted", actor="user")
    queue.confirm_outcome(rejected.job_id, "rejected", actor="user")
    queue.confirm_outcome(skipped.job_id, "skipped", actor="user")

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


def test_visible_snapshot_marks_missing_active_jobs_awaiting_outcome_and_opens_slots(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 8)]
    queue.add_recommendations(candidates)
    initial = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(initial.urls_to_open, actor="agent")

    remaining_urls = initial.urls_to_open[2:]
    queue.record_visible_snapshot(remaining_urls, actor="agent")
    replacement = queue.plan_refill(open_urls=remaining_urls)

    assert queue.get("job-1").state == "awaiting_outcome"
    assert queue.get("job-2").state == "awaiting_outcome"
    assert replacement.job_ids == ()
    assert replacement.urls_to_open == ()
    assert replacement.search_needed == 0
    assert [event.name for event in queue.history_for("job-1")] == ["recommended", "waiting", "open", "missing"]

    queue.confirm_outcome("job-1", "skipped", actor="user")
    queue.confirm_outcome("job-2", "skipped", actor="user")
    replacement = queue.plan_refill(open_urls=remaining_urls)
    assert replacement.job_ids == ("job-6", "job-7")
    assert replacement.urls_to_open == tuple(candidate.source_url for candidate in candidates[5:])


def test_insufficient_pool_reports_how_many_more_jobs_search_must_supply(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    initial = [_candidate(number, 100 - number) for number in range(1, 6)]
    queue.add_recommendations(initial)
    first = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(first.urls_to_open, actor="agent")
    three_still_open = first.urls_to_open[:3]
    queue.record_visible_snapshot(three_still_open, actor="agent")
    queue.confirm_outcome("job-4", "skipped", actor="user")
    queue.confirm_outcome("job-5", "skipped", actor="user")

    short = queue.plan_refill(open_urls=three_still_open)

    assert short.urls_to_open == ()
    assert short.search_needed == 2

    replacements = [_candidate(6, 93), _candidate(7, 92)]
    queue.add_recommendations(replacements)
    refill = queue.plan_refill(open_urls=three_still_open)
    queue.record_visible_snapshot([*three_still_open, *refill.urls_to_open], actor="agent")

    assert refill.urls_to_open == tuple(candidate.source_url for candidate in replacements)
    assert refill.search_needed == 0
    assert sum(queue.get(candidate.job_id).state == "open" for candidate in [*initial, *replacements]) == 5


def test_queue_rejects_more_than_five_known_visible_listings(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    queue.add_recommendations(candidates)
    visible = [candidate.source_url for candidate in candidates]

    with pytest.raises(QueuePolicyError, match="five"):
        queue.plan_refill(open_urls=visible)
    with pytest.raises(QueuePolicyError, match="five"):
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

    with pytest.raises(QueuePolicyError, match="user"):
        queue.confirm_outcome(candidate.job_id, outcome, actor="agent")

    queue.confirm_outcome(candidate.job_id, outcome, actor="user")
    assert queue.get(candidate.job_id).state == outcome
    assert queue.confirmed_submitted_count() == (1 if outcome == "submitted" else 0)


def test_confirmed_job_still_occupies_a_physical_slot_while_its_tab_is_visible(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(number, 100 - number) for number in range(1, 7)]
    queue.add_recommendations(candidates)
    first = queue.plan_refill(open_urls=[])
    queue.record_visible_snapshot(first.urls_to_open, actor="agent")
    queue.confirm_outcome("job-1", "submitted", actor="user")

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
    original.confirm_outcome("job-1", "submitted", actor="user")
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


def test_confirmed_outcome_events_are_replayable_user_owned_records(tmp_path: Path):
    queue = SmartJobQueue(tmp_path / "queue.sqlite3")
    candidates = [_candidate(1, 99), _candidate(2, 98)]
    queue.add_recommendations(candidates)
    queue.plan_refill(open_urls=[])
    queue.confirm_outcome("job-1", "submitted", actor="user")
    queue.confirm_outcome("job-2", "skipped", actor="user")

    events = queue.confirmed_outcome_events()

    assert [(event.job_id, event.name, event.actor) for event in events] == [
        ("job-1", "submitted", "user"),
        ("job-2", "skipped", "user"),
    ]
    assert queue.confirmed_outcome_events(after_event_id=events[0].event_id) == (events[1],)
