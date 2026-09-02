"""Bounded browser-bridge orchestration for one Smart Job Queue cycle.

This skill-level adapter joins the persistent queue to the two-operation
``BrowserTabAdapter`` protocol. It can observe tab URLs and request one exact
listing URL to open; it cannot inspect pages, close tabs, or take application
or form actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from jobapply_agent.smart_queue import QueueAction, QueueCandidate, QueueJob, SmartJobQueue


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
    """URL-only observations and data-only actions from one bounded cycle."""

    initial_snapshot: tuple[str, ...]
    follow_up_snapshot: tuple[str, ...]
    initial_action: QueueAction
    refill_action: QueueAction
    requested_action: QueueAction
    opened_job_ids: tuple[str, ...]
    open_failed_job_ids: tuple[str, ...]


class SmartQueueCoordinator:
    """Coordinate queue state with a listing-only browser tab adapter."""

    def __init__(self, queue: SmartJobQueue, browser: BrowserTabAdapter) -> None:
        if not isinstance(queue, SmartJobQueue):
            raise TypeError("queue must be a SmartJobQueue")
        if not isinstance(browser, BrowserTabAdapter):
            raise TypeError("browser must implement the listing-only BrowserTabAdapter protocol")
        self._queue = queue
        self._browser = browser

    def _snapshot(self) -> tuple[str, ...]:
        try:
            return tuple(self._browser.list_tab_urls())
        except Exception:
            raise QueueCoordinatorError("browser tab snapshot failed") from None

    @staticmethod
    def _combine_actions(initial: QueueAction, refill: QueueAction) -> QueueAction:
        return QueueAction(
            job_ids=(*initial.job_ids, *refill.job_ids),
            urls_to_open=(*initial.urls_to_open, *refill.urls_to_open),
            search_needed=refill.search_needed,
        )

    def cycle(self, recommendations: Iterable[QueueCandidate] = ()) -> QueueCycle:
        """Run snapshot → plan → optional recommendations → open → snapshot.

        A missing managed tab stays ``awaiting_outcome``. It is never reopened;
        a later user-confirmed outcome and a genuinely absent URL are both
        required before the queue plans a replacement.
        """

        initial_snapshot = self._snapshot()
        self._queue.record_visible_snapshot(initial_snapshot, actor="browser-bridge")
        initial_action = self._queue.plan_refill(open_urls=initial_snapshot)

        supplied_recommendations = tuple(recommendations)
        if supplied_recommendations:
            self._queue.add_recommendations(supplied_recommendations)
            refill_action = self._queue.plan_refill(open_urls=initial_snapshot)
            requested_action = self._combine_actions(initial_action, refill_action)
        else:
            refill_action = initial_action
            requested_action = initial_action

        opened_job_ids: list[str] = []
        open_failed_job_ids: list[str] = []
        for job_id, url in zip(requested_action.job_ids, requested_action.urls_to_open, strict=True):
            try:
                self._browser.open_listing(url)
            except Exception:
                self._queue.record_open_failure(job_id, actor="browser-bridge")
                open_failed_job_ids.append(job_id)
            else:
                opened_job_ids.append(job_id)

        try:
            follow_up_snapshot = self._snapshot()
        except QueueCoordinatorError:
            self._release_unverified_open_reservations(opened_job_ids, open_failed_job_ids)
            raise

        self._queue.record_visible_snapshot(follow_up_snapshot, actor="browser-bridge")
        self._release_unverified_open_reservations(opened_job_ids, open_failed_job_ids)

        return QueueCycle(
            initial_snapshot=initial_snapshot,
            follow_up_snapshot=follow_up_snapshot,
            initial_action=initial_action,
            refill_action=refill_action,
            requested_action=requested_action,
            opened_job_ids=tuple(opened_job_ids),
            open_failed_job_ids=tuple(open_failed_job_ids),
        )

    def _release_unverified_open_reservations(
        self, opened_job_ids: Iterable[str], open_failed_job_ids: list[str]
    ) -> None:
        for job_id in opened_job_ids:
            if self._queue.get(job_id).state == "waiting":
                self._queue.record_open_failure(job_id, actor="browser-bridge")
                open_failed_job_ids.append(job_id)

    def confirm_outcome(self, job_id: str, outcome: str, *, actor: str) -> QueueJob:
        """Forward only an explicit candidate-owned queue outcome."""

        return self._queue.confirm_outcome(job_id, outcome, actor=actor)


__all__ = ["BrowserTabAdapter", "QueueCoordinatorError", "QueueCycle", "SmartQueueCoordinator"]
