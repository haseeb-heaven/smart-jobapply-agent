"""Contract checks for the candidate-facing, upload-first onboarding docs."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parents[1]

# Keep the documentation boundary explicit: these are tracked public docs, not
# candidate-private manifests or runtime artifacts.
ONBOARDING_DOCS = (
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "skills" / "job-copilot" / "SKILL.md",
    PROJECT_ROOT / "skills" / "job-copilot" / "references" / "candidate-intake.md",
)
CANDIDATE_INTAKE_DOC = ONBOARDING_DOCS[-1]


def _onboarding_docs_text() -> str:
    return re.sub(
        r"\s+",
        " ",
        "\n".join(path.read_text(encoding="utf-8") for path in ONBOARDING_DOCS),
    ).lower()


def test_onboarding_docs_offer_upload_or_attachment_instead_of_answering():
    text = _onboarding_docs_text()

    assert re.search(
        r"\b(?:upload|attach\w*)\b[^.!?]{0,160}"
        r"\b(?:instead of|rather than|as an alternative to|alternative to)\b"
        r"[^.!?]{0,160}\banswer\w*\b",
        text,
    ), "onboarding must offer document upload or attachment as an alternative to answering"


@pytest.mark.parametrize("file_format", ("pdf", "docx", "txt"))
def test_onboarding_docs_name_common_document_formats(file_format: str):
    text = _onboarding_docs_text()

    assert re.search(rf"\b{file_format}\b", text), f"onboarding must name the {file_format} format"


def test_onboarding_docs_extract_a_draft_before_questioning_unresolved_items():
    text = CANDIDATE_INTAKE_DOC.read_text(encoding="utf-8").lower()
    draft = re.search(r"\b(?:extract|create|load)\b[^.!?]{0,100}\bdraft\b", text)
    questions = re.search(
        r"\bask\b[^.!?]{0,220}\b(?:unresolved|ambiguous|unknown|contradiction|pending)\w*\b",
        text,
    )

    assert draft and questions, "onboarding must extract a draft and ask about unresolved items"
    assert draft.start() < questions.start(), "onboarding must ask unresolved items after extracting a draft"


def test_onboarding_docs_require_final_explicit_candidate_confirmation():
    text = _onboarding_docs_text()

    assert re.search(r"\b(?:ask|request)\b[^.!?]{0,80}\bfinal confirmation\b", text)
    assert re.search(r"\bexplicit candidate confirmation\b|\bcandidate[- ]confirmed\b", text)


@pytest.mark.parametrize("document_path", ONBOARDING_DOCS, ids=lambda path: path.name)
def test_each_operational_onboarding_doc_requires_safe_structured_fact_review(
    document_path: Path,
):
    text = re.sub(r"\s+", " ", document_path.read_text(encoding="utf-8")).lower()

    assert re.search(r"\b(?:privacy[- ]safe|safe)\b", text)
    assert re.search(r"\bstructured\b", text)
    assert re.search(r"\bcandidate[- ](?:approved|confirmed)\b|\bcandidate confirms\b", text)
    assert re.search(r"\b(?:review|confirm)\w*\b", text)
    assert re.search(r"\b(?:raw|resume text|document contents?)\b", text)


@pytest.mark.parametrize("document_path", ONBOARDING_DOCS, ids=lambda path: path.name)
def test_each_operational_onboarding_doc_limits_uploads_to_candidate_host(
    document_path: Path,
):
    text = re.sub(r"\s+", " ", document_path.read_text(encoding="utf-8")).lower()

    assert re.search(r"\bcandidate-to-host upload\b", text)
    assert re.search(
        r"\bagent-to-job-board upload\b[^.!?]{0,180}"
        r"\b(?:prohibited|forbidden|not permitted|must not|cannot)\b|"
        r"\b(?:prohibited|forbidden|not permitted|must not|cannot)\b[^.!?]{0,180}"
        r"\bagent-to-job-board upload\b",
        text,
    )


def test_onboarding_docs_prohibit_uploading_documents_to_job_boards_and_submitting_applications():
    text = _onboarding_docs_text()

    assert re.search(
        r"\b(?:never|must not|cannot|prohibited|prohibition)\b[^.!?]{0,180}"
        r"\b(?:upload|attach\w*)\b[^.!?]{0,120}"
        r"\b(?:document|resume|file|attachment)\w*\b[^.!?]{0,120}"
        r"\bjob[- ]?boards?\b",
        text,
    ), "onboarding must prohibit document uploads to job boards"
    assert re.search(
        r"\b(?:never|must not|cannot|prohibited|prohibition|does not|neither)\b[^.!?]{0,180}"
        r"\b(?:submit|submission)\w*\b",
        text,
    ), "onboarding must prohibit submitting applications"
    assert re.search(r"\bsubmit\w*\s+applications?\b", text)
