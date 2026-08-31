"""Transparent, deterministic mid-level job profile matching."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping

from .models import CandidateProfile, JobListing, MatchResult
from .normalize import normalize_listing


_DEFAULT_CONFIG = {
    "weights": {
        "title_level_fit": 30,
        "verified_professional_skill_fit": 30,
        "responsibility_fit": 20,
        "location_and_work_mode_fit": 10,
        "salary_and_recency_fit": 5,
        "evidence_quality": 5,
    },
    "thresholds": {"recommended": 85, "review": 70, "reject": 0},
    "hard_reject_title_terms": ("senior", "staff", "principal", "lead", "architect", "manager", "head", "director"),
    "hard_reject_terms": (),
    "full_stack_terms": ("full stack", "full-stack"),
    "backend_responsibilities": (),
    "score_interpretation": "Profile fit only; does not predict hiring odds.",
}
MATCHER_POLICY_VERSION = 1

_SKILL_PATTERNS = {
    "python": r"\bpython\b",
    "fastapi": r"\bfastapi\b",
    "rest api": r"\b(?:rest|api|apis)\b",
    "api": r"\bapi(?:s)?\b",
    "postgresql": r"\bpostgres(?:ql)?\b",
    "database": r"\b(?:database|databases|sql)\b",
    "background jobs": r"\b(?:background jobs?|celery|task queue)\b",
    "unit testing": r"\b(?:unit tests?|pytest|automated tests?)\b",
    "integrations": r"\bintegrations?\b",
    "websocket": r"\bwebsockets?\b",
}
_REQUIRED_TECHNOLOGY_PATTERNS = {
    "rust": r"\brust\b",
    "kafka": r"\bkafka\b",
    "go": r"\bgolang\b|\bgo\s+(?:language|programming)\b",
    "kubernetes": r"\bkubernetes\b",
    "terraform": r"\bterraform\b",
    "scala": r"\bscala\b",
    "spark": r"\bapache\s+spark\b",
    "hadoop": r"\bhadoop\b",
    "redis": r"\bredis\b",
    "elasticsearch": r"\belasticsearch\b",
    "react": r"\breact(?:\.js|js)\b|\breact\s+(?:framework|library)\b",
    "angular": r"\bangular\b",
    "typescript": r"\btypescript\b",
    "pytorch": r"\bpytorch\b",
    "tensorflow": r"\btensorflow\b",
    "node.js": r"\bnode(?:\.js|js)\b",
    "aws lambda": r"\baws\s+lambda\b",
    "django": r"\bdjango\b",
    "celery": r"\bcelery\b",
    ".net core": r"(?<!\w)\.?net\s+core\b",
}
_RESPONSIBILITY_PATTERNS = (
    ("maintain or extend APIs", r"\b(?:maintain|maintenance|extend|enhance)\b.{0,45}\bapi(?:s)?\b"),
    ("implementation-focused feature delivery", r"\b(?:add|deliver|implement)\b.{0,30}\bfeatures?\b"),
    ("unit testing", r"\b(?:unit tests?|pytest|automated tests?)\b"),
    ("database work", r"\b(?:postgres(?:ql)?|database|databases|sql)\b"),
    ("background jobs", r"\b(?:background jobs?|celery|task queue)\b"),
    ("API integrations", r"\b(?:api |system )?integrations?\b"),
)
_NON_MID_LEVEL_TITLE_PATTERNS = (
    ("junior-level title", r"\b(?:junior|jr\.?)(?:[-\s]+level)?\b"),
    ("entry-level title", r"\bentry(?:[-\s]+level)?\b"),
    ("internship title", r"\bintern(?:ship)?\b"),
    ("graduate title", r"\b(?:graduate|new[-\s]+grad)\b"),
    ("senior alias title", r"\bsr\.?(?=\s|$|[-/])"),
)
_JOB_LEVEL_SUFFIX_PATTERN = re.compile(
    r"\b(?:sde|(?:[\w+.#/-]+\s+){0,4}(?:engineer|developer))\s*"
    r"(?:(?:[-–—,#/:;.]|\(|\[)\s*)*(?:level\b\s*)?(?:(?:[-–—,#/:;.]|\(|\[)\s*)*"
    r"(?P<suffix>l?0*[1-9]\d*|[mdclxvi]+)\b"
)
_VALID_ROMAN_NUMERAL = re.compile(
    r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
)
_ROLE_TARGET_TITLE_FORMS = {
    "backend developer": ("backend developer", "backend engineer"),
    "python developer": ("python developer", "python backend developer"),
    "fastapi developer": ("fastapi developer", "fastapi engineer"),
    "software developer": ("software developer", "software engineer"),
    "api integration developer": ("api integration developer", "api integration engineer"),
    "ai application developer": ("ai application developer", "llm application developer"),
}


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        return value


def load_scoring_rules(path: str | None = None) -> dict[str, Any]:
    """Read this project's deliberately small YAML subset without PyYAML."""

    rules: dict[str, Any] = {key: value.copy() if isinstance(value, dict) else value for key, value in _DEFAULT_CONFIG.items()}
    rule_path = Path(path) if path else Path(__file__).parents[2] / "config" / "scoring_rules.yaml"
    if not rule_path.exists():
        return rules

    active_section: str | None = None
    for raw_line in rule_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0 and ":" in stripped:
            key, value = stripped.split(":", 1)
            active_section = key.strip()
            if value.strip():
                rules[active_section] = _parse_scalar(value)
            elif active_section not in rules or not isinstance(rules[active_section], (dict, list, tuple)):
                rules[active_section] = {}
        elif active_section and stripped.startswith("- "):
            existing = rules.get(active_section)
            if not isinstance(existing, list):
                existing = list(existing) if isinstance(existing, tuple) else []
                rules[active_section] = existing
            existing.append(_parse_scalar(stripped[2:]))
        elif active_section and ":" in stripped:
            key, value = stripped.split(":", 1)
            section = rules.setdefault(active_section, {})
            if isinstance(section, dict):
                section[key.strip()] = _parse_scalar(value)
    return rules


def matcher_policy_revision(rules_path: str | None = None) -> str:
    """Hash the canonical matching policy used for discovery decisions."""

    projection = {
        "matcher_policy_version": MATCHER_POLICY_VERSION,
        "rules": load_scoring_rules(rules_path),
    }
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _contains_term(text: str, term: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(term.casefold()) + r"(?!\w)", text))


def _title_rejection_reasons(title: str, terms: tuple[str, ...]) -> list[str]:
    reasons: list[str] = []
    for term in terms:
        if _contains_term(title, term):
            suffix = "-level title" if term == "architect" else " title"
            reasons.append(f"{term}{suffix}")
    for reason, pattern in _NON_MID_LEVEL_TITLE_PATTERNS:
        if re.search(pattern, title):
            reasons.append(reason)
    if _has_explicit_job_level_suffix(title):
        reasons.append("explicit job-level suffix")
    return reasons


def _has_explicit_job_level_suffix(title: str) -> bool:
    """Reject unverified positive numeric or valid Roman title-level suffixes."""

    for match in _JOB_LEVEL_SUFFIX_PATTERN.finditer(title):
        suffix = match.group("suffix")
        numeric_suffix = suffix.removeprefix("l")
        if (numeric_suffix and numeric_suffix[0].isdigit()) or _VALID_ROMAN_NUMERAL.fullmatch(suffix):
            return True
    return False


def is_non_mid_level_title(title: str) -> bool:
    """Return whether a title/query uses an excluded senior, junior, or level alias.

    Search-profile validation shares this guard with the matcher so discovery
    cannot request titles that would later be rejected before recommendation.
    """

    normalized_title = title.casefold()
    return bool(_title_rejection_reasons(normalized_title, tuple(_DEFAULT_CONFIG["hard_reject_title_terms"])))


def _ownership_rejection_reasons(text: str) -> list[str]:
    patterns = (
        ("system-design/architecture requirement", r"\b(?:system|software|solution|technical)\s+design\b|\b(?:system|software|technical|solution|application|cloud|microservices?)\s+architecture\b|\barchitectural\s+design\b|\barchitecture\s+(?:design|patterns?|decisions?|documentation)\b"),
        ("explicit architecture ownership", r"\b(?:own|owns|ownership of|responsible for)\b.{0,40}\b(?:system |software )?architecture\b|\barchitecture ownership\b"),
        (
            "distributed-system design requirement",
            r"\b(?:own|owns|ownership of|responsible for|lead)\b.{0,50}\bdesign\b.{0,50}"
            r"\b(?:large|scalable|distributed)\s+(?:systems?|services?)\b"
            r"|\bdesign\b.{0,30}\b(?:large|scalable|distributed)\s+(?:systems?|services?)\b",
        ),
        ("explicit technical strategy ownership", r"\b(?:own|owns|ownership of|responsible for)\b.{0,40}\btechnical strategy\b|\btechnical strategy ownership\b"),
        ("explicit people management", r"\bpeople management\b|\b(?:manage|managing)\b.{0,25}\b(?:people|engineers|developers|a team)\b"),
    )
    return [reason for reason, pattern in patterns if re.search(pattern, text)]


def _requires_production_genai_ownership(text: str) -> bool:
    has_genai = bool(re.search(r"\b(?:genai|generative ai|llm)\b", text))
    has_production = "production" in text
    mandatory = bool(re.search(r"\b(?:required|must have|must|mandatory|minimum|needed|\d+\+? years?)\b", text))
    ownership = bool(re.search(r"\b(?:own|ownership|lead)\w*\b", text))
    explicit_production_experience = bool(
        re.search(r"\bproduction\s+experience\s+(?:with|in)\b.{0,80}\b(?:genai|generative ai|llm)\b", text)
    )
    explicit_operations_experience = bool(
        re.search(
            r"\bexperience\s+(?:operating|running|deploying|maintaining)\b.{0,80}"
            r"\b(?:genai|generative ai|llm)\b.{0,80}\bproduction\b",
            text,
        )
    )
    return has_genai and has_production and (
        mandatory or ownership or explicit_production_experience or explicit_operations_experience
    )


def _requires_ml_model_training_or_deployment_ownership(text: str) -> bool:
    """Identify explicit ownership of the ML lifecycle excluded by the profile."""

    has_ml_model = bool(re.search(r"\b(?:ml|machine learning)\s+models?\b", text))
    has_training_or_deployment = bool(re.search(r"\b(?:training|train|deployment|deploy)\b", text))
    mandatory_or_ownership = bool(
        re.search(r"\b(?:required|must have|must|mandatory|need(?:ed)?|own|ownership|lead|responsible for)\b", text)
    )
    return has_ml_model and has_training_or_deployment and mandatory_or_ownership


def _has_professional_evidence_for_technology(profile: CandidateProfile, technology: str) -> bool:
    """Check whether a required technology has classified professional evidence."""

    for skill in profile.professional_skills:
        if profile.evidence_label_for(skill) != "professional":
            continue
        if _matches_skill(technology, skill) or _matches_skill(skill, technology):
            return True
    return False


def _unsupported_explicit_mandatory_technologies(text: str, profile: CandidateProfile) -> list[str]:
    """Find named technologies in clear requirement clauses lacking professional evidence.

    The parser intentionally considers only a restricted list of common
    technology names and clauses with a direct mandatory cue or a minimum-years
    requirement. Incidental ecosystem mentions therefore do not become hard
    rejections.
    """

    requirement_cue = re.compile(
        r"\b(?:must(?:\s+have)?|required|mandatory|minimum(?:\s+of)?|need(?:ed)?|at\s+least|\d+\+?\s+years?)\b"
    )
    unsupported: set[str] = set()
    for clause in re.split(r"[.!?;\n]+", text):
        if not requirement_cue.search(clause):
            continue
        for technology, pattern in _REQUIRED_TECHNOLOGY_PATTERNS.items():
            if re.search(pattern, clause) and not _has_professional_evidence_for_technology(profile, technology):
                unsupported.add(technology)
    return sorted(unsupported)


def _mandatory_exclusion_reasons(text: str, profile: CandidateProfile) -> list[str]:
    """Apply explicitly approved mandatory-requirement exclusions to a listing."""

    reasons: list[str] = []
    for requirement in profile.mandatory_excluded_requirements:
        normalized_requirement = requirement.casefold().strip()
        if "production genai" in normalized_requirement and _requires_production_genai_ownership(text):
            reasons.append("mandatory production GenAI ownership is excluded")
        if (
            ("ml model" in normalized_requirement or "machine learning model" in normalized_requirement)
            and ("training" in normalized_requirement or "deployment" in normalized_requirement)
            and _requires_ml_model_training_or_deployment_ownership(text)
        ):
            reasons.append("mandatory ML model training/deployment ownership is excluded")
        if "unsupported technology" in normalized_requirement and "approved evidence" in normalized_requirement:
            unsupported = _unsupported_explicit_mandatory_technologies(text, profile)
            if unsupported:
                reasons.append("mandatory unsupported technology requirement: " + ", ".join(unsupported))
    return list(dict.fromkeys(reasons))


def _is_backend_dominant(text: str, configured_responsibilities: tuple[str, ...] = ()) -> bool:
    backend = len(re.findall(r"\b(?:python|fastapi|api|apis|postgres(?:ql)?|database|databases|background jobs?|celery|integrations?|unit tests?)\b", text))
    backend += sum(1 for responsibility in configured_responsibilities if _contains_term(text, responsibility))
    frontend = len(re.findall(r"\b(?:react|angular|vue|javascript|typescript|css|html|frontend|front-end|ui|ux)\b", text))
    return backend >= 3 and backend > frontend


def _title_points(title: str, maximum: int) -> tuple[int, str | None]:
    exact = ("backend developer", "backend engineer", "python developer", "python backend", "fastapi developer", "api integration")
    if any(term in title for term in exact):
        return maximum, "mid-level implementation-focused backend/API title"
    if "software developer" in title or "software engineer" in title:
        return min(20, maximum), "software title with possible implementation fit"
    if "full stack" in title or "full-stack" in title:
        return min(15, maximum), "full-stack title accepted only because backend responsibilities dominate"
    if "ai application developer" in title or "llm application developer" in title:
        return min(15, maximum), "AI application title requires evidence review"
    return 0, None


def _title_matches_role_targets(title: str, role_targets: tuple[str, ...]) -> bool:
    """Return whether a declared target (or its narrow role-equivalent) scopes title.

    A non-empty target list is an allow-list.  The narrow Engineer/Developer
    equivalents below preserve ordinary wording differences without admitting
    unrelated specialties such as data, platform, or ML engineering. A
    backend/Python/FastAPI/software target also permits a full-stack title only
    so the separate backend-dominance gate can evaluate it. An empty list keeps
    the model backward compatible for callers without an approved role policy.
    """

    if not role_targets:
        return True
    for target in role_targets:
        normalized_target = target.casefold().strip()
        forms = _ROLE_TARGET_TITLE_FORMS.get(normalized_target, (normalized_target,))
        if any(_contains_term(title, form) for form in forms):
            return True
    if (_contains_term(title, "full stack") or _contains_term(title, "full-stack")) and any(
        target.casefold().strip()
        in {"backend developer", "python developer", "fastapi developer", "software developer"}
        for target in role_targets
    ):
        return True
    return False


def _matches_skill(skill: str, text: str) -> bool:
    pattern = _SKILL_PATTERNS.get(skill.casefold().strip())
    if pattern:
        return bool(re.search(pattern, text))
    return _contains_term(text, skill)


def score_job(profile: CandidateProfile, job: JobListing, *, rules_path: str | None = None) -> MatchResult:
    """Score one visible listing against approved profile evidence.

    The result is a deterministic profile-fit classification. It does not infer
    interview likelihood, eligibility, or a hiring decision.
    """

    rules = load_scoring_rules(rules_path)
    weights: Mapping[str, int] = rules["weights"]
    thresholds: Mapping[str, int] = rules["thresholds"]
    normalized = normalize_listing(job)
    title = normalized.title.casefold()
    text = f"{title}\n{normalized.description.casefold()}"
    reasons = _title_rejection_reasons(
        title,
        tuple(dict.fromkeys((*_DEFAULT_CONFIG["hard_reject_title_terms"], *profile.excluded_title_terms, *rules.get("hard_reject_title_terms", ())))),
    )
    if not _title_matches_role_targets(title, profile.role_targets):
        reasons.append("title is outside approved role targets")
    configured_hard_reject_terms = tuple(
        str(term).casefold().strip() for term in rules.get("hard_reject_terms", ()) if str(term).strip()
    )
    reasons.extend(
        f"configured hard-reject requirement: {term}"
        for term in configured_hard_reject_terms
        if _contains_term(text, term)
    )
    reasons.extend(_ownership_rejection_reasons(text))
    reasons.extend(_mandatory_exclusion_reasons(text, profile))
    if _requires_production_genai_ownership(text):
        reasons.append("mandatory production GenAI ownership is not approved professional evidence")
    full_stack_terms = tuple(str(term) for term in rules.get("full_stack_terms", ()) if str(term).strip())
    full_stack = any(_contains_term(title, term) for term in full_stack_terms)
    configured_backend_responsibilities = tuple(
        str(term) for term in rules.get("backend_responsibilities", ()) if str(term).strip()
    )
    if full_stack and not _is_backend_dominant(text, configured_backend_responsibilities):
        gap = "general full-stack role lacks backend-dominant responsibilities"
        return MatchResult(0, "reject", reasons or ["not an implementation-focused backend role"], [gap])
    if reasons:
        return MatchResult(0, "reject", reasons, [])

    score = 0
    positive: list[str] = []
    gaps: list[str] = []
    evidence_explanations: list[str] = []
    title_score, title_reason = _title_points(title, int(weights["title_level_fit"]))
    score += title_score
    if title_reason:
        positive.append(title_reason)
    else:
        gaps.append("title is not a preferred mid-level backend/API target")

    professional_matches: list[str] = []
    non_professional_matches: list[tuple[str, str]] = []
    all_skills = (*profile.professional_skills, *profile.personal_open_source_skills, *profile.learning_or_exposure_skills)
    for skill in dict.fromkeys(value.casefold().strip() for value in all_skills if value.strip()):
        if not _matches_skill(skill, text):
            continue
        label = profile.evidence_label_for(skill)
        if label == "professional":
            professional_matches.append(skill)
        elif label:
            non_professional_matches.append((skill, label))
    skill_score = min(int(weights["verified_professional_skill_fit"]), len(professional_matches) * 10)
    score += skill_score
    if professional_matches:
        names = ", ".join(professional_matches[:3])
        positive.append(f"direct professional skill match: {names}")
        evidence_explanations.append(f"{names}: professional evidence supports this match.")
    else:
        gaps.append("no direct professional skill evidence matches the listing")
    for skill, label in non_professional_matches:
        evidence_explanations.append(f"{skill}: {label} evidence is informative only and adds no professional-fit points.")

    responsibility_matches = [name for name, pattern in _RESPONSIBILITY_PATTERNS if re.search(pattern, text)]
    responsibility_score = min(int(weights["responsibility_fit"]), len(responsibility_matches) * 5)
    score += responsibility_score
    if responsibility_matches:
        positive.append("implementation responsibilities: " + ", ".join(responsibility_matches[:4]))
    else:
        gaps.append("listing does not show maintain/extend, testing, database, background-job, or integration work")

    if profile.location_preferences and normalized.location:
        if any(preference.casefold() in normalized.location.casefold() for preference in profile.location_preferences):
            score += int(weights["location_and_work_mode_fit"])
            positive.append("location matches approved preference")
        else:
            gaps.append("location is outside approved preferences")
    elif profile.work_mode_preferences and normalized.work_mode:
        if normalized.work_mode in {mode.casefold() for mode in profile.work_mode_preferences}:
            score += int(weights["location_and_work_mode_fit"])
            positive.append("work mode matches approved preference")
        else:
            gaps.append("work mode is outside approved preferences")

    if professional_matches:
        score += int(weights["evidence_quality"])

    direct_professional_evidence = bool(professional_matches)
    hard_gaps = bool(gaps and (not direct_professional_evidence or not responsibility_matches))
    if score >= int(thresholds["recommended"]) and direct_professional_evidence and not hard_gaps:
        decision = "recommended"
    elif score >= int(thresholds["review"]):
        decision = "review"
        if score >= int(thresholds["recommended"]) and not direct_professional_evidence:
            gaps.append("high-confidence recommendation requires direct professional evidence")
    else:
        decision = "reject"
    return MatchResult(score, decision, positive, gaps, evidence_explanations)
