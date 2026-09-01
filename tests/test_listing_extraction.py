from __future__ import annotations

import copy

import pytest

from job_profession.listing_extraction import (
    listing_from_validated_extraction,
    validate_listing_extraction,
)
from job_profession.sources import listing_from_visible_payload


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_url": "https://www.linkedin.com/jobs/view/123456",
        "source_job_id": "123456",
        "observed_at": "2026-09-01T09:30:00+00:00",
        "title": "Python Backend Developer",
        "company": "Example Systems",
        "location": "Hyderabad, India",
        "work_mode": "hybrid",
        "employment_type": "full_time",
        "requirements": [
            {
                "id": "req-1",
                "text": "Three years of professional Python experience",
                "kind": "experience",
                "importance": "mandatory",
                "minimum_years": 3,
                "source_evidence": "Requirements: 3+ years of professional Python experience.",
                "evaluation": {
                    "value": "met",
                    "candidate_evidence_ids": ["employment-role-1", "skill-python-professional"],
                },
            }
        ],
    }


def test_closed_listing_extraction_accepts_provenance_and_evidence_grounded_requirements():
    validated = validate_listing_extraction(_valid_payload())

    assert validated == _valid_payload()
    assert validated["requirements"][0]["evaluation"]["candidate_evidence_ids"] == [
        "employment-role-1",
        "skill-python-professional",
    ]


@pytest.mark.parametrize("field", ["eligibility", "score", "decision", "hard_reject_override"])
def test_llm_cannot_set_decisions_scores_or_override_hard_rejections(field: str):
    payload = _valid_payload()
    payload[field] = True if field == "hard_reject_override" else "recommended"

    with pytest.raises(ValueError, match="closed|field|eligibility|score|decision|override"):
        validate_listing_extraction(payload)


def test_closed_listing_extraction_rejects_unknown_top_level_fields():
    payload = _valid_payload()
    payload["model_reasoning"] = "Trust me"

    with pytest.raises(ValueError, match="closed|field"):
        validate_listing_extraction(payload)


def test_closed_requirement_rejects_unknown_fields():
    payload = _valid_payload()
    payload["requirements"][0]["agent_decision"] = "ignore mandatory gap"

    with pytest.raises(ValueError, match="closed|field"):
        validate_listing_extraction(payload)


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        (("work_mode",), "sometimes_remote"),
        (("employment_type",), "permanent-ish"),
        (("requirements", 0, "kind"), "interesting"),
        (("requirements", 0, "importance"), "optional-ish"),
    ],
)
def test_listing_extraction_rejects_invalid_enums(field_path: tuple[str | int, ...], value: str):
    payload = _valid_payload()
    target = payload
    for segment in field_path[:-1]:
        target = target[segment]
    target[field_path[-1]] = value

    with pytest.raises(ValueError, match=str(field_path[-1])):
        validate_listing_extraction(payload)


@pytest.mark.parametrize(
    "observed_at",
    [
        "2026-09-01",
        "2026-09-01 09:30:00",
        "2026-09-01T09:30:00",
        "not-a-timestamp",
    ],
)
def test_listing_extraction_requires_timezone_aware_rfc3339_observation(observed_at: str):
    payload = _valid_payload()
    payload["observed_at"] = observed_at

    with pytest.raises(ValueError, match="observed_at|timestamp|timezone"):
        validate_listing_extraction(payload)


@pytest.mark.parametrize(
    "source_url",
    [
        "http://www.linkedin.com/jobs/view/123456",
        "javascript:alert(1)",
        "file:///tmp/listing.html",
        "https://user:password@example.com/jobs/123",
    ],
)
def test_listing_extraction_requires_credential_free_https_provenance(source_url: str):
    payload = _valid_payload()
    payload["source_url"] = source_url

    with pytest.raises(ValueError, match="source_url|HTTPS|credential"):
        validate_listing_extraction(payload)


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.linkedin.com:443/jobs/view/123456",
        "https://in.indeed.com:444/viewjob?jk=indeed-job-123",
    ],
)
def test_listing_extraction_rejects_any_explicit_port(source_url: str):
    payload = _valid_payload()
    payload["source_url"] = source_url

    with pytest.raises(ValueError, match="credential-free HTTPS"):
        validate_listing_extraction(payload)


@pytest.mark.parametrize("missing_field", ["source_url", "source_job_id", "observed_at", "title", "company"])
def test_listing_extraction_rejects_missing_required_provenance(missing_field: str):
    payload = _valid_payload()
    del payload[missing_field]

    with pytest.raises(ValueError, match=missing_field):
        validate_listing_extraction(payload)


@pytest.mark.parametrize("missing_field", ["id", "text", "source_evidence", "evaluation"])
def test_requirement_rejects_missing_source_or_evaluation_evidence(missing_field: str):
    payload = _valid_payload()
    del payload["requirements"][0][missing_field]

    with pytest.raises(ValueError, match=missing_field):
        validate_listing_extraction(payload)


def test_requirement_rejects_blank_source_evidence():
    payload = _valid_payload()
    payload["requirements"][0]["source_evidence"] = "  "

    with pytest.raises(ValueError, match="source_evidence"):
        validate_listing_extraction(payload)


@pytest.mark.parametrize(
    ("value", "candidate_evidence_ids"),
    [
        ("met", ["employment-role-1"]),
        ("partial", ["skill-python-professional"]),
        ("missing", []),
        ("unknown", []),
    ],
)
def test_requirement_evaluation_accepts_only_defined_uncertainty_values(
    value: str,
    candidate_evidence_ids: list[str],
):
    payload = _valid_payload()
    payload["requirements"][0]["evaluation"] = {
        "value": value,
        "candidate_evidence_ids": candidate_evidence_ids,
    }

    validated = validate_listing_extraction(payload)

    assert validated["requirements"][0]["evaluation"] == payload["requirements"][0]["evaluation"]


@pytest.mark.parametrize("value", ["verified", "eligible", "pass", "recommended", "true"])
def test_requirement_evaluation_uses_only_closed_uncertainty_values(value: str):
    payload = _valid_payload()
    payload["requirements"][0]["evaluation"]["value"] = value

    with pytest.raises(ValueError, match="evaluation|value"):
        validate_listing_extraction(payload)


def test_requirement_evaluation_rejects_unknown_fields_and_requires_candidate_evidence_ids():
    missing_ids = _valid_payload()
    del missing_ids["requirements"][0]["evaluation"]["candidate_evidence_ids"]
    with pytest.raises(ValueError, match="candidate_evidence_ids"):
        validate_listing_extraction(missing_ids)

    extra_field = _valid_payload()
    extra_field["requirements"][0]["evaluation"]["override"] = True
    with pytest.raises(ValueError, match="closed|field|override"):
        validate_listing_extraction(extra_field)


def test_missing_or_unknown_requirement_cannot_claim_candidate_evidence():
    for value in ("missing", "unknown"):
        payload = copy.deepcopy(_valid_payload())
        payload["requirements"][0]["evaluation"] = {
            "value": value,
            "candidate_evidence_ids": ["fabricated-evidence"],
        }

        with pytest.raises(ValueError, match="candidate_evidence_ids|missing|unknown"):
            validate_listing_extraction(payload)


@pytest.mark.parametrize(
    "source_url",
    (
        "https://example.com/jobs/view/123456",
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/jobs/search/?keywords=python",
        "https://www.linkedin.com/jobs/application/settings",
        "https://in.indeed.com/account/login",
        "https://in.indeed.com/jobs?q=python",
        "https://smartapply.indeed.com/beta/indeedapply/form/review-module?jk=123456",
    ),
)
def test_listing_extraction_accepts_only_canonical_supported_board_listing_urls(source_url: str):
    payload = _valid_payload()
    payload["source_url"] = source_url

    with pytest.raises(ValueError, match="source_url|listing|LinkedIn|Indeed"):
        validate_listing_extraction(payload)


def test_canonical_indeed_listing_url_is_accepted_with_matching_source_identity():
    payload = _valid_payload()
    payload["source_url"] = "https://in.indeed.com/viewjob?jk=indeed-job-123"
    payload["source_job_id"] = "indeed-job-123"

    validated = validate_listing_extraction(payload)

    assert validated["source_url"] == payload["source_url"]
    assert validated["source_job_id"] == "indeed-job-123"


@pytest.mark.parametrize(
    ("source_url", "source_job_id"),
    (
        ("https://www.linkedin.com/jobs/view/123456", "654321"),
        ("https://in.indeed.com/viewjob?jk=indeed-job-123", "different-job"),
    ),
)
def test_source_job_id_must_match_canonical_url_identity(source_url: str, source_job_id: str):
    payload = _valid_payload()
    payload["source_url"] = source_url
    payload["source_job_id"] = source_job_id

    with pytest.raises(ValueError, match="source_job_id|identity|URL"):
        validate_listing_extraction(payload)


@pytest.mark.parametrize("subject", (None, "", "RabbitMQ"))
def test_atomic_skill_requirement_needs_a_subject_bound_to_visible_evidence(
    subject: str | None,
):
    payload = _valid_payload()
    requirement = payload["requirements"][0]
    requirement.update(
        {
            "kind": "skill",
            "text": "Cassandra is required",
            "source_evidence": "Cassandra is required for this role.",
            "minimum_years": None,
        }
    )
    if subject is not None:
        requirement["subject"] = subject

    with pytest.raises(ValueError, match="subject|visible evidence"):
        validate_listing_extraction(payload)


def test_matcher_model_discards_llm_candidate_evaluation_entirely():
    payload = _valid_payload()
    requirement = payload["requirements"][0]
    requirement.update(
        {
            "kind": "skill",
            "text": "Cassandra is required",
            "subject": "Cassandra",
            "source_evidence": "Cassandra is required for this role.",
            "minimum_years": None,
            "evaluation": {
                "value": "met",
                "candidate_evidence_ids": ["llm-claimed-evidence"],
            },
        }
    )

    listing = listing_from_validated_extraction(payload)

    assert listing.requirements[0].subject == "Cassandra"
    assert not hasattr(listing.requirements[0], "evaluation")
    assert "llm-claimed-evidence" not in repr(listing)


def test_visible_payload_adapter_routes_closed_extraction_to_structured_matcher_model():
    payload = _valid_payload()

    listing = listing_from_visible_payload({"extraction": payload}, platform="linkedin")

    assert listing.employment_type == "full_time"
    assert listing.requirements[0].requirement_id == "req-1"
    assert listing.requirements[0].minimum_years == 3


def test_visible_payload_adapter_rejects_extraction_platform_confusion():
    with pytest.raises(ValueError, match="platform|source"):
        listing_from_visible_payload({"extraction": _valid_payload()}, platform="indeed")
