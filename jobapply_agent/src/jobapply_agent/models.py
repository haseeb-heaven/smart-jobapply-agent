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


def _normalized_strings(value: Any) -> tuple[str, ...]:
    """Return stable, case-insensitive policy values without duplicates."""

    return tuple(
        dict.fromkeys(" ".join(item.split()).casefold() for item in _as_strings(value) if item.strip())
    )


DEFAULT_EXCLUDED_TITLE_TERMS: tuple[str, ...] = (
    "senior",
    "staff",
    "principal",
    "lead",
    "architect",
    "manager",
    "head",
    "director",
)


def _excluded_title_terms(value: Any) -> tuple[str, ...]:
    """Preserve the safe default unless exclusions use the supported YAML shapes."""

    if value is None:
        return DEFAULT_EXCLUDED_TITLE_TERMS
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return _as_strings(value)
    return DEFAULT_EXCLUDED_TITLE_TERMS


@dataclass(frozen=True, slots=True)
class JobRequirement:
    """One validated listing requirement consumed by deterministic policy.

    ``subject`` is mandatory for atomic skill and authorization requirements.
    Candidate-evidence evaluations produced by an LLM are deliberately absent:
    they cannot enter or override eligibility through this model.
    """

    requirement_id: str
    text: str
    kind: str
    importance: str
    minimum_years: int | float | None = None
    subject: str = ""
    source_evidence: str = ""


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
    years_experience: int | None = None
    role_targets: tuple[str, ...] = ()
    excluded_title_terms: tuple[str, ...] = DEFAULT_EXCLUDED_TITLE_TERMS
    mandatory_excluded_requirements: tuple[str, ...] = ()
    location_preferences: tuple[str, ...] = ()
    work_mode_preferences: tuple[str, ...] = ()
    employment_type_preferences: tuple[str, ...] = ()
    work_authorizations: tuple[str, ...] = ()
    evidence_by_skill: Mapping[str, EvidenceLabel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            self.years_experience is not None
            and (isinstance(self.years_experience, bool) or not isinstance(self.years_experience, int))
        ):
            raise ValueError("years_experience must be an approved whole number or None")
        if self.years_experience is not None and self.years_experience < 0:
            raise ValueError("years_experience cannot be negative")
        for field_name in (
            "professional_skills",
            "personal_open_source_skills",
            "learning_or_exposure_skills",
        ):
            object.__setattr__(self, field_name, _as_strings(getattr(self, field_name)))

        skill_groups = {
            "professional": set(_normalized_strings(self.professional_skills)),
            "personal_open_source": set(_normalized_strings(self.personal_open_source_skills)),
            "learning_or_exposure": set(_normalized_strings(self.learning_or_exposure_skills)),
        }
        group_names = tuple(skill_groups)
        for index, group_name in enumerate(group_names):
            for other_name in group_names[index + 1 :]:
                overlap = skill_groups[group_name] & skill_groups[other_name]
                if overlap:
                    raise ValueError(
                        "skill evidence groups must be disjoint after normalization: "
                        + ", ".join(sorted(overlap))
                    )

        for field_name in (
            "role_targets",
            "excluded_title_terms",
            "mandatory_excluded_requirements",
            "location_preferences",
            "work_mode_preferences",
        ):
            object.__setattr__(self, field_name, _as_strings(getattr(self, field_name)))
        for field_name in ("employment_type_preferences", "work_authorizations"):
            object.__setattr__(self, field_name, _normalized_strings(getattr(self, field_name)))
        normalized_evidence: dict[str, EvidenceLabel] = {}
        for skill, label in self.evidence_by_skill.items():
            normalized_skill = " ".join(str(skill).split()).casefold()
            if not normalized_skill or label not in skill_groups:
                raise ValueError("evidence_by_skill must use known skills and closed evidence labels")
            if normalized_skill not in skill_groups[label]:
                raise ValueError(
                    f"evidence_by_skill label conflicts with the normalized skill group: {normalized_skill}"
                )
            previous = normalized_evidence.get(normalized_skill)
            if previous is not None and previous != label:
                raise ValueError(f"conflicting normalized evidence labels for skill: {normalized_skill}")
            normalized_evidence[normalized_skill] = label
        for label, skills in skill_groups.items():
            for skill in skills:
                if skill in normalized_evidence and normalized_evidence[skill] != label:
                    raise ValueError(f"conflicting normalized evidence labels for skill: {skill}")
                normalized_evidence[skill] = label  # type: ignore[assignment]
        object.__setattr__(self, "evidence_by_skill", normalized_evidence)

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
        experience = data.get("experience", {})
        if not isinstance(experience, Mapping):
            experience = {}
        targets = data.get("targets", {})
        if not isinstance(targets, Mapping):
            targets = {}
        work_authorization = data.get("work_authorization", {})
        if not isinstance(work_authorization, Mapping):
            work_authorization = {}

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

        if "exclude_title_terms" in data:
            excluded_title_terms = data["exclude_title_terms"]
        elif "exclude_title_terms" in roles:
            excluded_title_terms = roles["exclude_title_terms"]
        else:
            excluded_title_terms = DEFAULT_EXCLUDED_TITLE_TERMS

        return cls(
            professional_skills=_as_strings(professional),
            personal_open_source_skills=_as_strings(personal),
            learning_or_exposure_skills=_as_strings(learning),
            years_experience=data.get(
                "years_experience",
                experience.get("years_experience", experience.get("total_years")),
            ),
            role_targets=_as_strings(data.get("role_targets", roles.get("include", positioning.get("focus", ())))),
            excluded_title_terms=_excluded_title_terms(excluded_title_terms),
            mandatory_excluded_requirements=_as_strings(
                data.get("mandatory_excluded_requirements", hard_exclusions.get("mandatory_requirements", ()))
            ),
            location_preferences=_as_strings(data.get("locations", preferences.get("locations", ()))),
            work_mode_preferences=_as_strings(data.get("work_modes", preferences.get("work_modes", ()))),
            employment_type_preferences=_as_strings(
                data.get(
                    "employment_type_preferences",
                    targets.get("employment_types", data.get("employment_types", ())),
                )
            ),
            work_authorizations=_as_strings(
                data.get(
                    "work_authorizations",
                    work_authorization.get("authorized_locations", work_authorization.get("locations", ())),
                )
            ),
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
    employment_type: str = ""
    requirements: tuple[JobRequirement, ...] = ()
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
