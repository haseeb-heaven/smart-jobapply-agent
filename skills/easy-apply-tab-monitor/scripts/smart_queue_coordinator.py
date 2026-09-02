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

from jobapply_agent.smart_queue import QueueCandidate, QueuePolicyError, SmartJobQueue


_PRIVATE_RUNTIME_DIRECTORY = Path(__file__).resolve().parents[3] / "jobapply_agent" / "private"


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

    def _snapshot(self) -> tuple[str, ...]:
        try:
            return tuple(self._browser.list_tab_urls())
        except Exception:
            raise QueueCoordinatorError("browser tab snapshot failed") from None

    def cycle(self, recommendations: Iterable[QueueCandidate] = ()) -> QueueCycle:
        """Run snapshot → plan → optional recommendations → open → snapshot.

        A missing managed tab stays ``awaiting_outcome``. It is never reopened;
        a later user-confirmed outcome and a genuinely absent URL are both
        required before the queue plans a replacement.
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
        self._queue.record_visible_snapshot(initial_snapshot, actor="browser-bridge")
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
        open_failed_job_ids: list[str] = []
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
            search_needed=requested_action.search_needed,
        )

    def confirm_outcome(self, job_id: str, outcome: str, *, actor: str) -> QueueOutcome:
        """Record a candidate-owned outcome and return its opaque public view."""

        confirmed = self._queue.confirm_outcome(job_id, outcome, actor=actor)
        return QueueOutcome(job_id=confirmed.job_id, state=confirmed.state)


__all__ = [
    "BrowserTabAdapter",
    "QueueCoordinatorError",
    "QueueCycle",
    "QueueOutcome",
    "SmartQueueCoordinator",
]
