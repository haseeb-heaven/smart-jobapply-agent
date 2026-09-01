"""Validation boundary for untrusted LLM listing extraction output.

The schema deliberately contains source facts and evidence mappings only.  It
has no eligibility, scoring, recommendation, or hard-rejection override field.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .models import JobListing, JobRequirement


class ListingExtractionValidationError(ValueError):
    """Raised when an extracted listing violates the closed schema."""


_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "source_url",
        "source_job_id",
        "observed_at",
        "title",
        "company",
        "location",
        "work_mode",
        "employment_type",
        "requirements",
    }
)
_REQUIREMENT_FIELDS = frozenset(
    {"id", "text", "kind", "importance", "minimum_years", "subject", "source_evidence", "evaluation"}
)
_REQUIREMENT_REQUIRED_FIELDS = _REQUIREMENT_FIELDS - {"subject"}
_EVALUATION_FIELDS = frozenset({"value", "candidate_evidence_ids"})
_WORK_MODES = frozenset({"onsite", "hybrid", "remote", "unknown"})
_EMPLOYMENT_TYPES = frozenset(
    {"full_time", "part_time", "contract", "temporary", "internship", "apprenticeship", "volunteer", "unknown"}
)
_REQUIREMENT_KINDS = frozenset(
    {
        "experience",
        "skill",
        "education",
        "certification",
        "responsibility",
        "domain",
        "location",
        "work_mode",
        "employment_type",
        "authorization",
        "language",
        "other",
    }
)
_IMPORTANCE_VALUES = frozenset({"mandatory", "preferred", "informational"})
_EVALUATION_VALUES = frozenset({"met", "partial", "missing", "unknown"})
_RFC3339_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_CREDENTIAL_QUERY_KEYS = frozenset(
    {"access_token", "api_key", "apikey", "auth", "authorization", "cookie", "key", "password", "passwd", "session", "token"}
)
_LISTING_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+\Z")


def _require_closed_mapping(
    value: object,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    location: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ListingExtractionValidationError(f"{location} must be an object")
    keys = set(value)
    if not all(isinstance(key, str) for key in keys) or not keys <= allowed:
        raise ListingExtractionValidationError(f"{location} violates the closed field schema")
    missing = required - keys
    if missing:
        field = sorted(missing)[0]
        raise ListingExtractionValidationError(f"{location}.{field} is required")
    return value


def _require_nonblank_string(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ListingExtractionValidationError(f"{location} must be a non-blank string")
    return value


def _require_list(value: object, *, location: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ListingExtractionValidationError(f"{location} must be a list")
    return value


def _validate_source_url(value: object) -> tuple[str, str]:
    source_url = _require_nonblank_string(value, location="source_url")
    if any(character.isspace() for character in source_url):
        raise ListingExtractionValidationError("source_url must be a credential-free HTTPS URL")
    try:
        parsed = urlsplit(source_url)
        port = parsed.port
    except ValueError as error:
        raise ListingExtractionValidationError("source_url must be a credential-free HTTPS URL") from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise ListingExtractionValidationError("source_url must be a credential-free HTTPS URL")
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & _CREDENTIAL_QUERY_KEYS:
        raise ListingExtractionValidationError("source_url must not contain credentials")

    hostname = parsed.hostname.casefold().rstrip(".")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if hostname in {"linkedin.com", "www.linkedin.com"}:
        match = re.fullmatch(r"/jobs/view/([A-Za-z0-9_-]+)", parsed.path)
        if match is None or query or parsed.fragment:
            raise ListingExtractionValidationError(
                "source_url must be a canonical LinkedIn listing URL"
            )
        return source_url, match.group(1)

    if hostname == "indeed.com" or hostname.endswith(".indeed.com"):
        if parsed.path != "/viewjob" or parsed.fragment or len(query) != 1 or query[0][0].casefold() != "jk":
            raise ListingExtractionValidationError(
                "source_url must be a canonical Indeed listing URL"
            )
        source_identity = query[0][1]
        if _LISTING_ID_PATTERN.fullmatch(source_identity) is None:
            raise ListingExtractionValidationError(
                "source_url must contain a canonical Indeed listing identity"
            )
        if parsed.query != f"jk={source_identity}":
            raise ListingExtractionValidationError(
                "source_url must use the canonical Indeed listing identity query"
            )
        return source_url, source_identity

    raise ListingExtractionValidationError(
        "source_url must be a canonical LinkedIn or Indeed listing URL"
    )


def _validate_source_identity(source_job_id: object, *, url_identity: str) -> str:
    source_identity = _require_nonblank_string(source_job_id, location="source_job_id")
    if _LISTING_ID_PATTERN.fullmatch(source_identity) is None:
        raise ListingExtractionValidationError("source_job_id must be a canonical listing identity")
    if source_identity != url_identity:
        raise ListingExtractionValidationError("source_job_id must match the listing URL identity")
    return source_identity


def _validate_observed_at(value: object) -> str:
    observed_at = _require_nonblank_string(value, location="observed_at")
    if _RFC3339_PATTERN.fullmatch(observed_at) is None:
        raise ListingExtractionValidationError("observed_at must be a timezone-aware RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ListingExtractionValidationError(
            "observed_at must be a timezone-aware RFC3339 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ListingExtractionValidationError("observed_at must include a timezone")
    return observed_at


def _validate_candidate_evidence_ids(value: object, *, evaluation_value: str) -> list[str]:
    values = _require_list(value, location="evaluation.candidate_evidence_ids")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        evidence_id = _require_nonblank_string(item, location="evaluation.candidate_evidence_ids item")
        if evidence_id in seen:
            raise ListingExtractionValidationError("evaluation.candidate_evidence_ids must be unique")
        seen.add(evidence_id)
        result.append(evidence_id)
    if evaluation_value in {"missing", "unknown"} and result:
        raise ListingExtractionValidationError(
            "evaluation.candidate_evidence_ids must be empty for missing or unknown requirements"
        )
    if evaluation_value in {"met", "partial"} and not result:
        raise ListingExtractionValidationError(
            "evaluation.candidate_evidence_ids is required for met or partial requirements"
        )
    return result


def _validate_evaluation(value: object) -> dict[str, Any]:
    evaluation = _require_closed_mapping(
        value,
        allowed=_EVALUATION_FIELDS,
        required=_EVALUATION_FIELDS,
        location="evaluation",
    )
    evaluation_value = _require_nonblank_string(evaluation["value"], location="evaluation.value")
    if evaluation_value not in _EVALUATION_VALUES:
        raise ListingExtractionValidationError("evaluation.value is not in the closed enum")
    return {
        "value": evaluation_value,
        "candidate_evidence_ids": _validate_candidate_evidence_ids(
            evaluation["candidate_evidence_ids"], evaluation_value=evaluation_value
        ),
    }


def _validate_minimum_years(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ListingExtractionValidationError("requirement.minimum_years must be a non-negative number or null")
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise ListingExtractionValidationError("requirement.minimum_years must be a finite non-negative number or null")
    return value


def _validate_requirements(value: object) -> list[dict[str, Any]]:
    requirements = _require_list(value, location="requirements")
    result: list[dict[str, Any]] = []
    requirement_ids: set[str] = set()
    for item in requirements:
        requirement = _require_closed_mapping(
            item,
            allowed=_REQUIREMENT_FIELDS,
            required=_REQUIREMENT_REQUIRED_FIELDS,
            location="requirement",
        )
        requirement_id = _require_nonblank_string(requirement["id"], location="requirement.id")
        if requirement_id in requirement_ids:
            raise ListingExtractionValidationError("requirement.id must be unique")
        requirement_ids.add(requirement_id)
        kind = _require_nonblank_string(requirement["kind"], location="requirement.kind")
        if kind not in _REQUIREMENT_KINDS:
            raise ListingExtractionValidationError("requirement.kind is not in the closed enum")
        importance = _require_nonblank_string(requirement["importance"], location="requirement.importance")
        if importance not in _IMPORTANCE_VALUES:
            raise ListingExtractionValidationError("requirement.importance is not in the closed enum")
        text = _require_nonblank_string(requirement["text"], location="requirement.text")
        source_evidence = _require_nonblank_string(
            requirement["source_evidence"], location="requirement.source_evidence"
        )
        raw_subject = requirement.get("subject", "")
        if kind in {"skill", "authorization"}:
            subject = _require_nonblank_string(raw_subject, location="requirement.subject")
            visible_requirement = " ".join(f"{text} {source_evidence}".casefold().split())
            if " ".join(subject.casefold().split()) not in visible_requirement:
                raise ListingExtractionValidationError(
                    "requirement.subject must be present in its bounded visible evidence"
                )
        elif raw_subject == "":
            subject = ""
        else:
            subject = _require_nonblank_string(raw_subject, location="requirement.subject")
        validated_requirement = {
            "id": requirement_id,
            "text": text,
            "kind": kind,
            "importance": importance,
            "minimum_years": _validate_minimum_years(requirement["minimum_years"]),
            "source_evidence": source_evidence,
            "evaluation": _validate_evaluation(requirement["evaluation"]),
        }
        if subject:
            validated_requirement["subject"] = subject
        result.append(validated_requirement)
    return result


def validate_listing_extraction(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach one evidence-only listing extraction."""

    listing = _require_closed_mapping(
        payload,
        allowed=_TOP_LEVEL_FIELDS,
        required=_TOP_LEVEL_FIELDS,
        location="listing extraction",
    )
    schema_version = listing["schema_version"]
    if isinstance(schema_version, bool) or schema_version != 1:
        raise ListingExtractionValidationError("schema_version must be 1")
    work_mode = _require_nonblank_string(listing["work_mode"], location="work_mode")
    if work_mode not in _WORK_MODES:
        raise ListingExtractionValidationError("work_mode is not in the closed enum")
    employment_type = _require_nonblank_string(listing["employment_type"], location="employment_type")
    if employment_type not in _EMPLOYMENT_TYPES:
        raise ListingExtractionValidationError("employment_type is not in the closed enum")
    source_url, url_identity = _validate_source_url(listing["source_url"])
    return {
        "schema_version": 1,
        "source_url": source_url,
        "source_job_id": _validate_source_identity(listing["source_job_id"], url_identity=url_identity),
        "observed_at": _validate_observed_at(listing["observed_at"]),
        "title": _require_nonblank_string(listing["title"], location="title"),
        "company": _require_nonblank_string(listing["company"], location="company"),
        "location": _require_nonblank_string(listing["location"], location="location"),
        "work_mode": work_mode,
        "employment_type": employment_type,
        "requirements": _validate_requirements(listing["requirements"]),
    }


def listing_from_validated_extraction(payload: Mapping[str, Any]) -> JobListing:
    """Build the deterministic matcher model from closed extraction output.

    The LLM-provided ``evaluation`` object is intentionally discarded. The
    matcher receives only validated listing facts and recomputes every
    eligibility outcome from candidate-approved evidence.
    """

    extraction = validate_listing_extraction(payload)
    hostname = urlsplit(extraction["source_url"]).hostname or ""
    platform = "linkedin" if hostname.casefold().rstrip(".").endswith("linkedin.com") else "indeed"
    requirements = tuple(
        JobRequirement(
            requirement_id=requirement["id"],
            text=requirement["text"],
            kind=requirement["kind"],
            importance=requirement["importance"],
            minimum_years=requirement["minimum_years"],
            subject=requirement.get("subject", ""),
            source_evidence=requirement["source_evidence"],
        )
        for requirement in extraction["requirements"]
    )
    return JobListing(
        title=extraction["title"],
        description=" ".join(requirement.source_evidence for requirement in requirements),
        company=extraction["company"],
        platform=platform,
        url=extraction["source_url"],
        location=extraction["location"],
        work_mode=extraction["work_mode"],
        employment_type=extraction["employment_type"],
        discovered_at=extraction["observed_at"],
        source_job_id=extraction["source_job_id"],
        requirements=requirements,
    )
