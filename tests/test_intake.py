from __future__ import annotations

import copy

import pytest

from job_profession.intake import (
    activate_candidate_profile,
    completion_questions,
    pending_verification_batch,
    validate_candidate_intake,
    validate_active_candidate_profile,
)


def _draft_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "documents": [
            {
                "document_id": "resume-primary",
                "path": "documents/resume.pdf",
                "sha256": "a" * 64,
                "media_type": "application/pdf",
                "review_state": "untrusted",
            }
        ],
        "approved_facts": {
            "experience.total_years": 3,
            "skills.python.years": 3,
            "skills.django.years": 1,
        },
        "unknown_fields": [
            "work_authorization",
            "sponsorship",
            "eeo",
            "compensation",
            "availability.start_date",
            "employment.current.start_date",
            "employment.current.end_date",
        ],
        "contradictions": [
            {
                "field": "experience.current_title",
                "status": "VERIFY",
                "values": ["Backend Engineer", "Python Developer"],
                "evidence_ids": ["resume-primary", "candidate-note-1"],
            }
        ],
        "pending_facts": [
            {
                "field": "location_preferences",
                "status": "VERIFY",
                "question": "Which locations and work modes are acceptable?",
                "reason": "No candidate-approved preference is available.",
            }
        ],
    }


def _ready_payload() -> dict[str, object]:
    payload = copy.deepcopy(_draft_payload())
    payload["unknown_fields"] = []
    payload["contradictions"] = []
    payload["pending_facts"] = []
    return payload


def test_resume_is_registered_as_untrusted_metadata_without_document_contents():
    draft = validate_candidate_intake(_draft_payload())

    assert draft["documents"] == [
        {
            "document_id": "resume-primary",
            "path": "documents/resume.pdf",
            "sha256": "a" * 64,
            "media_type": "application/pdf",
            "review_state": "untrusted",
        }
    ]
    assert "contents" not in draft["documents"][0]
    assert "extracted_text" not in draft["documents"][0]


@pytest.mark.parametrize("content_field", ["contents", "text", "extracted_text", "bytes"])
def test_resume_document_payload_rejects_persisted_content(content_field: str):
    payload = _draft_payload()
    payload["documents"][0][content_field] = "untrusted resume contents"

    with pytest.raises(ValueError, match="document|closed|field|content"):
        validate_candidate_intake(payload)


def test_draft_preserves_explicit_unknowns_contradictions_and_pending_facts():
    draft = validate_candidate_intake(_draft_payload())

    assert draft["state"] == "draft"
    assert draft["unknown_fields"] == [
        "work_authorization",
        "sponsorship",
        "eeo",
        "compensation",
        "availability.start_date",
        "employment.current.start_date",
        "employment.current.end_date",
    ]
    assert draft["contradictions"][0]["status"] == "VERIFY"
    assert draft["contradictions"][0]["field"] == "experience.current_title"
    assert draft["pending_facts"][0]["status"] == "VERIFY"
    assert draft["pending_facts"][0]["field"] == "location_preferences"


def test_agent_receives_one_organized_pending_verification_batch():
    draft = validate_candidate_intake(_draft_payload())

    batch = pending_verification_batch(draft)

    assert batch == {
        "unknown_fields": draft["unknown_fields"],
        "contradictions": [{"field": "experience.current_title", "status": "VERIFY"}],
        "pending_facts": [{"field": "location_preferences", "status": "VERIFY"}],
    }


def test_completion_questions_covers_unknown_contradiction_and_pending_prompts():
    draft = validate_candidate_intake(_draft_payload())

    questions = completion_questions(draft)

    assert any("verification conflict" in item for item in questions)
    assert any("Provide a candidate-confirmed value" in item for item in questions)
    assert any("pending candidate verification item" in item for item in questions)
    assert "experience.current_title" in questions[0]


def test_outward_intake_helpers_redact_candidate_values_ids_reasons_and_questions():
    payload = _draft_payload()
    payload["contradictions"][0]["values"] = ["PRIVATE_CANDIDATE_VALUE", "PRIVATE_OTHER_VALUE"]
    payload["contradictions"][0]["evidence_ids"] = ["PRIVATE_EVIDENCE_ID"]
    payload["pending_facts"][0]["question"] = "PRIVATE_USER_QUESTION"
    payload["pending_facts"][0]["reason"] = "PRIVATE_USER_REASON"
    draft = validate_candidate_intake(payload)

    batch_text = str(pending_verification_batch(draft))
    questions_text = str(completion_questions(draft))

    for private_value in (
        "PRIVATE_CANDIDATE_VALUE",
        "PRIVATE_OTHER_VALUE",
        "PRIVATE_EVIDENCE_ID",
        "PRIVATE_USER_QUESTION",
        "PRIVATE_USER_REASON",
    ):
        assert private_value not in batch_text
        assert private_value not in questions_text


def test_outward_intake_helpers_replace_unallowlisted_fields_with_opaque_references():
    payload = _draft_payload()
    payload["unknown_fields"] = ["private.candidate.answer"]
    payload["contradictions"][0]["field"] = "private.candidate.conflict"
    payload["pending_facts"][0]["field"] = "private.candidate.pending"
    draft = validate_candidate_intake(payload)

    batch = pending_verification_batch(draft)
    questions = completion_questions(draft)

    assert batch["unknown_fields"] == ["review-unknown-1"]
    assert batch["contradictions"] == [{"field": "review-contradiction-1", "status": "VERIFY"}]
    assert batch["pending_facts"] == [{"field": "review-pending-1", "status": "VERIFY"}]
    assert all("private.candidate" not in question for question in questions)
    assert all(reference in " ".join(questions) for reference in (
        "review-contradiction-1",
        "review-unknown-1",
        "review-pending-1",
    ))


@pytest.mark.parametrize(
    "actor",
    ["agent", "automation", "system", "llm", "", "User", " user ", "user "],
)
def test_only_user_actor_can_activate_a_candidate_profile(actor: str):
    draft = validate_candidate_intake(_ready_payload())

    with pytest.raises(PermissionError, match="user"):
        activate_candidate_profile(draft, actor=actor)


def test_activation_requires_an_explicit_candidate_confirmed_fact():
    payload = _ready_payload()
    payload["approved_facts"] = {}
    draft = validate_candidate_intake(payload)

    with pytest.raises(ValueError, match="candidate-confirmed|approved_facts"):
        activate_candidate_profile(draft, actor="user")


@pytest.mark.parametrize(
    "empty_fact",
    [None, "", " \t\n", [], {}, {"nested": []}, {"nested": {"deeper": {}}}],
    ids=["null", "blank", "whitespace", "empty-list", "empty-map", "nested-empty-list", "nested-empty-map"],
)
def test_activation_rejects_semantically_empty_approved_facts(empty_fact: object):
    payload = _ready_payload()
    payload["approved_facts"] = {"candidate.confirmed": empty_fact}
    draft = validate_candidate_intake(payload)

    with pytest.raises(ValueError, match="explicit candidate-confirmed fact"):
        activate_candidate_profile(draft, actor="user")


@pytest.mark.parametrize(
    "approved_facts",
    [
        {"valid": 1, "authorization": None},
        {"valid": False, "availability": " \t\n"},
        {"valid": 0, "locations": []},
        {"valid": 1, "preferences": {}},
        {"valid": 1, "profile": {"nested": []}},
        {"valid": 1, "profile": {"nested": {"deeper": {}}}},
        {"valid": 1, "profile": {"title": "Engineer", "authorization": None}},
        {"valid": 1, "profile": [{"title": "Engineer"}, {"authorization": ""}]},
        {"valid": 1, "profile": {"titles": ["Engineer"], "authorization": []}},
        {"valid": 1, "profile": [{"title": "Engineer"}, {"authorization": {"sponsorship": None}}]},
    ],
    ids=[
        "null-sibling",
        "blank-sibling",
        "empty-list-sibling",
        "empty-map-sibling",
        "nested-empty-list-sibling",
        "recursively-empty-sibling",
        "nested-dict-mixed-null-sibling",
        "nested-list-mixed-blank-sibling",
        "nested-dict-list-empty-sibling",
        "nested-list-dict-empty-sibling",
    ],
)
def test_activation_rejects_semantically_empty_mixed_approved_facts(
    approved_facts: dict[str, object],
):
    payload = _ready_payload()
    payload["approved_facts"] = approved_facts
    draft = validate_candidate_intake(payload)

    with pytest.raises(ValueError, match="approved_facts|semantically empty"):
        activate_candidate_profile(draft, actor="user")


@pytest.mark.parametrize(
    "explicit_fact",
    [False, 0, 0.0, [False], {"nested": 0}],
    ids=["false", "zero", "float-zero", "nested-false", "nested-zero"],
)
def test_activation_preserves_false_and_zero_as_explicit_facts(explicit_fact: object):
    payload = _ready_payload()
    payload["approved_facts"] = {"candidate.confirmed": explicit_fact}
    draft = validate_candidate_intake(payload)

    active = activate_candidate_profile(draft, actor="user")

    assert active["approved_facts"] == {"candidate.confirmed": explicit_fact}


def test_user_activation_creates_a_stable_revision_from_approved_facts():
    draft = validate_candidate_intake(_ready_payload())

    first = activate_candidate_profile(draft, actor="user")
    repeated = activate_candidate_profile(draft, actor="user")

    assert first["state"] == "active"
    assert first["activated_by"] == "user"
    assert isinstance(first["confirmed_at"], str)
    assert first["confirmed_at"]
    assert first["approved_facts"] == draft["approved_facts"]
    assert first["unknown_fields"] == []
    assert first["revision_hash"] == repeated["revision_hash"]


def test_persisted_active_profile_rejects_reintroduced_unknown_fields():
    active = activate_candidate_profile(validate_candidate_intake(_ready_payload()), actor="user")
    active["unknown_fields"] = ["work_authorization"]

    with pytest.raises(ValueError, match="unknown_fields"):
        validate_active_candidate_profile(active)


def test_revision_hash_changes_when_candidate_approved_facts_change():
    first_draft = validate_candidate_intake(_ready_payload())
    changed_payload = copy.deepcopy(_ready_payload())
    changed_payload["approved_facts"]["skills.django.years"] = 2
    changed_draft = validate_candidate_intake(changed_payload)

    first = activate_candidate_profile(first_draft, actor="user")
    changed = activate_candidate_profile(changed_draft, actor="user")

    assert first["revision_hash"] != changed["revision_hash"]


def test_sensitive_fields_never_receive_default_values():
    draft = validate_candidate_intake(_draft_payload())

    sensitive_fields = {
        "work_authorization",
        "sponsorship",
        "eeo",
        "compensation",
        "availability.start_date",
        "employment.current.start_date",
        "employment.current.end_date",
    }
    assert sensitive_fields.isdisjoint(draft["approved_facts"])
    assert sensitive_fields.issubset(draft["unknown_fields"])
    with pytest.raises(ValueError, match="unknown_fields"):
        activate_candidate_profile(draft, actor="user")


@pytest.mark.parametrize("unresolved_field", ["unknown_fields", "contradictions", "pending_facts"])
def test_activation_rejects_unresolved_candidate_review_items(unresolved_field: str):
    payload = _ready_payload()
    payload[unresolved_field] = copy.deepcopy(_draft_payload()[unresolved_field])
    draft = validate_candidate_intake(payload)

    with pytest.raises(ValueError, match=f"unresolved|{unresolved_field}"):
        activate_candidate_profile(draft, actor="user")


def test_revision_hash_covers_unknown_review_state_and_document_evidence_metadata():
    base_payload = _ready_payload()
    changed_facts = copy.deepcopy(base_payload)
    changed_facts["approved_facts"]["skills.django.years"] = 2
    changed_document = copy.deepcopy(base_payload)
    changed_document["documents"][0]["sha256"] = "b" * 64

    base = activate_candidate_profile(validate_candidate_intake(base_payload), actor="user")
    facts_revision = activate_candidate_profile(validate_candidate_intake(changed_facts), actor="user")
    document_revision = activate_candidate_profile(validate_candidate_intake(changed_document), actor="user")

    assert base["revision_hash"] != facts_revision["revision_hash"]
    assert base["revision_hash"] != document_revision["revision_hash"]


@pytest.mark.parametrize("review_group", ("unknown_fields", "contradictions", "pending_facts"))
def test_approved_facts_cannot_overlap_any_unresolved_review_state(review_group: str):
    payload = _ready_payload()
    payload["approved_facts"]["work_authorization"] = {
        "authorized_locations": ["India"]
    }
    if review_group == "unknown_fields":
        payload[review_group] = ["work_authorization"]
        assert "work_authorization" in payload[review_group]
    elif review_group == "contradictions":
        payload["unknown_fields"] = [
            field for field in payload["unknown_fields"] if field != "work_authorization"
        ]
        payload[review_group] = [
            {
                "field": "work_authorization",
                "status": "VERIFY",
                "values": ["India", "unknown"],
                "evidence_ids": ["resume-primary", "candidate-note-1"],
            }
        ]
    else:
        payload["unknown_fields"] = [
            field for field in payload["unknown_fields"] if field != "work_authorization"
        ]
        payload[review_group] = [
            {
                "field": "work_authorization",
                "status": "VERIFY",
                "question": "Where are you authorized?",
                "reason": "Authorization must be candidate-approved.",
            }
        ]

    with pytest.raises(ValueError, match="disjoint|overlap"):
        validate_candidate_intake(payload)


def test_dotted_and_nested_approved_fact_duplicates_are_rejected():
    payload = _ready_payload()
    payload["approved_facts"]["skills.professional"] = ["Python"]
    payload["approved_facts"]["skills"] = {"professional": ["Python"]}

    with pytest.raises(ValueError, match="dotted|nested|same path"):
        validate_candidate_intake(payload)


def test_skill_evidence_groups_must_be_disjoint_after_normalization():
    payload = _ready_payload()
    payload["approved_facts"]["skills"] = {
        "professional": [" Python "],
        "personal_open_source": ["python"],
        "learning_or_exposure": [],
    }

    with pytest.raises(ValueError, match="disjoint|python"):
        validate_candidate_intake(payload)


def test_normalized_evidence_label_must_match_approved_skill_group():
    payload = _ready_payload()
    payload["approved_facts"]["skills"] = {
        "professional": ["Python"],
        "personal_open_source": [],
        "learning_or_exposure": [],
        "evidence_by_skill": {" python ": "learning_or_exposure"},
    }

    with pytest.raises(ValueError, match="label conflicts|python"):
        validate_candidate_intake(payload)
