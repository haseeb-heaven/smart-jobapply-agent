from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "jobapply_agent" / "src"))

from jobapply_agent.workflow import ApplicationRecord, prepare_application, transition_application


def test_missing_answer_stops_preparation_and_leaves_field_without_suggestion():
    prepared = prepare_application(
        "job-123",
        form_labels=("Cover letter", "Are you authorized to work in India?"),
        approved_field_values={"Cover letter": "A truthful, job-specific draft."},
    )

    assert prepared.stopped is True
    assert prepared.field_suggestions == {"Cover letter": "A truthful, job-specific draft."}
    assert any("authorized" in prompt.lower() for prompt in prepared.requires_user_answer)


def test_prepared_application_has_no_submit_interface():
    prepared = prepare_application("job-123")

    assert not hasattr(prepared, "submit")
    assert not hasattr(prepared, "click_submit")


def test_incomplete_review_returns_to_needs_review_and_only_user_can_record_submission():
    incomplete = ApplicationRecord(job_id="job-123", status="needs_review")
    returned = transition_application(incomplete, "ready_to_apply", actor="user")
    assert returned.status == "needs_review"

    ready = ApplicationRecord(
        job_id="job-123",
        status="needs_review",
        review_checklist={
            "claims": True,
            "company_name": True,
            "job_title": True,
            "salary_or_visa": True,
            "current_location": True,
            "screening_responses": True,
        },
    )
    ready = transition_application(ready, "ready_to_apply", actor="user")

    with pytest.raises(PermissionError):
        transition_application(ready, "submitted", actor="automation")

    submitted = transition_application(ready, "submitted", actor="user")
    assert submitted.status == "submitted"
    assert submitted.submitted_by == "user"
