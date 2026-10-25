"""The job worker: one job at a time, one worker per job database.

::

    with WorkerLock(lock file)           only one worker per database
        recover stale jobs               crashed workers' jobs are settled
        clean up abandoned temp folders  (handlers that have cleanup())
        loop:
            run_next(...)                one job; heartbeat while it runs
            nothing queued → wait poll_seconds, recover, clean up hourly
        until stop()                     (or after one job with once=True)

**Stopping.**  ``stop()`` lets the current job finish, then exits.
``stop(force=True)`` also asks the running job to stop now: the handler's
process control stops FFmpeg and the handler reports the job as interrupted
(see ``video/render_job.py``).  The CLI maps the first Ctrl-C / SIGTERM to
``stop()`` and a second to ``stop(force=True)``.

**Recovery.**  A ``running`` job whose heartbeat is older than
``stale_after_seconds`` and that is not this worker's own is settled by
``JobStore.recover``: cancelled if a cancellation was requested, requeued
if its handler says the attempt can safely be repeated (and attempts
remain), failed otherwise.  The lock guarantees no other worker is alive,
and the heartbeat age is re-checked under the database write lock, so a
live job is never taken over.

Nothing here knows about video; handlers supply ``run``, and optionally
``recovery(job, events)`` and ``cleanup()``.
"""

from __future__ import annotations

import errno
import logging
import os
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from jarvis_ext.jobs.models import InvalidTransitionError, Job, JobError
from jarvis_ext.jobs.runner import JobHandler, run_next
from jarvis_ext.jobs.settings import JobSettings
from jarvis_ext.jobs.store import JobStore

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CLEANUP_EVERY_SECONDS = 3600.0


class WorkerLockedError(JobError):
    """Another worker already holds the lock for this job database."""


class WorkerLock:
    """An exclusive ``flock`` on a file next to the database.

    The kernel releases it when the process exits for any reason, including
    a crash or SIGKILL, so a stale lock can never block a new worker.  The
    file is left in place (deleting it would let two processes lock
    different files); it only contains the holder's pid, for diagnostics.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        if fcntl is None:  # pragma: no cover - Windows
            raise JobError("The job worker needs macOS or Linux (fcntl locks)")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise JobError("The worker lock file must not be a symbolic link")
            raise JobError(
                f"Cannot open the worker lock ({exc.strerror or type(exc).__name__})"
            ) from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise JobError("The worker lock is not a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise WorkerLockedError(
                        "Another job worker is already running for this job database"
                    ) from None
                raise JobError(
                    f"Cannot lock the worker lock ({exc.strerror or 'error'})"
                ) from None
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "WorkerLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def new_worker_id() -> str:
    return f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def job_summary(job: Job) -> Dict[str, Any]:
    """A compact, path-free view of a job for logs and the CLI."""
    result = job.result if isinstance(job.result, dict) else {}
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status.value,
        "stage": job.stage,
        "progress": job.progress,
        "attempts": f"{job.attempts}/{job.max_attempts}",
        "output": result.get("output"),
        "ready_for_review": result.get("ready_for_review"),
        "error_code": job.error_code,
        "cancel_requested": job.cancel_requested,
        "retry_not_before": job.not_before,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
    }


class Worker:
    """Runs queued jobs one at a time until stopped."""

    def __init__(
        self,
        store: JobStore,
        handlers: Mapping[str, JobHandler],
        *,
        lock_path: Path,
        settings: Optional[JobSettings] = None,
        worker_id: Optional[str] = None,
        cleanup_every_seconds: float = CLEANUP_EVERY_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.settings = settings or JobSettings()
        self.worker_id = worker_id or new_worker_id()
        self._lock = WorkerLock(lock_path)
        self._stop = threading.Event()
        self._force = threading.Event()
        self._cleanup_every = cleanup_every_seconds
        self._monotonic = monotonic
        self._last_cleanup: Optional[float] = None

    # -- control -------------------------------------------------------------

    def stop(self, *, force: bool = False) -> None:
        """Finish the current job and exit; with *force*, stop it now."""
        self._stop.set()
        if force:
            self._force.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    # -- main loop -----------------------------------------------------------

    def run(self, *, once: bool = False) -> Dict[str, Any]:
        """Process jobs until stopped (or one job with *once*); returns a report.

        Raises :class:`WorkerLockedError` if another worker holds the lock.
        """
        report: Dict[str, Any] = {
            "worker_id": self.worker_id,
            "processed": [],
            "recovered": [],
            "cleaned_temp_dirs": [],
        }
        with self._lock:
            logger.info("Worker %s started", self.worker_id)
            self._recover(report)
            self._cleanup(report)
            while not self._stop.is_set():
                job = run_next(
                    self.store,
                    self.worker_id,
                    self.handlers,
                    retry_delay_seconds=self.settings.retry_delay_seconds,
                    heartbeat_seconds=self.settings.heartbeat_seconds,
                    shutdown=self._force,
                )
                if job is not None:
                    summary = job_summary(job)
                    report["processed"].append(summary)
                    _log_job(summary)
                    if once:
                        break
                    continue
                if once:
                    break
                if self._stop.wait(self.settings.poll_seconds):
                    break
                self._recover(report)
                self._cleanup(report)
            logger.info(
                "Worker %s stopped after %d job(s)",
                self.worker_id,
                len(report["processed"]),
            )
        report["stopped"] = (
            "forced"
            if self._force.is_set()
            else ("requested" if self._stop.is_set() else "idle")
        )
        return report

    # -- recovery and cleanup --------------------------------------------------

    def _recover(self, report: Dict[str, Any]) -> None:
        stale_before = self.store.time_ago(self.settings.stale_after_seconds)
        try:
            stale = self.store.stale_jobs(stale_before, exclude_worker=self.worker_id)
        except JobError as exc:
            logger.warning("Could not look for stale jobs: %s", exc)
            return
        for job in stale:
            handler = self.handlers.get(job.kind)
            if handler is not None and hasattr(handler, "recovery"):
                try:
                    requeue, code, message, result = handler.recovery(
                        job, self.store.events(job.id)
                    )
                except Exception:
                    logger.exception("Recovery check failed for job %s", job.id)
                    requeue, code, message, result = (
                        False,
                        "interrupted",
                        "The worker running this job stopped",
                        None,
                    )
            else:
                requeue, code, message, result = (
                    False,
                    "interrupted",
                    "The worker running this job stopped",
                    None,
                )
            try:
                settled = self.store.recover(
                    job.id,
                    stale_before=stale_before,
                    requeue=requeue,
                    error_code=code,
                    error_message=message,
                    result=result,
                    delay_seconds=self.settings.retry_delay_seconds,
                )
            except InvalidTransitionError:
                continue  # finished or alive after all
            except JobError as exc:
                logger.warning("Could not recover job %s: %s", job.id, exc)
                continue
            summary = job_summary(settled)
            report["recovered"].append(summary)
            logger.warning(
                "Recovered job %s from a stopped worker: now %s",
                job.id[:8],
                settled.status.value,
            )

    def _cleanup(self, report: Dict[str, Any]) -> None:
        now = self._monotonic()
        if (
            self._last_cleanup is not None
            and now - self._last_cleanup < self._cleanup_every
        ):
            return
        self._last_cleanup = now
        for handler in {id(h): h for h in self.handlers.values()}.values():
            cleanup = getattr(handler, "cleanup", None)
            if cleanup is None:
                continue
            try:
                removed: List[str] = list(cleanup())
            except Exception:
                logger.exception("Temp-folder cleanup failed")
                continue
            if removed:
                report["cleaned_temp_dirs"].extend(removed)
                logger.info("Removed %d abandoned temp folder(s)", len(removed))


def _log_job(summary: Dict[str, Any]) -> None:
    detail = summary["output"] or summary["error_code"] or ""
    logger.info(
        "Job %s (%s): %s %s",
        summary["id"][:8],
        summary["kind"],
        summary["status"],
        detail,
    )


__all__ = [
    "Worker",
    "WorkerLock",
    "WorkerLockedError",
    "job_summary",
    "new_worker_id",
]
