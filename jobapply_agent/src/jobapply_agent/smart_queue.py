"""Persistent, candidate-controlled five-listing review queue.

The queue is deliberately a data-only coordination primitive.  It has no
browser, network, form, upload, login, or application-submission authority.
Callers supply visible listing URLs and perform any permitted tab operation
through a separately bounded adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Iterable, Literal
import uuid
from .sources import canonical_listing_url


QueueState = Literal[
    "recommended",
    "waiting",
    "open",
    "open_failed",
    "awaiting_outcome",
    "submitted",
    "rejected",
    "skipped",
]

_TARGET_SIZE = 5
_DECISIONS = frozenset({"recommended", "review", "reject"})
_OUTCOMES = frozenset({"submitted", "rejected", "skipped"})
# A missing tab still reserves its physical slot until the user confirms an outcome.
_ACTIVE_STATES = frozenset({"waiting", "open", "awaiting_outcome"})
_MAX_IDENTIFIER_LENGTH = 256
_MAX_URL_LENGTH = 4096
_MAX_EVIDENCE_ITEMS = 100
_MAX_EVIDENCE_LENGTH = 4096


class QueuePolicyError(ValueError):
    """Raised when input or a transition violates the queue contract."""


class QueueStorageError(RuntimeError):
    """Raised for a redacted local persistence failure."""


@dataclass(frozen=True, slots=True)
class QueueCandidate:
    """One versioned, already-evaluated job offered to the deterministic queue."""

    job_id: str
    source_url: str
    fit_score: int
    eligible: bool
    decision: str
    evidence: tuple[str, ...]
    profile_revision: str
    matcher_policy_revision: str

    def __post_init__(self) -> None:
        job_id = _require_identifier(self.job_id, "job_id")
        source_url = _require_listing_url(self.source_url)
        if isinstance(self.fit_score, bool) or not isinstance(self.fit_score, int):
            raise QueuePolicyError("fit_score must be an integer")
        if not 0 <= self.fit_score <= 100:
            raise QueuePolicyError("fit_score must be between 0 and 100")
        if type(self.eligible) is not bool:
            raise QueuePolicyError("eligible must be a boolean")
        if not isinstance(self.decision, str) or self.decision not in _DECISIONS:
            raise QueuePolicyError("decision must be recommended, review, or reject")
        evidence = _require_evidence(self.evidence)
        profile_revision, matcher_policy_revision = _require_revision_pair(
            self.profile_revision,
            self.matcher_policy_revision,
            allow_unversioned=False,
        )
        object.__setattr__(self, "job_id", job_id)
        object.__setattr__(self, "source_url", source_url)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "profile_revision", profile_revision)
        object.__setattr__(self, "matcher_policy_revision", matcher_policy_revision)


@dataclass(frozen=True, slots=True)
class QueueJob:
    """Current derived queue view of one immutable recommendation."""

    job_id: str
    source_url: str
    fit_score: int
    eligible: bool
    decision: str
    evidence: tuple[str, ...]
    state: QueueState
    # Nullable only because rows created before revision tracking are migrated
    # without rewriting their immutable recommendation data.
    profile_revision: str | None = None
    matcher_policy_revision: str | None = None


@dataclass(frozen=True, slots=True)
class QueueEvent:
    """One immutable recommendation, tab, planning, or outcome event."""

    event_id: int
    job_id: str
    name: str
    actor: str
    occurred_at: str
    # Event IDs are local to one queue database.  The persisted queue ID is
    # required alongside them whenever an event crosses that database boundary.
    queue_id: str


@dataclass(frozen=True, slots=True)
class QueueAction:
    """A data-only refill plan for a caller-controlled browser adapter."""

    job_ids: tuple[str, ...]
    urls_to_open: tuple[str, ...]
    search_needed: int


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise QueuePolicyError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_IDENTIFIER_LENGTH or any(ord(character) < 32 for character in cleaned):
        raise QueuePolicyError(f"{label} must be a non-empty safe identifier")
    return cleaned


def _require_actor(value: object) -> str:
    return _require_identifier(value, "actor")


def _require_revision(value: object, label: str) -> str:
    return _require_identifier(value, label)


def _require_revision_pair(
    profile_revision: object,
    matcher_policy_revision: object,
    *,
    allow_unversioned: bool,
) -> tuple[str | None, str | None]:
    if profile_revision is None and matcher_policy_revision is None:
        if allow_unversioned:
            return None, None
        raise QueuePolicyError("profile_revision and matcher_policy_revision are required")
    if profile_revision is None or matcher_policy_revision is None:
        raise QueuePolicyError("profile_revision and matcher_policy_revision must be supplied together")
    return (
        _require_revision(profile_revision, "profile_revision"),
        _require_revision(matcher_policy_revision, "matcher_policy_revision"),
    )


def _require_evidence(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise QueuePolicyError("evidence must be a tuple of strings")
    if not value or len(value) > _MAX_EVIDENCE_ITEMS:
        raise QueuePolicyError("evidence must contain between 1 and 100 items")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise QueuePolicyError("evidence items must be strings")
        cleaned = item.strip()
        if not cleaned or len(cleaned) > _MAX_EVIDENCE_LENGTH:
            raise QueuePolicyError("evidence items must be non-empty and bounded")
        normalized.append(cleaned)
    return tuple(normalized)


def _require_listing_url(value: object) -> str:
    if not isinstance(value, str):
        raise QueuePolicyError("source_url must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > _MAX_URL_LENGTH:
        raise QueuePolicyError("source_url must be a bounded HTTPS listing URL")
    try:
        canonical = canonical_listing_url(cleaned)
    except ValueError:
        raise QueuePolicyError(
            "source_url must be a credential-free canonical LinkedIn or Indeed HTTPS listing URL"
        ) from None
    if len(canonical) > _MAX_URL_LENGTH:
        raise QueuePolicyError("source_url must be a bounded HTTPS listing URL")
    return canonical


class SmartJobQueue:
    """SQLite-backed queue whose fixed physical review capacity is five.

    Recommendation facts never change after insertion and queue history is
    append-only.  Logical state is derived from that history.  Planning holds
    an immediate SQLite transaction, so concurrent planners cannot reserve the
    same vacancy or grow the reserved queue beyond its fixed capacity.
    """

    def __init__(self, database_path: Path | str, *, target_size: int = _TARGET_SIZE):
        if isinstance(target_size, bool) or not isinstance(target_size, int) or target_size != _TARGET_SIZE:
            raise QueuePolicyError("the smart review queue is fixed at five jobs")
        if not isinstance(database_path, (str, Path)):
            raise QueuePolicyError("database_path must be a path")
        raw_path = str(database_path)
        if not raw_path.strip():
            raise QueuePolicyError("database_path must be non-empty")
        self.database_path = Path(raw_path)
        self.target_size = _TARGET_SIZE
        self._queue_id: str | None = None
        self._memory_connection: sqlite3.Connection | None = None
        try:
            if raw_path == ":memory:":
                self._memory_connection = self._new_connection(raw_path)
            else:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, QueueStorageError, sqlite3.Error):
            if self._memory_connection is not None:
                self._memory_connection.close()
                self._memory_connection = None
            raise QueueStorageError("smart queue storage initialization failed") from None

    @staticmethod
    def _new_connection(database: str | Path) -> sqlite3.Connection:
        connection = sqlite3.connect(database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._memory_connection or self._new_connection(self.database_path)

    def _close(self, connection: sqlite3.Connection) -> None:
        if connection is not self._memory_connection:
            connection.close()

    @property
    def queue_id(self) -> str:
        """Return the durable namespace for this queue database."""

        if self._queue_id is None:
            raise QueueStorageError("smart queue identity is unavailable")
        return self._queue_id

    @property
    def active_revisions(self) -> tuple[str | None, str | None]:
        """Return the durable profile and matcher-policy revision pair."""

        connection = self._connect()
        try:
            return self._read_active_revisions(connection)
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    @staticmethod
    def _new_queue_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _require_stored_queue_id(value: object) -> str:
        if not isinstance(value, str):
            raise QueueStorageError("smart queue identity is invalid")
        try:
            return uuid.UUID(value).hex
        except (AttributeError, ValueError):
            raise QueueStorageError("smart queue identity is invalid") from None

    @staticmethod
    def _read_active_revisions(connection: sqlite3.Connection) -> tuple[str | None, str | None]:
        row = connection.execute(
            """
            SELECT active_profile_revision, active_matcher_policy_revision
            FROM smart_queue_metadata WHERE metadata_id = 1
            """
        ).fetchone()
        if row is None:
            raise QueueStorageError("smart queue active revisions are unavailable")
        try:
            return _require_revision_pair(
                row["active_profile_revision"],
                row["active_matcher_policy_revision"],
                allow_unversioned=True,
            )
        except QueuePolicyError:
            raise QueueStorageError("smart queue active revisions are invalid") from None

    @staticmethod
    def _write_active_revisions(
        connection: sqlite3.Connection, revisions: tuple[str, str]
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE smart_queue_metadata
            SET active_profile_revision = ?, active_matcher_policy_revision = ?
            WHERE metadata_id = 1
            """,
            revisions,
        )
        if cursor.rowcount != 1:
            raise QueueStorageError("smart queue active revisions could not be persisted")

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS smart_queue_jobs (
                    job_id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL UNIQUE,
                    fit_score INTEGER NOT NULL CHECK(fit_score BETWEEN 0 AND 100),
                    eligible INTEGER NOT NULL CHECK(eligible IN (0, 1)),
                    decision TEXT NOT NULL CHECK(decision IN ('recommended', 'review', 'reject')),
                    evidence_json TEXT NOT NULL,
                    profile_revision TEXT,
                    matcher_policy_revision TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS smart_queue_metadata (
                    metadata_id INTEGER PRIMARY KEY CHECK(metadata_id = 1),
                    queue_id TEXT NOT NULL UNIQUE CHECK(length(queue_id) = 32),
                    active_profile_revision TEXT,
                    active_matcher_policy_revision TEXT
                );

                CREATE TABLE IF NOT EXISTS smart_queue_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES smart_queue_jobs(job_id),
                    name TEXT NOT NULL CHECK(name IN (
                        'recommended', 'waiting', 'open', 'missing',
                        'open_failed', 'submitted', 'rejected', 'skipped'
                    )),
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_smart_queue_events_job
                    ON smart_queue_events(job_id, event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_smart_queue_one_outcome
                    ON smart_queue_events(job_id)
                    WHERE name IN ('submitted', 'rejected', 'skipped');

                CREATE TRIGGER IF NOT EXISTS smart_queue_jobs_no_update
                BEFORE UPDATE ON smart_queue_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue recommendations are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS smart_queue_jobs_no_delete
                BEFORE DELETE ON smart_queue_jobs
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue recommendations are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS smart_queue_events_no_update
                BEFORE UPDATE ON smart_queue_events
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS smart_queue_events_no_delete
                BEFORE DELETE ON smart_queue_events
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue events are append-only');
                END;
                """
            )
            self._ensure_column(connection, "smart_queue_jobs", "profile_revision", "TEXT")
            self._ensure_column(connection, "smart_queue_jobs", "matcher_policy_revision", "TEXT")
            self._ensure_column(connection, "smart_queue_metadata", "active_profile_revision", "TEXT")
            self._ensure_column(connection, "smart_queue_metadata", "active_matcher_policy_revision", "TEXT")
            connection.execute(
                """
                INSERT OR IGNORE INTO smart_queue_metadata (metadata_id, queue_id)
                VALUES (1, ?)
                """,
                (self._new_queue_id(),),
            )
            metadata = connection.execute(
                "SELECT queue_id FROM smart_queue_metadata WHERE metadata_id = 1"
            ).fetchone()
            if metadata is None:
                raise QueueStorageError("smart queue identity could not be persisted")
            self._queue_id = self._require_stored_queue_id(metadata["queue_id"])
            connection.commit()
        except QueueStorageError:
            connection.rollback()
            raise
        except (OSError, sqlite3.Error):
            connection.rollback()
            raise QueueStorageError("smart queue storage initialization failed") from None
        finally:
            self._close(connection)

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _transaction(self) -> sqlite3.Connection:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error:
            self._close(connection)
            raise QueueStorageError("smart queue storage operation failed") from None
        return connection

    @staticmethod
    def _append_event(connection: sqlite3.Connection, job_id: str, name: str, actor: str) -> None:
        connection.execute(
            """
            INSERT INTO smart_queue_events (job_id, name, actor, occurred_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, name, actor, _utc_now()),
        )

    def set_active_revisions(
        self,
        profile_revision: str,
        matcher_policy_revision: str,
        *,
        actor: str = "host",
    ) -> tuple[str, str]:
        """Explicitly select the revision pair eligible for future refills.

        This changes queue metadata only. It never changes recommendation rows,
        queue history, or candidate-confirmed outcomes.
        """

        revisions = _require_revision_pair(
            profile_revision,
            matcher_policy_revision,
            allow_unversioned=False,
        )
        _require_actor(actor)
        assert revisions[0] is not None and revisions[1] is not None
        active_pair = (revisions[0], revisions[1])
        connection = self._transaction()
        try:
            self._write_active_revisions(connection, active_pair)
            connection.commit()
        except QueueStorageError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)
        return active_pair

    @staticmethod
    def _rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT
                jobs.job_id,
                jobs.source_url,
                jobs.fit_score,
                jobs.eligible,
                jobs.decision,
                jobs.evidence_json,
                jobs.profile_revision,
                jobs.matcher_policy_revision,
                jobs.rowid AS insertion_order,
                events.name AS latest_event,
                EXISTS(
                    SELECT 1 FROM smart_queue_events AS outcome
                    WHERE outcome.job_id = jobs.job_id
                      AND outcome.name IN ('submitted', 'rejected', 'skipped')
                ) AS has_outcome,
                (
                    SELECT outcome.name FROM smart_queue_events AS outcome
                    WHERE outcome.job_id = jobs.job_id
                      AND outcome.name IN ('submitted', 'rejected', 'skipped')
                    ORDER BY outcome.event_id DESC LIMIT 1
                ) AS outcome,
                (
                    SELECT visibility.name FROM smart_queue_events AS visibility
                    WHERE visibility.job_id = jobs.job_id
                      AND visibility.name IN ('open', 'missing')
                    ORDER BY visibility.event_id DESC LIMIT 1
                ) AS latest_visibility
            FROM smart_queue_jobs AS jobs
            JOIN smart_queue_events AS events ON events.event_id = (
                SELECT MAX(latest.event_id) FROM smart_queue_events AS latest
                WHERE latest.job_id = jobs.job_id
            )
            ORDER BY jobs.rowid
            """
        ).fetchall()

    @staticmethod
    def _state(row: sqlite3.Row) -> QueueState:
        if row["has_outcome"]:
            return str(row["outcome"])  # type: ignore[return-value]
        latest = str(row["latest_event"])
        if latest == "missing":
            return "awaiting_outcome"
        return latest  # type: ignore[return-value]

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> QueueJob:
        try:
            evidence = tuple(json.loads(row["evidence_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise QueueStorageError("smart queue stored data is invalid") from None
        profile_revision, matcher_policy_revision = SmartJobQueue._stored_revision_pair(row)
        return QueueJob(
            job_id=str(row["job_id"]),
            source_url=str(row["source_url"]),
            fit_score=int(row["fit_score"]),
            eligible=bool(row["eligible"]),
            decision=str(row["decision"]),
            evidence=evidence,
            state=SmartJobQueue._state(row),
            profile_revision=profile_revision,
            matcher_policy_revision=matcher_policy_revision,
        )

    @staticmethod
    def _stored_revision_pair(row: sqlite3.Row) -> tuple[str | None, str | None]:
        # Stored rows may be the legacy records migrated from a schema without
        # revision columns; new QueueCandidate values are never allowed here.
        try:
            return _require_revision_pair(
                row["profile_revision"],
                row["matcher_policy_revision"],
                allow_unversioned=True,
            )
        except QueuePolicyError:
            raise QueueStorageError("smart queue stored revisions are invalid") from None

    @staticmethod
    def _matches_active_revision(
        row: sqlite3.Row, active_revisions: tuple[str | None, str | None]
    ) -> bool:
        if active_revisions == (None, None):
            return True
        return SmartJobQueue._stored_revision_pair(row) == active_revisions

    @staticmethod
    def _candidate_matches_row(candidate: QueueCandidate, row: sqlite3.Row) -> bool:
        try:
            evidence = tuple(json.loads(row["evidence_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise QueueStorageError("smart queue stored data is invalid") from None
        return (
            str(row["job_id"]) == candidate.job_id
            and str(row["source_url"]) == candidate.source_url
            and int(row["fit_score"]) == candidate.fit_score
            and bool(row["eligible"]) == candidate.eligible
            and str(row["decision"]) == candidate.decision
            and evidence == candidate.evidence
            and SmartJobQueue._stored_revision_pair(row)
            == (candidate.profile_revision, candidate.matcher_policy_revision)
        )

    def _known_visible_urls(
        self,
        rows: Iterable[sqlite3.Row],
        visible_urls: Iterable[str],
    ) -> set[str]:
        """Validate the fixed capacity using only known canonical listings.

        Unrelated tabs, including unrelated listing pages, cannot consume queue
        capacity. Duplicate URLs for a managed listing make the physical-slot
        state ambiguous, so they are rejected rather than silently collapsed.
        """

        managed_urls = {str(row["source_url"]) for row in rows}
        known_visible_values = [url for url in visible_urls if url in managed_urls]
        known_visible = set(known_visible_values)
        if len(known_visible) != len(known_visible_values):
            raise QueuePolicyError("visible queue listings must not contain duplicate managed URLs")
        if len(known_visible) > self.target_size:
            raise QueuePolicyError("visible queue listings cannot exceed five jobs")
        return known_visible

    def add_recommendations(self, candidates: Iterable[QueueCandidate]) -> None:
        if isinstance(candidates, (str, bytes)):
            raise QueuePolicyError("candidates must be an iterable of QueueCandidate values")
        try:
            values = tuple(candidates)
        except TypeError:
            raise QueuePolicyError("candidates must be an iterable of QueueCandidate values") from None
        if any(not isinstance(candidate, QueueCandidate) for candidate in values):
            raise QueuePolicyError("candidates must contain only QueueCandidate values")

        connection = self._transaction()
        try:
            active_revisions = self._read_active_revisions(connection)
            candidate_revision_pairs = {
                _require_revision_pair(
                    candidate.profile_revision,
                    candidate.matcher_policy_revision,
                    allow_unversioned=False,
                )
                for candidate in values
            }
            if active_revisions == (None, None) and candidate_revision_pairs:
                if len(candidate_revision_pairs) != 1:
                    raise QueuePolicyError(
                        "versioned recommendation batches must use one active revision pair"
                    )
                pair = next(iter(candidate_revision_pairs))
                assert pair[0] is not None and pair[1] is not None
                self._write_active_revisions(connection, (pair[0], pair[1]))

            batch_by_id: dict[str, QueueCandidate] = {}
            batch_by_url: dict[str, QueueCandidate] = {}
            for candidate in values:
                prior_by_id = batch_by_id.get(candidate.job_id)
                if prior_by_id is not None and prior_by_id.source_url != candidate.source_url:
                    raise QueuePolicyError(
                        "recommendation batch contains conflicting source URLs for one job_id"
                    )
                if prior_by_id is not None and prior_by_id != candidate:
                    raise QueuePolicyError(
                        "recommendation batch contains a conflicting duplicate job_id/source_url record"
                    )
                prior_by_url = batch_by_url.get(candidate.source_url)
                if prior_by_url is not None and prior_by_url.job_id != candidate.job_id:
                    raise QueuePolicyError(
                        "recommendation batch contains conflicting job IDs for one source URL"
                    )
                if prior_by_url is not None and prior_by_url != candidate:
                    raise QueuePolicyError(
                        "recommendation batch contains a conflicting duplicate job_id/source_url record"
                    )
                batch_by_id[candidate.job_id] = candidate
                batch_by_url[candidate.source_url] = candidate

                existing_by_id = connection.execute(
                    """
                    SELECT
                        job_id, source_url, fit_score, eligible, decision, evidence_json,
                        profile_revision, matcher_policy_revision
                    FROM smart_queue_jobs WHERE job_id = ?
                    """,
                    (candidate.job_id,),
                ).fetchone()
                if existing_by_id is not None and str(existing_by_id["source_url"]) != candidate.source_url:
                    raise QueuePolicyError("job_id already exists with a different source URL")
                if existing_by_id is not None and not self._candidate_matches_row(candidate, existing_by_id):
                    raise QueuePolicyError(
                        "job_id/source_url already exists with conflicting recommendation data"
                    )
                existing_by_url = connection.execute(
                    "SELECT job_id FROM smart_queue_jobs WHERE source_url = ?",
                    (candidate.source_url,),
                ).fetchone()
                if existing_by_url is not None and str(existing_by_url["job_id"]) != candidate.job_id:
                    raise QueuePolicyError("source URL already exists with a different job_id")
                if existing_by_id is not None:
                    continue
                if existing_by_url is not None:
                    # A healthy schema cannot reach this branch with the same
                    # job_id, so fail closed rather than accepting inconsistent data.
                    raise QueuePolicyError("source URL already exists with a different job_id")
                cursor = connection.execute(
                    """
                    INSERT INTO smart_queue_jobs (
                        job_id, source_url, fit_score, eligible, decision, evidence_json,
                        profile_revision, matcher_policy_revision, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate.job_id,
                        candidate.source_url,
                        candidate.fit_score,
                        int(candidate.eligible),
                        candidate.decision,
                        json.dumps(candidate.evidence, ensure_ascii=False, separators=(",", ":")),
                        candidate.profile_revision,
                        candidate.matcher_policy_revision,
                        _utc_now(),
                    ),
                )
                if cursor.rowcount == 1:
                    self._append_event(connection, candidate.job_id, "recommended", "agent")
            connection.commit()
        except (QueuePolicyError, QueueStorageError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def plan_refill(self, *, open_urls: Iterable[str]) -> QueueAction:
        visible_urls = self._normalize_snapshot(open_urls)
        connection = self._transaction()
        try:
            rows = self._rows(connection)
            active_revisions = self._read_active_revisions(connection)
            known_visible = self._known_visible_urls(rows, visible_urls)
            occupied_urls = set(known_visible)
            occupied_urls.update(
                str(row["source_url"])
                for row in rows
                if self._state(row) in _ACTIVE_STATES
            )
            vacancies = max(0, self.target_size - len(occupied_urls))
            selectable = [
                row
                for row in rows
                if bool(row["eligible"])
                and row["decision"] == "recommended"
                and self._state(row) == "recommended"
                and row["source_url"] not in visible_urls
                and self._matches_active_revision(row, active_revisions)
            ]
            selectable.sort(key=lambda row: (-int(row["fit_score"]), int(row["insertion_order"])))
            selected = selectable[:vacancies]
            for row in selected:
                self._append_event(connection, str(row["job_id"]), "waiting", "agent")
            connection.commit()
        except (QueuePolicyError, QueueStorageError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

        return QueueAction(
            job_ids=tuple(str(row["job_id"]) for row in selected),
            urls_to_open=tuple(str(row["source_url"]) for row in selected),
            search_needed=max(0, vacancies - len(selected)),
        )

    def record_visible_snapshot(self, visible_urls: Iterable[str], *, actor: str) -> None:
        actor = _require_actor(actor)
        normalized_visible = self._normalize_snapshot(visible_urls)
        connection = self._transaction()
        try:
            rows = self._rows(connection)
            self._known_visible_urls(rows, normalized_visible)
            for row in rows:
                state = self._state(row)
                is_visible = str(row["source_url"]) in normalized_visible
                if state == "waiting" and is_visible:
                    self._append_event(connection, str(row["job_id"]), "open", actor)
                elif row["latest_visibility"] == "open" and not is_visible:
                    self._append_event(connection, str(row["job_id"]), "missing", actor)
            connection.commit()
        except QueuePolicyError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def record_open_failure(self, job_id: str, *, actor: str) -> QueueJob:
        """Release one failed browser-open reservation without inferring outcome.

        Only a currently ``waiting`` reservation may be released. The
        attributable append-only event makes retries and failures auditable,
        while the failed recommendation is not selected again automatically.
        A different eligible recommendation can therefore occupy the vacancy.
        """

        job_id = _require_identifier(job_id, "job_id")
        actor = _require_actor(actor)
        connection = self._transaction()
        try:
            row = next((row for row in self._rows(connection) if row["job_id"] == job_id), None)
            if row is None:
                raise KeyError("unknown job id")
            if self._state(row) != "waiting":
                raise QueuePolicyError("only a waiting browser-open reservation can record an open failure")
            self._append_event(connection, job_id, "open_failed", actor)
            connection.commit()
            current = self._job_from_row(row)
            return QueueJob(
                job_id=current.job_id,
                source_url=current.source_url,
                fit_score=current.fit_score,
                eligible=current.eligible,
                decision=current.decision,
                evidence=current.evidence,
                state="open_failed",
                profile_revision=current.profile_revision,
                matcher_policy_revision=current.matcher_policy_revision,
            )
        except (KeyError, QueuePolicyError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def confirm_outcome(self, job_id: str, outcome: str, *, actor: str) -> QueueJob:
        job_id = _require_identifier(job_id, "job_id")
        raw_actor = actor
        actor = _require_actor(actor)
        if raw_actor != "user":
            raise QueuePolicyError("an application outcome must be confirmed by the exact user actor")
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise QueuePolicyError("outcome must be submitted, rejected, or skipped")

        connection = self._transaction()
        try:
            row = next((item for item in self._rows(connection) if item["job_id"] == job_id), None)
            if row is None:
                raise KeyError("unknown job id")
            if self._state(row) not in {"open", "awaiting_outcome"}:
                raise QueuePolicyError(
                    "an application outcome may be confirmed only for an open or awaiting_outcome listing"
                )
            self._append_event(connection, job_id, outcome, actor)
            connection.commit()
            result = self._job_from_row(row)
            result = QueueJob(
                job_id=result.job_id,
                source_url=result.source_url,
                fit_score=result.fit_score,
                eligible=result.eligible,
                decision=result.decision,
                evidence=result.evidence,
                state=outcome,  # type: ignore[arg-type]
                profile_revision=result.profile_revision,
                matcher_policy_revision=result.matcher_policy_revision,
            )
        except (QueuePolicyError, KeyError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)
        return result

    def get(self, job_id: str) -> QueueJob:
        job_id = _require_identifier(job_id, "job_id")
        connection = self._connect()
        try:
            row = next((row for row in self._rows(connection) if row["job_id"] == job_id), None)
            if row is None:
                raise KeyError("unknown job id")
            return self._job_from_row(row)
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def history_for(self, job_id: str) -> tuple[QueueEvent, ...]:
        job_id = _require_identifier(job_id, "job_id")
        connection = self._connect()
        try:
            exists = connection.execute("SELECT 1 FROM smart_queue_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if exists is None:
                raise KeyError("unknown job id")
            rows = connection.execute(
                """
                SELECT event_id, job_id, name, actor, occurred_at
                FROM smart_queue_events WHERE job_id = ? ORDER BY event_id
                """,
                (job_id,),
            ).fetchall()
            return tuple(
                QueueEvent(
                    event_id=int(row["event_id"]),
                    job_id=str(row["job_id"]),
                    name=str(row["name"]),
                    actor=str(row["actor"]),
                    occurred_at=str(row["occurred_at"]),
                    queue_id=self.queue_id,
                )
                for row in rows
            )
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def confirmed_submitted_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT job_id) FROM smart_queue_events
                WHERE name = 'submitted' AND actor = 'user'
                """
            ).fetchone()
            return int(row[0])
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def confirmed_outcome_events(self, *, after_event_id: int = 0) -> tuple[QueueEvent, ...]:
        """Return replayable user-confirmed outcomes for lifecycle reconciliation.

        Callers may replay this append-only stream after a crash. The lifecycle
        tracker records the queue namespace and local event ID transactionally,
        so retrying an already-reconciled event is a no-op rather than a
        duplicate transition.
        """

        if (
            isinstance(after_event_id, bool)
            or not isinstance(after_event_id, int)
            or after_event_id < 0
        ):
            raise QueuePolicyError("after_event_id must be a non-negative integer")
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT event_id, job_id, name, actor, occurred_at
                FROM smart_queue_events
                WHERE event_id > ?
                  AND name IN ('submitted', 'rejected', 'skipped')
                  AND actor = 'user'
                ORDER BY event_id
                """,
                (after_event_id,),
            ).fetchall()
            return tuple(
                QueueEvent(
                    event_id=int(row["event_id"]),
                    job_id=str(row["job_id"]),
                    name=str(row["name"]),
                    actor=str(row["actor"]),
                    occurred_at=str(row["occurred_at"]),
                    queue_id=self.queue_id,
                )
                for row in rows
            )
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    @staticmethod
    def _normalize_snapshot(urls: Iterable[str]) -> tuple[str, ...]:
        if isinstance(urls, (str, bytes)):
            raise QueuePolicyError("visible URLs must be an iterable of listing URLs")
        try:
            values = tuple(urls)
        except TypeError:
            raise QueuePolicyError("visible URLs must be an iterable of listing URLs") from None
        normalized: list[str] = []
        for url in values:
            if not isinstance(url, str):
                raise QueuePolicyError("visible URLs must contain only strings")
            try:
                normalized.append(_require_listing_url(url))
            except QueuePolicyError:
                # Browser snapshots naturally include unrelated tabs. They do
                # not count toward queue capacity and cannot mutate queue state.
                continue
        return tuple(normalized)
