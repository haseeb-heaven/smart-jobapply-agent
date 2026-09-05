"""CandidateProfile validation, fallback, and helper contract tests.

Pins the post-init validation surface that the matcher relies on: skill
evidence groups must be disjoint after case/whitespace normalization,
``evidence_by_skill`` labels must agree with the owning group, and
``from_mapping`` must tolerate malformed/non-mapping profile sections by
falling back to empty defaults instead of raising.
"""

from __future__ import annotations

import pytest

from jobapply_agent.models import (
    CandidateProfile,
    DEFAULT_EXCLUDED_TITLE_TERMS,
    _as_strings,
    _normalized_strings,
)


# --- post-init validation ----------------------------------------------------


@pytest.mark.parametrize(
    ("first_field", "second_field"),
    (
        ("professional_skills", "personal_open_source_skills"),
        ("professional_skills", "learning_or_exposure_skills"),
        ("personal_open_source_skills", "learning_or_exposure_skills"),
    ),
)
def test_cross_group_skill_overlap_raises_for_every_group_pair(
    first_field: str, second_field: str
):
    with pytest.raises(ValueError, match="skill evidence groups must be disjoint"):
        CandidateProfile(**{first_field: ("Python",), second_field: ("python",)})


def test_cross_group_overlap_is_detected_after_casefold_and_whitespace_normalization():
    with pytest.raises(ValueError, match="python"):
        CandidateProfile(
            professional_skills=("  Python  ",),
            personal_open_source_skills=("python",),
        )


@pytest.mark.parametrize(
    ("evidence", "match"),
    (
        ({"python": "learning_or_exposure"}, "label conflicts"),
        ({"mystery-skill": "professional"}, "label conflicts"),
        ({"python": "not_a_label"}, "closed evidence labels"),
    ),
)
def test_evidence_label_disagreement_with_skill_group_raises(
    evidence: dict[str, str], match: str
):
    with pytest.raises(ValueError, match=match):
        CandidateProfile(professional_skills=("Python",), evidence_by_skill=evidence)


@pytest.mark.parametrize("years_experience", (True, 2.5, -1, "5"))
def test_years_experience_accepts_only_non_negative_whole_numbers(years_experience: object):
    with pytest.raises(ValueError):
        CandidateProfile(years_experience=years_experience)  # type: ignore[arg-type]


def test_evidence_by_skill_auto_completes_and_normalizes_group_skills():
    profile = CandidateProfile(
        professional_skills=(" Python ",),
        personal_open_source_skills=("Django",),
        learning_or_exposure_skills=("Kubernetes",),
    )

    assert profile.evidence_by_skill == {
        "python": "professional",
        "django": "personal_open_source",
        "kubernetes": "learning_or_exposure",
    }
    assert profile.evidence_label_for("python") == "professional"
    assert profile.evidence_label_for(" DJANGO ") == "personal_open_source"
    assert profile.evidence_label_for("unknown-skill") is None


# --- _as_strings / _normalized_strings ---------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (None, ()),
        ("x", ("x",)),
        ("", ("",)),
        (["a", None, "b"], ("a", "b")),
        ([1, 2], ("1", "2")),
        (7, ()),
        ((3.5,), ("3.5",)),
    ),
)
def test_as_strings_handles_none_strings_and_sequences(value: object, expected: tuple[str, ...]):
    assert _as_strings(value) == expected


def test_normalized_strings_deduplicates_case_insensitively_and_drops_blanks():
    assert _normalized_strings(["  Python ", "python", " FastAPI ", " ", "fastAPI"]) == (
        "python",
        "fastapi",
    )


# --- from_mapping fallbacks --------------------------------------------------


def test_from_mapping_tolerates_malformed_non_mapping_sections():
    profile = CandidateProfile.from_mapping(
        {
            "skills": 42,
            "experience": "junk",
            "location_preferences": ["not", "a", "mapping"],
            "targets": 7,
            "work_authorization": "junk",
            "positioning": [],
            "hard_exclusions": None,
            "roles": None,
            "evidence_by_skill": "junk",
        }
    )

    assert profile.professional_skills == ()
    assert profile.personal_open_source_skills == ()
    assert profile.learning_or_exposure_skills == ()
    assert profile.years_experience is None
    assert profile.role_targets == ()
    assert profile.work_authorizations == ()
    assert profile.mandatory_excluded_requirements == ()
    assert dict(profile.evidence_by_skill) == {}
    # Malformed sections must preserve fail-closed matching defaults.
    assert profile.excluded_title_terms == DEFAULT_EXCLUDED_TITLE_TERMS
    assert profile.location_preferences == ()
    assert profile.employment_type_preferences == ()


def test_from_mapping_populates_missing_evidence_labels_from_skill_groups():
    profile = CandidateProfile.from_mapping(
        {
            "skills": {
                "professional": ["Python"],
                "learning_or_exposure": ["Kubernetes"],
            }
        }
    )

    assert profile.evidence_by_skill == {
        "python": "professional",
        "kubernetes": "learning_or_exposure",
    }
    assert profile.evidence_label_for("kubernetes") == "learning_or_exposure"


def test_from_mapping_stringifies_sequence_items_through_as_strings():
    profile = CandidateProfile.from_mapping({"skills": {"professional": ["python", 42]}})

    assert profile.professional_skills == ("python", "42")
    assert profile.evidence_by_skill == {"python": "professional", "42": "professional"}


def test_from_mapping_top_level_fields_override_nested_sections():
    profile = CandidateProfile.from_mapping(
        {
            "years_experience": 9,
            "experience": {"years_experience": 4},
            "professional_skills": ["go"],
            "skills": {"professional": ["python"]},
        }
    )

    assert profile.years_experience == 9
    assert profile.professional_skills == ("go",)
    assert profile.evidence_by_skill == {"go": "professional"}


@pytest.mark.parametrize(
    ("data", "field", "expected"),
    (
        ({"experience": {"total_years": 5}}, "years_experience", 5),
        ({"roles": {"include": ["Backend Developer"]}}, "role_targets", ("Backend Developer",)),
        ({"positioning": {"focus": ["Software Developer"]}}, "role_targets", ("Software Developer",)),
        (
            {"work_authorization": {"authorized_locations": ["India"]}},
            "work_authorizations",
            ("india",),
        ),
        ({"work_authorization": {"locations": ["India"]}}, "work_authorizations", ("india",)),
        (
            {"targets": {"employment_types": ["Full-Time", "full-time"]}},
            "employment_type_preferences",
            ("full-time",),
        ),
        (
            {"location_preferences": {"locations": ["Hyderabad"]}},
            "location_preferences",
            ("Hyderabad",),
        ),
        (
            {"hard_exclusions": {"mandatory_requirements": ["ML ownership"]}},
            "mandatory_excluded_requirements",
            ("ML ownership",),
        ),
    ),
)
def test_from_mapping_nested_section_fallbacks(
    data: dict[str, object], field: str, expected: object
):
    profile = CandidateProfile.from_mapping(data)

    assert getattr(profile, field) == expected


def test_from_mapping_still_validates_evidence_label_conflicts():
    with pytest.raises(ValueError, match="label conflicts"):
        CandidateProfile.from_mapping(
            {"professional_skills": ["python"], "evidence_by_skill": {"python": "learning_or_exposure"}}
        )


# Seniority-guard default is derived from the production constant so the test
# cannot drift from the shipped policy.
_SENIORITY_DEFAULT = DEFAULT_EXCLUDED_TITLE_TERMS


@pytest.mark.parametrize(
    ("data", "expected"),
    (
        # Omitted exclusions fall back to the documented seniority default.
        ({}, DEFAULT_EXCLUDED_TITLE_TERMS),
        ({"roles": {"include": ["Backend Developer"]}}, DEFAULT_EXCLUDED_TITLE_TERMS),
        ({"roles": {"exclude_title_terms": ["junior"]}}, ("junior",)),
        ({"exclude_title_terms": ["Junior Backend Developer"]}, ("Junior Backend Developer",)),
        # YAML ``null`` decodes to None and means the default has not been overridden.
        ({"exclude_title_terms": None}, DEFAULT_EXCLUDED_TITLE_TERMS),
        ({"roles": {"exclude_title_terms": None}}, DEFAULT_EXCLUDED_TITLE_TERMS),
        # Malformed scalar values must not silently disable the seniority guard.
        ({"exclude_title_terms": 42}, DEFAULT_EXCLUDED_TITLE_TERMS),
        ({"roles": {"exclude_title_terms": 42}}, DEFAULT_EXCLUDED_TITLE_TERMS),
        # An explicitly empty sequence keeps the fail-closed seniority guard ON:
        # silently disabling it would change matching outcomes for existing intakes.
        ({"exclude_title_terms": []}, DEFAULT_EXCLUDED_TITLE_TERMS),
        ({"roles": {"exclude_title_terms": []}}, DEFAULT_EXCLUDED_TITLE_TERMS),
        ({"exclude_title_terms": [None]}, DEFAULT_EXCLUDED_TITLE_TERMS),
    ),
)
def test_from_mapping_excluded_title_terms_default_and_explicit_override(
    data: dict[str, object], expected: tuple[str, ...]
):
    profile = CandidateProfile.from_mapping(data)

    assert profile.excluded_title_terms == expected
    assert CandidateProfile().excluded_title_terms == DEFAULT_EXCLUDED_TITLE_TERMS
