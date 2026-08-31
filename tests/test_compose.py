from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "job_profession" / "src"))

from job_profession.compose import build_draft, lint_claims
from job_profession.models import CandidateProfile, JobListing


def profile() -> CandidateProfile:
    return CandidateProfile(
        professional_skills=("python", "fastapi", "rest api", "sql", "testing", "integrations"),
        personal_open_source_skills=("llm", "rag", "agents"),
        evidence_by_skill={
            "python": "professional",
            "fastapi": "professional",
            "rest api": "professional",
            "sql": "professional",
            "testing": "professional",
            "integrations": "professional",
            "llm": "personal_open_source",
            "rag": "personal_open_source",
            "agents": "personal_open_source",
        },
    )


def test_backend_draft_uses_only_mid_level_professional_positioning():
    draft = build_draft(
        profile(),
        JobListing(
            title="Python Backend Developer",
            company="Example Systems",
            description="Maintain FastAPI APIs, SQL data stores, integrations, and unit tests.",
        ),
    )

    assert "mid-level implementation-focused" in draft.cover_letter.lower()
    assert "FastAPI" in draft.cover_letter
    assert draft.claims_used
    assert not lint_claims(draft.cover_letter + "\n" + draft.hiring_manager_message)


def test_ai_draft_labels_open_source_work_truthfully():
    draft = build_draft(
        profile(),
        JobListing(
            title="AI Application Developer",
            company="Example Systems",
            description="Build LLM, RAG, and agent-assisted developer tools.",
        ),
    )

    text = draft.cover_letter.lower()
    assert "personal/open-source" in text
    assert "not employer production ai experience" in text
    assert "personal_open_source" in draft.claims_used


def test_prohibited_claim_linting_detects_seniority_architecture_and_production_ai_claims():
    violations = lint_claims(
        "I am a senior AI architect who led the platform architecture and production AI team."
    )

    assert "senior" in violations
    assert "architect" in violations
    assert "production ai" in violations
