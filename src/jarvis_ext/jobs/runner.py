"""Run one job: claim it, hand it to the handler for its kind, record the outcome.

This part is skill-neutral.  A *handler* knows how to do one kind of job
(``jarvis_ext.video.render_job`` handles ``video.render``) and returns a
:class:`JobOutcome`; this module turns that outcome into the permitted store
transitions:

=================  ======================================================
Outcome            Store
=================  ======================================================
``succeeded``      ``complete`` (with the handler's result)
``failed``         ``fail`` (final)
``retry``          ``retry`` while attempts remain and no cancellation was
                   requested; otherwise ``fail`` with the same error code
``cancelled``      ``confirm_cancelled`` if a cancellation was requested;
                   otherwise ``fail`` (``unexpected_result``)
=================  ======================================================

A handler that raises, or returns something that is not a ``JobOutcome``,
fails the job (``runner_error`` / ``unexpected_result``); it is never
reported as succeeded.  :class:`JobCancelled` raised from inside a handler
(once the work was stopped because of a cancel request) is treated as a
``cancelled`` outcome; :class:`WorkerInterrupted` (the worker is being shut
down) as a ``failed`` one (``interrupted``), unless the handler turned it
into a more precise outcome itself.

While a job runs, a background thread sends a heartbeat every
``heartbeat_seconds`` so a crashed worker's jobs can be told apart from
live ones (``JobStore.stale_jobs``).  :func:`current_stop_reason` lets
process control running inside the handler (FFmpeg) ask whether to stop:
``"cancel"`` after a cancel request, ``"shutdown"`` after a forced worker
stop.

**Progress.**  Handlers call ``context.progress(stage, percent)``; the
percentage is of the whole job, never decreases, and stays below 100 until
the store records a successful completion.  Writes are throttled (a stage
change at once; otherwise at most one per ``progress_min_interval`` and
only after a ``progress_min_step`` gain).  Each write, the claim and the
final status are published on the OpenJarvis event bus as
:data:`JOB_PROGRESS_EVENT`; a failing bus or subscriber is logged and
never affects the job.

``KeyboardInterrupt``/``SystemExit`` are not caught: the job then stays
``running`` and is recovered by the next worker, rather than being
recorded with an outcome nobody observed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol

from jarvis_ext.jobs.models import (
    InvalidJobError,
    InvalidTransitionError,
    Job,
    JobError,
    clean_error_message,
    clean_progress,
    validate_error_code,
    validate_stage,
)
from jarvis_ext.jobs.store import JobStore

logger = logging.getLogger(__name__)

SUCCEEDED = "succeeded"
FAILED = "failed"
RETRY = "retry"
CANCELLED = "cancelled"
_ACTIONS = frozenset({SUCCEEDED, FAILED, RETRY, CANCELLED})

STOP_CANCEL = "cancel"
STOP_SHUTDOWN = "shutdown"

# Custom event type, a plain string like the upstream scheduler's
# "scheduler_task_start" (the core EventType enum is not edited).
JOB_PROGRESS_EVENT = "jarvis_job_progress"
_DEFAULT_BUS = object()

_active = threading.local()


class JobCancelled(Exception):
    """Raised inside a handler once the work was stopped because of a cancel request."""


class WorkerInterrupted(Exception):
    """Raised inside a handler once the work was stopped for a worker shutdown."""


@dataclass(frozen=True)
class JobOutcome:
    """What a handler reports about the job it ran."""

    action: str
    result: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None
    error_message: str = ""

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise InvalidJobError(f"Unknown job outcome {self.action!r}")
        if self.action in (FAILED, RETRY):
            validate_error_code(self.error_code)
        object.__setattr__(
            self, "error_message", clean_error_message(self.error_message)
        )

    @classmethod
    def succeeded(cls, result: Optional[Dict[str, Any]] = None) -> "JobOutcome":
        return cls(SUCCEEDED, result)

    @classmethod
    def failed(
        cls, code: str, message: str = "", result: Optional[Dict[str, Any]] = None
    ) -> "JobOutcome":
        return cls(FAILED, result, code, message)

    @classmethod
    def retry(
        cls, code: str, message: str = "", result: Optional[Dict[str, Any]] = None
    ) -> "JobOutcome":
        return cls(RETRY, result, code, message)

    @classmethod
    def cancelled(cls, result: Optional[Dict[str, Any]] = None) -> "JobOutcome":
        return cls(CANCELLED, result)


@dataclass
class JobContext:
    """Given to a handler: the job, cancellation checks and stage notes."""

    job: Job
    store: JobStore
    worker_id: str
    notes: list = field(default_factory=list)
    shutdown: Optional[threading.Event] = None
    cancel_poll_seconds: float = 0.5
    bus: Any = None
    progress_min_step: float = 1.0
    progress_min_interval: float = 1.0
    _cancel_seen: bool = field(default=False, repr=False)
    _cancel_checked: float = field(default=float("-inf"), repr=False)
    _progress: Optional[float] = field(default=None, repr=False)
    _stage: Optional[str] = field(default=None, repr=False)
    _progress_at: float = field(default=float("-inf"), repr=False)

    def progress(self, stage: str, percent: float) -> None:
        """Report the job's stage and overall percentage (best effort).

        Never raises: a bad value or a busy database is logged and skipped.
        """
        try:
            value = clean_progress(percent)
            validate_stage(stage)
        except InvalidJobError as exc:
            logger.warning("Ignoring progress for job %s: %s", self.job.id, exc)
            return
        value = max(value, self._progress or 0.0)
        now = time.monotonic()
        if stage == self._stage and (
            value - (self._progress or 0.0) < self.progress_min_step
            or now - self._progress_at < self.progress_min_interval
        ):
            return
        try:
            job = self.store.report_progress(
                self.job.id, self.worker_id, stage=stage, progress=value
            )
        except JobError as exc:
            logger.warning("Progress for job %s not recorded: %s", self.job.id, exc)
            return
        self._progress, self._stage, self._progress_at = job.progress, job.stage, now
        publish_progress(self.bus, job)

    def cancel_requested(self) -> bool:
        """Re-read the store; handlers call this between stages."""
        try:
            self._cancel_seen = self.store.get(self.job.id).cancel_requested
        except JobError as exc:  # database busy: keep the last known answer
            logger.warning("Cancel check for job %s failed: %s", self.job.id, exc)
        self._cancel_checked = time.monotonic()
        return self._cancel_seen

    def stop_reason(self, *, fresh: bool = False) -> Optional[str]:
        """``"shutdown"``, ``"cancel"`` or ``None``.

        Handlers pass ``fresh=True`` between stages (always re-reads the
        store).  Process control polls without it, and the store is then
        read at most every ``cancel_poll_seconds``.
        """
        if self.shutdown is not None and self.shutdown.is_set():
            return STOP_SHUTDOWN
        if self._cancel_seen:
            return STOP_CANCEL
        due = time.monotonic() - self._cancel_checked >= self.cancel_poll_seconds
        if (fresh or due) and self.cancel_requested():
            return STOP_CANCEL
        return None

    def note(self, text: str) -> None:
        """Record a stage in the job's history (best effort)."""
        self.notes.append(text)
        try:
            self.store.add_note(self.job.id, self.worker_id, text)
        except (InvalidJobError, InvalidTransitionError) as exc:
            logger.warning("Could not record note for job %s: %s", self.job.id, exc)


class JobHandler(Protocol):
    def run(self, context: JobContext) -> JobOutcome: ...


def publish_progress(bus: Any, job: Job) -> None:
    """Publish a job's status/stage/progress; never raises."""
    if bus is None:
        return
    try:
        bus.publish(
            JOB_PROGRESS_EVENT,
            {
                "job_id": job.id,
                "kind": job.kind,
                "status": job.status.value,
                "stage": job.stage,
                "progress": job.progress,
                "attempt": job.attempts,
            },
        )
    except Exception:
        logger.warning("Publishing progress for job %s failed", job.id, exc_info=True)


def current_context() -> Optional[JobContext]:
    """The job context running in this thread, if any."""
    return getattr(_active, "context", None)


def current_stop_reason() -> Optional[str]:
    """Why the job running in this thread should stop, if it should.

    ``None`` outside a job, so code using it behaves normally elsewhere.
    """
    context = getattr(_active, "context", None)
    return context.stop_reason() if context is not None else None


class _Heartbeat:
    """Background heartbeats for one running job."""

    def __init__(self, store: JobStore, job: Job, worker_id: str, every: float):
        self._store, self._job, self._worker = store, job, worker_id
        self._every = every
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"jarvis-heartbeat-{job.id[:8]}", daemon=True
        )

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._done.set()
        self._thread.join()

    def _loop(self) -> None:
        while not self._done.wait(self._every):
            try:
                self._store.heartbeat(self._job.id, self._worker)
            except JobError as exc:
                logger.warning("Heartbeat for job %s failed: %s", self._job.id, exc)


def run_next(
    store: JobStore,
    worker_id: str,
    handlers: Mapping[str, JobHandler],
    *,
    retry_delay_seconds: float = 0.0,
    heartbeat_seconds: Optional[float] = None,
    shutdown: Optional[threading.Event] = None,
    bus: Any = _DEFAULT_BUS,
    progress_interval: float = 1.0,
) -> Optional[Job]:
    """Claim and run the oldest queued job of a kind in *handlers*.

    Returns the job as recorded afterwards, or ``None`` when nothing was
    queued.  Runs at most one job.  A ``retry`` outcome is not claimed again
    for *retry_delay_seconds*.  With *heartbeat_seconds*, a heartbeat is
    recorded at that interval while the handler runs.  Setting *shutdown*
    makes :func:`current_stop_reason` report ``"shutdown"``.  Progress
    events go to *bus* (default: the OpenJarvis event bus; ``None`` for none),
    at most one per *progress_interval* seconds within a stage.
    """
    if not handlers:
        return None
    if bus is _DEFAULT_BUS:
        bus = _default_bus()
    job = store.claim_next(worker_id, kinds=list(handlers))
    if job is None:
        return None
    publish_progress(bus, job)
    context = JobContext(
        job=job,
        store=store,
        worker_id=worker_id,
        shutdown=shutdown,
        bus=bus,
        progress_min_interval=progress_interval,
    )
    heartbeat = (
        _Heartbeat(store, job, worker_id, heartbeat_seconds)
        if heartbeat_seconds
        else None
    )
    _active.context = context
    try:
        if heartbeat is not None:
            heartbeat.__enter__()
        outcome = handlers[job.kind].run(context)
    except JobCancelled:
        outcome = JobOutcome.cancelled({"stopped": True, "notes": context.notes})
    except WorkerInterrupted:
        outcome = JobOutcome.failed(
            "interrupted",
            "The worker was stopped while this job ran",
            {"notes": context.notes},
        )
    except Exception as exc:
        logger.exception("Handler for %s failed on job %s", job.kind, job.id)
        outcome = JobOutcome.failed(
            "runner_error", f"{type(exc).__name__} in the {job.kind} handler"
        )
    finally:
        _active.context = None
        if heartbeat is not None:
            heartbeat.__exit__(None, None, None)
    if not isinstance(outcome, JobOutcome):
        outcome = JobOutcome.failed(
            "unexpected_result",
            f"The {job.kind} handler returned {type(outcome).__name__}, "
            "not a JobOutcome",
        )
    final = record_outcome(
        store, job, worker_id, outcome, retry_delay_seconds=retry_delay_seconds
    )
    publish_progress(bus, final)
    return final


def _default_bus() -> Any:
    try:
        from openjarvis.core.events import get_event_bus

        return get_event_bus()
    except Exception:  # pragma: no cover - the core always provides one
        logger.warning("No event bus available; progress events are off")
        return None


def record_outcome(
    store: JobStore,
    job: Job,
    worker_id: str,
    outcome: JobOutcome,
    *,
    retry_delay_seconds: float = 0.0,
) -> Job:
    """Apply *outcome* to a job this worker is running, never as false success."""
    try:
        return _apply(store, job, worker_id, outcome, retry_delay_seconds)
    except InvalidJobError as exc:
        # The handler's result could not be stored (not JSON, too large).
        # The work may have succeeded, but an unrecorded success is not a
        # success: fail with what can be stored.
        logger.warning("Job %s outcome could not be stored: %s", job.id, exc)
        return store.fail(
            job.id,
            worker_id,
            "invalid_result",
            f"The {job.kind} handler's result could not be stored: {exc}",
        )


def _apply(
    store: JobStore, job: Job, worker_id: str, outcome: JobOutcome, delay: float
) -> Job:
    if outcome.action == SUCCEEDED:
        return store.complete(job.id, worker_id, outcome.result)
    if outcome.action == FAILED:
        return store.fail(
            job.id, worker_id, outcome.error_code, outcome.error_message, outcome.result
        )
    if outcome.action == RETRY:
        try:
            return store.retry(
                job.id,
                worker_id,
                outcome.error_code,
                outcome.error_message,
                delay_seconds=delay,
            )
        except InvalidTransitionError as exc:
            # Attempts used up, or a cancellation arrived: the real outcome is
            # this failure (D23, D27).
            if store.get(job.id).cancel_requested:
                reason = "not retried: cancellation was requested"
            else:
                reason = f"not retried: {exc}"
            message = f"{outcome.error_message} ({reason})".strip()
            return store.fail(
                job.id, worker_id, outcome.error_code, message, outcome.result
            )
    # CANCELLED
    try:
        return store.confirm_cancelled(job.id, worker_id, outcome.result)
    except InvalidTransitionError:
        return store.fail(
            job.id,
            worker_id,
            "unexpected_result",
            f"The {job.kind} handler reported a cancellation nobody requested",
            outcome.result,
        )


__all__ = [
    "JOB_PROGRESS_EVENT",
    "current_context",
    "publish_progress",
    "STOP_CANCEL",
    "STOP_SHUTDOWN",
    "WorkerInterrupted",
    "current_stop_reason",
    "JobCancelled",
    "JobContext",
    "JobHandler",
    "JobOutcome",
    "record_outcome",
    "run_next",
]
