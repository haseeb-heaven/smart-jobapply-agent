"""Small, dependency-free data models for deterministic job matching."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


EvidenceLabel = Literal["professional", "personal_open_source", "learning_or_exposure"]
Decision = Literal["recommended", "review", "reject"]


def _as_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if item is not None)
    return ()


@dataclass(frozen=True, slots=True)
class CandidateProfile:
    """The minimum approved profile surface the matching engine needs.

    ``professional_skills`` denotes skills supported by professional evidence.
    Personal/open-source and learning skills are deliberately kept separate, so a
    listing can never turn them into a professional-experience claim.
    """

    professional_skills: tuple[str, ...] = ()
    personal_open_source_skills: tuple[str, ...] = ()
    learning_or_exposure_skills: tuple[str, ...] = ()
    role_targets: tuple[str, ...] = ()
    excluded_title_terms: tuple[str, ...] = (
        "senior",
        "staff",
        "principal",
        "lead",
        "architect",
        "manager",
        "head",
        "director",
    )
    mandatory_excluded_requirements: tuple[str, ...] = ()
    location_preferences: tuple[str, ...] = ()
    work_mode_preferences: tuple[str, ...] = ()
    evidence_by_skill: Mapping[str, EvidenceLabel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "professional_skills",
            "personal_open_source_skills",
            "learning_or_exposure_skills",
            "role_targets",
            "excluded_title_terms",
            "mandatory_excluded_requirements",
            "location_preferences",
            "work_mode_preferences",
        ):
            object.__setattr__(self, field_name, _as_strings(getattr(self, field_name)))
        object.__setattr__(
            self,
            "evidence_by_skill",
            {str(skill).casefold().strip(): label for skill, label in self.evidence_by_skill.items()},
        )

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CandidateProfile":
        """Build a matcher profile from the planned private YAML shape.

        The method is intentionally permissive about the source schema while
        preserving evidence labels. It lets profile loading remain independent
        from this pure matching core.
        """

        roles = data.get("roles", {}) if isinstance(data.get("roles"), Mapping) else {}
        skills = data.get("skills", {}) if isinstance(data.get("skills"), Mapping) else {}
        positioning = data.get("positioning", {}) if isinstance(data.get("positioning"), Mapping) else {}
        hard_exclusions = data.get("hard_exclusions", {}) if isinstance(data.get("hard_exclusions"), Mapping) else {}
        preferences = data.get("location_preferences", {})
        if not isinstance(preferences, Mapping):
            preferences = {}

        professional = data.get("professional_skills", skills.get("professional", ()))
        personal = data.get("personal_open_source_skills", skills.get("personal_open_source", ()))
        learning = data.get("learning_or_exposure_skills", skills.get("learning_or_exposure", ()))
        evidence = data.get("evidence_by_skill", {})
        if not isinstance(evidence, Mapping):
            evidence = {}
        evidence = dict(evidence)
        for skill in _as_strings(professional):
            evidence.setdefault(skill, "professional")
        for skill in _as_strings(personal):
            evidence.setdefault(skill, "personal_open_source")
        for skill in _as_strings(learning):
            evidence.setdefault(skill, "learning_or_exposure")

        return cls(
            professional_skills=_as_strings(professional),
            personal_open_source_skills=_as_strings(personal),
            learning_or_exposure_skills=_as_strings(learning),
            role_targets=_as_strings(data.get("role_targets", roles.get("include", positioning.get("focus", ())))),
            excluded_title_terms=_as_strings(data.get("exclude_title_terms", roles.get("exclude_title_terms", ())))
            or cls.excluded_title_terms,
            mandatory_excluded_requirements=_as_strings(
                data.get("mandatory_excluded_requirements", hard_exclusions.get("mandatory_requirements", ()))
            ),
            location_preferences=_as_strings(data.get("locations", preferences.get("locations", ()))),
            work_mode_preferences=_as_strings(data.get("work_modes", preferences.get("work_modes", ()))),
            evidence_by_skill=evidence,
        )

    def evidence_label_for(self, skill: str) -> EvidenceLabel | None:
        key = skill.casefold().strip()
        label = self.evidence_by_skill.get(key)
        if label in {"professional", "personal_open_source", "learning_or_exposure"}:
            return label
        if key in {value.casefold().strip() for value in self.professional_skills}:
            return "professional"
        if key in {value.casefold().strip() for value in self.personal_open_source_skills}:
            return "personal_open_source"
        if key in {value.casefold().strip() for value in self.learning_or_exposure_skills}:
            return "learning_or_exposure"
        return None


@dataclass(frozen=True, slots=True)
class JobListing:
    """A listing captured from a visible job page or card; no browser data needed."""

    title: str
    description: str = ""
    company: str = ""
    platform: str = ""
    url: str = ""
    location: str = ""
    work_mode: str = ""
    posted_at: str | None = None
    discovered_at: str | None = None
    source_job_id: str | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Auditable profile-fit evaluation, explicitly not a hiring prediction."""

    score: int
    decision: Decision
    reasons: list[str]
    gaps: list[str]
    evidence_explanations: list[str] = field(default_factory=list)
    score_explanation: str = "Profile-fit score, not hiring odds."
