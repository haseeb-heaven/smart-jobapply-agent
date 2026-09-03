#!/usr/bin/env python3
"""Run local discovery against a pre-exported visible-page JSON payload.

This command intentionally has no URL fetching, browser, login, CAPTCHA, or
application-submission capability. Supplying no payload produces an auditable
zero-listing run, which is safe for a scheduler heartbeat.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import NamedTemporaryFile
from typing import Any, Mapping, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if (SCRIPT_DIR / "src" / "jobapply_agent").exists():
    PROJECT_ROOT = SCRIPT_DIR
elif (SCRIPT_DIR.parent / "src" / "jobapply_agent").exists():
    PROJECT_ROOT = SCRIPT_DIR.parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from jobapply_agent.models import CandidateProfile  # noqa: E402
from jobapply_agent.smart_queue import QueuePolicyError, SmartJobQueue  # noqa: E402
from jobapply_agent.intake import activate_candidate_profile, validate_active_candidate_profile  # noqa: E402
from jobapply_agent.intake import (  # noqa: E402
    completion_questions,
    pending_verification_batch,
    validate_candidate_intake,
)
from jobapply_agent.scheduler import current_profile_recommendations, run_discovery  # noqa: E402
from jobapply_agent.sources import MappingVisiblePageAdapter, load_search_profiles  # noqa: E402


DEFAULT_CANDIDATE_PROFILE_PATH = PROJECT_ROOT / "private" / "candidate_profile.yaml"
DEFAULT_CANDIDATE_INTAKE_PATH = PROJECT_ROOT / "private" / "candidate_intake.json"
REDACTED_CANDIDATE_INTAKE_PATH = PROJECT_ROOT / "private.example" / "candidate_intake.json"
_INTERACTIVE_PRIVATE_ROOT = PROJECT_ROOT / "private"
_INTERACTIVE_PATH_ERROR = "Interactive onboarding intake path is not permitted."
INTERACTIVE_CONFIRMATION_TOKEN = "yes"
_UNSET_QUEUE_CAPACITY = object()
_JSON_STRING_LIST_FIELDS = frozenset(
    {
        "skills.professional",
        "skills.personal_open_source",
        "skills.learning_or_exposure",
    }
)
_SAFE_REVIEW_VALUE_FIELDS = frozenset(
    {
        "experience.current_title",
        "experience.title",
        "experience.years_experience",
        "experience.total_years",
        "experience.roles_and_dates",
        "experience.achievements_and_metrics",
        "education",
        "certifications",
        "projects",
        "open_source",
        "volunteer_work",
        "languages",
        "roles.include",
        "roles.exclude_title_terms",
        "hard_exclusions.mandatory_requirements",
        "skills.professional",
        "skills.personal_open_source",
        "skills.learning_or_exposure",
        "targets.roles_and_levels",
        "targets.locations_and_work_modes",
        "targets.employment_types",
        "targets.employment_types_and_industries",
        "targets.exclusions",
        "location_preferences.locations",
        "location_preferences.work_modes",
    }
)
_SAFE_REVIEW_STRUCTURED_FIELDS = {
    "experience.roles_and_dates": frozenset(
        {
            "title",
            "role",
            "company",
            "employer",
            "organization",
            "start",
            "end",
            "start_date",
            "end_date",
            "start_year",
            "end_year",
            "dates",
            "period",
            "current",
            "status",
            "employment_type",
            "location",
            "work_mode",
        }
    ),
    "experience.achievements_and_metrics": frozenset(
        {
            "achievement",
            "summary",
            "description",
            "impact",
            "result",
            "metric",
            "metrics",
            "role",
            "date",
            "dates",
            "technologies",
            "skills",
        }
    ),
    "education": frozenset(
        {
            "institution",
            "school",
            "university",
            "degree",
            "field",
            "subject",
            "program",
            "area",
            "start",
            "end",
            "start_date",
            "end_date",
            "start_year",
            "end_year",
            "year",
            "graduation_date",
            "dates",
            "percentage",
            "gpa",
            "gpa_scale",
            "score",
            "score_obtained",
            "score_total",
            "division",
            "board",
            "stream",
            "level",
            "status",
            "honors",
        }
    ),
    "certifications": frozenset(
        {
            "name",
            "title",
            "issuer",
            "organization",
            "date",
            "issue_date",
            "expiry_date",
            "expiration_date",
            "status",
        }
    ),
    "projects": frozenset(
        {
            "name",
            "title",
            "role",
            "summary",
            "description",
            "impact",
            "result",
            "metric",
            "metrics",
            "technologies",
            "skills",
            "start",
            "end",
            "start_date",
            "end_date",
            "dates",
            "status",
            "type",
        }
    ),
    "open_source": frozenset(
        {
            "name",
            "project",
            "title",
            "role",
            "summary",
            "description",
            "contribution",
            "impact",
            "result",
            "metric",
            "metrics",
            "technologies",
            "skills",
            "start",
            "end",
            "start_date",
            "end_date",
            "dates",
            "status",
            "type",
        }
    ),
    "volunteer_work": frozenset(
        {
            "name",
            "organization",
            "project",
            "title",
            "role",
            "summary",
            "description",
            "contribution",
            "impact",
            "result",
            "metric",
            "metrics",
            "start",
            "end",
            "start_date",
            "end_date",
            "dates",
            "status",
            "type",
        }
    ),
    "languages": frozenset(
        {
            "name",
            "language",
            "proficiency",
            "fluency",
            "level",
            "reading",
            "writing",
            "speaking",
            "listening",
        }
    ),
}
_REVIEW_FACT_METADATA_FIELDS = frozenset({"value", "source", "uncertainty"})
_REVIEW_SOURCE_DOCUMENT_TERMS = frozenset({"document", "resume", "cv", "upload", "file"})
_REVIEW_SOURCE_ANSWER_TERMS = frozenset({"answer", "conversation", "manual", "candidate"})
_REVIEW_UNCERTAINTY_LABELS = frozenset(
    {"unknown", "uncertain", "ambiguous", "pending", "verify", "unverified"}
)
_ACTIVE_REVIEW_SOURCE_LABEL = "candidate-approved"
_ACTIVE_REVIEW_UNCERTAINTY_LABEL = "confirmed"
_UNATTRIBUTED_REVIEW_SOURCE_LABEL = "source-unattributed"
_REVIEW_REDACTIONS = (
    "raw_document_text",
    "document_metadata",
    "contact_details",
    "compensation",
    "visa_or_authorization",
    "eeo",
    "screening_answers",
    "identifiers",
    "paths",
)
_REVIEW_MAX_ITEMS = 20
_REVIEW_MAX_OBJECT_KEYS = 20
_REVIEW_MAX_DEPTH = 3
_REVIEW_MAX_NUMBER = 1_000_000_000_000
_REVIEW_DATE_LIKE_PATTERN = re.compile(
    r"^(?:\d{4}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?(?:\s*[-–]\s*(?:\d{4}|present))?|"
    r"(?:present|current))$",
    re.IGNORECASE,
)
_REVIEW_PHONE_PATTERN = re.compile(
    r"(?<!\w)\+?[0-9][0-9 .()\-]{6,}[0-9](?!\w)"
)
_UNRESOLVED_INTERACTIVE_ANSWERS = frozenset(
    {
        "unknown",
        "uncertain",
        "ambiguous",
        "unsure",
        "unclear",
        "pending",
        "verify",
        "needs verification",
        "not sure",
        "n/a",
        "na",
    }
)
_APPROVED_SKILL_SECTIONS = frozenset({"professional", "personal_open_source", "learning_or_exposure"})
_APPROVED_ROLE_SECTIONS = frozenset({"include", "exclude_title_terms"})
_APPROVED_HARD_EXCLUSION_SECTIONS = frozenset({"mandatory_requirements"})
_APPROVED_LOCATION_PREFERENCE_SECTIONS = frozenset({"locations", "work_modes"})
_CURRENT_RECOMMENDATION_QUEUE_COLUMNS = (
    "profile_revision",
    "fingerprint",
    "platform",
    "score",
    "title",
    "company",
    "location",
    "work_mode",
    "url",
    "search_url",
    "discovered_at",
    "why_recommended",
    "gaps_before_applying",
    "human_action_required",
    "application_actions",
)


def _parse_inline_list(value: str, *, path: Path, line_number: int) -> list[str]:
    """Parse the small list form used by the approved profile's skill groups."""

    value = value.strip()
    if not value.startswith("[") or not value.endswith("]"):
        raise ValueError(f"Expected a bracketed list in approved profile {path} line {line_number}")
    return [item.strip().strip("\"'") for item in value[1:-1].split(",") if item.strip()]


def _approved_profile_mapping(path: Path) -> dict[str, Any]:
    """Extract only approved matching constraints and evidence from private YAML.

    This intentionally does not deserialize the complete private profile.  In
    particular, source status, compensation, contacts, and autofill policy are
    never placed in the mapping passed to ``CandidateProfile``.
    """

    if not path.exists():
        raise ValueError(f"Approved candidate profile is missing: {path}")
    roles: dict[str, list[str]] = {key: [] for key in _APPROVED_ROLE_SECTIONS}
    hard_exclusions: dict[str, list[str]] = {key: [] for key in _APPROVED_HARD_EXCLUSION_SECTIONS}
    skills: dict[str, list[str]] = {key: [] for key in _APPROVED_SKILL_SECTIONS}
    location_preferences: dict[str, list[str]] = {
        key: [] for key in _APPROVED_LOCATION_PREFERENCE_SECTIONS
    }
    years_experience: int | None = None
    active_top_level: str | None = None
    active_role_section: str | None = None
    active_hard_exclusion_section: str | None = None
    active_skill_section: str | None = None
    active_location_preference_section: str | None = None

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = re.split(r"\s+#", raw_line, maxsplit=1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0:
            active_top_level = stripped[:-1] if stripped.endswith(":") else None
            active_role_section = None
            active_hard_exclusion_section = None
            active_skill_section = None
            active_location_preference_section = None
            continue

        if active_top_level == "experience":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                if key == "years_experience" and value:
                    try:
                        years_experience = int(value)
                    except ValueError as exc:
                        raise ValueError(
                            f"Expected an integer years_experience in approved profile {path} line {line_number}"
                        ) from exc
            continue

        if active_top_level == "location_preferences":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                active_location_preference_section = (
                    key if key in _APPROVED_LOCATION_PREFERENCE_SECTIONS else None
                )
                if active_location_preference_section and value:
                    location_preferences[active_location_preference_section].extend(
                        _parse_inline_list(value, path=path, line_number=line_number)
                    )
            elif active_location_preference_section and stripped.startswith("- "):
                location_preferences[active_location_preference_section].append(
                    stripped[2:].strip().strip("\"'")
                )
            continue

        if active_top_level == "hard_exclusions":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                active_hard_exclusion_section = (
                    key if key in _APPROVED_HARD_EXCLUSION_SECTIONS else None
                )
                if active_hard_exclusion_section and value:
                    hard_exclusions[active_hard_exclusion_section].extend(
                        _parse_inline_list(value, path=path, line_number=line_number)
                    )
            elif active_hard_exclusion_section and stripped.startswith("- "):
                hard_exclusions[active_hard_exclusion_section].append(stripped[2:].strip().strip("\"'"))
            continue

        if active_top_level == "roles":
            if indent == 2 and ":" in stripped:
                key, value = (part.strip() for part in stripped.split(":", 1))
                active_role_section = key if key in _APPROVED_ROLE_SECTIONS else None
                if active_role_section and value:
                    roles[active_role_section].extend(_parse_inline_list(value, path=path, line_number=line_number))
            elif active_role_section and stripped.startswith("- "):
                roles[active_role_section].append(stripped[2:].strip().strip("\"'"))
            continue

        if active_top_level != "skills":
            continue
        if indent == 2 and ":" in stripped:
            key, value = (part.strip() for part in stripped.split(":", 1))
            active_skill_section = key if key in _APPROVED_SKILL_SECTIONS else None
            if active_skill_section and value:
                skills[active_skill_section].extend(_parse_inline_list(value, path=path, line_number=line_number))
        elif active_skill_section and stripped.startswith("- "):
            skills[active_skill_section].append(stripped[2:].strip().strip("\"'"))
        elif active_skill_section and ":" in stripped:
            _group, value = (part.strip() for part in stripped.split(":", 1))
            if value:
                skills[active_skill_section].extend(_parse_inline_list(value, path=path, line_number=line_number))

    return {
        "experience": {"years_experience": years_experience},
        "location_preferences": location_preferences,
        "roles": roles,
        "hard_exclusions": hard_exclusions,
        "skills": skills,
    }


def approved_candidate_profile(profile_path: Path | None = None) -> CandidateProfile:
    """Load the current evidence-only profile without importing autofill data."""

    return CandidateProfile.from_mapping(_approved_profile_mapping(profile_path or DEFAULT_CANDIDATE_PROFILE_PATH))


def _approved_fact(facts: Mapping[str, Any], dotted_path: str) -> Any:
    """Read an exact dotted fact or its equivalent nested representation."""

    if dotted_path in facts:
        return facts[dotted_path]
    current: Any = facts
    for segment in dotted_path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _fact_strings(value: Any) -> list[str]:
    """Project explicit string evidence while ignoring unsupported fact shapes."""

    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Mapping):
        strings: list[str] = []
        for nested in value.values():
            strings.extend(_fact_strings(nested))
        return strings
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _active_intake_profile_mapping(active_intake: Mapping[str, Any]) -> dict[str, Any]:
    """Project only matcher-approved evidence from a verified intake revision."""

    facts = active_intake["approved_facts"]
    years_experience = _approved_fact(facts, "experience.years_experience")
    if years_experience is None:
        years_experience = _approved_fact(facts, "experience.total_years")
    if isinstance(years_experience, bool) or not isinstance(years_experience, int):
        years_experience = None

    evidence_by_skill = _approved_fact(facts, "skills.evidence_by_skill")
    if not isinstance(evidence_by_skill, Mapping):
        evidence_by_skill = _approved_fact(facts, "evidence_by_skill")
    approved_evidence = {
        str(skill): label
        for skill, label in evidence_by_skill.items()
        if isinstance(skill, str)
        and skill.strip()
        and label in {"professional", "personal_open_source", "learning_or_exposure"}
    } if isinstance(evidence_by_skill, Mapping) else {}

    employment_types = _fact_strings(_approved_fact(facts, "targets.employment_types"))
    if not employment_types:
        employment_types = _fact_strings(
            _approved_fact(facts, "employment_type_preferences")
        )
    work_authorizations = _fact_strings(
        _approved_fact(facts, "work_authorization.authorized_locations")
    )
    if not work_authorizations:
        work_authorizations = _fact_strings(_approved_fact(facts, "work_authorizations"))

    return {
        "experience": {"years_experience": years_experience},
        "roles": {
            "include": _fact_strings(_approved_fact(facts, "roles.include")),
            "exclude_title_terms": _fact_strings(
                _approved_fact(facts, "roles.exclude_title_terms")
            ),
        },
        "hard_exclusions": {
            "mandatory_requirements": _fact_strings(
                _approved_fact(facts, "hard_exclusions.mandatory_requirements")
            )
        },
        "skills": {
            "professional": _fact_strings(_approved_fact(facts, "skills.professional")),
            "personal_open_source": _fact_strings(
                _approved_fact(facts, "skills.personal_open_source")
            ),
            "learning_or_exposure": _fact_strings(
                _approved_fact(facts, "skills.learning_or_exposure")
            ),
        },
        "location_preferences": {
            "locations": _fact_strings(
                _approved_fact(facts, "location_preferences.locations")
            ),
            "work_modes": _fact_strings(
                _approved_fact(facts, "location_preferences.work_modes")
            ),
        },
        "targets": {"employment_types": employment_types},
        "work_authorizations": work_authorizations,
        "evidence_by_skill": approved_evidence,
    }


def active_candidate_profile(intake_path: Path) -> CandidateProfile:
    """Load only an integrity-checked, explicitly user-confirmed intake."""

    active_intake = _active_candidate_intake(intake_path)
    return CandidateProfile.from_mapping(_active_intake_profile_mapping(active_intake))


def _active_candidate_intake(intake_path: Path) -> Mapping[str, Any]:
    """Read one active intake only through its integrity-check boundary."""

    if not intake_path.exists():
        raise ValueError("Candidate intake is missing")
    payload = json.loads(intake_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Candidate intake JSON must be an object")
    return validate_active_candidate_profile(payload)


def smart_queue_for_active_intake(
    intake_path: Path,
    database_path: Path | str,
    *,
    target_size: int | object = _UNSET_QUEUE_CAPACITY,
) -> SmartJobQueue:
    """Construct a queue from the active candidate's confirmed capacity.

    This is the live-host construction seam. The active, integrity-checked
    intake is the source of the capacity. A caller may repeat that exact value
    for defensive configuration checks, but a different value fails closed;
    it cannot invent a non-default live capacity. An absent optional preference
    uses the documented default of five without mutating candidate facts.
    """

    requested_target_size = target_size
    if requested_target_size is not _UNSET_QUEUE_CAPACITY:
        if (
            isinstance(requested_target_size, bool)
            or not isinstance(requested_target_size, int)
            or not 1 <= requested_target_size <= 10
        ):
            raise QueuePolicyError("target_size must be an integer between 1 and 10")

    active_intake = _active_candidate_intake(intake_path)
    confirmed_capacity = _approved_fact(
        active_intake["approved_facts"], "targets.smart_queue_capacity"
    )
    if confirmed_capacity is None:
        active_target_size = 5
    elif isinstance(confirmed_capacity, bool) or not isinstance(confirmed_capacity, int):
        # validate_active_candidate_profile normally makes this unreachable.
        # Keep the construction seam fail-closed if a future validator changes.
        raise ValueError("Active candidate smart queue capacity is invalid")
    else:
        active_target_size = confirmed_capacity
    if (
        requested_target_size is not _UNSET_QUEUE_CAPACITY
        and requested_target_size != active_target_size
    ):
        raise QueuePolicyError("configured target_size conflicts with the active candidate capacity")
    if active_target_size == 5:
        # The default never needs extra provenance to be accepted by a live
        # coordinator, which preserves existing default-capacity queues.
        return SmartJobQueue(database_path, target_size=active_target_size)
    return SmartJobQueue.for_active_candidate_intake(
        database_path,
        target_size=active_target_size,
        intake_revision_hash=str(active_intake["revision_hash"]),
    )


# The longer name remains a harmless discover-module alias for host code that
# adopted it before the public construction seam was stabilized.
active_smart_job_queue = smart_queue_for_active_intake


def _load_visible_payloads(path: Path | None) -> Mapping[str, Sequence[Mapping[str, Any]]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("pages"), dict):
        raw = raw["pages"]
    if not isinstance(raw, dict):
        raise ValueError("Visible payload JSON must be an object mapping search URLs to listing arrays")
    payloads: dict[str, Sequence[Mapping[str, Any]]] = {}
    for url, listings in raw.items():
        if not isinstance(url, str) or not isinstance(listings, list) or not all(isinstance(item, dict) for item in listings):
            raise ValueError("Every visible payload entry must map a URL to an array of listing objects")
        payloads[url] = listings
    return payloads


def _safe_csv_cell(value: Any) -> str | int:
    """Render review data without enabling spreadsheet-formula execution."""

    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        text = "; ".join(str(item) for item in value)
    elif value is None:
        text = ""
    else:
        text = str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text


def _read_candidate_intake(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        raise ValueError("Candidate intake is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Candidate intake JSON must be an object")
    return payload


class _InteractivePathPolicyError(ValueError):
    """Reject an interactive intake location without disclosing that location."""


def _raise_interactive_path_error() -> None:
    raise _InteractivePathPolicyError(_INTERACTIVE_PATH_ERROR)


def _is_link_like(path: Path, status: os.stat_result) -> bool:
    """Recognize links, Windows junctions, and other reparse-point indirection."""

    if stat.S_ISLNK(status.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(getattr(status, "st_file_attributes", 0) & reparse_flag)


def _validated_interactive_intake_path(
    intake_path: Path,
    *,
    _private_root: Path | None = None,
) -> Path:
    """Return a canonical regular-file target confined to the interactive root.

    ``_private_root`` is an internal dependency-injection seam for isolated unit
    tests. Production CLI callers cannot set it through arguments or the
    environment.
    """

    try:
        if not isinstance(intake_path, Path) or not str(intake_path).strip():
            _raise_interactive_path_error()
        if ".." in intake_path.parts:
            _raise_interactive_path_error()

        root_value = _INTERACTIVE_PRIVATE_ROOT if _private_root is None else _private_root
        if not isinstance(root_value, Path) or not str(root_value).strip():
            _raise_interactive_path_error()

        root = Path(os.path.abspath(root_value))
        candidate = Path(os.path.abspath(intake_path))
        if candidate == root or not candidate.is_relative_to(root):
            _raise_interactive_path_error()

        root_status = os.lstat(root)
        if _is_link_like(root, root_status) or not stat.S_ISDIR(root_status.st_mode):
            _raise_interactive_path_error()
        canonical_root = root.resolve(strict=True)
        canonical_candidate = candidate.resolve(strict=False)
        if canonical_candidate == canonical_root or not canonical_candidate.is_relative_to(
            canonical_root
        ):
            _raise_interactive_path_error()

        relative_candidate = candidate.relative_to(root)
        current = canonical_root
        for index, component in enumerate(relative_candidate.parts):
            current /= component
            try:
                current_status = os.lstat(current)
            except FileNotFoundError:
                continue
            if _is_link_like(current, current_status):
                _raise_interactive_path_error()
            is_target = index == len(relative_candidate.parts) - 1
            if is_target:
                if not stat.S_ISREG(current_status.st_mode):
                    _raise_interactive_path_error()
            elif not stat.S_ISDIR(current_status.st_mode):
                _raise_interactive_path_error()
        return canonical_candidate
    except _InteractivePathPolicyError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise _InteractivePathPolicyError(_INTERACTIVE_PATH_ERROR) from None


def _read_interactive_candidate_intake(
    intake_path: Path,
    *,
    _private_root: Path | None = None,
) -> Mapping[str, Any]:
    """Validate immediately before reading an interactive intake."""

    validated_path = _validated_interactive_intake_path(
        intake_path,
        _private_root=_private_root,
    )
    return _read_candidate_intake(validated_path)


def _question_draft_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Select draft fields without exposing the source payload."""

    if "state" in payload:
        return {
            "schema_version": payload.get("schema_version"),
            "documents": payload.get("documents"),
            "approved_facts": payload.get("approved_facts"),
            "unknown_fields": payload.get("unknown_fields"),
            "contradictions": payload.get("contradictions"),
            "pending_facts": payload.get("pending_facts"),
        }
    return payload


def _review_fact_entries(
    value: Any,
    field: str,
    *,
    source_hint: str | None = None,
    uncertainty_hint: str | None = None,
) -> list[tuple[str, Any, str | None, str | None]]:
    """Find allowlisted fact roots while retaining only optional safe labels."""

    if isinstance(value, Mapping):
        if "value" in value and set(value).issubset(_REVIEW_FACT_METADATA_FIELDS):
            return _review_fact_entries(
                value["value"],
                field,
                source_hint=value.get("source") if isinstance(value.get("source"), str) else None,
                uncertainty_hint=(
                    value.get("uncertainty")
                    if isinstance(value.get("uncertainty"), str)
                    else None
                ),
            )
        if _review_field_is_safe(field):
            return [(field, value, source_hint, uncertainty_hint)]
        entries: list[tuple[str, Any, str | None, str | None]] = []
        for nested_field, nested_value in value.items():
            if not isinstance(nested_field, str) or not nested_field.strip():
                continue
            child_field = f"{field}.{nested_field}" if field else nested_field
            entries.extend(
                _review_fact_entries(
                    nested_value,
                    child_field,
                    source_hint=source_hint,
                    uncertainty_hint=uncertainty_hint,
                )
            )
        return entries
    return [(field, value, source_hint, uncertainty_hint)]


def _review_field_is_safe(field: str) -> bool:
    """Allow only known matching or non-sensitive onboarding fact fields."""

    return field in _SAFE_REVIEW_VALUE_FIELDS


def _safe_review_scalar(value: Any) -> tuple[bool, Any]:
    """Return a bounded scalar that cannot carry contact or document metadata."""

    if isinstance(value, bool):
        return True, value
    if isinstance(value, int):
        return (
            (True, value)
            if abs(value) <= _REVIEW_MAX_NUMBER
            else (False, None)
        )
    if isinstance(value, float):
        return (
            (True, value)
            if math.isfinite(value) and abs(value) <= _REVIEW_MAX_NUMBER
            else (False, None)
        )
    if not isinstance(value, str):
        return False, None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > 120 or len(normalized.split()) > 16:
        return False, None
    if "@" in normalized or "://" in normalized:
        return False, None
    if not _REVIEW_DATE_LIKE_PATTERN.fullmatch(normalized) and _REVIEW_PHONE_PATTERN.search(normalized):
        return False, None
    return True, normalized


def _safe_review_mapping(
    value: Mapping[str, Any],
    allowed_keys: frozenset[str],
    *,
    depth: int,
) -> tuple[bool, dict[str, Any]]:
    """Render only allowlisted keys from one bounded structured fact object."""

    if depth > _REVIEW_MAX_DEPTH or len(value) > _REVIEW_MAX_OBJECT_KEYS:
        return False, {}
    rendered: dict[str, Any] = {}
    for key, nested_value in value.items():
        if not isinstance(key, str):
            continue
        normalized_key = key.strip().casefold()
        if normalized_key not in allowed_keys:
            continue
        valid, safe_value = _safe_review_value(
            nested_value,
            allowed_keys=allowed_keys,
            depth=depth + 1,
        )
        if valid:
            rendered[normalized_key] = safe_value
    return bool(rendered), rendered


def _safe_review_value(
    value: Any,
    *,
    allowed_keys: frozenset[str] | None = None,
    depth: int = 0,
) -> tuple[bool, Any]:
    """Render bounded JSON scalars/lists or an allowlisted structured object."""

    if depth > _REVIEW_MAX_DEPTH:
        return False, None
    if isinstance(value, Mapping):
        if allowed_keys is None:
            return False, None
        return _safe_review_mapping(value, allowed_keys, depth=depth)

    if isinstance(value, list):
        if not value or len(value) > _REVIEW_MAX_ITEMS:
            return False, None
        rendered: list[Any] = []
        for item in value:
            valid, safe_item = _safe_review_value(
                item,
                allowed_keys=allowed_keys,
                depth=depth + 1,
            )
            if not valid:
                return False, None
            rendered.append(safe_item)
        return True, rendered
    return _safe_review_scalar(value)


def _review_source_label(
    source_hint: str | None,
    *,
    has_documents: bool,
    candidate_confirmed: bool,
) -> str:
    """Map untrusted source text to a small, non-identifying label set."""

    if candidate_confirmed:
        return "candidate-provided-answer"
    source_terms = set(re.findall(r"[a-z0-9]+", (source_hint or "").casefold()))
    if source_terms & _REVIEW_SOURCE_DOCUMENT_TERMS:
        return "candidate-uploaded-document"
    if source_terms & _REVIEW_SOURCE_ANSWER_TERMS:
        return "candidate-provided-answer"
    if has_documents:
        return _UNATTRIBUTED_REVIEW_SOURCE_LABEL
    return "candidate-provided-structured-intake"


def _review_uncertainty_label(
    uncertainty_hint: str | None,
    *,
    candidate_confirmed: bool,
) -> str:
    """Map untrusted uncertainty text to deterministic review labels."""

    if candidate_confirmed:
        return "candidate-confirmed-pending-activation"
    normalized = " ".join((uncertainty_hint or "").casefold().split())
    if normalized in _REVIEW_UNCERTAINTY_LABELS:
        return normalized
    if normalized in {"confirmed", "candidate-confirmed", "verified"}:
        return "candidate-confirmed-pending-activation"
    return "requires-candidate-confirmation"


def _candidate_review_rendering(
    draft: Mapping[str, Any],
    *,
    active: bool = False,
    candidate_confirmed_fields: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Build the only structured fact rendering permitted before activation."""

    safe_batch = pending_verification_batch(draft)
    requires_confirmation = bool(
        draft["unknown_fields"] or draft["contradictions"] or draft["pending_facts"]
    )
    facts: list[dict[str, Any]] = []
    has_documents = bool(draft["documents"])
    for field, value in draft["approved_facts"].items():
        for fact_field, fact_value, source_hint, uncertainty_hint in _review_fact_entries(value, field):
            if not _review_field_is_safe(fact_field):
                continue
            structured_keys = _SAFE_REVIEW_STRUCTURED_FIELDS.get(fact_field)
            valid, safe_value = _safe_review_value(
                fact_value,
                allowed_keys=structured_keys,
            )
            if not valid:
                continue
            facts.append(
                {
                    "field": fact_field,
                    "value": safe_value,
                    "uncertainty": _review_uncertainty_label(
                        uncertainty_hint,
                        candidate_confirmed=fact_field in candidate_confirmed_fields,
                    ),
                    "source": _review_source_label(
                        source_hint,
                        has_documents=has_documents,
                        candidate_confirmed=fact_field in candidate_confirmed_fields,
                    ),
                }
            )
    facts.sort(key=lambda item: item["field"])
    if active:
        for fact in facts:
            fact["source"] = _ACTIVE_REVIEW_SOURCE_LABEL
            fact["uncertainty"] = _ACTIVE_REVIEW_UNCERTAINTY_LABEL
    return {
        "status": "active" if active else ("blocked" if requires_confirmation else "ready-for-confirmation"),
        "required_before_activation": not active,
        "facts": facts,
        "fact_count": len(facts),
        "unresolved": safe_batch,
        "redactions": list(_REVIEW_REDACTIONS),
    }


def _valid_active_intake_payload(payload: Mapping[str, Any]) -> bool:
    """Return whether a payload proves an active user-confirmed revision."""

    if payload.get("state") != "active":
        return False
    try:
        validate_active_candidate_profile(payload)
    except (TypeError, ValueError):
        return False
    return True


def _print_pending_intake_questions(payload: Mapping[str, Any]) -> tuple[int, str]:
    draft = validate_candidate_intake(_question_draft_payload(payload))
    questions = completion_questions(draft)
    if not questions:
        if not _valid_active_intake_payload(payload):
            return (
                2,
                "Candidate intake has no unresolved items. Show the privacy-safe structured review, "
                "obtain final candidate confirmation, activate with actor='user', and rerun discovery.",
            )
        return (
            0,
            "Candidate intake is active and has no unresolved items.",
        )
    rendered = ["Candidate onboarding questions (ask all at once):"]
    rendered.extend(f"- {question}" for question in questions)
    rendered.append(
        "Do not infer or default sensitive values. Apply only explicit candidate-confirmed answers."
    )
    return (2, "\n".join(rendered))


def _intake_question_bundle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a machine-readable onboarding payload for host orchestration."""

    draft = validate_candidate_intake(_question_draft_payload(payload))
    has_unresolved_items = bool(
        draft["unknown_fields"] or draft["contradictions"] or draft["pending_facts"]
    )
    # A resolved draft is still inactive. It must be shown to the candidate
    # for one final confirmation before the host may call the activation gate.
    active = _valid_active_intake_payload(payload) and not has_unresolved_items
    requires_confirmation = not active
    questions = completion_questions(draft)
    safe_batch = pending_verification_batch(draft)
    contradiction_questions = iter(questions)
    safe_contradictions = [
        {
            "kind": "contradiction",
            "field": item["field"],
            "prompt": next(contradiction_questions),
        }
        for item in safe_batch["contradictions"]
    ]
    safe_unknowns = [
        {
            "kind": "unknown",
            "field": item,
            "prompt": next(contradiction_questions),
        }
        for item in safe_batch["unknown_fields"]
    ]
    safe_pending = [
        {
            "kind": "pending",
            "field": item["field"],
            "prompt": next(contradiction_questions),
        }
        for item in safe_batch["pending_facts"]
    ]
    question_groups = {
        "contradictions": safe_contradictions,
        "unknown_fields": safe_unknowns,
        "pending_facts": safe_pending,
    }
    prompt_block = [
        {
            "kind": "ask_once",
            "instructions": "Ask all unresolved items in one candidate-facing round before continuing discovery."
        }
    ] + safe_contradictions + safe_unknowns + safe_pending
    return {
        "status": "ready" if not requires_confirmation else "blocked",
        "schema_version": draft["schema_version"],
        "questions": questions,
        "question_count": len(questions),
        "question_plan": {
            "ask_once": True,
            "unknown_count": len(draft["unknown_fields"]),
            "contradiction_count": len(draft["contradictions"]),
            "pending_count": len(draft["pending_facts"]),
            "groups": question_groups,
            "prompts": prompt_block,
        },
        "pending_verification_batch": {
            **safe_batch,
        },
        "candidate_review": _candidate_review_rendering(draft, active=active),
        "requires_user_confirmation": requires_confirmation,
    }


def _interactive_onboarding_draft(
    intake_path: Path,
    *,
    _private_root: Path | None = None,
) -> dict[str, Any]:
    """Load the explicit target or the redacted starter without creating either."""

    validated_path = _validated_interactive_intake_path(
        intake_path,
        _private_root=_private_root,
    )
    payload = (
        _read_interactive_candidate_intake(validated_path, _private_root=_private_root)
        if validated_path.exists()
        else _read_candidate_intake(REDACTED_CANDIDATE_INTAKE_PATH)
    )
    return validate_candidate_intake(_question_draft_payload(payload))


def _interactive_review_items(draft: Mapping[str, Any]) -> list[tuple[str, str, str, str]]:
    """Pair each private storage key with its safe, deterministic prompt."""

    questions = iter(completion_questions(draft))
    safe_batch = pending_verification_batch(draft)
    items: list[tuple[str, str, str, str]] = []
    for contradiction, safe_item in zip(draft["contradictions"], safe_batch["contradictions"]):
        items.append(("contradiction", contradiction["field"], safe_item["field"], next(questions)))
    for field, safe_field in zip(draft["unknown_fields"], safe_batch["unknown_fields"]):
        items.append(("unknown", field, safe_field, next(questions)))
    for pending, safe_item in zip(draft["pending_facts"], safe_batch["pending_facts"]):
        items.append(("pending", pending["field"], safe_item["field"], next(questions)))
    return items


def _parse_interactive_answer(answer: str) -> Any:
    """Preserve a candidate's explicit JSON value, with text as a safe fallback."""

    try:
        return json.loads(answer)
    except json.JSONDecodeError:
        return answer


def _is_unresolved_interactive_answer(answer: Any) -> bool:
    """Keep explicit uncertainty markers in the draft review state."""

    if answer is None or answer == [] or answer == {}:
        return True
    if not isinstance(answer, str):
        return False
    normalized = " ".join(answer.casefold().strip(" .,!?;:").split())
    if normalized in _UNRESOLVED_INTERACTIVE_ANSWERS or normalized in {
        "i don't know",
        "i do not know",
        "i'm unsure",
        "i am unsure",
        "i'm uncertain",
        "i am uncertain",
        "cannot confirm",
        "can't confirm",
        "need to verify",
    }:
        return True
    return bool(
        re.fullmatch(
            r"(?:i(?:'m| am)\s+)?(?:unknown|uncertain|ambiguous|unsure|unclear|not sure)",
            normalized,
        )
    )


def _interactive_answer_prompt(field: str) -> str:
    """Describe required structure without reflecting any candidate-provided value."""

    if field in _JSON_STRING_LIST_FIELDS:
        return (
            "Expected value shape: a JSON array of strings.\n"
            "Candidate-approved answer (leave blank to keep this item unresolved): "
        )
    return (
        "Candidate-approved answer (leave blank to keep this item unresolved; "
        "use JSON for a list, object, number, or boolean): "
    )


def _record_interactive_answer(draft: dict[str, Any], *, group: str, field: str, answer: Any) -> None:
    """Record exactly one supplied answer and clear its corresponding review item."""

    draft["approved_facts"][field] = answer
    if group == "unknown":
        draft["unknown_fields"].remove(field)
    elif group == "contradiction":
        draft["contradictions"] = [item for item in draft["contradictions"] if item["field"] != field]
    else:
        draft["pending_facts"] = [item for item in draft["pending_facts"] if item["field"] != field]


def _persist_active_candidate_intake(
    intake_path: Path,
    active_intake: Mapping[str, Any],
    *,
    _private_root: Path | None = None,
) -> None:
    """Atomically replace an interactive intake confined to its private root."""

    validated_path = _validated_interactive_intake_path(
        intake_path,
        _private_root=_private_root,
    )
    validated_path.parent.mkdir(parents=True, exist_ok=True)
    validated_path = _validated_interactive_intake_path(
        validated_path,
        _private_root=_private_root,
    )
    temporary_path: Path | None = None
    try:
        validated_path = _validated_interactive_intake_path(
            validated_path,
            _private_root=_private_root,
        )
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=validated_path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(active_intake, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        validated_path = _validated_interactive_intake_path(
            validated_path,
            _private_root=_private_root,
        )
        temporary_path.replace(validated_path)
        temporary_path = None
        validated_path = _validated_interactive_intake_path(
            validated_path,
            _private_root=_private_root,
        )
        _sync_parent_directory(validated_path.parent)
    except BaseException:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _sync_parent_directory(directory: Path) -> None:
    """Persist a replacement's directory entry when the platform supports it."""

    directory_flag = getattr(os, "O_DIRECTORY", None)
    if directory_flag is None:
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _interactive_confirmation_prompt(
    collected_safe_fields: Sequence[str], unresolved_safe_fields: Sequence[str]
) -> str:
    """Describe the exact lowercase confirmation token without exposing values."""

    fields = ", ".join(collected_safe_fields) if collected_safe_fields else "none"
    unresolved = ", ".join(unresolved_safe_fields) if unresolved_safe_fields else "none"
    return (
        f"Privacy-safe structured candidate review is shown above. Collected "
        f"{len(collected_safe_fields)} field(s): {fields}. "
        f"Unresolved field(s): {unresolved}. "
        f"Type exactly {INTERACTIVE_CONFIRMATION_TOKEN!r} to confirm activation; every other response declines: "
    )


def _run_interactive_onboarding(
    intake_path: Path,
    *,
    _private_root: Path | None = None,
) -> int:
    """Collect explicit candidate answers and activate only after final confirmation."""

    validated_path = _validated_interactive_intake_path(
        intake_path,
        _private_root=_private_root,
    )
    if validated_path.exists():
        existing_intake = _read_interactive_candidate_intake(
            validated_path,
            _private_root=_private_root,
        )
        if existing_intake.get("state") == "active":
            try:
                validate_active_candidate_profile(existing_intake)
            except ValueError:
                print("Existing active candidate intake failed validation; interactive onboarding made no changes.", file=sys.stderr)
                return 2
            print("Candidate intake is already active; interactive onboarding made no changes.")
            return 0

    draft = _interactive_onboarding_draft(
        validated_path,
        _private_root=_private_root,
    )
    collected_safe_fields: list[str] = []
    unresolved_safe_fields: list[str] = []
    candidate_confirmed_fields: set[str] = set()
    try:
        for group, field, safe_field, question in _interactive_review_items(draft):
            answer = input(f"{question}\n{_interactive_answer_prompt(field)}").strip()
            if not answer:
                continue
            parsed_answer = _parse_interactive_answer(answer)
            if _is_unresolved_interactive_answer(parsed_answer):
                unresolved_safe_fields.append(safe_field)
                continue
            _record_interactive_answer(
                draft,
                group=group,
                field=field,
                answer=parsed_answer,
            )
            candidate_confirmed_fields.add(field)
            collected_safe_fields.append(safe_field)

        print("Privacy-safe structured candidate review (values are limited to safe fields):")
        print(
            json.dumps(
                _candidate_review_rendering(
                    draft,
                    candidate_confirmed_fields=candidate_confirmed_fields,
                ),
                indent=2,
                sort_keys=True,
            )
        )
        confirmation = input(
            _interactive_confirmation_prompt(collected_safe_fields, unresolved_safe_fields)
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("Interactive onboarding ended before confirmation; intake was not changed.", file=sys.stderr)
        return 2

    if confirmation != INTERACTIVE_CONFIRMATION_TOKEN:
        print("Interactive onboarding was not confirmed; intake was not changed.", file=sys.stderr)
        return 2
    if draft["unknown_fields"] or draft["contradictions"] or draft["pending_facts"]:
        print("Interactive onboarding is incomplete; intake was not changed.", file=sys.stderr)
        return 2

    active_intake = activate_candidate_profile(draft, actor="user")
    try:
        _persist_active_candidate_intake(
            validated_path,
            active_intake,
            _private_root=_private_root,
        )
    except KeyboardInterrupt:
        print("Interactive onboarding was interrupted while saving; inspect the intake before retrying.", file=sys.stderr)
        return 2
    print("Candidate intake activated after explicit user confirmation.")
    return 0


def export_current_profile_recommendation_queue(
    profile: CandidateProfile, recommendation_export_path: Path, queue_path: Path
) -> int:
    """Write a CSV review queue for only the active sanitized profile revision.

    Raw JSONL remains the immutable audit history.  This command does not
    discover listings, browse a board, or perform an application action.
    """

    if queue_path.suffix.casefold() != ".csv":
        raise ValueError("Current-profile recommendation queues must use a .csv path")
    rows = current_profile_recommendations(profile, recommendation_export_path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile("w", newline="", encoding="utf-8", dir=queue_path.parent, delete=False) as stream:
            temporary_path = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=_CURRENT_RECOMMENDATION_QUEUE_COLUMNS, extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "profile_revision": _safe_csv_cell(row["profile_revision"]),
                        "fingerprint": _safe_csv_cell(row["fingerprint"]),
                        "platform": _safe_csv_cell(row["platform"]),
                        "score": _safe_csv_cell(row["score"]),
                        "title": _safe_csv_cell(row.get("title")),
                        "company": _safe_csv_cell(row.get("company")),
                        "location": _safe_csv_cell(row.get("location")),
                        "work_mode": _safe_csv_cell(row.get("work_mode")),
                        "url": _safe_csv_cell(row["url"]),
                        "search_url": _safe_csv_cell(row.get("search_url")),
                        "discovered_at": _safe_csv_cell(row.get("discovered_at")),
                        "why_recommended": _safe_csv_cell(row.get("reasons", ())),
                        "gaps_before_applying": _safe_csv_cell(row.get("gaps", ())),
                        "human_action_required": _safe_csv_cell(row.get("human_action_required")),
                        "application_actions": _safe_csv_cell(row["application_actions"]),
                    }
                )
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.replace(queue_path)
    except Exception:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
        raise
    return len(rows)


def main(
    argv: Sequence[str] | None = None,
    *,
    _private_root: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Export only 85+ profile-fit jobs from injected visible-page JSON.")
    parser.add_argument("--visible-payloads", type=Path, help="Offline JSON mapping of visible search URLs to listing payloads.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data", help="Local directory for state, exports, and run logs.")
    parser.add_argument("--profiles", type=Path, default=PROJECT_ROOT / "config" / "search_profiles.yaml")
    parser.add_argument(
        "--candidate-intake",
        type=Path,
        help=(
            "Required candidate intake revision. Interactive onboarding may write only to a "
            "validated regular-file target beneath this script's private directory."
        ),
    )
    parser.add_argument(
        "--show-intake-questions",
        action="store_true",
        help="Validate a draft intake and print unresolved onboarding questions before discovery.",
    )
    parser.add_argument(
        "--interactive-onboarding",
        action="store_true",
        help=(
            "Collect explicit candidate answers locally, writing only beneath this script's "
            "private directory after exact lowercase 'yes' confirmation."
        ),
    )
    parser.add_argument(
        "--onboarding-format",
        choices=("text", "json"),
        default="text",
        help="Render onboarding output as plain text or JSON.",
    )
    parser.add_argument(
        "--candidate-profile",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--export-current-recommendations",
        type=Path,
        nargs="?",
        const=PROJECT_ROOT / "output" / "Current_Profile_Recommended_Queue.csv",
        help=(
            "Write a read-only CSV queue for the current profile revision; defaults to "
            "output/Current_Profile_Recommended_Queue.csv."
        ),
    )
    arguments = parser.parse_args(argv)
    try:
        if arguments.candidate_intake is None:
            raise ValueError("--candidate-intake is required and must reference an active user-confirmed intake")
        if arguments.interactive_onboarding and arguments.show_intake_questions:
            raise ValueError("interactive onboarding and question-display modes cannot be combined")
        if arguments.interactive_onboarding:
            return _run_interactive_onboarding(
                arguments.candidate_intake,
                _private_root=_private_root,
            )
        if arguments.show_intake_questions:
            raw_candidate_intake = (
                _read_candidate_intake(arguments.candidate_intake)
                if arguments.candidate_intake.exists()
                else _read_candidate_intake(REDACTED_CANDIDATE_INTAKE_PATH)
            )
            if arguments.onboarding_format == "json":
                bundle = _intake_question_bundle(raw_candidate_intake)
                print(json.dumps(bundle, indent=2, sort_keys=True))
                return 0 if bundle["status"] == "ready" else 2

            status, message = _print_pending_intake_questions(raw_candidate_intake)
            print(message)
            return status

        raw_candidate_intake = _read_candidate_intake(arguments.candidate_intake)
        try:
            active_intake = validate_active_candidate_profile(raw_candidate_intake)
        except ValueError:
            status, message = _print_pending_intake_questions(raw_candidate_intake)
            if status == 0:
                message = "Discovery is blocked until candidate intake is activated with actor='user'."
            print(f"Discovery blocked until intake is fully confirmed: {message}", file=sys.stderr)
            return 2 if status == 0 else status

        candidate_profile = CandidateProfile.from_mapping(_active_intake_profile_mapping(active_intake))
        if arguments.export_current_recommendations is not None:
            queue_rows = export_current_profile_recommendation_queue(
                candidate_profile,
                arguments.output_dir / "recommended_jobs.jsonl",
                arguments.export_current_recommendations,
            )
            print(
                "Exported current-profile recommendation queue: "
                f"{arguments.export_current_recommendations} ({queue_rows} rows; application_actions=0)"
            )
            return 0
        payloads = _load_visible_payloads(arguments.visible_payloads)
        result = run_discovery(
            candidate_profile, load_search_profiles(arguments.profiles), MappingVisiblePageAdapter(payloads),
            state_path=arguments.output_dir / "discovery_state.json",
            export_path=arguments.output_dir / "recommended_jobs.jsonl",
            run_log_path=arguments.output_dir / "discovery_runs.jsonl",
        )
    except json.JSONDecodeError:
        print("Discovery did not run: local input is not valid JSON.", file=sys.stderr)
        return 2
    except OSError:
        print("Discovery did not run: unable to read or write local input.", file=sys.stderr)
        return 2
    except ValueError as exc:
        if str(exc) == "Candidate intake is missing":
            print("Discovery did not run: candidate intake is missing.", file=sys.stderr)
            return 2
        print("Discovery did not run: local input failed validation.", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
