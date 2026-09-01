"""Synthetic integration coverage for the agent-assisted, candidate-controlled loop.

These tests intentionally join existing public boundaries instead of defining a
new orchestrator.  They use no browser, network, resume contents, or private
candidate data.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from jobapply_agent.intake import activate_candidate_profile, validate_candidate_intake
from jobapply_agent.listing_extraction import (
    listing_from_validated_extraction,
    validate_listing_extraction,
)
from jobapply_agent.matcher import score_job
from jobapply_agent.models import JobListing
from jobapply_agent.smart_queue import QueueCandidate, SmartJobQueue
from jobapply_agent.tracker_lifecycle import LifecycleTracker


PROJECT_ROOT = Path(__file__).parents[1]


def _load_discover_module():
    script_path = PROJECT_ROOT / "jobapply_agent" / "scripts" / "discover.py"
    spec = importlib.util.spec_from_file_location("discover_for_workflow_integration", script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ready_intake() -> dict[str, object]:
    professional = [
        "Python",
        "FastAPI",
        "REST API",
        "PostgreSQL",
        "unit testing",
        "integrations",
    ]
    return {
        "schema_version": 1,
        "documents": [
            {
                "document_id": "synthetic-resume",
                "path": "/synthetic/candidate-resume.pdf",
                "sha256": "a" * 64,
                "media_type": "application/pdf",
                "review_state": "untrusted",
            }
        ],
        "approved_facts": {
            "experience": {"years_experience": 3},
            "roles": {
                "include": ["Python Backend Developer"],
                "exclude_title_terms": ["senior", "lead"],
            },
            "skills": {
                "professional": professional,
                "personal_open_source": ["Docker"],
                "learning_or_exposure": ["Redis"],
                "evidence_by_skill": {skill: "professional" for skill in professional},
            },
            "location_preferences": {
                "locations": ["Hyderabad"],
                "work_modes": ["hybrid"],
            },
        },
        "unknown_fields": [],
        "contradictions": [],
        "pending_facts": [],
    }


def _listing_extraction(job_number: int, *, mandatory_skill: str | None = None) -> dict[str, object]:
    requirements: list[dict[str, object]] = [
        {
            "id": f"req-{job_number}-experience",
            "text": "Three years of professional Python experience",
            "kind": "experience",
            "importance": "mandatory",
            "minimum_years": 3,
            "source_evidence": "Requires 3+ years of professional Python experience.",
            "evaluation": {
                "value": "met",
                "candidate_evidence_ids": ["experience-approved", "skill-python-professional"],
            },
        },
        {
            "id": f"req-{job_number}-delivery",
            "text": "Maintain FastAPI REST APIs with PostgreSQL and unit tests",
            "kind": "responsibility",
            "importance": "mandatory",
            "minimum_years": None,
            "source_evidence": (
                "Maintain FastAPI REST APIs, implement features, write unit tests, "
                "work with PostgreSQL, and deliver API integrations."
            ),
            "evaluation": {
                "value": "met",
                "candidate_evidence_ids": ["skill-fastapi-professional"],
            },
        },
    ]
    if mandatory_skill is not None:
        requirements.append(
            {
                "id": f"req-{job_number}-unsupported",
                "text": f"{mandatory_skill} is required",
                "kind": "skill",
                "importance": "mandatory",
                "minimum_years": None,
                "subject": mandatory_skill,
                "source_evidence": f"{mandatory_skill} is required for this role.",
                "evaluation": {"value": "missing", "candidate_evidence_ids": []},
            }
        )
    return {
        "schema_version": 1,
        "source_url": f"https://www.linkedin.com/jobs/view/{100_000 + job_number}",
        "source_job_id": str(100_000 + job_number),
        "observed_at": "2026-09-01T09:30:00+00:00",
        "title": "Python Backend Developer",
        "company": f"Synthetic Company {job_number}",
        "location": "Hyderabad, India",
        "work_mode": "hybrid",
        "employment_type": "full_time",
        "requirements": requirements,
    }


def _job_listing(extraction: dict[str, object]) -> JobListing:
    return listing_from_validated_extraction(extraction)


def test_verified_profile_drives_five_job_queue_refill_and_user_owned_lifecycle(tmp_path: Path):
    discover = _load_discover_module()
    draft = validate_candidate_intake(_ready_intake())
    active = activate_candidate_profile(draft, actor="user")
    intake_path = tmp_path / "private" / "candidate_intake.json"
    intake_path.parent.mkdir(parents=True)
    intake_path.write_text(json.dumps(active), encoding="utf-8")

    profile = discover.active_candidate_profile(intake_path)
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3")
    tracker = LifecycleTracker(tmp_path / "lifecycle.sqlite3")
    recommendations: list[QueueCandidate] = []
    for job_number in range(1, 8):
        extraction = validate_listing_extraction(_listing_extraction(job_number))
        result = score_job(profile, _job_listing(extraction))
        assert result.decision == "recommended"
        recommendations.append(
            QueueCandidate(
                job_id=str(extraction["source_job_id"]),
                source_url=str(extraction["source_url"]),
                fit_score=result.score,
                eligible=True,
                decision=result.decision,
                evidence=tuple(result.reasons),
                profile_revision="integration-profile-v1",
                matcher_policy_revision="integration-policy-v1",
            )
        )

    queue.add_recommendations(recommendations)
    initial = queue.plan_refill(open_urls=[])
    assert initial.job_ids == tuple(candidate.job_id for candidate in recommendations[:5])
    assert len(initial.urls_to_open) == 5
    assert initial.search_needed == 0
    queue.record_visible_snapshot(initial.urls_to_open, actor="agent")

    for candidate in recommendations[:5]:
        tracker.shortlist(candidate.job_id, candidate.source_url, actor="agent")
    review_round = tracker.create_round("synthetic-round-1", initial.job_ids, actor="agent")
    assert review_round.job_ids == initial.job_ids

    submitted_job_id = initial.job_ids[0]
    remaining_visible_urls = initial.urls_to_open[1:]
    queue.record_visible_snapshot(remaining_visible_urls, actor="agent")
    assert queue.get(submitted_job_id).state == "awaiting_outcome"

    queue.confirm_outcome(submitted_job_id, "submitted", actor="user")
    assert queue.get(submitted_job_id).state == "submitted"
    assert queue.confirmed_submitted_count() == 1

    tracker.record_tab_event(submitted_job_id, "opened", actor="agent")
    tracker.transition(submitted_job_id, "manual_applying", actor="user")
    tracker.transition(submitted_job_id, "submitted", actor="user")
    tracker.transition(submitted_job_id, "interview", actor="user")
    assert tracker.get_job(submitted_job_id).state == "interview"
    assert tracker.unique_manual_submitted_count() == 1

    refill = queue.plan_refill(open_urls=remaining_visible_urls)
    assert refill.job_ids == (recommendations[5].job_id,)
    assert refill.urls_to_open == (recommendations[5].source_url,)
    assert refill.search_needed == 0
    queue.record_visible_snapshot([*remaining_visible_urls, *refill.urls_to_open], actor="agent")
    assert sum(queue.get(candidate.job_id).state == "open" for candidate in recommendations) == 5


def test_unsupported_mandatory_skill_is_rejected_before_queue_admission(tmp_path: Path):
    discover = _load_discover_module()
    active = activate_candidate_profile(validate_candidate_intake(_ready_intake()), actor="user")
    intake_path = tmp_path / "candidate_intake.json"
    intake_path.write_text(json.dumps(active), encoding="utf-8")
    profile = discover.active_candidate_profile(intake_path)

    extraction = validate_listing_extraction(_listing_extraction(90, mandatory_skill="React"))
    result = score_job(profile, _job_listing(extraction))
    queue = SmartJobQueue(tmp_path / "smart-queue.sqlite3")
    if result.decision == "recommended":
        queue.add_recommendations(
            [
                QueueCandidate(
                    job_id=str(extraction["source_job_id"]),
                    source_url=str(extraction["source_url"]),
                    fit_score=result.score,
                    eligible=True,
                    decision=result.decision,
                    evidence=tuple(result.reasons),
                    profile_revision="integration-profile-v1",
                    matcher_policy_revision="integration-policy-v1",
                )
            ]
        )

    assert result.decision == "reject"
    assert result.score == 0
    assert any("mandatory structured skill" in reason for reason in result.reasons)
    assert queue.plan_refill(open_urls=[]).job_ids == ()
    with pytest.raises(KeyError, match="unknown job"):
        queue.get(str(extraction["source_job_id"]))


def test_unconfirmed_intake_cannot_be_discovered_as_an_active_profile(tmp_path: Path):
    discover = _load_discover_module()
    draft = validate_candidate_intake(_ready_intake())
    intake_path = tmp_path / "candidate_intake.json"
    intake_path.write_text(json.dumps(draft), encoding="utf-8")

    with pytest.raises(ValueError, match="active"):
        discover.active_candidate_profile(intake_path)
