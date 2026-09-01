from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "job_profession" / "src"))

from job_profession.models import JobListing, MatchResult
from job_profession.tracker import (
    ApplicationRecord,
    InvalidTransition,
    Tracker,
    WorkbookDependencyError,
    export_tracker,
    transition_application,
)


def _listing() -> JobListing:
    return JobListing(
        platform="linkedin",
        title="Python Backend Developer",
        company="Example Co",
        url="https://www.linkedin.com/jobs/view/123?utm_source=alert",
        location="Bengaluru, Karnataka",
        work_mode="remote",
        description="Maintain FastAPI APIs and add tested features.",
        source_job_id="123",
        discovered_at="2026-08-31T09:00:00+00:00",
    )


def _match() -> MatchResult:
    return MatchResult(
        score=90,
        decision="recommended",
        reasons=["direct professional skill match: python, fastapi"],
        gaps=[],
        evidence_explanations=["python, fastapi: professional evidence supports this match."],
        score_explanation="Profile-fit score, not hiring odds.",
    )


def test_duplicate_listing_keeps_one_job_and_an_auditable_observation_history(tmp_path: Path):
    tracker = Tracker(tmp_path / "jobs.sqlite3")

    first = tracker.record_listing(_listing(), _match())
    second = tracker.record_listing(_listing(), _match())

    assert first.job_id == second.job_id
    assert first.is_new is True
    assert second.is_new is False
    assert len(tracker.list_jobs()) == 1
    assert tracker.observation_count(first.job_id) == 2


def test_only_reviewed_job_can_move_to_ready_to_apply_and_submission_needs_user_actor(tmp_path: Path):
    tracker = Tracker(tmp_path / "jobs.sqlite3")
    application = tracker.record_listing(_listing(), _match())

    with pytest.raises(InvalidTransition):
        tracker.transition_application(application.job_id, "ready_to_apply", actor="automation")
    reviewed = tracker.transition_application(application.job_id, "reviewed", actor="user")
    ready = tracker.transition_application(reviewed.job_id, "ready_to_apply", actor="user")
    with pytest.raises(InvalidTransition, match="user actor"):
        tracker.transition_application(ready.job_id, "submitted", actor="automation")

    submitted = tracker.transition_application(ready.job_id, "submitted", actor="user")

    assert submitted.status == "submitted"
    assert submitted.submitted_by == "user"
    assert submitted.submitted_at is not None
    assert tracker.history_for(application.job_id)[-1].actor == "user"


@pytest.mark.parametrize("actor", ["User", " user ", "user "])
def test_submission_requires_exact_user_actor_in_tracker(tmp_path: Path, actor: str):
    tracker = Tracker(tmp_path / "jobs.sqlite3")
    application = tracker.record_listing(_listing(), _match())
    reviewed = tracker.transition_application(application.job_id, "reviewed", actor="agent")
    ready = tracker.transition_application(reviewed.job_id, "ready_to_apply", actor="agent")

    with pytest.raises(InvalidTransition, match="user actor"):
        tracker.transition_application(ready.job_id, "submitted", actor=actor)


@pytest.mark.parametrize("actor", ["User", " user ", "user "])
def test_pure_transition_requires_exact_user_actor(actor: str):
    reviewed = ApplicationRecord(job_id="job-1", status="ready_to_apply")

    with pytest.raises(InvalidTransition):
        transition_application(reviewed, "submitted", actor=actor)


def test_pure_transition_records_manual_submission_for_exact_user_actor():
    reviewed = ApplicationRecord(job_id="job-1", status="ready_to_apply")

    submitted = transition_application(reviewed, "submitted", actor="user")

    assert submitted.submitted_by == "user"
    assert submitted.submitted_at is not None


def test_csv_export_contains_auditable_tracker_columns(tmp_path: Path):
    database = tmp_path / "jobs.sqlite3"
    tracker = Tracker(database)
    tracker.record_listing(_listing(), _match())
    output = tmp_path / "Job_Application_Tracker.csv"

    assert export_tracker(database, output) == output

    with output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert {
        "job_id",
        "platform",
        "source_url",
        "score",
        "score_explanation",
        "gaps",
        "fit_score_is_not_offer_probability",
        "status",
    }.issubset(rows[0])
    assert rows[0]["fit_score_is_not_offer_probability"] == "true"


def test_xlsx_export_reports_the_required_workbook_dependency_blocker(tmp_path: Path):
    with pytest.raises(WorkbookDependencyError, match="XLSX"):
        export_tracker(tmp_path / "jobs.sqlite3", tmp_path / "Job_Application_Tracker.xlsx")
