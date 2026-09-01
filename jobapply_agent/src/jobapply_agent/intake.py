"""Candidate-controlled intake validation with no document-reading authority.

This module validates already-supplied metadata and approved facts.  It never
opens a document path, extracts resume text, or invents a value for an unknown
field.  Returned objects do not share mutable containers with caller input.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re
from typing import Any


class CandidateIntakeValidationError(ValueError):
    """Raised when candidate intake violates the closed local schema."""


_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "documents",
        "approved_facts",
        "unknown_fields",
        "contradictions",
        "pending_facts",
    }
)
_DOCUMENT_FIELDS = frozenset({"document_id", "path", "sha256", "media_type", "review_state"})
_CONTRADICTION_FIELDS = frozenset({"field", "status", "values", "evidence_ids"})
_PENDING_FACT_FIELDS = frozenset({"field", "status", "question", "reason"})
_DRAFT_FIELDS = _TOP_LEVEL_FIELDS | {"state"}
_ACTIVE_FIELDS = _DRAFT_FIELDS | {"activated_by", "confirmed_at", "revision_hash"}
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}\Z")

# Candidate intake keeps arbitrary fact keys so the validator can preserve a
# candidate-controlled draft without inventing a schema.  Candidate-facing
# helpers use this narrower allowlist: anything outside it is represented only
# by a stable, content-free review reference.
_PUBLIC_INTAKE_FIELDS = frozenset(
    {
        "candidate_profile",
        "identity.contact",
        "experience.current_title",
        "experience.title",
        "experience.total_years",
        "experience.roles_and_dates",
        "experience.achievements_and_metrics",
        "education",
        "certifications",
        "projects",
        "open_source",
        "volunteer_work",
        "languages",
        "skills.professional",
        "skills.personal_open_source",
        "skills.learning_or_exposure",
        "targets.roles_and_levels",
        "targets.locations_and_work_modes",
        "targets.employment_types_and_industries",
        "targets.exclusions",
        "work_authorization",
        "sponsorship",
        "compensation",
        "availability",
        "availability.start_date",
        "employment.current.start_date",
        "employment.current.end_date",
        "eeo",
        "location_preferences",
    }
)


def _require_closed_mapping(
    value: object,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    location: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CandidateIntakeValidationError(f"{location} must be an object")
    keys = set(value)
    if not all(isinstance(key, str) for key in keys) or not keys <= allowed:
        raise CandidateIntakeValidationError(f"{location} violates the closed field schema")
    missing = required - keys
    if missing:
        field = sorted(missing)[0]
        raise CandidateIntakeValidationError(f"{location}.{field} is required")
    return value


def _require_nonblank_string(value: object, *, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateIntakeValidationError(f"{location} must be a non-blank string")
    return value


def _require_list(value: object, *, location: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CandidateIntakeValidationError(f"{location} must be a list")
    return value


def _validate_unique_strings(value: object, *, location: str) -> list[str]:
    items = _require_list(value, location=location)
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        string = _require_nonblank_string(item, location=f"{location} item")
        if string in seen:
            raise CandidateIntakeValidationError(f"{location} must not contain duplicate items")
        seen.add(string)
        result.append(string)
    return result


def _copy_json_fact(value: object, *, location: str) -> Any:
    """Validate an approved fact as finite JSON data and return a detached copy."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CandidateIntakeValidationError(f"{location} must contain finite JSON values")
        return value
    if isinstance(value, Mapping):
        copied: dict[str, Any] = {}
        for key, nested_value in value.items():
            if not isinstance(key, str) or not key.strip():
                raise CandidateIntakeValidationError(f"{location} object keys must be non-blank strings")
            copied[key] = _copy_json_fact(nested_value, location=location)
        return copied
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_json_fact(item, location=location) for item in value]
    raise CandidateIntakeValidationError(f"{location} must contain only JSON-compatible values")


def _contains_explicit_fact(value: object) -> bool:
    """Return whether every part of a JSON fact is an explicit value."""

    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value) and all(_contains_explicit_fact(nested) for nested in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value) and all(_contains_explicit_fact(item) for item in value)
    return True


def _validate_documents(value: object) -> list[dict[str, str]]:
    documents = _require_list(value, location="documents")
    validated: list[dict[str, str]] = []
    document_ids: set[str] = set()
    for item in documents:
        document = _require_closed_mapping(
            item,
            allowed=_DOCUMENT_FIELDS,
            required=_DOCUMENT_FIELDS,
            location="document",
        )
        document_id = _require_nonblank_string(document["document_id"], location="document.document_id")
        if document_id in document_ids:
            raise CandidateIntakeValidationError("document.document_id must be unique")
        document_ids.add(document_id)
        path = _require_nonblank_string(document["path"], location="document.path")
        digest = _require_nonblank_string(document["sha256"], location="document.sha256")
        if _SHA256_PATTERN.fullmatch(digest) is None:
            raise CandidateIntakeValidationError("document.sha256 must be a 64-character hexadecimal digest")
        media_type = _require_nonblank_string(document["media_type"], location="document.media_type")
        review_state = _require_nonblank_string(document["review_state"], location="document.review_state")
        if review_state != "untrusted":
            raise CandidateIntakeValidationError("document.review_state must be untrusted during intake")
        validated.append(
            {
                "document_id": document_id,
                "path": path,
                "sha256": digest,
                "media_type": media_type,
                "review_state": review_state,
            }
        )
    return validated


def _validate_approved_facts(value: object) -> dict[str, Any]:
    facts = _require_closed_mapping(
        value,
        allowed=frozenset(value) if isinstance(value, Mapping) else frozenset(),
        required=frozenset(),
        location="approved_facts",
    )
    result: dict[str, Any] = {}
    for field, fact in facts.items():
        if not field.strip():
            raise CandidateIntakeValidationError("approved_facts field names must be non-blank")
        result[field] = _copy_json_fact(fact, location="approved_facts")
    return result


def _validate_contradictions(value: object) -> list[dict[str, Any]]:
    contradictions = _require_list(value, location="contradictions")
    result: list[dict[str, Any]] = []
    seen_fields: set[str] = set()
    for item in contradictions:
        contradiction = _require_closed_mapping(
            item,
            allowed=_CONTRADICTION_FIELDS,
            required=_CONTRADICTION_FIELDS,
            location="contradiction",
        )
        field = _require_nonblank_string(contradiction["field"], location="contradiction.field")
        if field in seen_fields:
            raise CandidateIntakeValidationError("contradiction.field must be unique")
        seen_fields.add(field)
        if contradiction["status"] != "VERIFY":
            raise CandidateIntakeValidationError("contradiction.status must be VERIFY")
        values = _require_list(contradiction["values"], location="contradiction.values")
        if len(values) < 2:
            raise CandidateIntakeValidationError("contradiction.values must contain at least two values")
        copied_values = [_copy_json_fact(item, location="contradiction.values") for item in values]
        evidence_ids = _validate_unique_strings(
            contradiction["evidence_ids"], location="contradiction.evidence_ids"
        )
        result.append(
            {
                "field": field,
                "status": "VERIFY",
                "values": copied_values,
                "evidence_ids": evidence_ids,
            }
        )
    return result


def _validate_pending_facts(value: object) -> list[dict[str, str]]:
    pending_facts = _require_list(value, location="pending_facts")
    result: list[dict[str, str]] = []
    seen_fields: set[str] = set()
    for item in pending_facts:
        pending = _require_closed_mapping(
            item,
            allowed=_PENDING_FACT_FIELDS,
            required=_PENDING_FACT_FIELDS,
            location="pending_fact",
        )
        if pending["status"] != "VERIFY":
            raise CandidateIntakeValidationError("pending_fact.status must be VERIFY")
        field = _require_nonblank_string(pending["field"], location="pending_fact.field")
        if field in seen_fields:
            raise CandidateIntakeValidationError("pending_fact.field must be unique")
        seen_fields.add(field)
        result.append(
            {
                "field": field,
                "status": "VERIFY",
                "question": _require_nonblank_string(pending["question"], location="pending_fact.question"),
                "reason": _require_nonblank_string(pending["reason"], location="pending_fact.reason"),
            }
        )
    return result


def _approved_fact_paths(value: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Return leaf paths while preserving already-dotted approved fact keys."""

    paths: list[str] = []
    for key, nested in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(nested, Mapping) and nested:
            paths.extend(_approved_fact_paths(nested, path))
        else:
            paths.append(path)
    return paths


def _paths_overlap(left: str, right: str) -> bool:
    return left == right or left.startswith(right + ".") or right.startswith(left + ".")


def _ensure_review_state_is_disjoint(
    approved_facts: Mapping[str, Any],
    unknown_fields: Sequence[str],
    contradictions: Sequence[Mapping[str, Any]],
    pending_facts: Sequence[Mapping[str, Any]],
) -> None:
    approved_paths = _approved_fact_paths(approved_facts)
    if len(approved_paths) != len(set(approved_paths)):
        raise CandidateIntakeValidationError(
            "approved_facts must not encode the same path in dotted and nested forms"
        )
    groups = {
        "approved_facts": set(approved_paths),
        "unknown_fields": set(unknown_fields),
        "contradictions": {str(item["field"]) for item in contradictions},
        "pending_facts": {str(item["field"]) for item in pending_facts},
    }
    group_names = tuple(groups)
    for index, group_name in enumerate(group_names):
        for other_name in group_names[index + 1 :]:
            overlap = next(
                (
                    (left, right)
                    for left in groups[group_name]
                    for right in groups[other_name]
                    if _paths_overlap(left, right)
                ),
                None,
            )
            if overlap is not None:
                raise CandidateIntakeValidationError(
                    f"candidate fact review states must be disjoint: {group_name} and "
                    f"{other_name} overlap at {overlap[0]!r}/{overlap[1]!r}"
                )


def _approved_fact(value: Mapping[str, Any], dotted_path: str) -> Any:
    if dotted_path in value:
        return value[dotted_path]
    current: Any = value
    for segment in dotted_path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def _normalized_skill_values(value: object, *, location: str) -> set[str]:
    if value is None:
        return set()
    values = _require_list(value, location=location)
    return {
        " ".join(_require_nonblank_string(item, location=f"{location} item").split()).casefold()
        for item in values
    }


def _ensure_skill_evidence_is_consistent(approved_facts: Mapping[str, Any]) -> None:
    groups = {
        label: _normalized_skill_values(
            _approved_fact(approved_facts, f"skills.{label}"),
            location=f"approved_facts.skills.{label}",
        )
        for label in ("professional", "personal_open_source", "learning_or_exposure")
    }
    group_names = tuple(groups)
    for index, group_name in enumerate(group_names):
        for other_name in group_names[index + 1 :]:
            overlap = groups[group_name] & groups[other_name]
            if overlap:
                raise CandidateIntakeValidationError(
                    "approved skill evidence groups must be disjoint after normalization: "
                    + ", ".join(sorted(overlap))
                )

    evidence = _approved_fact(approved_facts, "skills.evidence_by_skill")
    if evidence is None:
        evidence = _approved_fact(approved_facts, "evidence_by_skill")
    if evidence is None:
        return
    if not isinstance(evidence, Mapping):
        raise CandidateIntakeValidationError("approved_facts.skills.evidence_by_skill must be an object")
    normalized_labels: dict[str, str] = {}
    for raw_skill, raw_label in evidence.items():
        skill = " ".join(
            _require_nonblank_string(raw_skill, location="evidence_by_skill skill").split()
        ).casefold()
        if not isinstance(raw_label, str) or raw_label not in groups:
            raise CandidateIntakeValidationError("evidence_by_skill uses an invalid evidence label")
        previous = normalized_labels.get(skill)
        if previous is not None and previous != raw_label:
            raise CandidateIntakeValidationError(
                f"evidence_by_skill has conflicting normalized labels for {skill}"
            )
        if skill not in groups[raw_label]:
            raise CandidateIntakeValidationError(
                f"evidence_by_skill label conflicts with approved skill group for {skill}"
            )
        normalized_labels[skill] = raw_label


def validate_candidate_intake(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate candidate-provided intake data and return an inactive draft.

    The schema is closed so resume text or an agent-created sensitive default
    cannot be smuggled into a document record.  Approved facts are preserved
    exactly as explicit JSON data; no absent field is populated.
    """

    intake = _require_closed_mapping(
        payload,
        allowed=_TOP_LEVEL_FIELDS,
        required=_TOP_LEVEL_FIELDS,
        location="candidate intake",
    )
    schema_version = intake["schema_version"]
    if isinstance(schema_version, bool) or schema_version != 1:
        raise CandidateIntakeValidationError("schema_version must be 1")
    approved_facts = _validate_approved_facts(intake["approved_facts"])
    unknown_fields = _validate_unique_strings(intake["unknown_fields"], location="unknown_fields")
    contradictions = _validate_contradictions(intake["contradictions"])
    pending_facts = _validate_pending_facts(intake["pending_facts"])
    _ensure_review_state_is_disjoint(
        approved_facts, unknown_fields, contradictions, pending_facts
    )
    _ensure_skill_evidence_is_consistent(approved_facts)
    return {
        "schema_version": 1,
        "documents": _validate_documents(intake["documents"]),
        "approved_facts": approved_facts,
        "unknown_fields": unknown_fields,
        "contradictions": contradictions,
        "pending_facts": pending_facts,
        "state": "draft",
    }


def _validated_draft(profile: Mapping[str, Any]) -> dict[str, Any]:
    draft = _require_closed_mapping(
        profile,
        allowed=_DRAFT_FIELDS,
        required=_DRAFT_FIELDS,
        location="candidate draft",
    )
    if draft["state"] != "draft":
        raise CandidateIntakeValidationError("candidate draft state must be draft")
    source = {field: draft[field] for field in _TOP_LEVEL_FIELDS}
    return validate_candidate_intake(source)


def pending_verification_batch(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Return a safe, detached summary of unresolved candidate verification.

    The stored draft remains available to the validation and activation
    boundaries.  This outward helper deliberately omits candidate values,
    evidence identifiers, user-authored questions/reasons, and document
    metadata.
    """

    draft = _validated_draft(profile)
    return {
        "unknown_fields": [
            _public_review_field(field, group="unknown", index=index)
            for index, field in enumerate(draft["unknown_fields"])
        ],
        "contradictions": [
            {
                "field": _public_review_field(
                    contradiction["field"], group="contradiction", index=index
                ),
                "status": "VERIFY",
            }
            for index, contradiction in enumerate(draft["contradictions"])
        ],
        "pending_facts": [
            {
                "field": _public_review_field(
                    pending["field"], group="pending", index=index
                ),
                "status": "VERIFY",
            }
            for index, pending in enumerate(draft["pending_facts"])
        ],
    }


def _opaque_review_reference(*, group: str, index: int) -> str:
    """Return a content-free identifier for an unallowlisted review item."""

    return f"review-{group}-{index + 1}"


def _public_review_field(field: str, *, group: str, index: int) -> str:
    """Expose only an allowlisted field name or an opaque safe reference."""

    if field in _PUBLIC_INTAKE_FIELDS:
        return field
    return _opaque_review_reference(group=group, index=index)


def _public_review_target(field: str, *, group: str, index: int) -> str:
    """Format a safe field target for a candidate-facing prompt."""

    public_field = _public_review_field(field, group=group, index=index)
    if field in _PUBLIC_INTAKE_FIELDS:
        return f"field {public_field!r}"
    return f"review reference {public_field!r}"


def _safe_completion_question(field: str, *, group: str, index: int) -> str:
    """Build a prompt without copying any candidate-controlled text."""

    target = _public_review_target(field, group=group, index=index)
    if group == "contradiction":
        return (
            f"Resolve a candidate verification conflict for {target}. "
            "Choose the correct candidate-confirmed value and keep one fact only."
        )
    if group == "unknown":
        return (
            f"Provide a candidate-confirmed value for {target}, or leave it unknown. "
            "Do not infer or default a sensitive value."
        )
    return (
        f"Review the pending candidate verification item for {target}. "
        "Provide an explicit candidate-confirmed answer or leave it unresolved."
    )


def completion_questions(profile: Mapping[str, Any]) -> tuple[str, ...]:
    """Build one deterministic, privacy-safe batch of candidate questions."""

    draft = _validated_draft(profile)
    questions: list[str] = []
    questions.extend(
        _safe_completion_question(
            contradiction["field"], group="contradiction", index=index
        )
        for index, contradiction in enumerate(draft["contradictions"])
    )
    questions.extend(
        _safe_completion_question(field, group="unknown", index=index)
        for index, field in enumerate(draft["unknown_fields"])
    )
    questions.extend(
        _safe_completion_question(pending["field"], group="pending", index=index)
        for index, pending in enumerate(draft["pending_facts"])
    )
    if not questions:
        return tuple()
    return tuple(questions)


def _ensure_activation_ready(profile: Mapping[str, Any], *, context: str) -> None:
    """Refuse activation whenever any candidate review state remains unresolved."""

    unresolved_groups = (
        ("unknown_fields", profile["unknown_fields"]),
        ("contradictions", profile["contradictions"]),
        ("pending_facts", profile["pending_facts"]),
    )
    for group_name, items in unresolved_groups:
        if items:
            raise CandidateIntakeValidationError(f"{context}: unresolved {group_name}")
    approved_facts = profile["approved_facts"]
    empty_fields = [
        field
        for field, fact in approved_facts.items()
        if not _contains_explicit_fact(fact)
    ]
    if empty_fields:
        fields = ", ".join(repr(field) for field in empty_fields)
        raise CandidateIntakeValidationError(
            f"{context}: every approved_facts value must be an explicit "
            f"candidate-confirmed fact; semantically empty values at {fields}"
        )
    if not approved_facts:
        raise CandidateIntakeValidationError(
            f"{context}: at least one explicit candidate-confirmed fact is required"
        )


def activate_candidate_profile(profile: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
    """Activate a draft only after an explicitly attributable candidate action."""

    if actor != "user":
        raise PermissionError("candidate profile activation requires a user actor")
    draft = _validated_draft(profile)
    _ensure_activation_ready(draft, context="candidate profile activation refused")
    active = deepcopy(draft)
    active["state"] = "active"
    active["activated_by"] = "user"
    active["confirmed_at"] = datetime.now(timezone.utc).isoformat()
    active["revision_hash"] = _activation_revision(draft)
    return active


def _activation_revision(profile: Mapping[str, Any]) -> str:
    """Hash every approved fact and review input that controls activation.

    The closed document records contain metadata only.  Their paths and
    digests identify what the candidate reviewed without reading or
    persisting document contents.
    """

    revision_input = {
        "schema_version": profile["schema_version"],
        "documents": sorted(
            (
                {
                    "document_id": document["document_id"],
                    "path": document["path"],
                    "sha256": document["sha256"].casefold(),
                    "media_type": document["media_type"],
                    "review_state": document["review_state"],
                }
                for document in profile["documents"]
            ),
            key=lambda document: document["document_id"],
        ),
        "approved_facts": profile["approved_facts"],
        "unknown_fields": sorted(profile["unknown_fields"]),
        "contradictions": profile["contradictions"],
        "pending_facts": profile["pending_facts"],
        "state": "active",
        "activated_by": "user",
    }
    canonical_facts = json.dumps(
        revision_input,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(canonical_facts).hexdigest()


def validate_active_candidate_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one persisted, candidate-confirmed intake revision.

    Callers must use this boundary rather than trusting mutable ``state`` or
    ``activated_by`` fields from a JSON file.  Integrity is verified against
    all activation-relevant approved evidence and review state.
    """

    active = _require_closed_mapping(
        profile,
        allowed=_ACTIVE_FIELDS,
        required=_ACTIVE_FIELDS,
        location="active candidate profile",
    )
    if active["state"] != "active":
        raise CandidateIntakeValidationError("candidate intake must be active")
    if active["activated_by"] != "user":
        raise CandidateIntakeValidationError("candidate intake must be confirmed by the user")

    confirmed_at = _require_nonblank_string(active["confirmed_at"], location="confirmed_at")
    try:
        confirmed = datetime.fromisoformat(confirmed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise CandidateIntakeValidationError(
            "confirmed_at must be a timezone-aware ISO 8601 timestamp"
        ) from error
    if confirmed.tzinfo is None or confirmed.utcoffset() is None:
        raise CandidateIntakeValidationError("confirmed_at must include a timezone")

    source = {field: active[field] for field in _TOP_LEVEL_FIELDS}
    draft = validate_candidate_intake(source)
    _ensure_activation_ready(draft, context="active candidate intake contains")
    revision_hash = _require_nonblank_string(active["revision_hash"], location="revision_hash")
    if _SHA256_PATTERN.fullmatch(revision_hash) is None:
        raise CandidateIntakeValidationError("revision_hash must be a SHA-256 digest")
    expected_revision = _activation_revision(draft)
    if revision_hash.casefold() != expected_revision:
        raise CandidateIntakeValidationError("candidate intake revision_hash does not match approved state")

    validated = deepcopy(draft)
    validated.update(
        {
            "state": "active",
            "activated_by": "user",
            "confirmed_at": confirmed_at,
            "revision_hash": expected_revision,
        }
    )
    return validated
