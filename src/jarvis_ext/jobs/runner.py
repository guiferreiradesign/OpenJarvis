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
(for example by the Step 3 worker's process control once it has stopped
FFmpeg) is treated as a ``cancelled`` outcome.

``KeyboardInterrupt``/``SystemExit`` are not caught: the job then stays
``running`` and is recovered by the worker (Step 3), rather than being
recorded with an outcome nobody observed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Protocol

from jarvis_ext.jobs.models import (
    InvalidJobError,
    InvalidTransitionError,
    Job,
    clean_error_message,
    validate_error_code,
)
from jarvis_ext.jobs.store import JobStore

logger = logging.getLogger(__name__)

SUCCEEDED = "succeeded"
FAILED = "failed"
RETRY = "retry"
CANCELLED = "cancelled"
_ACTIONS = frozenset({SUCCEEDED, FAILED, RETRY, CANCELLED})


class JobCancelled(Exception):
    """Raised inside a handler once the work was stopped because of a cancel request."""


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

    def cancel_requested(self) -> bool:
        """Re-read the store; handlers call this between stages."""
        return self.store.get(self.job.id).cancel_requested

    def note(self, text: str) -> None:
        """Record a stage in the job's history (best effort)."""
        self.notes.append(text)
        try:
            self.store.add_note(self.job.id, self.worker_id, text)
        except (InvalidJobError, InvalidTransitionError) as exc:
            logger.warning("Could not record note for job %s: %s", self.job.id, exc)


class JobHandler(Protocol):
    def run(self, context: JobContext) -> JobOutcome: ...


def run_next(
    store: JobStore, worker_id: str, handlers: Mapping[str, JobHandler]
) -> Optional[Job]:
    """Claim and run the oldest queued job of a kind in *handlers*.

    Returns the job as recorded afterwards, or ``None`` when nothing was
    queued.  Runs at most one job.
    """
    if not handlers:
        return None
    job = store.claim_next(worker_id, kinds=list(handlers))
    if job is None:
        return None
    context = JobContext(job=job, store=store, worker_id=worker_id)
    try:
        outcome = handlers[job.kind].run(context)
    except JobCancelled:
        outcome = JobOutcome.cancelled({"stopped": True, "notes": context.notes})
    except Exception as exc:
        logger.exception("Handler for %s failed on job %s", job.kind, job.id)
        outcome = JobOutcome.failed(
            "runner_error", f"{type(exc).__name__} in the {job.kind} handler"
        )
    if not isinstance(outcome, JobOutcome):
        outcome = JobOutcome.failed(
            "unexpected_result",
            f"The {job.kind} handler returned {type(outcome).__name__}, "
            "not a JobOutcome",
        )
    return record_outcome(store, job, worker_id, outcome)


def record_outcome(
    store: JobStore, job: Job, worker_id: str, outcome: JobOutcome
) -> Job:
    """Apply *outcome* to a job this worker is running, never as false success."""
    try:
        return _apply(store, job, worker_id, outcome)
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


def _apply(store: JobStore, job: Job, worker_id: str, outcome: JobOutcome) -> Job:
    if outcome.action == SUCCEEDED:
        return store.complete(job.id, worker_id, outcome.result)
    if outcome.action == FAILED:
        return store.fail(
            job.id, worker_id, outcome.error_code, outcome.error_message, outcome.result
        )
    if outcome.action == RETRY:
        try:
            return store.retry(
                job.id, worker_id, outcome.error_code, outcome.error_message
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
    "JobCancelled",
    "JobContext",
    "JobHandler",
    "JobOutcome",
    "record_outcome",
    "run_next",
]
