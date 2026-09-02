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

from .smart_queue import QueueCandidate, QueueEvent, QueuePolicyError, QueueStorageError, SmartJobQueue
from .sources import canonical_listing_url


_SCHEMA_VERSION = 1
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_memory_schema_versions (
                    version INTEGER PRIMARY KEY CHECK(version > 0),
                    applied_at TEXT NOT NULL
                );

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
                );

                CREATE INDEX IF NOT EXISTS idx_candidate_memory_outcomes_source_url
                    ON candidate_memory_outcomes(source_url);

                CREATE TRIGGER IF NOT EXISTS candidate_memory_schema_versions_no_update
                BEFORE UPDATE ON candidate_memory_schema_versions
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory schema history is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS candidate_memory_schema_versions_no_delete
                BEFORE DELETE ON candidate_memory_schema_versions
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory schema history is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS candidate_memory_outcomes_no_update
                BEFORE UPDATE ON candidate_memory_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory outcomes are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS candidate_memory_outcomes_no_delete
                BEFORE DELETE ON candidate_memory_outcomes
                BEGIN
                    SELECT RAISE(ABORT, 'candidate memory outcomes are append-only');
                END;
                """
            )
            connection.execute("BEGIN IMMEDIATE")
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

    @staticmethod
    def _require_user_vacated(*, actor: str, vacated: bool) -> None:
        if actor != "user":
            raise CandidateMemoryPolicyError("candidate-memory writes require the exact user actor")
        if type(vacated) is not bool or vacated is not True:
            raise CandidateMemoryPolicyError("candidate-memory writes require explicit vacated=True")

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
        """Record one authentic user outcome once and return its reconciliation.

        Replaying the exact source event is a no-op.  A conflicting reuse of a
        queue event identity fails closed rather than overwriting audit data.
        """

        self._require_user_vacated(actor=actor, vacated=vacated)
        queue_event, source_url = self._authenticated_queue_outcome(
            queue=queue,
            event=event,
            actor=actor,
        )
        connection = self._connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT job_id, source_url, outcome, actor, vacated, recorded_at
                FROM candidate_memory_outcomes
                WHERE queue_id = ? AND event_id = ?
                """,
                (queue_event.queue_id, queue_event.event_id),
            ).fetchone()
            if existing is not None:
                expected = (queue_event.job_id, source_url, queue_event.name, actor, 1)
                actual = (
                    str(existing["job_id"]),
                    str(existing["source_url"]),
                    str(existing["outcome"]),
                    str(existing["actor"]),
                    int(existing["vacated"]),
                )
                if actual != expected:
                    raise CandidateMemoryStorageError("candidate memory event identity conflicts with stored facts")
                connection.commit()
                return CandidateMemoryOutcome(
                    queue_id=queue_event.queue_id,
                    event_id=queue_event.event_id,
                    source_url=source_url,
                    outcome=queue_event.name,
                    recorded_at=str(existing["recorded_at"]),
                    inserted=False,
                )

            recorded_at = _utc_now()
            connection.execute(
                """
                INSERT INTO candidate_memory_outcomes (
                    queue_id, event_id, job_id, source_url, outcome, actor, vacated, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    queue_event.queue_id,
                    queue_event.event_id,
                    queue_event.job_id,
                    source_url,
                    queue_event.name,
                    actor,
                    1,
                    recorded_at,
                ),
            )
            connection.commit()
            return CandidateMemoryOutcome(
                queue_id=queue_event.queue_id,
                event_id=queue_event.event_id,
                source_url=source_url,
                outcome=queue_event.name,
                recorded_at=recorded_at,
                inserted=True,
            )
        except CandidateMemoryStorageError:
            connection.rollback()
            raise
        except sqlite3.Error:
            connection.rollback()
            raise CandidateMemoryStorageError("candidate memory storage operation failed") from None
        finally:
            connection.close()

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
    ) -> tuple[QueueCandidate, ...]:
        """Keep prevalidated candidates whose exact listing identity is unseen.

        The input is not normalized, ranked, deduplicated, or otherwise
        modified.  This filter only removes entries with an exact canonical URL
        present in the candidate's durable suppression memory.
        """

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
        connection = self._connect(self.database_path)
        try:
            return tuple(
                candidate
                for candidate in values
                if connection.execute(
                    "SELECT 1 FROM candidate_memory_outcomes WHERE source_url = ? LIMIT 1",
                    (candidate.source_url,),
                ).fetchone()
                is None
            )
        except sqlite3.Error:
            raise CandidateMemoryStorageError("candidate memory storage operation failed") from None
        finally:
            connection.close()
