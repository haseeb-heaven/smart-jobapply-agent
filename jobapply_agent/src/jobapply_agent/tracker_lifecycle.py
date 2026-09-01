"""Append-only, candidate-controlled job-application lifecycle tracking.

This module records local facts only. It has no browser, network, form-filling,
upload, or submission authority. In particular, tab observations are kept
separate from candidate-recorded application transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Literal, Mapping, Sequence
import uuid

from .smart_queue import QueueStorageError, SmartJobQueue
from .sources import canonical_listing_url


LifecycleState = Literal[
    "shortlisted",
    "opened",
    "manual_applying",
    "submitted",
    "interview",
    "rejected",
    "offer",
    "withdrawn",
]

_CANDIDATE_OWNED_STATES = frozenset(
    {"manual_applying", "submitted", "interview", "rejected", "offer", "withdrawn"}
)
_ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "shortlisted": frozenset({"manual_applying"}),
    "opened": frozenset({"manual_applying"}),
    "manual_applying": frozenset({"submitted", "withdrawn"}),
    "submitted": frozenset({"interview", "rejected", "withdrawn"}),
    "interview": frozenset({"offer", "rejected", "withdrawn"}),
    "rejected": frozenset(),
    "offer": frozenset({"withdrawn"}),
    "withdrawn": frozenset(),
}
_TAB_EVENTS = frozenset({"opened", "reopened", "closed"})
_QUEUE_OUTCOMES = frozenset({"submitted", "rejected", "skipped"})
_ROUND_LIMIT = 5
_LEGACY_QUEUE_ID = "legacy-unscoped"


class InvalidLifecycleTransition(ValueError):
    """Raised when an actor or state change violates the lifecycle contract."""


class RoundLimitExceeded(ValueError):
    """Raised when a review round contains more than five unique jobs."""


@dataclass(frozen=True, slots=True)
class LifecycleJob:
    """Current derived view of a shortlisted job."""

    job_id: str
    source_url: str
    state: LifecycleState
    shortlisted_at: str


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """One immutable and attributable lifecycle observation."""

    event_id: int
    job_id: str
    category: str
    name: str
    actor: str
    payload: dict[str, Any]
    occurred_at: str


@dataclass(frozen=True, slots=True)
class ReviewRound:
    """An ordered, bounded group of jobs presented for candidate review."""

    round_id: str
    job_ids: tuple[str, ...]
    actor: str
    created_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _require_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} must be non-empty")
    return cleaned


def _require_actor(actor: str) -> str:
    try:
        return _require_text(actor, "actor")
    except ValueError as error:
        raise InvalidLifecycleTransition(str(error)) from error


def _require_listing_url(value: str) -> str:
    try:
        return canonical_listing_url(_require_text(value, "source_url"))
    except ValueError:
        raise ValueError(
            "source_url must be a credential-free canonical LinkedIn or Indeed listing URL"
        ) from None


def _require_user(actor: str, target: str) -> None:
    if actor != "user":
        raise InvalidLifecycleTransition(f"{target!r} must be recorded by the user")


def _encode_payload(payload: Mapping[str, Any] | None = None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class LifecycleTracker:
    """SQLite-backed append-only lifecycle and review-round tracker.

    Lifecycle state is derived from immutable events. ``opened`` is a review
    stage inferred from a tab observation only while the application remains
    shortlisted; it is not an application transition and cannot imply that the
    candidate started or submitted an application.
    """

    def __init__(self, database_path: Path | str):
        raw_path = str(database_path)
        self.database_path = Path(raw_path)
        self._memory_connection: sqlite3.Connection | None = None
        if raw_path == ":memory:":
            self._memory_connection = self._new_connection(raw_path)
        else:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @staticmethod
    def _new_connection(database: str | Path) -> sqlite3.Connection:
        connection = sqlite3.connect(database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _connect(self) -> sqlite3.Connection:
        return self._memory_connection or self._new_connection(self.database_path)

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_jobs (
                    job_id TEXT PRIMARY KEY,
                    source_url TEXT NOT NULL UNIQUE,
                    shortlisted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES lifecycle_jobs(job_id),
                    category TEXT NOT NULL,
                    name TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lifecycle_rounds (
                    round_id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS lifecycle_round_jobs (
                    round_id TEXT NOT NULL REFERENCES lifecycle_rounds(round_id),
                    job_id TEXT NOT NULL REFERENCES lifecycle_jobs(job_id),
                    position INTEGER NOT NULL,
                    PRIMARY KEY (round_id, job_id),
                    UNIQUE (round_id, position)
                );

                CREATE TABLE IF NOT EXISTS lifecycle_queue_reconciliations (
                    queue_id TEXT NOT NULL CHECK(length(queue_id) BETWEEN 1 AND 64),
                    queue_event_id INTEGER NOT NULL CHECK(queue_event_id > 0),
                    job_id TEXT NOT NULL REFERENCES lifecycle_jobs(job_id),
                    source_url TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                    actor TEXT NOT NULL CHECK(actor = 'user'),
                    reconciled_at TEXT NOT NULL,
                    PRIMARY KEY (queue_id, queue_event_id)
                );

                CREATE INDEX IF NOT EXISTS idx_lifecycle_events_job
                    ON lifecycle_events(job_id, event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_manual_submission_per_job
                    ON lifecycle_events(job_id)
                    WHERE category = 'lifecycle' AND name = 'submitted';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_lifecycle_jobs_source_url
                    ON lifecycle_jobs(source_url);

                CREATE TRIGGER IF NOT EXISTS lifecycle_events_no_update
                BEFORE UPDATE ON lifecycle_events
                BEGIN
                    SELECT RAISE(ABORT, 'lifecycle events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS lifecycle_events_no_delete
                BEFORE DELETE ON lifecycle_events
                BEGIN
                    SELECT RAISE(ABORT, 'lifecycle events are append-only');
                END;


                """
            )
            connection.execute("BEGIN IMMEDIATE")
            self._migrate_queue_reconciliations(connection)
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS lifecycle_queue_reconciliations_no_update
                BEFORE UPDATE ON lifecycle_queue_reconciliations
                BEGIN
                    SELECT RAISE(ABORT, 'queue reconciliations are append-only');
                END;
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS lifecycle_queue_reconciliations_no_delete
                BEFORE DELETE ON lifecycle_queue_reconciliations
                BEGIN
                    SELECT RAISE(ABORT, 'queue reconciliations are append-only');
                END;
                """
            )
            connection.commit()
        except QueueStorageError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise QueueStorageError("lifecycle storage initialization failed") from None
        finally:
            if connection is not self._memory_connection:
                connection.close()

    @staticmethod
    def _queue_namespace(value: object) -> str:
        if value == _LEGACY_QUEUE_ID:
            return _LEGACY_QUEUE_ID
        if not isinstance(value, str):
            raise QueueStorageError("stored queue identity is invalid")
        try:
            return uuid.UUID(value).hex
        except (AttributeError, ValueError):
            raise QueueStorageError("stored queue identity is invalid") from None

    @staticmethod
    def _reconciliation_schema_is_namespaced(connection: sqlite3.Connection) -> bool:
        columns = connection.execute(
            "PRAGMA table_info(lifecycle_queue_reconciliations)"
        ).fetchall()
        primary_key = sorted(
            (int(row["pk"]), str(row["name"])) for row in columns if int(row["pk"])
        )
        return primary_key == [(1, "queue_id"), (2, "queue_event_id")]

    @classmethod
    def _migrate_queue_reconciliations(cls, connection: sqlite3.Connection) -> None:
        """Move pre-namespace rows into an explicit, fail-closed legacy scope."""

        if cls._reconciliation_schema_is_namespaced(connection):
            return

        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(lifecycle_queue_reconciliations)")
        }
        required_columns = {
            "queue_event_id",
            "job_id",
            "source_url",
            "outcome",
            "actor",
            "reconciled_at",
        }
        if not required_columns.issubset(columns):
            raise QueueStorageError("lifecycle queue reconciliation schema is invalid")
        legacy_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'lifecycle_queue_reconciliations_legacy'
            """
        ).fetchone()
        if legacy_exists is not None:
            raise QueueStorageError("lifecycle queue reconciliation migration is incomplete")

        connection.execute("DROP TRIGGER IF EXISTS lifecycle_queue_reconciliations_no_update")
        connection.execute("DROP TRIGGER IF EXISTS lifecycle_queue_reconciliations_no_delete")
        connection.execute(
            """
            ALTER TABLE lifecycle_queue_reconciliations
            RENAME TO lifecycle_queue_reconciliations_legacy
            """
        )
        connection.execute(
            """
            CREATE TABLE lifecycle_queue_reconciliations (
                queue_id TEXT NOT NULL CHECK(length(queue_id) BETWEEN 1 AND 64),
                queue_event_id INTEGER NOT NULL CHECK(queue_event_id > 0),
                job_id TEXT NOT NULL REFERENCES lifecycle_jobs(job_id),
                source_url TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                actor TEXT NOT NULL CHECK(actor = 'user'),
                reconciled_at TEXT NOT NULL,
                PRIMARY KEY (queue_id, queue_event_id)
            )
            """
        )
        has_queue_id = "queue_id" in columns
        if has_queue_id:
            old_rows = connection.execute(
                """
                SELECT queue_id, queue_event_id, job_id, source_url, outcome, actor, reconciled_at
                FROM lifecycle_queue_reconciliations_legacy
                ORDER BY queue_event_id
                """
            ).fetchall()
        else:
            old_rows = connection.execute(
                """
                SELECT queue_event_id, job_id, source_url, outcome, actor, reconciled_at
                FROM lifecycle_queue_reconciliations_legacy
                ORDER BY queue_event_id
                """
            ).fetchall()
        migrated_rows = []
        for row in old_rows:
            queue_event_id = row["queue_event_id"]
            if isinstance(queue_event_id, bool) or not isinstance(queue_event_id, int) or queue_event_id <= 0:
                raise QueueStorageError("stored queue event identity is invalid")
            queue_id = cls._queue_namespace(row["queue_id"]) if has_queue_id else _LEGACY_QUEUE_ID
            migrated_rows.append(
                (
                    queue_id,
                    queue_event_id,
                    row["job_id"],
                    row["source_url"],
                    row["outcome"],
                    row["actor"],
                    row["reconciled_at"],
                )
            )
        connection.executemany(
            """
            INSERT INTO lifecycle_queue_reconciliations (
                queue_id, queue_event_id, job_id, source_url, outcome, actor, reconciled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            migrated_rows,
        )

    def _transaction(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        category: str,
        name: str,
        actor: str,
        payload: Mapping[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> LifecycleEvent:
        timestamp = occurred_at or _utc_now()
        cursor = connection.execute(
            """
            INSERT INTO lifecycle_events (job_id, category, name, actor, payload_json, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (job_id, category, name, actor, _encode_payload(payload), timestamp),
        )
        return LifecycleEvent(
            event_id=int(cursor.lastrowid),
            job_id=job_id,
            category=category,
            name=name,
            actor=actor,
            payload=dict(payload or {}),
            occurred_at=timestamp,
        )

    @staticmethod
    def _require_known_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT job_id, source_url, shortlisted_at FROM lifecycle_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown job id: {job_id}")
        return row

    @staticmethod
    def _state_in(connection: sqlite3.Connection, job_id: str) -> LifecycleState:
        lifecycle = connection.execute(
            """
            SELECT name FROM lifecycle_events
            WHERE job_id = ? AND category = 'lifecycle'
            ORDER BY event_id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if lifecycle is None:
            raise KeyError(f"unknown job id: {job_id}")
        state = str(lifecycle["name"])
        if state == "shortlisted":
            tab = connection.execute(
                """
                SELECT 1 FROM lifecycle_events
                WHERE job_id = ? AND category = 'tab' AND name IN ('opened', 'reopened')
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if tab is not None:
                return "opened"
        return state  # type: ignore[return-value]

    def shortlist(self, job_id: str, source_url: str, *, actor: str) -> LifecycleJob:
        job_id = _require_text(job_id, "job_id")
        source_url = _require_listing_url(source_url)
        actor = _require_actor(actor)
        timestamp = _utc_now()
        connection = self._transaction()
        try:
            existing = connection.execute("SELECT 1 FROM lifecycle_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if existing is not None:
                raise InvalidLifecycleTransition(f"job {job_id!r} is already shortlisted")
            duplicate = connection.execute(
                "SELECT 1 FROM lifecycle_jobs WHERE source_url = ?", (source_url,)
            ).fetchone()
            if duplicate is not None:
                raise InvalidLifecycleTransition("the canonical listing is already shortlisted")
            connection.execute(
                "INSERT INTO lifecycle_jobs (job_id, source_url, shortlisted_at) VALUES (?, ?, ?)",
                (job_id, source_url, timestamp),
            )
            self._insert_event(
                connection,
                job_id=job_id,
                category="lifecycle",
                name="shortlisted",
                actor=actor,
                occurred_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return LifecycleJob(job_id=job_id, source_url=source_url, state="shortlisted", shortlisted_at=timestamp)

    def reconcile_queue_outcome(
        self,
        *,
        queue: SmartJobQueue,
        queue_event_id: int,
        job_id: str,
        source_url: str,
        outcome: str,
        actor: str,
    ) -> LifecycleJob:
        """Replay one explicit Smart Queue outcome transactionally and idempotently.

        The Smart Queue's append-only user outcome event is the source record.
        Hosts replay ``confirmed_outcome_events`` after startup or failure. A
        queue's durable namespace is authenticated together with its local
        event ID, so independent queue databases cannot collide. A submitted
        event may create the missing shortlist and deterministic lifecycle
        prerequisites because it is already explicit user evidence. ``rejected``
        and ``skipped`` are always audited but never manufacture a submission
        when no submitted lifecycle state exists.
        """

        if not isinstance(queue, SmartJobQueue):
            raise InvalidLifecycleTransition("a SmartJobQueue is required to authenticate queue outcomes")
        queue_id = queue.queue_id
        if isinstance(queue_event_id, bool) or not isinstance(queue_event_id, int) or queue_event_id <= 0:
            raise InvalidLifecycleTransition("queue_event_id must be a positive integer")
        job_id = _require_text(job_id, "job_id")
        source_url = _require_listing_url(source_url)
        raw_actor = actor
        actor = _require_actor(actor)
        if raw_actor != "user":
            raise InvalidLifecycleTransition("queue outcomes must be recorded by the exact user actor")
        if not isinstance(outcome, str) or outcome not in _QUEUE_OUTCOMES:
            raise InvalidLifecycleTransition("queue outcome must be submitted, rejected, or skipped")

        try:
            queue_event = next(
                (
                    event
                    for event in queue.confirmed_outcome_events(after_event_id=queue_event_id - 1)
                    if event.event_id == queue_event_id
                ),
                None,
            )
            queue_job = queue.get(job_id)
        except (KeyError, ValueError):
            raise InvalidLifecycleTransition("queue outcome event is not an authenticated queue record") from None
        if queue_event is None:
            raise InvalidLifecycleTransition("queue outcome event is not an authenticated queue record")
        if (
            queue_event.job_id != job_id
            or queue_event.name != outcome
            or queue_event.actor != actor
            or queue_event.queue_id != queue_id
            or queue_job.source_url != source_url
        ):
            raise InvalidLifecycleTransition("queue outcome event facts do not match the authenticated queue record")

        connection = self._transaction()
        try:
            prior = connection.execute(
                """
                SELECT job_id, source_url, outcome, actor
                FROM lifecycle_queue_reconciliations
                WHERE queue_id = ? AND queue_event_id = ?
                """,
                (queue_id, queue_event_id),
            ).fetchone()
            if prior is not None:
                expected = (job_id, source_url, outcome, actor)
                actual = (str(prior["job_id"]), str(prior["source_url"]), str(prior["outcome"]), str(prior["actor"]))
                if actual != expected:
                    raise InvalidLifecycleTransition("queue event is already reconciled with different facts")
                row = self._require_known_job(connection, job_id)
                state = self._state_in(connection, job_id)
                connection.commit()
                return LifecycleJob(
                    job_id=job_id,
                    source_url=str(row["source_url"]),
                    state=state,
                    shortlisted_at=str(row["shortlisted_at"]),
                )

            legacy_prior = connection.execute(
                """
                SELECT 1 FROM lifecycle_queue_reconciliations
                WHERE queue_id = ? AND queue_event_id = ?
                """,
                (_LEGACY_QUEUE_ID, queue_event_id),
            ).fetchone()
            if legacy_prior is not None:
                raise InvalidLifecycleTransition(
                    "queue outcome event belongs to an unscoped legacy record and requires re-authentication"
                )

            row = connection.execute(
                "SELECT job_id, source_url, shortlisted_at FROM lifecycle_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                duplicate = connection.execute(
                    "SELECT 1 FROM lifecycle_jobs WHERE source_url = ?", (source_url,)
                ).fetchone()
                if duplicate is not None:
                    raise InvalidLifecycleTransition("the canonical listing is already tracked under another job id")
                shortlisted_at = _utc_now()
                connection.execute(
                    "INSERT INTO lifecycle_jobs (job_id, source_url, shortlisted_at) VALUES (?, ?, ?)",
                    (job_id, source_url, shortlisted_at),
                )
                self._insert_event(
                    connection,
                    job_id=job_id,
                    category="lifecycle",
                    name="shortlisted",
                    actor=actor,
                    occurred_at=shortlisted_at,
                )
                row = self._require_known_job(connection, job_id)
            elif str(row["source_url"]) != source_url:
                raise InvalidLifecycleTransition("queue outcome listing identity does not match the tracked job")

            current = self._state_in(connection, job_id)
            if outcome == "submitted":
                submitted = connection.execute(
                    """
                    SELECT 1 FROM lifecycle_events
                    WHERE job_id = ? AND category = 'lifecycle' AND name = 'submitted'
                    """,
                    (job_id,),
                ).fetchone()
                if submitted is None:
                    if current in {"shortlisted", "opened"}:
                        self._insert_event(
                            connection,
                            job_id=job_id,
                            category="lifecycle",
                            name="manual_applying",
                            actor=actor,
                        )
                    elif current != "manual_applying":
                        raise InvalidLifecycleTransition(
                            "confirmed submission conflicts with the existing lifecycle state"
                        )
                    self._insert_event(
                        connection,
                        job_id=job_id,
                        category="lifecycle",
                        name="submitted",
                        actor=actor,
                    )
            elif outcome == "rejected" and current in {"submitted", "interview"}:
                self._insert_event(
                    connection,
                    job_id=job_id,
                    category="lifecycle",
                    name="rejected",
                    actor=actor,
                )

            reconciled_at = _utc_now()
            self._insert_event(
                connection,
                job_id=job_id,
                category="queue_outcome",
                name=outcome,
                actor=actor,
                payload={"queue_id": queue_id, "queue_event_id": queue_event_id},
                occurred_at=reconciled_at,
            )
            connection.execute(
                """
                INSERT INTO lifecycle_queue_reconciliations (
                    queue_id, queue_event_id, job_id, source_url, outcome, actor, reconciled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (queue_id, queue_event_id, job_id, source_url, outcome, actor, reconciled_at),
            )
            connection.commit()
            final_row = self._require_known_job(connection, job_id)
            final_state = self._state_in(connection, job_id)
            return LifecycleJob(
                job_id=job_id,
                source_url=str(final_row["source_url"]),
                state=final_state,
                shortlisted_at=str(final_row["shortlisted_at"]),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            if connection is not self._memory_connection:
                connection.close()

    def get_job(self, job_id: str) -> LifecycleJob:
        job_id = _require_text(job_id, "job_id")
        connection = self._connect()
        try:
            row = self._require_known_job(connection, job_id)
            state = self._state_in(connection, job_id)
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return LifecycleJob(
            job_id=row["job_id"],
            source_url=row["source_url"],
            state=state,
            shortlisted_at=row["shortlisted_at"],
        )

    def transition(self, job_id: str, target: str, *, actor: str) -> LifecycleJob:
        job_id = _require_text(job_id, "job_id")
        target = _require_text(target, "target")
        raw_actor = actor
        actor = _require_actor(actor)
        if target in _CANDIDATE_OWNED_STATES:
            _require_user(raw_actor, target)

        connection = self._transaction()
        try:
            row = self._require_known_job(connection, job_id)
            current = self._state_in(connection, job_id)
            if target not in _ALLOWED_TRANSITIONS[current]:
                raise InvalidLifecycleTransition(f"cannot move {current!r} to {target!r}")
            self._insert_event(
                connection,
                job_id=job_id,
                category="lifecycle",
                name=target,
                actor=actor,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return LifecycleJob(
            job_id=row["job_id"],
            source_url=row["source_url"],
            state=target,  # type: ignore[arg-type]
            shortlisted_at=row["shortlisted_at"],
        )

    def record_tab_event(self, job_id: str, name: str, *, actor: str) -> LifecycleEvent:
        job_id = _require_text(job_id, "job_id")
        name = _require_text(name, "tab event")
        actor = _require_actor(actor)
        if name not in _TAB_EVENTS:
            raise ValueError(f"unknown tab event: {name!r}")
        connection = self._transaction()
        try:
            self._require_known_job(connection, job_id)
            event = self._insert_event(
                connection,
                job_id=job_id,
                category="tab",
                name=name,
                actor=actor,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return event

    def create_round(self, round_id: str, job_ids: Sequence[str], *, actor: str) -> ReviewRound:
        round_id = _require_text(round_id, "round_id")
        actor = _require_actor(actor)
        ordered_job_ids = tuple(dict.fromkeys(_require_text(job_id, "job_id") for job_id in job_ids))
        if not ordered_job_ids:
            raise ValueError("a review round requires at least one job")
        if len(ordered_job_ids) > _ROUND_LIMIT:
            raise RoundLimitExceeded("a review round may contain at most five unique jobs")

        timestamp = _utc_now()
        connection = self._transaction()
        try:
            for job_id in ordered_job_ids:
                self._require_known_job(connection, job_id)
            connection.execute(
                "INSERT INTO lifecycle_rounds (round_id, actor, created_at) VALUES (?, ?, ?)",
                (round_id, actor, timestamp),
            )
            connection.executemany(
                "INSERT INTO lifecycle_round_jobs (round_id, job_id, position) VALUES (?, ?, ?)",
                ((round_id, job_id, position) for position, job_id in enumerate(ordered_job_ids)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return ReviewRound(round_id=round_id, job_ids=ordered_job_ids, actor=actor, created_at=timestamp)

    def record_attention(self, job_id: str, *, kind: str, message: str, actor: str) -> LifecycleEvent:
        job_id = _require_text(job_id, "job_id")
        kind = _require_text(kind, "attention kind")
        message = _require_text(message, "attention message")
        actor = _require_actor(actor)
        return self._record_annotation(job_id, "attention", kind, actor, {"message": message})

    def record_follow_up(self, job_id: str, *, due_at: str, note: str, actor: str) -> LifecycleEvent:
        job_id = _require_text(job_id, "job_id")
        due_at = _require_text(due_at, "due_at")
        note = _require_text(note, "follow-up note")
        actor = _require_actor(actor)
        try:
            due = datetime.fromisoformat(due_at)
        except ValueError as error:
            raise ValueError("due_at must be an ISO 8601 timestamp") from error
        if due.tzinfo is None or due.utcoffset() is None:
            raise ValueError("due_at must include a timezone offset")
        return self._record_annotation(
            job_id,
            "follow_up",
            "scheduled",
            actor,
            {"due_at": due_at, "note": note},
        )

    def _record_annotation(
        self,
        job_id: str,
        category: str,
        name: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> LifecycleEvent:
        connection = self._transaction()
        try:
            self._require_known_job(connection, job_id)
            event = self._insert_event(
                connection,
                job_id=job_id,
                category=category,
                name=name,
                actor=actor,
                payload=payload,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return event

    def events_for(self, job_id: str) -> list[LifecycleEvent]:
        job_id = _require_text(job_id, "job_id")
        connection = self._connect()
        try:
            self._require_known_job(connection, job_id)
            rows = connection.execute(
                """
                SELECT event_id, job_id, category, name, actor, payload_json, occurred_at
                FROM lifecycle_events WHERE job_id = ? ORDER BY event_id
                """,
                (job_id,),
            ).fetchall()
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return [
            LifecycleEvent(
                event_id=row["event_id"],
                job_id=row["job_id"],
                category=row["category"],
                name=row["name"],
                actor=row["actor"],
                payload=json.loads(row["payload_json"]),
                occurred_at=row["occurred_at"],
            )
            for row in rows
        ]

    def unique_manual_submitted_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT job_id) FROM lifecycle_events
                WHERE category = 'lifecycle' AND name = 'submitted' AND actor = 'user'
                """
            ).fetchone()
        finally:
            if connection is not self._memory_connection:
                connection.close()
        return int(row[0])
