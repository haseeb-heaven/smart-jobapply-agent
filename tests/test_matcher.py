from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "jobapply_agent" / "src"))

from jobapply_agent.matcher import load_scoring_rules, score_job
from jobapply_agent.models import CandidateProfile, JobListing, JobRequirement


def profile() -> CandidateProfile:
    return CandidateProfile(
        professional_skills=("python", "fastapi", "rest api", "postgresql", "background jobs", "unit testing", "integrations"),
        role_targets=("Backend Developer", "Python Developer", "FastAPI Developer", "Software Developer", "API Integration Developer"),
        evidence_by_skill={
            "python": "professional",
            "fastapi": "professional",
            "rest api": "professional",
            "postgresql": "professional",
            "background jobs": "professional",
            "unit testing": "professional",
            "integrations": "professional",
        },
    )


def test_yaml_rules_enforce_selective_profile_fit_thresholds():
    rules = load_scoring_rules()

    assert rules["thresholds"] == {"recommended": 85, "review": 70, "reject": 0}
    assert rules["weights"]["verified_professional_skill_fit"] == 30


def test_candidate_profile_rejects_cross_group_skill_overlap_after_normalization():
    with pytest.raises(ValueError, match="disjoint|python"):
        CandidateProfile(
            professional_skills=(" Python ",),
            personal_open_source_skills=("python",),
        )


def test_candidate_profile_rejects_evidence_label_conflicting_with_skill_group():
    with pytest.raises(ValueError, match="label conflicts|python"):
        CandidateProfile(
            professional_skills=("Python",),
            evidence_by_skill={" python ": "learning_or_exposure"},
        )


@pytest.mark.parametrize("subject", ("Cassandra", "RabbitMQ", "C++"))
def test_rejects_any_atomic_mandatory_structured_skill_without_professional_evidence(
    subject: str,
):
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
        requirements=(
            JobRequirement(
                requirement_id="req-skill",
                text=f"{subject} is required",
                kind="skill",
                importance="mandatory",
                subject=subject,
                source_evidence=f"{subject} is required.",
            ),
        ),
    )

    result = score_job(profile(), job)

    assert result.decision == "reject"
    assert result.score == 0
    assert any(subject.casefold() in reason.casefold() for reason in result.reasons)


def test_supported_structured_skill_uses_only_professional_evidence():
    base = profile()
    personal_only = CandidateProfile(
        professional_skills=base.professional_skills,
        personal_open_source_skills=("Cassandra",),
        role_targets=base.role_targets,
        evidence_by_skill={
            **base.evidence_by_skill,
            "Cassandra": "personal_open_source",
        },
    )
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
        requirements=(
            JobRequirement(
                requirement_id="req-cassandra",
                text="Cassandra is required",
                kind="skill",
                importance="mandatory",
                subject="Cassandra",
            ),
        ),
    )

    assert score_job(personal_only, job).decision == "reject"


def test_employment_type_preference_is_a_hard_gate_when_visible():
    base = profile()
    full_time_only = CandidateProfile(
        professional_skills=base.professional_skills,
        role_targets=base.role_targets,
        employment_type_preferences=("full-time",),
        evidence_by_skill=base.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
        employment_type="contract",
    )

    result = score_job(full_time_only, job)

    assert result.decision == "reject"
    assert "employment type is outside approved preferences" in result.gaps


def test_unknown_employment_type_cannot_be_recommended_when_preference_is_restrictive():
    base = profile()
    full_time_only = CandidateProfile(
        professional_skills=base.professional_skills,
        role_targets=base.role_targets,
        employment_type_preferences=("full_time",),
        evidence_by_skill=base.evidence_by_skill,
    )

    result = score_job(
        full_time_only,
        JobListing(
            title="Python Backend Developer",
            description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
        ),
    )

    assert result.decision == "review"
    assert any("employment type is not visible" in gap for gap in result.gaps)


def test_authorization_requirement_uses_only_approved_candidate_authorization():
    base = profile()
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
        requirements=(
            JobRequirement(
                requirement_id="req-auth",
                text="Must be authorized to work in India",
                kind="authorization",
                importance="mandatory",
                subject="India",
            ),
        ),
    )
    unknown = CandidateProfile(
        professional_skills=base.professional_skills,
        role_targets=base.role_targets,
        evidence_by_skill=base.evidence_by_skill,
    )
    mismatched = CandidateProfile(
        professional_skills=base.professional_skills,
        role_targets=base.role_targets,
        work_authorizations=("United Arab Emirates",),
        evidence_by_skill=base.evidence_by_skill,
    )
    approved = CandidateProfile(
        professional_skills=base.professional_skills,
        role_targets=base.role_targets,
        work_authorizations=("India",),
        evidence_by_skill=base.evidence_by_skill,
    )

    assert score_job(unknown, job).decision == "review"
    assert score_job(mismatched, job).decision == "reject"
    assert score_job(approved, job).decision == "recommended"


@pytest.mark.parametrize(
    "description",
    ("No more than 2 years of experience.", "Experience of up to 2 years."),
)
def test_maximum_only_experience_is_enforced(description: str):
    base = profile()
    three_year_profile = CandidateProfile(
        professional_skills=base.professional_skills,
        role_targets=base.role_targets,
        years_experience=3,
        evidence_by_skill=base.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            + description
        ),
    )

    result = score_job(three_year_profile, job)

    assert result.decision == "reject"
    assert any("maximum (2 years)" in reason for reason in result.reasons)


def test_configured_hard_reject_terms_change_matching(tmp_path: Path):
    rules_path = tmp_path / "scoring_rules.yaml"
    rules_path.write_text("hard_reject_terms:\n  - forbidden-token\n", encoding="utf-8")
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. forbidden-token"
        ),
    )

    result = score_job(profile(), job, rules_path=str(rules_path))

    assert result.score == 0
    assert result.decision == "reject"
    assert "configured hard-reject requirement: forbidden-token" in result.reasons


def test_rejects_architect_role_even_when_python_matches():
    job = JobListing(title="Backend Architect", description="Own system architecture and technical strategy with Python")

    result = score_job(profile(), job)

    assert result.decision == "reject"
    assert "architect-level title" in result.reasons


def test_rejects_explicit_technical_strategy_and_people_management_ownership():
    job = JobListing(
        title="Python Backend Developer",
        description="You will own the technical strategy and directly manage engineers.",
    )

    result = score_job(profile(), job)

    assert result.decision == "reject"
    assert any("technical strategy" in reason for reason in result.reasons)
    assert any("people management" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "requirement",
    (
        "Own design of large distributed systems.",
        "Own the design of distributed services.",
        "Design scalable distributed systems.",
    ),
)
def test_rejects_distributed_system_design_requirement_variants(requirement: str):
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. " + requirement
        ),
    )

    result = score_job(profile(), job)

    assert result.score == 0
    assert result.decision == "reject"
    assert "distributed-system design requirement" in result.reasons


def test_rejects_production_ai_requirement_not_supported_by_profile():
    job = JobListing(title="LLM Engineer", description="3 years production GenAI platform ownership required")

    result = score_job(profile(), job)

    assert result.decision == "reject"
    assert any("production GenAI" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "description",
    (
        "Production experience with LLM systems is needed.",
        "Experience operating GenAI services in production.",
    ),
)
def test_rejects_clear_production_genai_experience_requirement_wording(description: str):
    job = JobListing(title="Python Backend Developer", description=description)

    result = score_job(profile(), job)

    assert result.score == 0
    assert result.decision == "reject"
    assert any("production GenAI" in reason for reason in result.reasons)


def test_rejects_approved_mandatory_ml_model_training_or_deployment_exclusion():
    base_profile = profile()
    exclusion_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        mandatory_excluded_requirements=("ML model training or deployment ownership",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            "You must own ML model training and deployment."
        ),
    )

    result = score_job(exclusion_profile, job)

    assert result.score == 0
    assert result.decision == "reject"
    assert "mandatory ML model training/deployment ownership is excluded" in result.reasons


def test_rejects_explicit_unsupported_mandatory_technology_requirements():
    base_profile = profile()
    exclusion_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        mandatory_excluded_requirements=("unsupported technology with no comparable approved evidence",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            "Must have 5 years of Rust and Kafka experience."
        ),
    )

    result = score_job(exclusion_profile, job)

    assert result.score == 0
    assert result.decision == "reject"
    assert "mandatory unsupported technology requirement: kafka, rust" in result.reasons


@pytest.mark.parametrize(
    "mandatory_stack",
    (
        "Node.js and AWS Lambda",
        "Django/Celery",
        ".NET Core",
    ),
)
def test_rejects_controlled_explicit_unsupported_technology_stacks(mandatory_stack: str):
    base_profile = profile()
    exclusion_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        mandatory_excluded_requirements=("unsupported technology with no comparable approved evidence",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            f"Experience with {mandatory_stack} is required."
        ),
    )

    result = score_job(exclusion_profile, job)

    assert result.score == 0
    assert result.decision == "reject"
    assert any("mandatory unsupported technology requirement" in reason for reason in result.reasons)


def test_does_not_reject_incidental_unsupported_technology_mention():
    base_profile = profile()
    exclusion_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        mandatory_excluded_requirements=("unsupported technology with no comparable approved evidence",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            "The wider company also uses Kafka for analytics."
        ),
    )

    result = score_job(exclusion_profile, job)

    assert result.decision == "recommended"
    assert not any("unsupported technology" in reason for reason in result.reasons)


def test_does_not_treat_ordinary_go_word_as_a_required_go_language_skill():
    base_profile = profile()
    exclusion_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        mandatory_excluded_requirements=("unsupported technology with no comparable approved evidence",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            "You must go to the office twice each week."
        ),
    )

    result = score_job(exclusion_profile, job)

    assert result.decision == "recommended"
    assert not any("unsupported technology" in reason for reason in result.reasons)


@pytest.mark.parametrize(
    "ordinary_phrase",
    ("You must react to production incidents.", "You must spark change across the team."),
)
def test_does_not_treat_ordinary_words_as_required_technology_skills(ordinary_phrase: str):
    base_profile = profile()
    exclusion_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        mandatory_excluded_requirements=("unsupported technology with no comparable approved evidence",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. " + ordinary_phrase
        ),
    )

    result = score_job(exclusion_profile, job)

    assert result.decision == "recommended"
    assert not any("unsupported technology" in reason for reason in result.reasons)


def test_does_not_treat_negated_technology_requirement_as_mandatory():
    base_profile = profile()
    exclusion_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        mandatory_excluded_requirements=("unsupported technology with no comparable approved evidence",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL. "
            "Django experience is not required."
        ),
    )

    result = score_job(exclusion_profile, job)

    assert result.decision == "recommended"
    assert not any("mandatory unsupported technology" in reason for reason in result.reasons)


def test_recommends_mid_level_fastapi_maintenance_role():
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(profile(), job)

    assert result.decision == "recommended"
    assert result.score >= 85
    assert "Profile-fit score, not hiring odds." in result.score_explanation
    assert any("professional evidence" in explanation for explanation in result.evidence_explanations)


def test_rejects_title_outside_declared_mid_level_role_targets():
    job = JobListing(
        title="Data Engineer",
        description="Maintain Python FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(profile(), job)

    assert result.score == 0
    assert result.decision == "reject"
    assert "title is outside approved role targets" in result.reasons


def test_scores_are_selective_and_only_review_below_high_confidence_threshold():
    job = JobListing(
        title="Software Engineer",
        description="Maintain Python REST APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(profile(), job)

    assert 70 <= result.score < 85
    assert result.decision == "review"


def test_rejects_general_full_stack_role_without_backend_dominance():
    job = JobListing(
        title="Full Stack Developer",
        description="Build React user interfaces, CSS components, and JavaScript experiences across the frontend.",
    )

    result = score_job(profile(), job)

    assert result.decision == "reject"
    assert any("full-stack" in gap for gap in result.gaps)


def test_full_stack_title_can_be_reviewed_when_backend_work_dominates():
    job = JobListing(
        title="Full Stack Developer",
        description=(
            "Maintain Python FastAPI APIs, PostgreSQL databases, background jobs, REST integrations, "
            "and unit tests. Occasionally update React screens."
        ),
    )

    result = score_job(profile(), job)

    assert result.decision in {"review", "recommended"}
    assert not any("full-stack" in gap for gap in result.gaps)


@pytest.mark.parametrize(
    ("title", "reason"),
    [
        ("Junior Python Developer", "junior-level title"),
        ("Junior-Level FastAPI Developer", "junior-level title"),
        ("Entry Python Developer", "entry-level title"),
        ("Entry-Level API Integration Developer", "entry-level title"),
        ("Software Engineering Intern", "internship title"),
        ("Graduate Backend Developer", "graduate title"),
        ("Sr Python Backend Developer", "senior alias title"),
        ("Sr. Python Backend Developer", "senior alias title"),
        ("SDE II - Python Backend", "explicit job-level suffix"),
        ("Software Development Engineer II", "explicit job-level suffix"),
        ("FastAPI Developer II", "explicit job-level suffix"),
        ("Python Developer I", "explicit job-level suffix"),
        ("SDE 1", "explicit job-level suffix"),
        ("Python Backend Developer III", "explicit job-level suffix"),
        ("Python Developer 3", "explicit job-level suffix"),
        ("Backend Engineer IV", "explicit job-level suffix"),
        ("Python Developer XI", "explicit job-level suffix"),
        ("Backend Engineer XX", "explicit job-level suffix"),
        ("SDE 20", "explicit job-level suffix"),
        ("FastAPI Developer 100", "explicit job-level suffix"),
        ("Python Backend Developer, II", "explicit job-level suffix"),
        ("Python Backend Developer (II)", "explicit job-level suffix"),
        ("Python Backend Developer: II", "explicit job-level suffix"),
        ("Python Backend Developer [II]", "explicit job-level suffix"),
        ("Python Backend Developer, 2", "explicit job-level suffix"),
        ("Python Backend Developer (2)", "explicit job-level suffix"),
        ("Python Backend Developer: 2", "explicit job-level suffix"),
        ("Python Backend Developer [2]", "explicit job-level suffix"),
        ("Python Backend Developer / II", "explicit job-level suffix"),
        ("Python Backend Developer - Level II", "explicit job-level suffix"),
        ("Python Backend Developer (Level II)", "explicit job-level suffix"),
        ("Python Backend Developer: Level II", "explicit job-level suffix"),
        ("Python Backend Developer — (II)", "explicit job-level suffix"),
        ("Python Backend Developer 02", "explicit job-level suffix"),
        ("Python Backend Developer 001", "explicit job-level suffix"),
        ("Python Backend Developer Level 02", "explicit job-level suffix"),
        ("Python Backend Developer L2", "explicit job-level suffix"),
        ("Python Backend Developer #II", "explicit job-level suffix"),
    ],
)
def test_rejects_non_mid_level_title_aliases_before_recommendation(title: str, reason: str):
    job = JobListing(
        title=title,
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(profile(), job)

    assert result.score == 0
    assert result.decision == "reject"
    assert reason in result.reasons


@pytest.mark.parametrize(
    "title",
    ("Python Backend Developer", "Backend Engineer", "Software Engineer", "FastAPI Developer"),
)
def test_ordinary_mid_level_titles_are_not_rejected_by_level_alias_gate(title: str):
    job = JobListing(
        title=title,
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(profile(), job)

    assert result.decision != "reject"
    assert not set(result.reasons).intersection(
        {
            "junior-level title",
            "entry-level title",
            "internship title",
            "graduate title",
            "senior alias title",
            "explicit job-level suffix",
        }
    )


def test_title_experience_years_are_not_misclassified_as_a_job_level_suffix():
    job = JobListing(
        title="Python Developer - 3 Years Experience",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(profile(), job)

    assert result.decision == "recommended"
    assert "explicit job-level suffix" not in result.reasons


def test_rejects_mandatory_experience_above_candidate_approved_years():
    base_profile = profile()
    three_year_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        evidence_by_skill=base_profile.evidence_by_skill,
        years_experience=3,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "10+ years of professional software development experience is mandatory. "
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL."
        ),
    )

    result = score_job(three_year_profile, job)

    assert result.score == 0
    assert result.decision == "reject"
    assert any("experience" in reason for reason in result.reasons)


def test_rejects_onsite_location_outside_explicit_location_allowlist():
    base_profile = profile()
    hyderabad_only_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        role_targets=base_profile.role_targets,
        location_preferences=("Hyderabad",),
        work_mode_preferences=("onsite",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
        location="New York, NY",
        work_mode="onsite",
    )

    result = score_job(hyderabad_only_profile, job)

    assert result.score == 0
    assert result.decision == "reject"
    assert "location is outside approved preferences" in result.gaps


def test_mandatory_unsupported_skill_is_a_hard_reject_without_profile_opt_in():
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Must have 5 years of Rust. Maintain FastAPI APIs, add features, "
            "write unit tests, and work with PostgreSQL."
        ),
    )

    result = score_job(profile(), job)

    assert result.score == 0
    assert result.decision == "reject"
    assert any("rust" in reason.casefold() for reason in result.reasons)


def test_unknown_candidate_experience_cannot_satisfy_mandatory_years():
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Must have 10+ years of professional software development experience. "
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL."
        ),
    )

    result = score_job(profile(), job)

    assert result.decision != "recommended"
    assert any("experience" in value.casefold() for value in (*result.reasons, *result.gaps))


def test_explicit_required_experience_range_enforces_its_maximum():
    base_profile = profile()
    three_year_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        years_experience=3,
        role_targets=base_profile.role_targets,
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "Required: 1-2 years of professional software development experience. "
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL."
        ),
    )

    result = score_job(three_year_profile, job)

    assert result.score == 0
    assert result.decision == "reject"
    assert any("maximum" in reason.casefold() for reason in result.reasons)


def test_preferred_experience_range_does_not_create_a_maximum_gate():
    base_profile = profile()
    three_year_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        years_experience=3,
        role_targets=base_profile.role_targets,
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description=(
            "1-2 years of professional software development experience preferred. "
            "Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL."
        ),
    )

    result = score_job(three_year_profile, job)

    assert result.decision == "recommended"
    assert not any("maximum" in reason.casefold() for reason in result.reasons)


def test_missing_listing_location_and_work_mode_block_recommendation_for_restrictive_preferences():
    base_profile = profile()
    restricted_profile = CandidateProfile(
        professional_skills=base_profile.professional_skills,
        years_experience=3,
        role_targets=base_profile.role_targets,
        location_preferences=("Hyderabad",),
        work_mode_preferences=("onsite",),
        evidence_by_skill=base_profile.evidence_by_skill,
    )
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(restricted_profile, job)

    assert result.decision != "recommended"
    assert any("location" in gap.casefold() for gap in result.gaps)
    assert any("work mode" in gap.casefold() for gap in result.gaps)


# --- exact decision-threshold boundaries -------------------------------------


def test_score_exactly_at_the_recommended_threshold_is_recommended():
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(profile(), job)
    rules = load_scoring_rules()

    assert result.score >= rules["thresholds"]["recommended"]
    assert result.decision == "recommended"
    assert result.gaps == []


def test_score_exactly_at_the_review_threshold_is_review():
    job = JobListing(
        title="Python Backend Developer",
        company="Example Ltd",
        description="Maintain FastAPI APIs.",
    )

    result = score_job(profile(), job)
    rules = load_scoring_rules()

    assert result.score >= rules["thresholds"]["review"]
    assert result.score < rules["thresholds"]["recommended"]
    assert result.decision == "review"
    assert result.decision != "recommended"
    assert any(
        "professional evidence supports this match" in explanation
        for explanation in result.evidence_explanations
    )


def test_score_below_the_review_threshold_without_hard_gaps_is_reject():
    narrow = CandidateProfile(
        professional_skills=("python", "fastapi"),
        role_targets=("Backend Developer",),
        evidence_by_skill={"python": "professional", "fastapi": "professional"},
    )
    job = JobListing(
        title="Python Backend Developer",
        description="Add features and write unit tests while maintaining FastAPI services.",
    )

    result = score_job(narrow, job)
    rules = load_scoring_rules()

    assert result.score < rules["thresholds"]["review"]
    assert result.decision == "reject"


def test_score_at_or_above_recommended_without_direct_professional_evidence_is_never_recommended(
    tmp_path: Path,
):
    rules_path = tmp_path / "high_title_weight.yaml"
    rules_path.write_text(
        "weights:\n"
        "  title_level_fit: 85\n"
        "  verified_professional_skill_fit: 0\n"
        "  responsibility_fit: 0\n"
        "  location_and_work_mode_fit: 0\n"
        "  salary_and_recency_fit: 0\n"
        "  evidence_quality: 0\n",
        encoding="utf-8",
    )
    personal_only = CandidateProfile(
        personal_open_source_skills=("python", "fastapi", "rest api"),
        role_targets=("Backend Developer",),
        evidence_by_skill={
            "python": "personal_open_source",
            "fastapi": "personal_open_source",
            "rest api": "personal_open_source",
        },
    )
    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )

    result = score_job(personal_only, job, rules_path=str(rules_path))

    # Point breakdown under the inline rules file above: title_level_fit is the
    # only nonzero weight (85), so a title match scores exactly 85 while the
    # missing direct professional evidence caps the decision at review.
    assert result.score == 85
    assert result.decision == "review"
    assert result.decision != "recommended"
    assert "high-confidence recommendation requires direct professional evidence" in result.gaps


@pytest.mark.parametrize(
    "requirement",
    (
        JobRequirement(
            requirement_id="req-exp-1",
            text="Relevant experience is required",
            kind="experience",
            importance="mandatory",
        ),
        JobRequirement(
            requirement_id="req-exp-2",
            text="Five years of experience is required",
            kind="experience",
            importance="mandatory",
            minimum_years=5,
        ),
    ),
)
def test_eligibility_unknowns_force_review_even_when_the_score_would_reject(
    requirement: JobRequirement,
):
    strong_job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
        requirements=(requirement,),
    )
    weak_job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs.",
        requirements=(requirement,),
    )
    narrow = CandidateProfile(
        professional_skills=("python", "fastapi"),
        role_targets=("Backend Developer",),
        evidence_by_skill={"python": "professional", "fastapi": "professional"},
    )

    strong = score_job(profile(), strong_job)
    weak = score_job(narrow, weak_job)

    assert strong.decision == "review"
    assert weak.decision == "review"
    assert weak.score < 70
    assert any("experience" in gap.casefold() for gap in weak.gaps)


def test_load_scoring_rules_rejects_a_scalar_weights_section(tmp_path: Path):
    rules_path = tmp_path / "scalar.yaml"
    rules_path.write_text("weights: 5\n", encoding="utf-8")

    with pytest.raises(ValueError, match="weights"):
        load_scoring_rules(str(rules_path))


def test_load_scoring_rules_rejects_a_scalar_thresholds_section(tmp_path: Path):
    rules_path = tmp_path / "scalar-thresholds.yaml"
    rules_path.write_text("thresholds: 5\n", encoding="utf-8")

    with pytest.raises(ValueError, match="thresholds"):
        load_scoring_rules(str(rules_path))


def test_load_scoring_rules_partial_thresholds_merge_with_defaults(tmp_path: Path):
    rules_path = tmp_path / "partial.yaml"
    rules_path.write_text("thresholds:\n  recommended: 90\n", encoding="utf-8")

    rules = load_scoring_rules(str(rules_path))
    shipped = load_scoring_rules()

    assert rules["thresholds"]["recommended"] == 90
    assert rules["thresholds"]["review"] == shipped["thresholds"]["review"]
    assert rules["thresholds"]["reject"] == shipped["thresholds"]["reject"]

    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )
    result = score_job(profile(), job, rules_path=str(rules_path))

    assert result.score < rules["thresholds"]["recommended"]
    assert result.score >= rules["thresholds"]["review"]
    assert result.decision == "review"


def test_load_scoring_rules_carries_unknown_keys_but_the_matcher_ignores_them(tmp_path: Path):
    rules_path = tmp_path / "unknown.yaml"
    rules_path.write_text(
        "shopping_list:\n"
        "  - milk\n"
        "weights:\n"
        "  nonsense_weight: 100\n",
        encoding="utf-8",
    )

    rules = load_scoring_rules(str(rules_path))
    assert rules["shopping_list"] == ["milk"]
    assert rules["weights"]["nonsense_weight"] == 100

    job = JobListing(
        title="Python Backend Developer",
        description="Maintain FastAPI APIs, add features, write unit tests, and work with PostgreSQL.",
    )
    result = score_job(profile(), job, rules_path=str(rules_path))

    assert result.score >= rules["thresholds"]["recommended"]
    assert result.decision == "recommended"
