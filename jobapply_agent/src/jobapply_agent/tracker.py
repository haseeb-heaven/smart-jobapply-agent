"""Local, auditable job-tracker storage and review-export helpers.

The tracker deliberately records job sightings and status changes separately from
the current job view. A later discovery can refresh a listing without erasing
the data that was previously reviewed, and no function in this module can
submit an application.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Literal

from .models import JobListing, MatchResult
from .normalize import listing_fingerprint, normalize_listing


ReviewState = Literal["discovered", "reviewed", "ready_to_apply", "submitted", "rejected"]
_VALID_STATES = frozenset({"discovered", "reviewed", "ready_to_apply", "submitted", "rejected"})
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "discovered": frozenset({"reviewed", "rejected"}),
    "reviewed": frozenset({"ready_to_apply", "rejected"}),
    "ready_to_apply": frozenset({"reviewed", "submitted", "rejected"}),
    "submitted": frozenset(),
    "rejected": frozenset({"reviewed"}),
}


class InvalidTransition(ValueError):
    """Raised when a human review-state transition is not allowed."""


class WorkbookDependencyError(RuntimeError):
    """Raised instead of producing a misleading file with an ``.xlsx`` suffix."""


@dataclass(frozen=True, slots=True)
class ApplicationRecord:
    """The review state for one deduplicated job listing."""

    job_id: str
    status: ReviewState
    submitted_at: str | None = None
    submitted_by: str | None = None
    follow_up_date: str | None = None
    response_status: str | None = None
    is_new: bool = False


@dataclass(frozen=True, slots=True)
class StatusHistory:
    """One immutable, attributable state-change event."""

    job_id: str
    previous_status: ReviewState
    target_status: ReviewState
    actor: str
    changed_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _description_hash(description: str) -> str:
    return sha256(description.encode("utf-8")).hexdigest()


def _as_json(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _require_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise InvalidTransition("a non-empty actor is required for an auditable status transition")
    return actor


def _is_user_actor(actor: str) -> bool:
    """Keep the manual-submission gate explicit and machine-checkable.

    This local tracker has no identity provider. It therefore accepts the
    deliberate audit label ``user`` only; automation may record discoveries but
    cannot represent a manual final submission.
    """

    return actor == "user"


def transition_application(application: ApplicationRecord, target: ReviewState, actor: str) -> ApplicationRecord:
    """Validate a pure review-state transition without performing any action.

    ``submitted`` and ``rejected`` only record an explicit user-reported
    outcome. They do not visit a source URL, fill a form, or click a control.
    """

    actor = _require_actor(actor)
    if target not in _VALID_STATES:
        raise InvalidTransition(f"unknown review state: {target!r}")
    if target not in _ALLOWED_TRANSITIONS[application.status]:
        raise InvalidTransition(f"cannot move {application.status!r} to {target!r}")
    if target in {"submitted", "rejected"} and not _is_user_actor(actor):
        raise InvalidTransition("submitted and rejected statuses require the exact user actor")
    if target == "submitted":
        return replace(application, status=target, submitted_at=_utc_now(), submitted_by=actor)
    return replace(application, status=target)


class Tracker:
    """SQLite-backed current tracker view plus immutable observation history."""

    def __init__(self, database_path: Path | str):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    source_job_id TEXT,
                    platform TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    work_mode TEXT NOT NULL,
                    description_snapshot_hash TEXT NOT NULL,
                    description_snapshot TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    score_explanation TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    gaps_json TEXT NOT NULL,
                    evidence_explanations_json TEXT NOT NULL,
                    discovered_at TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    resume_selected TEXT,
                    cover_letter_status TEXT,
                    questions_needing_answer TEXT,
                    review_state TEXT,
                    submission_owner TEXT,
                    deadline TEXT,
                    submitted_at TEXT,
                    submitted_by TEXT,
                    follow_up_date TEXT,
                    response_status TEXT,
                    CHECK (status IN ('discovered', 'reviewed', 'ready_to_apply', 'submitted', 'rejected'))
                );
                CREATE TABLE IF NOT EXISTS job_observations (
                    observation_id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    observed_at TEXT NOT NULL,
                    source_job_id TEXT,
                    platform TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT NOT NULL,
                    work_mode TEXT NOT NULL,
                    description_snapshot_hash TEXT NOT NULL,
                    description_snapshot TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    score_explanation TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    gaps_json TEXT NOT NULL,
                    evidence_explanations_json TEXT NOT NULL,
                    discovered_at TEXT
                );
                CREATE TABLE IF NOT EXISTS status_history (
                    history_id INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    previous_status TEXT NOT NULL,
                    target_status TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_observations_job_id ON job_observations(job_id);
                CREATE INDEX IF NOT EXISTS idx_status_history_job_id ON status_history(job_id);
                """
            )

    def record_listing(self, job: JobListing, match: MatchResult) -> ApplicationRecord:
        """Store one visible listing sighting and return its deduplicated record.

        A duplicate does not create another job row. It always adds a new
        observation, retaining the seen URL, score explanation, gaps, and fit
        evidence that informed the current record.
        """

        normalized = normalize_listing(job)
        job_id = listing_fingerprint(normalized.platform, normalized.url, normalized.title, normalized.company)
        observed_at = _utc_now()
        description_hash = _description_hash(normalized.description)
        payload = {
            "source_job_id": normalized.source_job_id,
            "platform": normalized.platform,
            "source_url": normalized.url,
            "title": normalized.title,
            "company": normalized.company,
            "location": normalized.location,
            "work_mode": normalized.work_mode,
            "description_snapshot_hash": description_hash,
            "description_snapshot": normalized.description,
            "score": match.score,
            "decision": match.decision,
            "score_explanation": match.score_explanation,
            "reasons_json": _as_json(match.reasons),
            "gaps_json": _as_json(match.gaps),
            "evidence_explanations_json": _as_json(match.evidence_explanations),
            "discovered_at": normalized.discovered_at or observed_at,
        }
        with self._connect() as connection:
            existing = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, source_job_id, platform, source_url, title, company, location, work_mode,
                        description_snapshot_hash, description_snapshot, score, decision, score_explanation,
                        reasons_json, gaps_json, evidence_explanations_json, discovered_at, first_seen, last_seen
                    ) VALUES (
                        :job_id, :source_job_id, :platform, :source_url, :title, :company, :location, :work_mode,
                        :description_snapshot_hash, :description_snapshot, :score, :decision, :score_explanation,
                        :reasons_json, :gaps_json, :evidence_explanations_json, :discovered_at, :first_seen, :last_seen
                    )
                    """,
                    {**payload, "job_id": job_id, "first_seen": observed_at, "last_seen": observed_at},
                )
                row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                is_new = True
            else:
                connection.execute(
                    """
                    UPDATE jobs SET
                        source_job_id = :source_job_id, platform = :platform, source_url = :source_url,
                        title = :title, company = :company, location = :location, work_mode = :work_mode,
                        description_snapshot_hash = :description_snapshot_hash,
                        description_snapshot = :description_snapshot, score = :score, decision = :decision,
                        score_explanation = :score_explanation, reasons_json = :reasons_json, gaps_json = :gaps_json,
                        evidence_explanations_json = :evidence_explanations_json, last_seen = :last_seen
                    WHERE job_id = :job_id
                    """,
                    {**payload, "job_id": job_id, "last_seen": observed_at},
                )
                row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                is_new = False
            connection.execute(
                """
                INSERT INTO job_observations (
                    job_id, observed_at, source_job_id, platform, source_url, title, company, location, work_mode,
                    description_snapshot_hash, description_snapshot, score, decision, score_explanation,
                    reasons_json, gaps_json, evidence_explanations_json, discovered_at
                ) VALUES (
                    :job_id, :observed_at, :source_job_id, :platform, :source_url, :title, :company, :location,
                    :work_mode, :description_snapshot_hash, :description_snapshot, :score, :decision,
                    :score_explanation, :reasons_json, :gaps_json, :evidence_explanations_json, :discovered_at
                )
                """,
                {**payload, "job_id": job_id, "observed_at": observed_at},
            )
        return self._application_from_row(row, is_new=is_new)

    def _application_from_row(self, row: sqlite3.Row, *, is_new: bool = False) -> ApplicationRecord:
        return ApplicationRecord(
            job_id=row["job_id"],
            status=row["status"],
            submitted_at=row["submitted_at"],
            submitted_by=row["submitted_by"],
            follow_up_date=row["follow_up_date"],
            response_status=row["response_status"],
            is_new=is_new,
        )

    def get_application(self, job_id: str) -> ApplicationRecord:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown job id: {job_id}")
        return self._application_from_row(row)

    def list_jobs(self) -> list[ApplicationRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY score DESC, first_seen DESC").fetchall()
        return [self._application_from_row(row) for row in rows]

    def observation_count(self, job_id: str) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM job_observations WHERE job_id = ?", (job_id,)).fetchone()[0])

    def history_for(self, job_id: str) -> list[StatusHistory]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM status_history WHERE job_id = ? ORDER BY history_id", (job_id,)
            ).fetchall()
        return [
            StatusHistory(
                job_id=row["job_id"],
                previous_status=row["previous_status"],
                target_status=row["target_status"],
                actor=row["actor"],
                changed_at=row["changed_at"],
            )
            for row in rows
        ]

    def transition_application(self, job_id: str, target: ReviewState, actor: str) -> ApplicationRecord:
        """Record a human review transition; this method never applies to a job."""

        current = self.get_application(job_id)
        updated = transition_application(current, target, actor)
        changed_at = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs SET status = ?, submitted_at = ?, submitted_by = ? WHERE job_id = ?
                """,
                (updated.status, updated.submitted_at, updated.submitted_by, job_id),
            )
            connection.execute(
                """
                INSERT INTO status_history (job_id, previous_status, target_status, actor, changed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, current.status, updated.status, actor.strip(), changed_at),
            )
        return updated

    def export_rows(self) -> list[dict[str, str | int | None]]:
        """Return the flat, CSV-compatible all-history review view."""

        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY score DESC, first_seen DESC").fetchall()
        return [
            {
                "job_id": row["job_id"],
                "source_job_id": row["source_job_id"],
                "source_url": row["source_url"],
                "platform": row["platform"],
                "title": row["title"],
                "company": row["company"],
                "location": row["location"],
                "work_mode": row["work_mode"],
                "description_snapshot_hash": row["description_snapshot_hash"],
                "status": row["status"],
                "review_state": row["review_state"],
                "score": row["score"],
                "decision": row["decision"],
                "score_explanation": row["score_explanation"],
                "why_matched": "; ".join(json.loads(row["reasons_json"])),
                "gaps": "; ".join(json.loads(row["gaps_json"])),
                "evidence_explanations": "; ".join(json.loads(row["evidence_explanations_json"])),
                "discovered_at": row["discovered_at"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "resume_selected": row["resume_selected"],
                "cover_letter_status": row["cover_letter_status"],
                "questions_needing_answer": row["questions_needing_answer"],
                "submission_owner": row["submission_owner"],
                "deadline": row["deadline"],
                "submitted_at": row["submitted_at"],
                "submitted_by": row["submitted_by"],
                "follow_up_date": row["follow_up_date"],
                "response_status": row["response_status"],
                "fit_score_is_not_offer_probability": "true",
            }
            for row in rows
        ]


CSV_COLUMNS = (
    "job_id",
    "source_job_id",
    "source_url",
    "platform",
    "title",
    "company",
    "location",
    "work_mode",
    "description_snapshot_hash",
    "status",
    "review_state",
    "score",
    "decision",
    "score_explanation",
    "why_matched",
    "gaps",
    "evidence_explanations",
    "discovered_at",
    "first_seen",
    "last_seen",
    "resume_selected",
    "cover_letter_status",
    "questions_needing_answer",
    "submission_owner",
    "deadline",
    "submitted_at",
    "submitted_by",
    "follow_up_date",
    "response_status",
    "fit_score_is_not_offer_probability",
)


def _safe_csv_value(value: str | int | None) -> str | int:
    """Prevent visible-listing text from becoming a spreadsheet formula on import."""

    if value is None:
        return ""
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def export_tracker(database_path: Path, output_path: Path) -> Path:
    """Export the complete deduplicated review view to a safe CSV file.

    The requested six-sheet, formula-driven `.xlsx` workbook requires the
    approved workbook authoring dependency. It is not available in this runtime,
    so `.xlsx` is deliberately blocked instead of writing CSV bytes under an
    Excel suffix. Open the CSV in Google Sheets or Excel, then save it as XLSX
    manually until that dependency is supplied.
    """

    output_path = Path(output_path)
    if output_path.suffix.casefold() == ".xlsx":
        raise WorkbookDependencyError(
            "XLSX export requires the approved @oai/artifact-tool workbook dependency, which is unavailable. "
            "Export to a .csv path, open it in Google Sheets or Excel, and save as .xlsx manually."
        )
    if output_path.suffix.casefold() != ".csv":
        raise ValueError("tracker exports must use a .csv path until XLSX workbook tooling is available")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = Tracker(database_path).export_rows()
    with output_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, extrasaction="raise")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _safe_csv_value(row[column]) for column in CSV_COLUMNS})
    return output_path
