from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "job_profession" / "src"))

from job_profession.matcher import load_scoring_rules, score_job
from job_profession.models import CandidateProfile, JobListing


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
