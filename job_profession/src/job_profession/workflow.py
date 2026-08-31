"""Local-only preparation state machine; it cannot operate application forms."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Mapping, Sequence


ReviewState = Literal["discovered", "needs_review", "ready_to_apply", "submitted"]

REQUIRED_REVIEW_ITEMS = (
    "claims",
    "company_name",
    "job_title",
    "salary_or_visa",
    "current_location",
    "screening_responses",
)
_APPROVED_TEXT_FIELDS = frozenset({"cover letter", "hiring manager message", "professional summary"})
_STOP_TERMS = (
    "work authorization",
    "authorized to work",
    "sponsorship",
    "visa",
    "salary",
    "compensation",
    "notice period",
    "availability",
    "criminal",
    "disability",
    "gender",
    "race",
    "veteran",
    "eeo",
    "screening",
    "captcha",
    "attest",
    "certif",
    "consent",
    "agree to",
)


@dataclass(frozen=True, slots=True)
class PreparedApplication:
    """Local suggestions and candidate prompts only; intentionally no submit API."""

    job_id: str
    field_suggestions: Mapping[str, str] = field(default_factory=dict)
    requires_user_answer: tuple[str, ...] = ()
    stopped: bool = False
    review_state: ReviewState = "needs_review"


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    """An auditable local record, not a browser-application controller."""

    job_id: str
    status: ReviewState = "discovered"
    review_checklist: Mapping[str, bool] = field(default_factory=dict)
    requires_user_answer: tuple[str, ...] = ()
    submitted_by: str | None = None


def _normalise_label(label: str) -> str:
    return " ".join(label.casefold().split())


def _requires_human_answer(label: str) -> bool:
    normalised = _normalise_label(label)
    return any(term in normalised for term in _STOP_TERMS)


def prepare_application(
    job_id: str,
    *,
    form_labels: Sequence[str] = (),
    approved_field_values: Mapping[str, str] | None = None,
) -> PreparedApplication:
    """Prepare local suggestions from supplied labels without accessing a browser.

    ``form_labels`` are inert strings (for example, from a local test fixture or
    a candidate's notes).  This function does not navigate, read a page, fill a
    field, upload files, or transmit information.  Every unapproved or sensitive
    label becomes a candidate-owned stop.
    """

    values = approved_field_values or {}
    suggestions: dict[str, str] = {}
    prompts: list[str] = []
    for label in form_labels:
        clean_label = str(label).strip()
        if not clean_label:
            continue
        normalised = _normalise_label(clean_label)
        if _requires_human_answer(clean_label):
            prompts.append(f"Candidate must answer manually: {clean_label}")
            continue
        value = values.get(clean_label)
        if normalised in _APPROVED_TEXT_FIELDS and isinstance(value, str) and value.strip():
            suggestions[clean_label] = value.strip()
        else:
            prompts.append(f"No preapproved value is available; candidate review required: {clean_label}")
    return PreparedApplication(
        job_id=job_id,
        field_suggestions=suggestions,
        requires_user_answer=tuple(prompts),
        stopped=bool(prompts),
    )


def transition_application(
    application: ApplicationRecord,
    target: ReviewState,
    *,
    actor: str,
) -> ApplicationRecord:
    """Move a local record through review gates; this does not submit anything.

    ``submitted`` means the user reports an external, manual submission for
    tracking.  No transition opens a site, invokes a form control, or accepts an
    attestation.
    """

    if actor != "user":
        raise PermissionError("Only the candidate may move an application through review or record a manual submission.")
    if target == "ready_to_apply":
        if application.status not in {"discovered", "needs_review"}:
            raise ValueError(f"Cannot move {application.status} to ready_to_apply")
        complete = all(application.review_checklist.get(item, False) for item in REQUIRED_REVIEW_ITEMS)
        if not complete or application.requires_user_answer:
            return replace(application, status="needs_review")
        return replace(application, status="ready_to_apply")
    if target == "needs_review":
        return replace(application, status="needs_review")
    if target == "submitted":
        if application.status != "ready_to_apply":
            raise ValueError("Only a reviewed, ready-to-apply record can record a manual submission.")
        return replace(application, status="submitted", submitted_by="user")
    if target == "discovered":
        raise ValueError("Application records cannot be moved back to discovered.")
    raise ValueError(f"Unknown review state: {target}")
