"""Pure, evidence-labelled application-draft composition.

This module creates text for a candidate to review and use manually.  It has no
browser, network, upload, or form-filling capability.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .models import CandidateProfile, JobListing


_AI_TERMS = ("ai", "llm", "rag", "agent", "generative")
_PROHIBITED_ROLE_TERMS = (
    "senior",
    "staff",
    "principal",
    "lead",
    "architect",
    "manager",
    "head",
    "director",
)


@dataclass(frozen=True, slots=True)
class ApplicationDraft:
    """A review-only draft built from separated evidence types."""

    cover_letter: str
    hiring_manager_message: str
    claims_used: tuple[str, ...]
    claims_rejected: tuple[str, ...]
    requires_user_answer: tuple[str, ...]


def _has_skill(profile: CandidateProfile, evidence_label: str, *terms: str) -> bool:
    skills = {
        "professional": profile.professional_skills,
        "personal_open_source": profile.personal_open_source_skills,
    }.get(evidence_label, ())
    available = " ".join(skills).casefold()
    return any(term.casefold() in available for term in terms)


def _mentions_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in terms)


def _prohibited_role(job: JobListing) -> bool:
    return _mentions_any(job.title, _PROHIBITED_ROLE_TERMS) or _mentions_any(
        job.description, ("architecture ownership", "technical strategy", "people management", "production genai")
    )


def lint_claims(text: str) -> tuple[str, ...]:
    """Return disallowed *positive claims* found in candidate-authored text.

    The linter intentionally permits explicit negative evidence labels such as
    ``not employer production AI experience``; those are safeguards, not claims.
    """

    lowered = text.casefold()
    violations: list[str] = []
    if re.search(r"\b(senior|staff|principal)\b", lowered):
        violations.append("senior")
    if re.search(
        r"\b(ai\s+architect|architect|architected|architecture\s+(?:owner|ownership)|led the platform architecture)\b",
        lowered,
    ):
        violations.append("architect")
    if re.search(r"\b(production\s+(?:ai|ml)(?:\s+(?:leader|leadership|platform|experience))?)\b", lowered):
        if not re.search(r"\bnot\s+(?:employer\s+)?production\s+(?:ai|ml)\b", lowered):
            violations.append("production ai")
    if re.search(r"\b(managed|management of)\s+(?:people|engineers|a team|teams)\b", lowered):
        violations.append("people management")
    return tuple(violations)


def lint_draft(draft: ApplicationDraft) -> tuple[str, ...]:
    """Lint the candidate-facing portions of a prepared draft."""

    return lint_claims(f"{draft.cover_letter}\n{draft.hiring_manager_message}")


def build_draft(profile: CandidateProfile, job: JobListing) -> ApplicationDraft:
    """Build a truthful, job-specific draft without extrapolating experience.

    Professional backend evidence and personal/open-source AI evidence are
    deliberately emitted in separate sentences.  A senior, architecture, or
    unsupported production-AI role receives a safe hold message instead of a
    persuasive application draft.
    """

    company = job.company.strip() or "your team"
    job_text = f"{job.title} {job.description}".casefold()
    user_prompts = [
        "Confirm the company name, job title, and every claim against the selected listing.",
        "Candidate must answer salary, work authorization, sponsorship, location, notice-period, and screening questions manually when required.",
    ]

    if _prohibited_role(job):
        return ApplicationDraft(
            cover_letter=(
                "A job-specific draft is on hold because this listing appears to require a level or "
                "responsibility outside the approved mid-level implementation-focused profile."
            ),
            hiring_manager_message=(
                "I am only pursuing mid-level implementation-focused roles supported by my documented experience."
            ),
            claims_used=(),
            claims_rejected=(
                "The listing appears to request seniority, architecture or strategy ownership, people management, "
                "or unsupported production-AI experience."
            ),
            requires_user_answer=tuple(user_prompts + ["Select a role that remains within the approved mid-level scope."]),
        )

    claims_used: list[str] = []
    paragraphs = [
        f"Dear {company} hiring team,",
        "I am a mid-level implementation-focused backend/software developer interested in this opportunity. "
        "My professional backend experience is separate from my personal/open-source AI work.",
    ]

    if _has_skill(profile, "professional", "python", "fastapi", "api", "rest", "integration"):
        paragraphs.append(
            "My professional experience includes implementing Python and FastAPI backend services, API integrations, "
            "and scoped feature work with a team."
        )
        claims_used.append("professional: Python/FastAPI backend services and API integrations")
    if _has_skill(profile, "professional", "sql", "database", "clickhouse"):
        paragraphs.append(
            "I also have professional experience with SQL/database work in backend and trading contexts."
        )
        claims_used.append("professional: SQL/database work")
    if _has_skill(profile, "professional", "test", "debug", "automation"):
        paragraphs.append(
            "My implementation work also includes testing, debugging, and automation tooling."
        )
        claims_used.append("professional: testing, debugging, and automation")

    needs_ai = _mentions_any(job_text, _AI_TERMS)
    if needs_ai and _has_skill(profile, "personal_open_source", "llm", "rag", "agent", "ai"):
        paragraphs.append(
            "Separately, my personal/open-source projects demonstrate hands-on LLM, RAG, agent, and developer-tool "
            "experimentation; this is not employer production AI experience."
        )
        # Keep the evidence label as a standalone value for auditable consumers,
        # in addition to the human-readable claim description.
        claims_used.extend(
            (
                "personal_open_source",
                "personal_open_source: LLM/RAG/agent project experimentation",
            )
        )
    elif needs_ai:
        user_prompts.append("Confirm whether any AI-related requirement can be supported without overstating personal/open-source work.")

    paragraphs.extend(
        (
            "I would welcome the chance to discuss the implementation details in the listing after reviewing them with you.",
            "Sincerely,\nCandidate",
        )
    )
    message = (
        f"Hello {company} hiring team — I am interested in the implementation-focused backend aspects of this role. "
        "My professional backend experience covers APIs, integrations, databases, testing, and scoped feature delivery. "
        "I would be glad to discuss fit after a manual review of the listing."
    )
    return ApplicationDraft(
        cover_letter="\n\n".join(paragraphs),
        hiring_manager_message=message,
        claims_used=tuple(claims_used),
        claims_rejected=(),
        requires_user_answer=tuple(user_prompts),
    )
