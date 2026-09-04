"""Persistent, candidate-controlled listing review queue.

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
import re
import sqlite3
from typing import Iterable, Literal
import uuid
from .sources import canonical_listing_url


QueueState = Literal[
    "recommended",
    "waiting",
    "open",
    "open_failed",
    "released",
    "submitted",
    "rejected",
    "skipped",
]

_DEFAULT_TARGET_SIZE = 5
_MAX_TARGET_SIZE = 10
_UNSET_TARGET_SIZE = object()
_CAPACITY_PROVENANCE_DEFAULT = "default"
_CAPACITY_PROVENANCE_ACTIVE_INTAKE = "active-candidate-intake"
_CAPACITY_PROVENANCE_HOST_CONFIGURED = "host-configured"
_CAPACITY_PROVENANCE_LEGACY_UNVERIFIED = "legacy-unverified"
_CAPACITY_PROVENANCE_VALUES = frozenset(
    {
        _CAPACITY_PROVENANCE_DEFAULT,
        _CAPACITY_PROVENANCE_ACTIVE_INTAKE,
        _CAPACITY_PROVENANCE_HOST_CONFIGURED,
        _CAPACITY_PROVENANCE_LEGACY_UNVERIFIED,
    }
)
_INTAKE_REVISION_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DECISIONS = frozenset({"recommended", "review", "reject"})
_OUTCOMES = frozenset({"submitted", "rejected", "skipped"})
# A missing tab is released without inferring an application outcome.
_ACTIVE_STATES = frozenset({"waiting", "open"})
_MAX_IDENTIFIER_LENGTH = 256
_MAX_OPAQUE_JOB_ID_LENGTH = 128
_MAX_URL_LENGTH = 4096
_MAX_EVIDENCE_ITEMS = 100
_MAX_EVIDENCE_LENGTH = 4096
_OPAQUE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


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
        job_id = _require_opaque_job_id(self.job_id)
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
class CapacityPolicyEvent:
    """One immutable, attributable managed-listing capacity change."""

    event_id: int
    prior_target_size: int
    target_size: int
    actor: str
    occurred_at: str
    queue_id: str


@dataclass(frozen=True, slots=True)
class RevisionPolicyEvent:
    """One immutable, attributable active-revision-pair change.

    Prior revisions are ``None`` only when the queue moved out of the
    unbound state. Event rows carry no listing URLs, candidate facts, or
    browser state.
    """

    event_id: int
    prior_profile_revision: str | None
    prior_matcher_policy_revision: str | None
    profile_revision: str
    matcher_policy_revision: str
    actor: str
    occurred_at: str
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


def _require_opaque_job_id(value: object) -> str:
    """Require a bounded token, never a URL, path, or candidate-data value."""

    if (
        not isinstance(value, str)
        or len(value) > _MAX_OPAQUE_JOB_ID_LENGTH
        or _OPAQUE_JOB_ID.fullmatch(value) is None
    ):
        raise QueuePolicyError("job_id must be a bounded opaque identifier")
    return value


def _require_actor(value: object) -> str:
    return _require_identifier(value, "actor")


def _require_user_vacated_outcome_actor(value: object, vacated: object) -> str:
    """Require the candidate's exact dual outcome-and-vacancy attestation."""

    actor = _require_actor(value)
    if type(value) is not str or value != "user":
        raise QueuePolicyError("an application outcome must be confirmed by the exact user actor")
    if type(vacated) is not bool or vacated is not True:
        raise QueuePolicyError("an application outcome requires explicit vacated=True confirmation")
    return actor


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


def _require_target_size(value: object) -> int:
    """Require a candidate-selected bounded number of managed listing tabs."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise QueuePolicyError("target_size must be an integer")
    if not 1 <= value <= _MAX_TARGET_SIZE:
        raise QueuePolicyError(f"target_size must be between 1 and {_MAX_TARGET_SIZE}")
    return value


def _require_capacity_provenance(value: object) -> str:
    """Require a bounded, non-sensitive capacity-policy source label."""

    if not isinstance(value, str) or value not in _CAPACITY_PROVENANCE_VALUES:
        raise QueuePolicyError("capacity provenance is invalid")
    return value


def _require_intake_revision_hash(value: object) -> str:
    """Require the active-intake digest without persisting intake facts."""

    if not isinstance(value, str) or _INTAKE_REVISION_HASH.fullmatch(value) is None:
        raise QueuePolicyError("active candidate intake revision is invalid")
    return value


class SmartJobQueue:
    """SQLite-backed queue with candidate-selected physical review capacity.

    Recommendation facts never change after insertion and queue history is
    append-only.  Logical state is derived from that history.  Planning holds
    an immediate SQLite transaction, so concurrent planners cannot reserve the
    same vacancy or grow the reserved queue beyond the configured capacity.
    """

    def __init__(
        self,
        database_path: Path | str,
        *,
        target_size: int | object = _UNSET_TARGET_SIZE,
        _capacity_provenance: str | None = None,
        _capacity_intake_revision: str | None = None,
    ):
        requested_target_size = (
            None if target_size is _UNSET_TARGET_SIZE else _require_target_size(target_size)
        )
        if _capacity_provenance is None:
            requested_capacity_provenance = (
                _CAPACITY_PROVENANCE_DEFAULT
                if requested_target_size in (None, _DEFAULT_TARGET_SIZE)
                else _CAPACITY_PROVENANCE_HOST_CONFIGURED
            )
        else:
            requested_capacity_provenance = _require_capacity_provenance(_capacity_provenance)
        if requested_capacity_provenance == _CAPACITY_PROVENANCE_ACTIVE_INTAKE:
            requested_capacity_intake_revision = _require_intake_revision_hash(
                _capacity_intake_revision
            )
        elif _capacity_intake_revision is not None:
            raise QueuePolicyError("only active intake capacity provenance may include an intake revision")
        else:
            requested_capacity_intake_revision = None
        if (
            requested_capacity_provenance == _CAPACITY_PROVENANCE_DEFAULT
            and requested_target_size not in (None, _DEFAULT_TARGET_SIZE)
        ):
            raise QueuePolicyError("default capacity provenance requires the default target_size")
        if not isinstance(database_path, (str, Path)):
            raise QueuePolicyError("database_path must be a path")
        raw_path = str(database_path)
        if not raw_path.strip():
            raise QueuePolicyError("database_path must be non-empty")
        self.database_path = Path(raw_path)
        self._requested_target_size = requested_target_size
        self._requested_capacity_provenance = requested_capacity_provenance
        self._requested_capacity_intake_revision = requested_capacity_intake_revision
        self._target_size = _DEFAULT_TARGET_SIZE
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

    @classmethod
    def for_active_candidate_intake(
        cls,
        database_path: Path | str,
        *,
        target_size: int,
        intake_revision_hash: str,
    ) -> "SmartJobQueue":
        """Create a live queue whose non-default capacity has intake proof.

        Only the active intake's SHA-256 revision is stored. Candidate facts,
        document paths, and browser state never enter queue metadata.
        """

        return cls(
            database_path,
            target_size=target_size,
            _capacity_provenance=_CAPACITY_PROVENANCE_ACTIVE_INTAKE,
            _capacity_intake_revision=intake_revision_hash,
        )

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

    def _candidate_memory_scope(
        self,
    ) -> tuple[str, tuple[str | None, str | None]]:
        """Read the authenticated queue identity and active revisions together."""

        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT queue_id, active_profile_revision, active_matcher_policy_revision
                FROM smart_queue_metadata WHERE metadata_id = 1
                """
            ).fetchone()
            if row is None:
                raise QueueStorageError("smart queue identity is unavailable")
            queue_id = self._require_stored_queue_id(row["queue_id"])
            if queue_id != self.queue_id:
                raise QueueStorageError("smart queue identity is inconsistent")
            try:
                revisions = _require_revision_pair(
                    row["active_profile_revision"],
                    row["active_matcher_policy_revision"],
                    allow_unversioned=True,
                )
            except QueuePolicyError:
                raise QueueStorageError("smart queue active revisions are invalid") from None
            return queue_id, revisions
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    @property
    def target_size(self) -> int:
        """Return the durable candidate-selected managed-listing capacity."""

        connection = self._connect()
        try:
            target_size = self._read_target_size(connection)
            self._target_size = target_size
            return target_size
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    @property
    def capacity_provenance(self) -> str:
        """Return the durable, URL-free source of the current capacity policy."""

        connection = self._connect()
        try:
            provenance, _ = self._read_capacity_provenance(connection)
            return provenance
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    @property
    def has_active_intake_capacity_provenance(self) -> bool:
        """Whether live non-default capacity is bound to an active intake digest."""

        connection = self._connect()
        try:
            provenance, intake_revision = self._read_capacity_provenance(connection)
            return (
                provenance == _CAPACITY_PROVENANCE_ACTIVE_INTAKE
                and intake_revision is not None
            )
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
    def _read_target_size(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT target_size FROM smart_queue_metadata WHERE metadata_id = 1"
        ).fetchone()
        if row is None:
            raise QueueStorageError("smart queue target size is unavailable")
        try:
            return _require_target_size(row["target_size"])
        except QueuePolicyError:
            raise QueueStorageError("smart queue target size is invalid") from None

    @staticmethod
    def _read_capacity_provenance(connection: sqlite3.Connection) -> tuple[str, str | None]:
        row = connection.execute(
            """
            SELECT capacity_provenance, capacity_intake_revision
            FROM smart_queue_metadata WHERE metadata_id = 1
            """
        ).fetchone()
        if row is None:
            raise QueueStorageError("smart queue capacity provenance is unavailable")
        try:
            provenance = _require_capacity_provenance(row["capacity_provenance"])
            intake_revision = row["capacity_intake_revision"]
            if provenance == _CAPACITY_PROVENANCE_ACTIVE_INTAKE:
                return provenance, _require_intake_revision_hash(intake_revision)
            if intake_revision is not None:
                raise QueuePolicyError("capacity provenance contains an unexpected intake revision")
            return provenance, None
        except QueuePolicyError:
            raise QueueStorageError("smart queue capacity provenance is invalid") from None

    @staticmethod
    def _write_target_size(connection: sqlite3.Connection, target_size: int) -> None:
        cursor = connection.execute(
            "UPDATE smart_queue_metadata SET target_size = ? WHERE metadata_id = 1",
            (target_size,),
        )
        if cursor.rowcount != 1:
            raise QueueStorageError("smart queue target size could not be persisted")

    @staticmethod
    def _write_active_revisions(
        connection: sqlite3.Connection, revisions: tuple[str | None, str | None]
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
                    active_matcher_policy_revision TEXT,
                    target_size INTEGER NOT NULL DEFAULT 5 CHECK(target_size BETWEEN 1 AND 10),
                    capacity_provenance TEXT NOT NULL DEFAULT 'default'
                        CHECK(capacity_provenance IN (
                            'default', 'active-candidate-intake', 'host-configured', 'legacy-unverified'
                        )),
                    capacity_intake_revision TEXT
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

                CREATE TABLE IF NOT EXISTS smart_queue_capacity_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prior_target_size INTEGER NOT NULL
                        CHECK(prior_target_size BETWEEN 1 AND 10),
                    target_size INTEGER NOT NULL CHECK(target_size BETWEEN 1 AND 10),
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS smart_queue_revision_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prior_profile_revision TEXT,
                    prior_matcher_policy_revision TEXT,
                    profile_revision TEXT NOT NULL,
                    matcher_policy_revision TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_smart_queue_events_job
                    ON smart_queue_events(job_id, event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_smart_queue_one_outcome
                    ON smart_queue_events(job_id)
                    WHERE name IN ('submitted', 'rejected', 'skipped');
                CREATE INDEX IF NOT EXISTS idx_smart_queue_capacity_events
                    ON smart_queue_capacity_events(event_id);
                CREATE INDEX IF NOT EXISTS idx_smart_queue_revision_events
                    ON smart_queue_revision_events(event_id);

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

                CREATE TRIGGER IF NOT EXISTS smart_queue_capacity_events_no_update
                BEFORE UPDATE ON smart_queue_capacity_events
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue capacity events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS smart_queue_capacity_events_no_delete
                BEFORE DELETE ON smart_queue_capacity_events
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue capacity events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS smart_queue_revision_events_no_update
                BEFORE UPDATE ON smart_queue_revision_events
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue revision events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS smart_queue_revision_events_no_delete
                BEFORE DELETE ON smart_queue_revision_events
                BEGIN
                    SELECT RAISE(ABORT, 'smart queue revision events are append-only');
                END;
                """
            )
            self._ensure_column(connection, "smart_queue_jobs", "profile_revision", "TEXT")
            self._ensure_column(connection, "smart_queue_jobs", "matcher_policy_revision", "TEXT")
            self._ensure_column(connection, "smart_queue_metadata", "active_profile_revision", "TEXT")
            self._ensure_column(connection, "smart_queue_metadata", "active_matcher_policy_revision", "TEXT")
            self._ensure_column(
                connection,
                "smart_queue_metadata",
                "target_size",
                "INTEGER NOT NULL DEFAULT 5",
            )
            self._ensure_column(
                connection,
                "smart_queue_metadata",
                "capacity_provenance",
                "TEXT NOT NULL DEFAULT 'default'",
            )
            self._ensure_column(
                connection,
                "smart_queue_metadata",
                "capacity_intake_revision",
                "TEXT",
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO smart_queue_metadata
                    (metadata_id, queue_id, target_size, capacity_provenance, capacity_intake_revision)
                VALUES (1, ?, ?, ?, ?)
                """,
                (
                    self._new_queue_id(),
                    self._requested_target_size or _DEFAULT_TARGET_SIZE,
                    self._requested_capacity_provenance,
                    self._requested_capacity_intake_revision,
                ),
            )
            metadata = connection.execute(
                """
                SELECT queue_id, target_size, capacity_provenance, capacity_intake_revision
                FROM smart_queue_metadata WHERE metadata_id = 1
                """
            ).fetchone()
            if metadata is None:
                raise QueueStorageError("smart queue identity could not be persisted")
            self._queue_id = self._require_stored_queue_id(metadata["queue_id"])
            stored_target_size = self._read_target_size(connection)
            stored_capacity_provenance, stored_capacity_intake_revision = (
                self._read_capacity_provenance(connection)
            )
            # A database created before provenance tracking may already have a
            # non-default target. It must not silently become live-authorized.
            if (
                stored_target_size != _DEFAULT_TARGET_SIZE
                and stored_capacity_provenance == _CAPACITY_PROVENANCE_DEFAULT
                and stored_capacity_intake_revision is None
            ):
                connection.execute(
                    """
                    UPDATE smart_queue_metadata
                    SET capacity_provenance = ?
                    WHERE metadata_id = 1
                    """,
                    (_CAPACITY_PROVENANCE_LEGACY_UNVERIFIED,),
                )
                stored_capacity_provenance = _CAPACITY_PROVENANCE_LEGACY_UNVERIFIED
            if (
                self._requested_target_size is not None
                and self._requested_target_size != stored_target_size
            ):
                raise QueuePolicyError(
                    "configured target_size conflicts with the persisted candidate-selected target_size"
                )
            if (
                self._requested_capacity_provenance == _CAPACITY_PROVENANCE_ACTIVE_INTAKE
                and (
                    stored_capacity_provenance != _CAPACITY_PROVENANCE_ACTIVE_INTAKE
                    or stored_capacity_intake_revision != self._requested_capacity_intake_revision
                )
            ):
                raise QueuePolicyError(
                    "active candidate intake capacity provenance conflicts with persisted queue metadata"
                )
            self._target_size = stored_target_size
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
    def _append_event(connection: sqlite3.Connection, job_id: str, name: str, actor: str) -> int:
        cursor = connection.execute(
            """
            INSERT INTO smart_queue_events (job_id, name, actor, occurred_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, name, actor, _utc_now()),
        )
        if cursor.lastrowid is None:
            raise QueueStorageError("smart queue event could not be persisted")
        return int(cursor.lastrowid)

    @staticmethod
    def _append_capacity_policy_event(
        connection: sqlite3.Connection,
        *,
        prior_target_size: int,
        target_size: int,
        actor: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO smart_queue_capacity_events
                (prior_target_size, target_size, actor, occurred_at)
            VALUES (?, ?, ?, ?)
            """,
            (prior_target_size, target_size, actor, _utc_now()),
        )

    @staticmethod
    def _append_revision_policy_event(
        connection: sqlite3.Connection,
        *,
        prior_revisions: tuple[str | None, str | None],
        revisions: tuple[str, str],
        actor: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO smart_queue_revision_events
                (prior_profile_revision, prior_matcher_policy_revision,
                 profile_revision, matcher_policy_revision, actor, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                prior_revisions[0],
                prior_revisions[1],
                revisions[0],
                revisions[1],
                actor,
                _utc_now(),
            ),
        )

    @staticmethod
    def _write_capacity_provenance(
        connection: sqlite3.Connection,
        provenance: str,
        intake_revision: str | None,
    ) -> None:
        cursor = connection.execute(
            """
            UPDATE smart_queue_metadata
            SET capacity_provenance = ?, capacity_intake_revision = ?
            WHERE metadata_id = 1
            """,
            (provenance, intake_revision),
        )
        if cursor.rowcount != 1:
            raise QueueStorageError("smart queue capacity provenance could not be persisted")

    def set_active_revisions(
        self,
        profile_revision: str,
        matcher_policy_revision: str,
        *,
        actor: str = "host",
    ) -> tuple[str, str]:
        """Explicitly select the revision pair eligible for future refills.

        The pair may change only while the queue holds no stored
        recommendations; rewriting it underneath stored rows would strand
        them outside refill selection with no auditable recovery. Repeating
        the current pair is an idempotent no-op. An accepted change writes
        one append-only revision-policy event. Legacy queues that already
        hold unversioned rows therefore stay unbound; this never changes
        recommendation rows, queue history, or candidate-confirmed outcomes.
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
            current_pair = self._read_active_revisions(connection)
            if active_pair == current_pair:
                connection.rollback()
                assert current_pair[0] is not None and current_pair[1] is not None
                return (current_pair[0], current_pair[1])
            has_stored_job = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM smart_queue_jobs)"
            ).fetchone()
            if has_stored_job is None:
                raise QueueStorageError("smart queue stored jobs are unavailable")
            if bool(has_stored_job[0]):
                raise QueuePolicyError(
                    "queue revisions may be changed only while the queue is empty"
                )
            self._write_active_revisions(connection, active_pair)
            self._append_revision_policy_event(
                connection,
                prior_revisions=current_pair,
                revisions=active_pair,
                actor=actor,
            )
            connection.commit()
        except (QueuePolicyError, QueueStorageError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)
        return active_pair

    def bind_empty_queue_revisions(
        self, profile_revision: str, matcher_policy_revision: str
    ) -> None:
        """Atomically seed an otherwise unused queue with one revision pair."""

        revisions = _require_revision_pair(
            profile_revision,
            matcher_policy_revision,
            allow_unversioned=False,
        )
        assert revisions[0] is not None and revisions[1] is not None
        requested_pair = (revisions[0], revisions[1])
        connection = self._transaction()
        try:
            has_stored_job = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM smart_queue_jobs)"
            ).fetchone()
            if has_stored_job is None:
                raise QueueStorageError("smart queue stored jobs are unavailable")
            if bool(has_stored_job[0]):
                raise QueuePolicyError("queue revisions may be bound only while the queue is empty")

            active_pair = self._read_active_revisions(connection)
            if active_pair == (None, None):
                self._write_active_revisions(connection, requested_pair)
            elif active_pair != requested_pair:
                raise QueuePolicyError("queue revisions conflict with the existing active pair")
            connection.commit()
        except (QueuePolicyError, QueueStorageError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def reset_empty_queue_revisions(self) -> None:
        """Restore an unused queue's newly bound revision pair to unbound.

        This is the rollback half of :meth:`bind_empty_queue_revisions`.  It
        never touches job rows, capacity, history, or outcomes.
        """

        connection = self._transaction()
        try:
            has_stored_job = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM smart_queue_jobs)"
            ).fetchone()
            if has_stored_job is None:
                raise QueueStorageError("smart queue stored jobs are unavailable")
            if bool(has_stored_job[0]):
                raise QueuePolicyError("queue revisions may be reset only while the queue is empty")

            active_pair = self._read_active_revisions(connection)
            if active_pair != (None, None):
                self._write_active_revisions(connection, (None, None))
            connection.commit()
        except (QueuePolicyError, QueueStorageError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def set_target_size(
        self,
        target_size: int,
        *,
        actor: str,
        capacity_provenance: str | None = None,
        intake_revision_hash: str | None = None,
    ) -> int:
        """Persist a user-selected capacity without changing jobs or outcomes.

        Reducing capacity never closes visible tabs, releases reservations, or
        infers an application outcome. Refill planning simply remains idle
        until candidate-controlled activity naturally creates a vacancy.

        Capacity provenance is explicit and never inferred. The documented
        default of five always resolves to ``default`` provenance and needs
        no proof. Any other capacity resolves to ``host-configured`` unless
        the caller supplies ``capacity_provenance="active-candidate-intake"``
        with the fresh intake revision hash that attests it; a user choice
        is therefore never silently marked intake-proven. A
        ``host-configured`` non-default capacity is honestly recorded but is
        not live-authorized: the live coordinator refuses non-default
        capacity without active-intake proof, so keeping a live queue
        authorized across a resize requires binding fresh intake proof (for
        example through the active-intake factory). Inconsistent
        combinations -- a non-default size labeled ``default``, an intake
        revision without active-intake provenance, or proof supplied for the
        default size -- fail closed without mutation.
        """

        target_size = _require_target_size(target_size)
        actor = _require_actor(actor)
        if actor != "user":
            raise QueuePolicyError("target_size may be changed only by the exact user actor")
        if capacity_provenance is None:
            requested_provenance: str | None = None
        else:
            requested_provenance = _require_capacity_provenance(capacity_provenance)
        if requested_provenance == _CAPACITY_PROVENANCE_ACTIVE_INTAKE:
            requested_intake_revision: str | None = _require_intake_revision_hash(
                intake_revision_hash
            )
        elif intake_revision_hash is not None:
            raise QueuePolicyError("only active intake capacity provenance may include an intake revision")
        else:
            requested_intake_revision = None
        if target_size == _DEFAULT_TARGET_SIZE:
            if requested_provenance not in (None, _CAPACITY_PROVENANCE_DEFAULT):
                raise QueuePolicyError(
                    "default capacity provenance requires the default target_size"
                )
            resolved_provenance = _CAPACITY_PROVENANCE_DEFAULT
            resolved_intake_revision = None
        elif requested_provenance is None:
            resolved_provenance = _CAPACITY_PROVENANCE_HOST_CONFIGURED
            resolved_intake_revision = None
        elif requested_provenance == _CAPACITY_PROVENANCE_HOST_CONFIGURED:
            resolved_provenance = _CAPACITY_PROVENANCE_HOST_CONFIGURED
            resolved_intake_revision = None
        elif requested_provenance == _CAPACITY_PROVENANCE_ACTIVE_INTAKE:
            resolved_provenance = _CAPACITY_PROVENANCE_ACTIVE_INTAKE
            resolved_intake_revision = requested_intake_revision
        else:
            raise QueuePolicyError(
                "non-default target_size requires host-configured or active intake capacity provenance"
            )
        connection = self._transaction()
        try:
            previous_target_size = self._read_target_size(connection)
            stored_provenance, stored_intake_revision = self._read_capacity_provenance(connection)
            if (
                target_size == previous_target_size
                and resolved_provenance == stored_provenance
                and resolved_intake_revision == stored_intake_revision
            ):
                connection.rollback()
                self._target_size = previous_target_size
                return previous_target_size
            self._write_target_size(connection, target_size)
            self._write_capacity_provenance(
                connection, resolved_provenance, resolved_intake_revision
            )
            if target_size != previous_target_size:
                self._append_capacity_policy_event(
                    connection,
                    prior_target_size=previous_target_size,
                    target_size=target_size,
                    actor=actor,
                )
            connection.commit()
            self._target_size = target_size
            return target_size
        except QueueStorageError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def capacity_history(self, *, after_event_id: int = 0) -> tuple[CapacityPolicyEvent, ...]:
        """Return the append-only, candidate-attributed capacity policy stream.

        This exposes no listing URLs, candidate facts, or browser state.  A
        capacity event is written only when the exact ``user`` actor changes
        the durable capacity; retrying the current value does not fabricate a
        policy revision.
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
                SELECT event_id, prior_target_size, target_size, actor, occurred_at
                FROM smart_queue_capacity_events
                WHERE event_id > ?
                ORDER BY event_id
                """,
                (after_event_id,),
            ).fetchall()
            return tuple(
                CapacityPolicyEvent(
                    event_id=int(row["event_id"]),
                    prior_target_size=_require_target_size(row["prior_target_size"]),
                    target_size=_require_target_size(row["target_size"]),
                    actor=_require_actor(row["actor"]),
                    occurred_at=str(row["occurred_at"]),
                    queue_id=self.queue_id,
                )
                for row in rows
            )
        except QueuePolicyError:
            raise QueueStorageError("smart queue capacity history is invalid") from None
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def capacity_policy_history(self, *, after_event_id: int = 0) -> tuple[CapacityPolicyEvent, ...]:
        """Compatibility alias for the read-only capacity-policy event stream."""

        return self.capacity_history(after_event_id=after_event_id)

    def revision_history(self, *, after_event_id: int = 0) -> tuple[RevisionPolicyEvent, ...]:
        """Return the append-only, attributable active-revision-pair stream.

        This exposes no listing URLs, candidate facts, or browser state. A
        revision event is written only when :meth:`set_active_revisions`
        changes the durable pair on an empty queue; repeating the current
        pair does not fabricate a policy revision.
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
                SELECT event_id, prior_profile_revision, prior_matcher_policy_revision,
                       profile_revision, matcher_policy_revision, actor, occurred_at
                FROM smart_queue_revision_events
                WHERE event_id > ?
                ORDER BY event_id
                """,
                (after_event_id,),
            ).fetchall()
            events: list[RevisionPolicyEvent] = []
            for row in rows:
                try:
                    prior_pair = _require_revision_pair(
                        row["prior_profile_revision"],
                        row["prior_matcher_policy_revision"],
                        allow_unversioned=True,
                    )
                    pair = _require_revision_pair(
                        row["profile_revision"],
                        row["matcher_policy_revision"],
                        allow_unversioned=False,
                    )
                    assert pair[0] is not None and pair[1] is not None
                    actor = _require_actor(row["actor"])
                except QueuePolicyError:
                    raise QueueStorageError("smart queue revision history is invalid") from None
                events.append(
                    RevisionPolicyEvent(
                        event_id=int(row["event_id"]),
                        prior_profile_revision=prior_pair[0],
                        prior_matcher_policy_revision=prior_pair[1],
                        profile_revision=pair[0],
                        matcher_policy_revision=pair[1],
                        actor=actor,
                        occurred_at=str(row["occurred_at"]),
                        queue_id=self.queue_id,
                    )
                )
            return tuple(events)
        except QueueStorageError:
            raise
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

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
            return "released"
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
        """Validate the bounded capacity using only known canonical listings.

        Unrelated tabs, including unrelated listing pages, cannot consume queue
        capacity. Duplicate URLs for a managed listing make the physical-slot
        state ambiguous, so they are rejected rather than silently collapsed.
        """

        managed_urls = {str(row["source_url"]) for row in rows}
        known_visible_values = [url for url in visible_urls if url in managed_urls]
        known_visible = set(known_visible_values)
        if len(known_visible) != len(known_visible_values):
            raise QueuePolicyError("visible queue listings must not contain duplicate managed URLs")
        if len(known_visible) > _MAX_TARGET_SIZE:
            raise QueuePolicyError(
                f"visible queue listings cannot exceed {_MAX_TARGET_SIZE} managed jobs"
            )
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
            elif (
                active_revisions != (None, None)
                and candidate_revision_pairs
                and candidate_revision_pairs != {active_revisions}
            ):
                # A bound queue admits only its active pair. Inserting a
                # mismatched batch would create dead rows that refill
                # selection can never reach, so the whole batch fails
                # closed atomically before any recommendation is stored.
                raise QueuePolicyError(
                    "recommendation batch revisions conflict with the active queue pair"
                )

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
            target_size = self._read_target_size(connection)
            known_visible = self._known_visible_urls(rows, visible_urls)
            occupied_urls = set(known_visible)
            occupied_urls.update(
                str(row["source_url"])
                for row in rows
                if self._state(row) in _ACTIVE_STATES
            )
            vacancies = max(0, target_size - len(occupied_urls))
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

    def refill_search_needed(self, *, open_urls: Iterable[str]) -> int:
        """Return the current refill shortage without reserving candidates."""

        visible_urls = self._normalize_snapshot(open_urls)
        connection = self._connect()
        try:
            rows = self._rows(connection)
            active_revisions = self._read_active_revisions(connection)
            target_size = self._read_target_size(connection)
            known_visible = self._known_visible_urls(rows, visible_urls)
            occupied_urls = set(known_visible)
            occupied_urls.update(
                str(row["source_url"])
                for row in rows
                if self._state(row) in _ACTIVE_STATES
            )
            vacancies = max(0, target_size - len(occupied_urls))
            available = sum(
                1
                for row in rows
                if bool(row["eligible"])
                and row["decision"] == "recommended"
                and self._state(row) == "recommended"
                and row["source_url"] not in visible_urls
                and self._matches_active_revision(row, active_revisions)
            )
            return max(0, vacancies - available)
        except (QueuePolicyError, QueueStorageError):
            raise
        except sqlite3.Error:
            raise QueueStorageError("smart queue storage operation failed") from None
        finally:
            self._close(connection)

    def record_visible_snapshot(
        self,
        visible_urls: Iterable[str],
        *,
        actor: str,
        recover_stale_waiting: bool = False,
    ) -> tuple[str, ...]:
        """Atomically reconcile one URL-only browser snapshot.

        A reliable initial cycle snapshot may opt into recovering reservations
        left ``waiting`` by an interrupted prior cycle. Visible reservations
        become open; absent stale reservations become open failures and release
        their slots. Follow-up snapshots must keep the default so reservations
        created during the current cycle survive an unavailable observation.
        """

        actor = _require_actor(actor)
        if type(recover_stale_waiting) is not bool:
            raise QueuePolicyError("recover_stale_waiting must be a boolean")
        normalized_visible = self._normalize_snapshot(visible_urls)
        connection = self._transaction()
        try:
            rows = self._rows(connection)
            self._known_visible_urls(rows, normalized_visible)
            recovered_open_failed_job_ids: list[str] = []
            for row in rows:
                state = self._state(row)
                is_visible = str(row["source_url"]) in normalized_visible
                if state == "waiting" and is_visible:
                    self._append_event(connection, str(row["job_id"]), "open", actor)
                elif state == "waiting" and recover_stale_waiting:
                    job_id = str(row["job_id"])
                    self._append_event(connection, job_id, "open_failed", actor)
                    recovered_open_failed_job_ids.append(job_id)
                elif row["latest_visibility"] == "open" and not is_visible:
                    self._append_event(connection, str(row["job_id"]), "missing", actor)
            connection.commit()
            return tuple(recovered_open_failed_job_ids)
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

        job_id = _require_opaque_job_id(job_id)
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

    def confirm_outcome(
        self,
        job_id: str,
        outcome: str,
        *,
        actor: str,
        vacated: bool,
        candidate_memory: object,
    ) -> QueueJob:
        """Finalize an outcome through the atomic candidate-memory boundary.

        A queue outcome is durable suppression state.  The public queue API
        therefore delegates to :class:`CandidateMemory`, whose attached
        SQLite transaction commits the queue event and suppression row together.
        """

        # Import only when this optional public convenience API is invoked:
        # CandidateMemory imports SmartJobQueue at module load time.
        from .candidate_memory import CandidateMemory

        if not isinstance(candidate_memory, CandidateMemory):
            raise QueuePolicyError("candidate_memory must be a CandidateMemory finalization authority")
        candidate_memory.finalize_queue_outcome(
            queue=self,
            job_id=job_id,
            outcome=outcome,
            actor=actor,
            vacated=vacated,
        )
        return self.get(job_id)

    def _confirm_outcome_with_candidate_memory(
        self,
        job_id: str,
        outcome: str,
        *,
        actor: str,
        vacated: bool,
        memory_database: Path,
    ) -> tuple[QueueEvent, str, str, bool]:
        """Atomically record one user outcome and its private suppression row.

        This internal bridge is intentionally reachable only through
        ``CandidateMemory`` after it has validated and initialized a private
        memory database.  SQLite's attached-database transaction gives the
        queue event and candidate-memory row one commit boundary.  It refuses
        WAL mode because SQLite cannot guarantee an atomic multi-database
        commit there. Both databases must use a durable rollback-journal mode
        before the transaction begins.
        """

        job_id = _require_opaque_job_id(job_id)
        actor = _require_user_vacated_outcome_actor(actor, vacated)
        if not isinstance(outcome, str) or outcome not in _OUTCOMES:
            raise QueuePolicyError("outcome must be submitted, rejected, or skipped")

        connection = self._connect()
        attached = False
        try:
            connection.execute("ATTACH DATABASE ? AS candidate_memory", (str(memory_database),))
            attached = True
            journal_modes = tuple(
                connection.execute(f"PRAGMA {database}.journal_mode").fetchone()
                for database in ("main", "candidate_memory")
            )
            if any(
                journal_mode is None
                or str(journal_mode[0]).lower() not in {"delete", "truncate", "persist"}
                for journal_mode in journal_modes
            ):
                raise QueueStorageError("atomic candidate outcome storage is unavailable")
            connection.execute("BEGIN IMMEDIATE")
            queue_metadata = connection.execute(
                """
                SELECT queue_id, active_profile_revision, active_matcher_policy_revision
                FROM smart_queue_metadata WHERE metadata_id = 1
                """
            ).fetchone()
            if queue_metadata is None:
                raise QueueStorageError("smart queue identity is unavailable")
            transaction_queue_id = self._require_stored_queue_id(queue_metadata["queue_id"])
            if transaction_queue_id != self.queue_id:
                raise QueueStorageError("smart queue identity changed during outcome finalization")
            try:
                _require_revision_pair(
                    queue_metadata["active_profile_revision"],
                    queue_metadata["active_matcher_policy_revision"],
                    allow_unversioned=False,
                )
            except QueuePolicyError:
                raise QueuePolicyError(
                    "active queue revisions are required for candidate memory"
                ) from None
            memory_scope = connection.execute(
                """
                SELECT queue_id
                FROM candidate_memory.candidate_memory_queue_scope
                WHERE scope_id = 1
                """
            ).fetchone()
            if memory_scope is None:
                legacy_memory = connection.execute(
                    "SELECT 1 FROM candidate_memory.candidate_memory_outcomes LIMIT 1"
                ).fetchone()
                if legacy_memory is not None:
                    raise QueueStorageError(
                        "candidate memory queue scope is unavailable"
                    )
                connection.execute(
                    """
                    INSERT INTO candidate_memory.candidate_memory_queue_scope (
                        scope_id, queue_id
                    ) VALUES (1, ?)
                    """,
                    (transaction_queue_id,),
                )
            else:
                try:
                    stored_memory_queue_id = self._require_stored_queue_id(
                        memory_scope["queue_id"]
                    )
                except QueueStorageError:
                    raise QueueStorageError(
                        "candidate memory queue scope is invalid"
                    ) from None
                if stored_memory_queue_id != transaction_queue_id:
                    raise QueuePolicyError(
                        "candidate memory queue scope does not match the authenticated queue"
                    )
            row = next((item for item in self._rows(connection) if item["job_id"] == job_id), None)
            if row is None:
                raise KeyError("unknown job id")

            state = self._state(row)
            if state in {"open", "released"}:
                event_id = self._append_event(connection, job_id, outcome, actor)
                occurred_at = str(
                    connection.execute(
                        "SELECT occurred_at FROM smart_queue_events WHERE event_id = ?", (event_id,)
                    ).fetchone()["occurred_at"]
                )
            elif state == outcome:
                existing_event = connection.execute(
                    """
                    SELECT event_id, occurred_at
                    FROM smart_queue_events
                    WHERE job_id = ? AND name = ? AND actor = 'user'
                    ORDER BY event_id DESC
                    LIMIT 1
                    """,
                    (job_id, outcome),
                ).fetchone()
                if existing_event is None:
                    raise QueueStorageError("smart queue outcome event is unavailable")
                event_id = int(existing_event["event_id"])
                occurred_at = str(existing_event["occurred_at"])
            else:
                raise QueuePolicyError(
                    "an application outcome may be confirmed only for an open or released listing"
                )

            source_url = str(row["source_url"])
            existing_memory = connection.execute(
                """
                SELECT job_id, source_url, outcome, actor, vacated, recorded_at
                FROM candidate_memory.candidate_memory_outcomes
                WHERE queue_id = ? AND event_id = ?
                """,
                (self.queue_id, event_id),
            ).fetchone()
            if existing_memory is None:
                recorded_at = _utc_now()
                connection.execute(
                    """
                    INSERT INTO candidate_memory.candidate_memory_outcomes (
                        queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (self.queue_id, event_id, job_id, source_url, outcome, actor, 1, recorded_at),
                )
                inserted = True
            else:
                expected = (job_id, source_url, outcome, actor, 1)
                actual = (
                    str(existing_memory["job_id"]),
                    str(existing_memory["source_url"]),
                    str(existing_memory["outcome"]),
                    str(existing_memory["actor"]),
                    int(existing_memory["vacated"]),
                )
                if actual != expected:
                    raise QueueStorageError("candidate memory event identity conflicts with stored facts")
                recorded_at = str(existing_memory["recorded_at"])
                inserted = False

            connection.commit()
            return (
                QueueEvent(
                    event_id=event_id,
                    job_id=job_id,
                    name=outcome,
                    actor=actor,
                    occurred_at=occurred_at,
                    queue_id=self.queue_id,
                ),
                source_url,
                recorded_at,
                inserted,
            )
        except (KeyError, QueuePolicyError, QueueStorageError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("atomic candidate outcome storage failed") from None
        finally:
            if attached:
                try:
                    connection.execute("DETACH DATABASE candidate_memory")
                except sqlite3.Error:
                    pass
            self._close(connection)

    def get(self, job_id: str) -> QueueJob:
        job_id = _require_opaque_job_id(job_id)
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
        job_id = _require_opaque_job_id(job_id)
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
