"""Bounded browser-bridge orchestration for one Smart Job Queue cycle.

This skill-level adapter joins the persistent queue to the two-operation
``BrowserTabAdapter`` protocol. It can observe tab URLs and request one exact
listing URL to open; it cannot inspect pages, close tabs, or take application
or form actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from jobapply_agent.candidate_memory import CandidateMemory
from jobapply_agent.smart_queue import QueueCandidate, QueuePolicyError, SmartJobQueue


_PRIVATE_RUNTIME_DIRECTORY = Path(__file__).resolve().parents[3] / "jobapply_agent" / "private"
_DEFAULT_TARGET_SIZE = 5


@runtime_checkable
class BrowserTabAdapter(Protocol):
    """The two browser operations permitted to the tab-monitor skill."""

    def list_tab_urls(self) -> tuple[str, ...]:
        """Return visible tab URLs without inspecting their content."""

    def open_listing(self, url: str) -> None:
        """Open one exact canonical listing URL."""


class QueueCoordinatorError(RuntimeError):
    """A redacted failure from the untrusted browser bridge."""


@dataclass(frozen=True, slots=True)
class QueueCycle:
    """Redacted public outcome from one bounded reconciliation cycle.

    Browser URLs are private reconciliation inputs. Callers receive only
    counts and opaque queue IDs, never snapshots or URL-bearing actions.
    """

    requested_open_job_ids: tuple[str, ...]
    opened_job_ids: tuple[str, ...]
    open_failed_job_ids: tuple[str, ...]
    search_needed: int


@dataclass(frozen=True, slots=True)
class QueueOutcome:
    """A candidate-confirmed outcome without recommendation or URL data."""

    job_id: str
    state: str


class SmartQueueCoordinator:
    """Coordinate a private live queue through a bounded host tab adapter."""

    def __init__(self, queue: SmartJobQueue, browser: BrowserTabAdapter) -> None:
        if not isinstance(queue, SmartJobQueue):
            raise TypeError("queue must be a SmartJobQueue")
        self._require_private_runtime_database(queue)
        self._require_live_capacity_provenance(queue)
        if not isinstance(browser, BrowserTabAdapter):
            raise TypeError("browser must implement the listing-only BrowserTabAdapter protocol")
        self._queue = queue
        self._browser = browser

    @staticmethod
    def _require_private_runtime_database(queue: SmartJobQueue) -> None:
        """Fail closed if the durable live queue leaves the ignored runtime root."""

        if str(queue.database_path) == ":memory:":
            raise QueueCoordinatorError("live Smart Queue requires a private runtime database")
        try:
            if _PRIVATE_RUNTIME_DIRECTORY.is_symlink():
                raise ValueError("private runtime directory must not be a symlink")
            private_runtime = _PRIVATE_RUNTIME_DIRECTORY.resolve()
            database_path = queue.database_path.resolve()
            database_path.relative_to(private_runtime)
        except (OSError, RuntimeError, ValueError):
            raise QueueCoordinatorError("live Smart Queue requires a private runtime database") from None

    @staticmethod
    def _require_live_capacity_provenance(queue: SmartJobQueue) -> None:
        """Require active-intake proof before a live host may open extra tabs.

        The documented default of five remains compatible for legacy and
        test-only queues. Any other live capacity must be durably bound to the
        integrity-checked active candidate intake by the discover factory.
        """

        if queue.target_size != _DEFAULT_TARGET_SIZE and not queue.has_active_intake_capacity_provenance:
            raise QueueCoordinatorError(
                "non-default live Smart Queue capacity requires active candidate intake provenance"
            )

    def _snapshot(self) -> tuple[str, ...]:
        try:
            return tuple(self._browser.list_tab_urls())
        except Exception:
            raise QueueCoordinatorError("browser tab snapshot failed") from None

    def cycle(self, recommendations: Iterable[QueueCandidate] = ()) -> QueueCycle:
        """Run snapshot → plan → optional recommendations → open → snapshot.

        A missing managed tab becomes ``released`` through its append-only
        visibility event. It is never reopened; the same cycle may fill the
        resulting vacancy only from distinct, already-admitted recommendations.
        A later user-confirmed outcome remains required before CandidateMemory
        can record an application outcome.
        """

        if isinstance(recommendations, (str, bytes)):
            raise QueuePolicyError("recommendations must be an iterable of QueueCandidate values")
        try:
            supplied_recommendations = tuple(recommendations)
        except TypeError:
            raise QueuePolicyError("recommendations must be an iterable of QueueCandidate values") from None
        if supplied_recommendations:
            self._queue.add_recommendations(supplied_recommendations)

        initial_snapshot = self._snapshot()
        recovered_open_failed_job_ids = self._queue.record_visible_snapshot(
            initial_snapshot,
            actor="browser-bridge",
            recover_stale_waiting=True,
        )
        requested_action = self._queue.plan_refill(open_urls=initial_snapshot)

        attempted_job_ids: list[str] = []
        for job_id, url in zip(requested_action.job_ids, requested_action.urls_to_open, strict=True):
            attempted_job_ids.append(job_id)
            try:
                self._browser.open_listing(url)
            except Exception:
                # A bridge can open the tab before timing out. Wait for a
                # successful URL-only snapshot before classifying this slot.
                pass

        follow_up_snapshot = self._snapshot()
        self._queue.record_visible_snapshot(follow_up_snapshot, actor="browser-bridge")
        open_failed_job_ids = list(recovered_open_failed_job_ids)
        for job_id in attempted_job_ids:
            if self._queue.get(job_id).state == "waiting":
                self._queue.record_open_failure(job_id, actor="browser-bridge")
                open_failed_job_ids.append(job_id)
        opened_job_ids = tuple(
            job_id
            for job_id in attempted_job_ids
            if self._queue.get(job_id).state == "open"
        )

        return QueueCycle(
            requested_open_job_ids=requested_action.job_ids,
            opened_job_ids=opened_job_ids,
            open_failed_job_ids=tuple(dict.fromkeys(open_failed_job_ids)),
            search_needed=self._queue.refill_search_needed(open_urls=follow_up_snapshot),
        )

    def confirm_outcome(
        self,
        job_id: str,
        outcome: str,
        *,
        actor: str,
        vacated: bool,
        candidate_memory: CandidateMemory,
    ) -> QueueOutcome:
        """Atomically finalize an explicit candidate outcome/vacancy attestation."""

        if type(actor) is not str or actor != "user":
            raise QueuePolicyError("an application outcome must be confirmed by the exact user actor")
        if type(vacated) is not bool or vacated is not True:
            raise QueuePolicyError("an application outcome requires explicit vacated=True confirmation")
        if not isinstance(candidate_memory, CandidateMemory):
            raise TypeError("candidate_memory must be a CandidateMemory")
        finalized = candidate_memory.finalize_queue_outcome(
            queue=self._queue,
            job_id=job_id,
            outcome=outcome,
            actor=actor,
            vacated=vacated,
        )
        return QueueOutcome(job_id=job_id, state=finalized.outcome)


__all__ = [
    "BrowserTabAdapter",
    "QueueCoordinatorError",
    "QueueCycle",
    "QueueOutcome",
    "SmartQueueCoordinator",
]
