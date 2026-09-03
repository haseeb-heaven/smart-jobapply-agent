"""Private, append-only suppression memory for candidate-confirmed queue outcomes.

The memory stores only canonical listing identities and authenticated Smart Job
Queue outcome events.  It has no browser, network, form, or application-action
capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterable
import uuid

from .smart_queue import QueueCandidate, QueueEvent, QueuePolicyError, QueueStorageError, SmartJobQueue
from .sources import canonical_listing_url


_SCHEMA_VERSION = 2
_OUTCOMES = frozenset({"submitted", "rejected", "skipped"})


class CandidateMemoryPolicyError(ValueError):
    """Raised when a candidate-memory input violates the local-only contract."""


class CandidateMemoryStorageError(RuntimeError):
    """Raised for redacted local candidate-memory persistence failures."""


@dataclass(frozen=True, slots=True)
class CandidateMemoryOutcome:
    """One immutable queue outcome reconciliation stored in candidate memory."""

    queue_id: str
    event_id: int
    source_url: str
    outcome: str
    recorded_at: str
    inserted: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _default_private_root() -> Path:
    return Path(__file__).resolve().parents[2] / "private"


def private_database_path(
    database_path: Path | str,
    *,
    private_root: Path | str | None = None,
) -> Path:
    """Resolve one SQLite path inside the explicit candidate-private root.

    Resolving both paths makes an existing symlink escape fail closed.  The
    caller owns creation of the root and database; this helper never accepts a
    repository, home-directory, or arbitrary temporary location by default.
    Tests may provide an isolated ``private_root``.
    """

    if not isinstance(database_path, (Path, str)) or not str(database_path).strip():
        raise CandidateMemoryPolicyError("database_path must be a non-empty path")
    root_value = _default_private_root() if private_root is None else private_root
    if not isinstance(root_value, (Path, str)) or not str(root_value).strip():
        raise CandidateMemoryPolicyError("private_root must be a non-empty path")
    raw_root = Path(root_value)
    if raw_root.is_symlink():
        raise CandidateMemoryPolicyError("private_root must not be a symlink")
    root = raw_root.resolve(strict=False)
    candidate = Path(database_path).resolve(strict=False)
    if candidate == root or not candidate.is_relative_to(root):
        raise CandidateMemoryPolicyError("database_path must resolve inside the private runtime directory")
    return candidate


class CandidateMemory:
    """SQLite-backed, candidate-scoped suppression memory.

    One private database belongs to one candidate scope.  Rows are keyed by an
    authenticated ``(queue_id, event_id)`` pair and suppress only the exact
    canonical listing URL.  Similar titles, companies, and cross-board jobs are
    intentionally never compared or merged.
    """

    def __init__(self, database_path: Path | str, *, private_root: Path | str | None = None):
        root_value = _default_private_root() if private_root is None else private_root
        self.database_path = private_database_path(database_path, private_root=root_value)
        self.private_root = Path(root_value).resolve(strict=False)
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error, CandidateMemoryStorageError):
            raise CandidateMemoryStorageError("candidate memory storage initialization failed") from None

    @staticmethod
    def _connect(database_path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._preflight_unscoped_legacy_outcomes(connection)
            statements = (
                """
                CREATE TABLE IF NOT EXISTS candidate_memory_schema_versions (
                    version INTEGER PRIMARY KEY CHECK(version > 0),
                    applied_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS candidate_memory_outcomes (
                    queue_id TEXT NOT NULL CHECK(length(queue_id) = 32),
                    event_id INTEGER NOT NULL CHECK(event_id > 0),
                    job_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('submitted', 'rejected', 'skipped')),
                    actor TEXT NOT NULL CHECK(actor = 'user'),
                    vacated INTEGER NOT NULL CHECK(vacated = 1),
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (queue_id, event_id)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS candidate_memory_queue_scope (
                    scope_id INTEGER PRIMARY KEY CHECK(scope_id = 1),
                    queue_id TEXT NOT NULL CHECK(length(queue_id) = 32)
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS idx_candidate_memory_outcomes_source_url
                    ON candidate_memory_outcomes(source_url)
                """,
                """
                CREATE TRIGGER IF NOT EXISTS candidate_memory_schema_versions_no_update
                BEFORE UPDATE ON candidate_memory_schema_versions
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory schema history is append-only');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS candidate_memory_schema_versions_no_delete
                BEFORE DELETE ON candidate_memory_schema_versions
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory schema history is append-only');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS candidate_memory_outcomes_no_update
                BEFORE UPDATE ON candidate_memory_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory outcomes are append-only');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS candidate_memory_outcomes_no_delete
                BEFORE DELETE ON candidate_memory_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory outcomes are append-only');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS candidate_memory_queue_scope_no_update
                BEFORE UPDATE ON candidate_memory_queue_scope
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory queue scope is immutable');
                END
                """,
                """
                CREATE TRIGGER IF NOT EXISTS candidate_memory_queue_scope_no_delete
                BEFORE DELETE ON candidate_memory_queue_scope
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory queue scope is immutable');
                END
                """,
            )
            for statement in statements:
                connection.execute(statement)
            versions = [
                int(row["version"])
                for row in connection.execute(
                    "SELECT version FROM candidate_memory_schema_versions ORDER BY version"
                )
            ]
            if versions and versions[-1] > _SCHEMA_VERSION:
                raise CandidateMemoryStorageError("candidate memory schema version is unsupported")
            for version in range((versions[-1] if versions else 0) + 1, _SCHEMA_VERSION + 1):
                connection.execute(
                    "INSERT INTO candidate_memory_schema_versions (version, applied_at) VALUES (?, ?)",
                    (version, _utc_now()),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @classmethod
    def _preflight_unscoped_legacy_outcomes(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Reject populated legacy memory before migration mutates its schema."""

        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "candidate_memory_outcomes" not in tables:
            return
        if connection.execute(
            "SELECT 1 FROM candidate_memory_outcomes LIMIT 1"
        ).fetchone() is None:
            return
        if "candidate_memory_queue_scope" not in tables:
            raise CandidateMemoryStorageError("candidate memory queue scope is unavailable")
        scope_rows = connection.execute(
            "SELECT scope_id, queue_id FROM candidate_memory_queue_scope"
        ).fetchall()
        if len(scope_rows) != 1 or scope_rows[0]["scope_id"] != 1:
            raise CandidateMemoryStorageError("candidate memory queue scope is invalid")
        scope_queue_id = cls._require_queue_id(scope_rows[0]["queue_id"])
        outcome_queue_rows = connection.execute(
            "SELECT DISTINCT queue_id FROM candidate_memory_outcomes LIMIT 2"
        ).fetchall()
        if len(outcome_queue_rows) != 1:
            raise CandidateMemoryStorageError("candidate memory outcome queue scope is invalid")
        outcome_queue_id = cls._require_queue_id(outcome_queue_rows[0]["queue_id"])
        if outcome_queue_id != scope_queue_id:
            raise CandidateMemoryStorageError("candidate memory outcome queue scope is invalid")

    @staticmethod
    def _require_user_vacated(*, actor: str, vacated: bool) -> None:
        if type(actor) is not str or actor != "user":
            raise CandidateMemoryPolicyError("candidate-memory writes require the exact user actor")
        if type(vacated) is not bool or vacated is not True:
            raise CandidateMemoryPolicyError("candidate-memory writes require explicit vacated=True")

    @staticmethod
    def _require_queue_id(value: object) -> str:
        if type(value) is not str:
            raise CandidateMemoryStorageError("candidate memory queue scope is invalid")
        try:
            normalized = uuid.UUID(value).hex
        except (AttributeError, ValueError):
            raise CandidateMemoryStorageError("candidate memory queue scope is invalid") from None
        if value != normalized:
            raise CandidateMemoryStorageError("candidate memory queue scope is invalid")
        return normalized

    @classmethod
    def _bind_or_validate_queue_scope(
        cls,
        connection: sqlite3.Connection,
        queue_id: str,
    ) -> None:
        """Bind an empty memory once, or require its immutable queue scope."""

        queue_id = cls._require_queue_id(queue_id)
        row = connection.execute(
            "SELECT queue_id FROM candidate_memory_queue_scope WHERE scope_id = 1"
        ).fetchone()
        if row is None:
            legacy_outcome = connection.execute(
                "SELECT 1 FROM candidate_memory_outcomes LIMIT 1"
            ).fetchone()
            if legacy_outcome is not None:
                raise CandidateMemoryStorageError(
                    "candidate memory queue scope is unavailable"
                )
            connection.execute(
                """
                INSERT INTO candidate_memory_queue_scope (scope_id, queue_id)
                VALUES (1, ?)
                """,
                (queue_id,),
            )
            return

        try:
            stored_queue_id = cls._require_queue_id(row["queue_id"])
        except CandidateMemoryStorageError:
            raise
        if stored_queue_id != queue_id:
            raise CandidateMemoryPolicyError(
                "candidate memory queue scope does not match the authenticated queue"
            )

    @staticmethod
    def _authenticated_queue_outcome(
        *,
        queue: SmartJobQueue,
        event: QueueEvent,
        actor: str,
    ) -> tuple[QueueEvent, str]:
        if not isinstance(queue, SmartJobQueue) or not isinstance(event, QueueEvent):
            raise CandidateMemoryPolicyError("an authenticated SmartJobQueue outcome event is required")
        if event.queue_id != queue.queue_id or event.actor != actor or event.name not in _OUTCOMES:
            raise CandidateMemoryPolicyError("queue outcome event is not an authenticated user outcome")
        try:
            authentic_event = next(
                (
                    candidate
                    for candidate in queue.confirmed_outcome_events(after_event_id=event.event_id - 1)
                    if candidate.event_id == event.event_id
                ),
                None,
            )
            queue_job = queue.get(event.job_id)
        except (KeyError, QueuePolicyError, QueueStorageError):
            raise CandidateMemoryPolicyError("queue outcome event is not an authenticated queue record") from None
        if authentic_event != event or queue_job.source_url != canonical_listing_url(queue_job.source_url):
            raise CandidateMemoryPolicyError("queue outcome event facts do not match the authenticated queue record")
        return authentic_event, queue_job.source_url

    def reconcile_queue_outcome(
        self,
        *,
        queue: SmartJobQueue,
        event: QueueEvent,
        actor: str,
        vacated: bool,
    ) -> CandidateMemoryOutcome:
        """Reject the legacy non-atomic replay path without writing memory.

        The queue event has already committed before this method can observe
        it. Persisting matching suppression data here would therefore use two
        commit boundaries. Call :meth:`finalize_queue_outcome` instead.
        """

        del queue, event, actor, vacated
        raise CandidateMemoryPolicyError(
            "candidate-memory outcomes must be finalized atomically with the SmartJobQueue"
        )

    def finalize_queue_outcome(
        self,
        *,
        queue: SmartJobQueue,
        job_id: str,
        outcome: str,
        actor: str,
        vacated: bool,
    ) -> CandidateMemoryOutcome:
        """Atomically finalize one candidate-confirmed queue outcome.

        Memory initialization precedes the queue transition.  The queue then
        attaches this already-private database and commits its append-only
        outcome event and this suppression record as one transaction.
        """

        self._require_user_vacated(actor=actor, vacated=vacated)
        if not isinstance(queue, SmartJobQueue):
            raise CandidateMemoryPolicyError("an authenticated SmartJobQueue is required")
        try:
            event, source_url, recorded_at, inserted = queue._confirm_outcome_with_candidate_memory(
                job_id,
                outcome,
                actor=actor,
                vacated=vacated,
                memory_database=self.database_path,
            )
        except QueueStorageError:
            raise CandidateMemoryStorageError("candidate memory storage operation failed") from None
        return CandidateMemoryOutcome(
            queue_id=event.queue_id,
            event_id=event.event_id,
            source_url=source_url,
            outcome=event.name,
            recorded_at=recorded_at,
            inserted=inserted,
        )

    def is_suppressed(self, source_url: str) -> bool:
        """Return whether an exact canonical listing URL has a user outcome."""

        try:
            canonical_url = canonical_listing_url(source_url)
        except ValueError:
            raise CandidateMemoryPolicyError("source_url must be a canonical LinkedIn or Indeed listing URL") from None
        connection = self._connect(self.database_path)
        try:
            return connection.execute(
                "SELECT 1 FROM candidate_memory_outcomes WHERE source_url = ? LIMIT 1",
                (canonical_url,),
            ).fetchone() is not None
        except sqlite3.Error:
            raise CandidateMemoryStorageError("candidate memory storage operation failed") from None
        finally:
            connection.close()

    def filter_unsuppressed_candidates(
        self,
        candidates: Iterable[QueueCandidate],
        *,
        queue: SmartJobQueue,
    ) -> tuple[QueueCandidate, ...]:
        """Keep prevalidated candidates whose exact listing identity is unseen.

        The input is not normalized, ranked, deduplicated, or otherwise
        modified.  This filter only removes entries with an exact canonical URL
        present in the candidate's durable suppression memory.
        """

        if not isinstance(queue, SmartJobQueue):
            raise CandidateMemoryPolicyError("an authenticated SmartJobQueue is required")
        try:
            authenticated_queue_id, active_revisions = queue._candidate_memory_scope()
            queue_id = self._require_queue_id(authenticated_queue_id)
        except (QueuePolicyError, QueueStorageError):
            raise CandidateMemoryPolicyError("an authenticated SmartJobQueue is required") from None
        if isinstance(candidates, (str, bytes)):
            raise CandidateMemoryPolicyError("candidates must be QueueCandidate values")
        try:
            values = tuple(candidates)
        except TypeError:
            raise CandidateMemoryPolicyError("candidates must be an iterable of QueueCandidate values") from None
        if any(not isinstance(candidate, QueueCandidate) for candidate in values):
            raise CandidateMemoryPolicyError("candidates must contain only QueueCandidate values")
        if not values:
            return ()
        if active_revisions == (None, None):
            raise CandidateMemoryPolicyError("authenticated queue revisions are unavailable")
        if any(
            (candidate.profile_revision, candidate.matcher_policy_revision) != active_revisions
            for candidate in values
        ):
            raise CandidateMemoryPolicyError(
                "candidate batch revisions do not match the authenticated queue"
            )
        connection = self._connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._bind_or_validate_queue_scope(connection, queue_id)
            unsuppressed = tuple(
                candidate
                for candidate in values
                if connection.execute(
                    "SELECT 1 FROM candidate_memory_outcomes WHERE source_url = ? LIMIT 1",
                    (candidate.source_url,),
                ).fetchone()
                is None
            )
            connection.commit()
            return unsuppressed
        except (CandidateMemoryPolicyError, CandidateMemoryStorageError):
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise CandidateMemoryStorageError("candidate memory storage operation failed") from None
        finally:
            connection.close()

    def discard_outcome_empty_queue_scope(self) -> None:
        """Roll back a newly bound queue scope while outcomes stay empty.

        This is the rollback half of scope binding during admission.  It is a
        no-op when no scope row exists and fails closed without mutating
        anything once any outcome has been recorded.
        """

        connection = self._connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            scope_row = connection.execute(
                "SELECT queue_id FROM candidate_memory_queue_scope WHERE scope_id = 1"
            ).fetchone()
            if scope_row is None:
                connection.commit()
                return
            has_outcome = connection.execute(
                "SELECT 1 FROM candidate_memory_outcomes LIMIT 1"
            ).fetchone()
            if has_outcome is not None:
                raise CandidateMemoryPolicyError(
                    "candidate memory queue scope is discarded only while outcomes are empty"
                )
            # The no-delete trigger keeps the scope immutable for normal
            # traffic, so this rollback path drops and recreates it inside
            # the same transaction as the single scope-row deletion.
            connection.execute(
                "DROP TRIGGER IF EXISTS candidate_memory_queue_scope_no_delete"
            )
            connection.execute("DELETE FROM candidate_memory_queue_scope WHERE scope_id = 1")
            connection.execute(
                """
                CREATE TRIGGER candidate_memory_queue_scope_no_delete
                BEFORE DELETE ON candidate_memory_queue_scope
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory queue scope is immutable');
                END
                """
            )
            connection.commit()
        except CandidateMemoryPolicyError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise CandidateMemoryStorageError("candidate memory storage operation failed") from None
        finally:
            connection.close()
