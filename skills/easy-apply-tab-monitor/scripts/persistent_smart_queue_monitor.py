"""Persistent, cancellable scheduling around the bounded Smart Queue coordinator.

This module deliberately owns neither a browser nor a browser adapter.  It
calls :class:`SmartQueueCoordinator`, whose only browser authority is to list
tab URLs and open exact approved listing URLs through a supplied bounded
adapter.  Public monitor results contain counts and opaque queue IDs only.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import importlib.util
import inspect
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Callable, Iterable, Iterator, Protocol, runtime_checkable
import uuid

from jobapply_agent.smart_queue import QueueCandidate


def _load_coordinator_module():
    """Load the sibling module when this file is imported by path in tests."""

    existing = sys.modules.get("smart_queue_coordinator")
    if existing is not None:
        return existing
    module_path = Path(__file__).with_name("smart_queue_coordinator.py")
    spec = importlib.util.spec_from_file_location("smart_queue_coordinator", module_path)
    if spec is None or spec.loader is None:
        raise ImportError("Smart Queue coordinator is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


try:
    from smart_queue_coordinator import QueueCoordinatorError, SmartQueueCoordinator
except ModuleNotFoundError:  # pragma: no cover - exercised by path-based hosts.
    _coordinator_module = _load_coordinator_module()
    QueueCoordinatorError = _coordinator_module.QueueCoordinatorError
    SmartQueueCoordinator = _coordinator_module.SmartQueueCoordinator


DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_MAX_BACKOFF_SECONDS = 300.0


class MonitorLeaseError(RuntimeError):
    """Raised when another monitor owns the database-scoped lease."""


class CandidateProviderError(RuntimeError):
    """Raised when the host candidate provider cannot safely supply a batch."""


@runtime_checkable
class CandidateProvider(Protocol):
    """Host-owned source of already-validated candidates when a search is needed."""

    def __call__(self, search_needed: int) -> Iterable[QueueCandidate]:
        """Return candidates for at most the requested number of vacant slots."""


@runtime_checkable
class CancellationSignal(Protocol):
    """Small cancellation seam compatible with ``threading.Event``."""

    def is_set(self) -> bool:
        """Return whether the persistent loop should stop."""


@runtime_checkable
class MonitorLease(Protocol):
    """The minimal exclusive-lease seam used by the monitor."""

    def acquire(self) -> bool | None:
        """Acquire the lease, returning ``False`` only when it is unavailable."""

    def release(self) -> None:
        """Release a lease acquired by this monitor."""


@dataclass(frozen=True, slots=True)
class MonitorTick:
    """Redacted public result from one persistent monitor iteration.

    IDs are queue-issued opaque tokens.  No browser snapshots, URLs, candidate
    evidence, or application/outcome state is exposed here.
    """

    requested_open_job_ids: tuple[str, ...]
    opened_job_ids: tuple[str, ...]
    open_failed_job_ids: tuple[str, ...]
    search_needed: int
    candidate_provider_called: bool
    bridge_error: bool
    candidate_provider_error: bool
    degraded: bool
    consecutive_failures: int
    consecutive_bridge_errors: int
    next_delay_seconds: float

    @property
    def consecutive_transient_failures(self) -> int:
        """Return the generic transient-failure count used for backoff."""

        return self.consecutive_failures

    @property
    def requested_open_count(self) -> int:
        """Number of exact approved listing opens requested this iteration."""

        return len(self.requested_open_job_ids)

    @property
    def opened_count(self) -> int:
        """Number of requested listings confirmed open by a later URL snapshot."""

        return len(self.opened_job_ids)

    @property
    def open_failed_count(self) -> int:
        """Number of waiting reservations released after a confirmed failed open."""

        return len(self.open_failed_job_ids)


class DatabaseLease:
    """Fail-closed, process-safe exclusive lease for one queue database.

    The lock is a sibling file in the queue's private runtime directory.  It is
    intentionally never stolen: an orphaned lock requires an explicit local
    operator decision instead of risking two monitors acting on one queue.
    """

    def __init__(self, database_path: Path | str) -> None:
        path = Path(database_path)
        if str(path) == ":memory:":
            raise MonitorLeaseError("persistent monitoring requires a durable queue database")
        self._path = path.with_name(f"{path.name}.persistent-smart-queue-monitor.lock")
        self._descriptor: int | None = None
        self._identity: tuple[int, int] | None = None
        self._token: str | None = None

    @property
    def path(self) -> Path:
        """Return the private lock path without reading or exposing its contents."""

        return self._path

    @property
    def held(self) -> bool:
        return self._descriptor is not None

    def acquire(self) -> None:
        """Acquire the lease or fail without replacing a pre-existing lock."""

        if self.held:
            return
        try:
            descriptor = os.open(
                self._path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            raise MonitorLeaseError("another persistent monitor already owns this queue database") from None
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                raise MonitorLeaseError("persistent monitor lease is unavailable") from None
            raise MonitorLeaseError("persistent monitor lease could not be acquired") from None

        token = uuid.uuid4().hex
        try:
            os.write(descriptor, f"pid={os.getpid()} token={token}\n".encode("ascii"))
            identity = os.fstat(descriptor)
        except OSError:
            os.close(descriptor)
            try:
                self._path.unlink()
            except OSError:
                pass
            raise MonitorLeaseError("persistent monitor lease could not be initialized") from None

        self._descriptor = descriptor
        self._identity = (identity.st_dev, identity.st_ino)
        self._token = token

    def release(self) -> None:
        """Release only the exact lock file this lease created."""

        descriptor, identity = self._descriptor, self._identity
        self._descriptor = None
        self._identity = None
        self._token = None
        if descriptor is None:
            return
        try:
            if identity is not None:
                try:
                    current = self._path.stat()
                except FileNotFoundError:
                    current = None
                if current is not None and (current.st_dev, current.st_ino) == identity:
                    self._path.unlink()
        finally:
            os.close(descriptor)

    def __enter__(self) -> DatabaseLease:
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


class PersistentSmartQueueMonitor:
    """Run a Smart Queue reconciliation loop without browser-session authority.

    The host supplies an initialized coordinator and, optionally, a candidate
    provider.  The provider is called only after a URL-only coordinator cycle
    reports a positive ``search_needed`` count.
    """

    def __init__(
        self,
        coordinator: SmartQueueCoordinator,
        *,
        candidate_provider: CandidateProvider | None = None,
        lease: MonitorLease | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not callable(getattr(coordinator, "cycle", None)):
            raise TypeError("coordinator must provide a cycle method")
        if candidate_provider is not None and not isinstance(candidate_provider, CandidateProvider):
            raise TypeError("candidate_provider must be callable")
        self._interval_seconds = self._require_duration(interval_seconds, "interval_seconds")
        self._max_backoff_seconds = self._require_duration(max_backoff_seconds, "max_backoff_seconds")
        if self._max_backoff_seconds < self._interval_seconds:
            raise ValueError("max_backoff_seconds must be at least interval_seconds")
        if not callable(sleeper):
            raise TypeError("sleeper must be callable")

        if lease is None:
            database_path = getattr(getattr(coordinator, "_queue", None), "database_path", None)
            if not isinstance(database_path, Path):
                raise TypeError("coordinator must expose a durable queue database")
            lease = DatabaseLease(database_path)
        if not isinstance(lease, MonitorLease):
            raise TypeError("lease must provide acquire and release methods")
        self._coordinator = coordinator
        self._candidate_provider = candidate_provider
        self._lease = lease
        self._sleeper = sleeper
        self._consecutive_failures = 0
        self._consecutive_bridge_errors = 0
        self._operation_lock = threading.RLock()
        self._lease_lock = threading.RLock()
        self._lease_depth = 0

    @staticmethod
    def _require_duration(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a positive finite number")
        duration = float(value)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"{name} must be a positive finite number")
        return duration

    @property
    def lease_path(self) -> Path:
        """Expose the lease location for local operational diagnostics only."""

        path = getattr(self._lease, "path", None)
        if not isinstance(path, Path):
            raise MonitorLeaseError("the supplied lease does not expose a path")
        return path

    @contextmanager
    def lease(self) -> Iterator[bool]:
        """Hold the exclusive queue lease across one or more monitor ticks."""

        acquired = True
        with self._lease_lock:
            if self._lease_depth == 0:
                acquired = self._lease.acquire() is not False
            if acquired:
                self._lease_depth += 1
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            with self._lease_lock:
                self._lease_depth -= 1
                if self._lease_depth == 0:
                    self._lease.release()

    def _next_delay(self) -> float:
        multiplier = 2 ** max(0, self._consecutive_failures - 1)
        return min(self._max_backoff_seconds, self._interval_seconds * multiplier)

    def _degraded_failure(
        self,
        *,
        bridge_error: bool,
        candidate_provider_error: bool,
        search_needed: int = 0,
        candidate_provider_called: bool = False,
        cycles: Iterable[object] = (),
    ) -> MonitorTick:
        """Report a redacted transient failure without inventing its source."""

        self._consecutive_failures += 1
        if bridge_error:
            self._consecutive_bridge_errors += 1
        else:
            # A provider failure still needs generic retry backoff, but it is
            # not evidence that the bounded browser bridge remains unhealthy.
            self._consecutive_bridge_errors = 0
        return MonitorTick(
            requested_open_job_ids=self._operation_ids(cycles, "requested_open_job_ids"),
            opened_job_ids=self._operation_ids(cycles, "opened_job_ids"),
            open_failed_job_ids=self._operation_ids(cycles, "open_failed_job_ids"),
            search_needed=search_needed,
            candidate_provider_called=candidate_provider_called,
            bridge_error=bridge_error,
            candidate_provider_error=candidate_provider_error,
            degraded=True,
            consecutive_failures=self._consecutive_failures,
            consecutive_bridge_errors=self._consecutive_bridge_errors,
            next_delay_seconds=self._next_delay(),
        )

    @staticmethod
    def _operation_ids(cycles: Iterable[object], field: str) -> tuple[str, ...]:
        """Collect each redacted operation ID once, in cycle order."""

        operation_ids: list[str] = []
        seen: set[str] = set()
        for cycle in cycles:
            for job_id in getattr(cycle, field):
                if job_id not in seen:
                    seen.add(job_id)
                    operation_ids.append(job_id)
        return tuple(operation_ids)

    @classmethod
    def _public_tick(
        cls,
        *cycles: object,
        provider_called: bool,
        next_delay_seconds: float,
    ) -> MonitorTick:
        """Convert one or more already-redacted cycles into monitor data."""

        if not cycles:
            raise ValueError("at least one coordinator cycle is required")
        final_cycle = cycles[-1]

        return MonitorTick(
            requested_open_job_ids=cls._operation_ids(cycles, "requested_open_job_ids"),
            opened_job_ids=cls._operation_ids(cycles, "opened_job_ids"),
            open_failed_job_ids=cls._operation_ids(cycles, "open_failed_job_ids"),
            search_needed=final_cycle.search_needed,
            candidate_provider_called=provider_called,
            bridge_error=False,
            candidate_provider_error=False,
            degraded=False,
            consecutive_failures=0,
            consecutive_bridge_errors=0,
            next_delay_seconds=next_delay_seconds,
        )

    def _candidates_for(self, search_needed: int) -> tuple[QueueCandidate, ...]:
        if self._candidate_provider is None:
            return ()
        try:
            supplied = self._candidate_provider(search_needed)
            if isinstance(supplied, (str, bytes)):
                raise TypeError
            candidates = tuple(supplied)
        except Exception as error:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CandidateProviderError("candidate provider failed") from None
        if any(not isinstance(candidate, QueueCandidate) for candidate in candidates):
            raise CandidateProviderError("candidate provider returned invalid candidates")
        return candidates

    def tick(self, recommendations: Iterable[QueueCandidate] = ()) -> MonitorTick | None:
        """Reconcile once and ask the host for candidates only when needed."""

        if isinstance(recommendations, (str, bytes)):
            raise CandidateProviderError("recommendations must contain QueueCandidate values")
        try:
            supplied_recommendations = tuple(recommendations)
        except TypeError:
            raise CandidateProviderError("recommendations must contain QueueCandidate values") from None
        if any(not isinstance(candidate, QueueCandidate) for candidate in supplied_recommendations):
            raise CandidateProviderError("recommendations must contain QueueCandidate values")

        # Keep one database lease while excluding other threads for the whole
        # reconciliation and candidate-provider refill transaction.  ``RLock``
        # permits ``run()`` to hold its outer lease while invoking ``tick()``.
        with self._operation_lock:
            with self.lease() as acquired:
                if not acquired:
                    return None
                try:
                    initial = self._coordinator.cycle(supplied_recommendations)
                except QueueCoordinatorError:
                    return self._degraded_failure(bridge_error=True, candidate_provider_error=False)

                if initial.search_needed <= 0 or self._candidate_provider is None:
                    self._consecutive_failures = 0
                    self._consecutive_bridge_errors = 0
                    return self._public_tick(
                        initial,
                        provider_called=False,
                        next_delay_seconds=self._interval_seconds,
                    )

                try:
                    candidates = self._candidates_for(initial.search_needed)
                except CandidateProviderError:
                    return self._degraded_failure(
                        bridge_error=False,
                        candidate_provider_error=True,
                        search_needed=initial.search_needed,
                        candidate_provider_called=True,
                        cycles=(initial,),
                    )
                try:
                    refilled = self._coordinator.cycle(candidates)
                except QueueCoordinatorError:
                    return self._degraded_failure(
                        bridge_error=True,
                        candidate_provider_error=False,
                        search_needed=initial.search_needed,
                        candidate_provider_called=True,
                        cycles=(initial,),
                    )
                self._consecutive_failures = 0
                self._consecutive_bridge_errors = 0
                return self._public_tick(
                    initial,
                    refilled,
                    provider_called=True,
                    next_delay_seconds=self._interval_seconds,
                )

    def confirm_outcome(
        self,
        job_id: str,
        outcome: str,
        *,
        actor: str,
        vacated: bool,
    ) -> object | None:
        """Pass through only an explicitly candidate-owned outcome confirmation.

        The monitor never derives an outcome from a missing tab or bridge
        failure.  It merely serializes the coordinator's candidate-owned API
        under the same database lease used for reconciliation.
        """

        confirm = getattr(self._coordinator, "confirm_outcome", None)
        if not callable(confirm):
            raise TypeError("coordinator must provide confirm_outcome")
        if actor != "user":
            raise PermissionError("only the candidate may confirm an outcome")
        if vacated is not True:
            return None
        # An outcome alone does not establish a vacancy: the candidate may be
        # on a manual application or continuation URL.  Serialize the explicit
        # attestation and coordinator mutation with reconciliation.
        with self._operation_lock:
            with self.lease() as acquired:
                if not acquired:
                    return None
                if self._confirm_accepts_vacated_attestation(confirm):
                    return confirm(job_id, outcome, actor=actor, vacated=vacated)
                return confirm(job_id, outcome, actor=actor)

    @staticmethod
    def _confirm_accepts_vacated_attestation(confirm: Callable[..., object]) -> bool:
        """Preserve compatibility with the unchanged coordinator boundary."""

        try:
            parameters = inspect.signature(confirm).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == "vacated"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    @staticmethod
    def _cancelled(cancellation: CancellationSignal | None) -> bool:
        return cancellation is not None and cancellation.is_set()

    def _wait(self, delay_seconds: float, cancellation: CancellationSignal | None) -> None:
        """Wait interruptibly when the supplied signal provides Event-style waiting."""

        wait = getattr(cancellation, "wait", None) if cancellation is not None else None
        if callable(wait):
            wait(delay_seconds)
        else:
            self._sleeper(delay_seconds)

    def run(
        self,
        cancellation: CancellationSignal | None = None,
        *,
        max_ticks: int | None = None,
        result_sink: Callable[[MonitorTick], object] | None = None,
    ) -> tuple[MonitorTick, ...]:
        """Run until cancelled, streaming unbounded status through ``result_sink``.

        ``max_ticks`` exists for hosts and tests that need a finite polling run;
        those runs return their redacted results as a tuple.  An unbounded run
        must provide a host-owned ``result_sink`` so the monitor never retains
        an unbounded history of ticks in memory.  The sink receives only the
        redacted :class:`MonitorTick` value for each completed iteration.
        """

        if cancellation is not None and not isinstance(cancellation, CancellationSignal):
            raise TypeError("cancellation must implement is_set")
        if max_ticks is not None and (
            isinstance(max_ticks, bool) or not isinstance(max_ticks, int) or max_ticks < 0
        ):
            raise ValueError("max_ticks must be a non-negative integer or None")
        if result_sink is not None and not callable(result_sink):
            raise TypeError("result_sink must be callable")
        if max_ticks is None and result_sink is None:
            raise ValueError("unbounded runs require a host-owned result_sink")

        results: list[MonitorTick] = []
        with self.lease() as acquired:
            if not acquired:
                return ()
            while not self._cancelled(cancellation) and (max_ticks is None or len(results) < max_ticks):
                result = self.tick()
                if result is None:
                    break
                if result_sink is not None:
                    result_sink(result)
                if max_ticks is not None:
                    results.append(result)
                if self._cancelled(cancellation) or (max_ticks is not None and len(results) >= max_ticks):
                    break
                self._wait(result.next_delay_seconds, cancellation)
        return tuple(results)


__all__ = [
    "CandidateProvider",
    "CandidateProviderError",
    "CancellationSignal",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DatabaseLease",
    "MonitorLease",
    "MonitorLeaseError",
    "MonitorTick",
    "PersistentSmartQueueMonitor",
]
