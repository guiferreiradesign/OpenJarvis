"""SQLite persistence for jobs, following the ``scheduler/store.py`` pattern.

Differences from the upstream scheduler store, all deliberate:

* Every write runs in a ``BEGIN IMMEDIATE`` transaction, so a status change
  is checked and applied under the database's write lock.  Claiming is
  therefore atomic across threads *and* processes: two workers can never
  claim the same job.
* Status changes go through :func:`~jarvis_ext.jobs.models.check_transition`
  and are conditional on the state that was checked, and every change is
  appended to ``job_events`` in the same transaction.
* The file carries an ``application_id`` and a ``user_version`` (the schema
  version).  A file from a newer JARVIS, or a SQLite file that is not a job
  database, is refused without being modified.
* WAL journal mode, so readers (status queries) do not block the worker.
* Every final status also creates the job's row in ``job_notifications``
  (an outbox) in the same transaction, so a finished job always owes
  exactly one notification, even if the worker dies right after.

Error messages never contain the database path; they can reach a model
through tool results (see DECISIONS.md D3).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, List, Optional

from jarvis_ext.jobs.models import (
    MAX_ATTEMPTS,
    MAX_PAYLOAD_BYTES,
    MAX_RESULT_BYTES,
    InvalidJobError,
    InvalidTransitionError,
    Job,
    JobError,
    JobEvent,
    JobNotFoundError,
    JobNotification,
    JobStatus,
    NotificationState,
    check_transition,
    clean_error_message,
    clean_progress,
    encode_json_object,
    validate_error_code,
    validate_kind,
    validate_stage,
    validate_worker_id,
)
from jarvis_ext.jobs.settings import JobSettings

logger = logging.getLogger(__name__)

MAX_NOTE_CHARS = 500
_ID_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{6,32}$")

SCHEMA_VERSION = 4
MAX_RETRY_DELAY_SECONDS = 86400.0
# Notification delivery: attempts per job (a failed attempt is retried after
# NOTIFICATION_RETRY_SECONDS) before the notification is given up.
MAX_NOTIFICATION_ATTEMPTS = 3
NOTIFICATION_RETRY_SECONDS = 30.0
# "JRVJ" — marks the file as a JARVIS job database (PRAGMA application_id).
APPLICATION_ID = 0x4A52564A

_STATUS_VALUES = ", ".join(f"'{s.value}'" for s in JobStatus)
_FINAL_VALUES = ", ".join(f"'{s.value}'" for s in JobStatus if s.is_final)
_NOTIFY_VALUES = ", ".join(f"'{s.value}'" for s in NotificationState)

_SCHEMA_V1 = (
    f"""
    CREATE TABLE jobs (
        seq              INTEGER PRIMARY KEY AUTOINCREMENT,
        id               TEXT    NOT NULL UNIQUE,
        kind             TEXT    NOT NULL,
        status           TEXT    NOT NULL CHECK (status IN ({_STATUS_VALUES})),
        payload          TEXT    NOT NULL,
        parent_id        TEXT    REFERENCES jobs(id),
        attempts         INTEGER NOT NULL DEFAULT 0,
        max_attempts     INTEGER NOT NULL CHECK (max_attempts >= 1),
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        claimed_by       TEXT,
        result           TEXT,
        error_code       TEXT,
        error_message    TEXT,
        created_at       TEXT    NOT NULL,
        updated_at       TEXT    NOT NULL,
        started_at       TEXT,
        finished_at      TEXT
    )
    """,
    "CREATE INDEX jobs_status_seq ON jobs (status, seq)",
    "CREATE INDEX jobs_parent ON jobs (parent_id)",
    """
    CREATE TABLE job_events (
        seq         INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      TEXT NOT NULL REFERENCES jobs(id),
        at          TEXT NOT NULL,
        from_status TEXT,
        to_status   TEXT NOT NULL,
        note        TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX job_events_job ON job_events (job_id, seq)",
)

# Upgrades from version N to N+1, applied in order inside one transaction.
# A new database is created at version 1 and upgraded the same way, so the
# upgrade path is exercised by every test.
_MIGRATIONS: dict = {
    # Phase 3 step 3: worker heartbeat (stale-job detection) and retry
    # back-off (a requeued job is not claimed before not_before).
    1: (
        "ALTER TABLE jobs ADD COLUMN heartbeat_at TEXT",
        "ALTER TABLE jobs ADD COLUMN not_before TEXT",
    ),
    # Phase 3 step 4: progress (percent of the whole job) and current stage.
    2: (
        "ALTER TABLE jobs ADD COLUMN progress REAL",
        "ALTER TABLE jobs ADD COLUMN stage TEXT",
    ),
    # Phase 3 step 5: notification outbox, one row per finished job.  Jobs
    # that finished before the upgrade get no row: nobody is notified
    # about old work when the database is upgraded.
    3: (
        f"""
        CREATE TABLE job_notifications (
            job_id       TEXT PRIMARY KEY REFERENCES jobs(id),
            outcome      TEXT NOT NULL CHECK (outcome IN ({_FINAL_VALUES})),
            state        TEXT NOT NULL CHECK (state IN ({_NOTIFY_VALUES})),
            attempts     INTEGER NOT NULL DEFAULT 0,
            claimed_by   TEXT,
            claimed_at   TEXT,
            not_before   TEXT,
            last_error   TEXT,
            created_at   TEXT NOT NULL,
            updated_at   TEXT NOT NULL,
            delivered_at TEXT
        )
        """,
        "CREATE INDEX job_notifications_state ON job_notifications (state, created_at)",
    ),
}


class JobStoreError(JobError):
    """The job database cannot be opened or used."""


class SchemaVersionError(JobStoreError):
    """The database was written by a different (usually newer) schema."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JobStore:
    """SQLite-backed job store.

    One instance holds one connection and may be shared between threads.
    Separate instances (or processes) may open the same file concurrently.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        default_max_attempts: int = MAX_ATTEMPTS,
        busy_timeout_seconds: float = 10.0,
        clock: Callable[[], str] = _now,
    ) -> None:
        if not 1 <= default_max_attempts <= MAX_ATTEMPTS:
            raise ValueError(f"default_max_attempts must be 1–{MAX_ATTEMPTS}")
        self._default_max_attempts = default_max_attempts
        self._clock = clock
        self._lock = threading.RLock()
        path = Path(path)
        _prepare_file(path)
        try:
            self._conn = sqlite3.connect(
                str(path),
                timeout=busy_timeout_seconds,
                isolation_level=None,  # transactions are managed explicitly
                check_same_thread=False,
            )
        except sqlite3.Error as exc:
            raise JobStoreError(
                f"Cannot open the job database ({type(exc).__name__})"
            ) from None
        self._conn.row_factory = sqlite3.Row
        try:
            self._initialise()
        except BaseException:
            self._conn.close()
            raise

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def journal_mode(self) -> str:
        return self._read_one("PRAGMA journal_mode")[0]

    @property
    def schema_version(self) -> int:
        return self._read_one("PRAGMA user_version")[0]

    # -- submitting and reading ---------------------------------------------

    def submit(
        self,
        kind: str,
        payload: dict,
        *,
        parent_id: Optional[str] = None,
        max_attempts: Optional[int] = None,
    ) -> Job:
        """Queue a new job and return it."""
        validate_kind(kind)
        payload_text = encode_json_object(payload, "payload", MAX_PAYLOAD_BYTES)
        if max_attempts is None:
            max_attempts = self._default_max_attempts
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_ATTEMPTS
        ):
            raise InvalidJobError(f"max_attempts must be 1–{MAX_ATTEMPTS}")
        job_id = uuid.uuid4().hex
        with self._write() as conn:
            if parent_id is not None and self._fetch(conn, parent_id) is None:
                raise JobNotFoundError(f"Parent job {parent_id!r} does not exist")
            now = self._clock()
            conn.execute(
                "INSERT INTO jobs (id, kind, status, payload, parent_id, "
                "max_attempts, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    kind,
                    JobStatus.QUEUED.value,
                    payload_text,
                    parent_id,
                    max_attempts,
                    now,
                    now,
                ),
            )
            self._event(conn, job_id, None, JobStatus.QUEUED, "submitted", now)
            return self._get(conn, job_id)

    def get(self, job_id: str) -> Job:
        with self._read() as conn:
            return self._get(conn, job_id)

    def list_jobs(
        self,
        *,
        status: Optional[JobStatus] = None,
        kind: Optional[str] = None,
        limit: int = 100,
    ) -> List[Job]:
        """Most recent jobs first, optionally filtered."""
        clauses, args = [], []
        if status is not None:
            clauses.append("status = ?")
            args.append(JobStatus(status).value)
        if kind is not None:
            clauses.append("kind = ?")
            args.append(validate_kind(kind))
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where}ORDER BY seq DESC LIMIT ?",
                (*args, max(0, int(limit))),
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def events(self, job_id: str) -> List[JobEvent]:
        """A job's status history, oldest first."""
        with self._read() as conn:
            self._get(conn, job_id)
            rows = conn.execute(
                "SELECT * FROM job_events WHERE job_id = ? ORDER BY seq", (job_id,)
            ).fetchall()
        return [
            JobEvent(
                job_id=r["job_id"],
                at=r["at"],
                from_status=JobStatus(r["from_status"]) if r["from_status"] else None,
                to_status=JobStatus(r["to_status"]),
                note=r["note"],
            )
            for r in rows
        ]

    # -- worker operations ---------------------------------------------------

    def claim_next(
        self, worker_id: str, *, kinds: Optional[Iterable[str]] = None
    ) -> Optional[Job]:
        """Atomically move the oldest queued job (of *kinds*) to ``running``.

        Returns ``None`` when nothing is queued.  The check and the update
        happen under the database write lock, so concurrent claimers in any
        thread or process always receive different jobs.
        """
        validate_worker_id(worker_id)
        kind_list = None if kinds is None else [validate_kind(k) for k in kinds]
        if kind_list == []:
            return None
        sql = (
            "SELECT id FROM jobs WHERE status = ? "
            "AND (not_before IS NULL OR not_before <= ?) "
        )
        if kind_list is not None:
            sql += f"AND kind IN ({', '.join('?' * len(kind_list))}) "
        sql += "ORDER BY seq LIMIT 1"
        with self._write() as conn:
            now = self._clock()
            args: list = [JobStatus.QUEUED.value, now, *(kind_list or [])]
            row = conn.execute(sql, args).fetchone()
            if row is None:
                return None
            job_id = row["id"]
            cursor = conn.execute(
                "UPDATE jobs SET status = ?, claimed_by = ?, attempts = attempts + 1, "
                "started_at = ?, updated_at = ?, heartbeat_at = ?, not_before = NULL, "
                "progress = 0, stage = NULL WHERE id = ? AND status = ?",
                (
                    JobStatus.RUNNING.value,
                    worker_id,
                    now,
                    now,
                    now,
                    job_id,
                    JobStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:  # pragma: no cover - guarded by the write lock
                raise JobStoreError("The job was claimed concurrently; try again")
            self._event(
                conn,
                job_id,
                JobStatus.QUEUED,
                JobStatus.RUNNING,
                f"claimed by {worker_id}",
                now,
            )
            return self._get(conn, job_id)

    def complete(
        self, job_id: str, worker_id: str, result: Optional[dict] = None
    ) -> Job:
        """``running`` → ``succeeded``, storing the runner's result."""
        return self._finish_running(
            job_id,
            worker_id,
            JobStatus.SUCCEEDED,
            {"result": _encode_result(result), "progress": 100.0},
            "succeeded",
        )

    def fail(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str = "",
        result: Optional[dict] = None,
    ) -> Job:
        """``running`` → ``failed`` (final), optionally keeping partial results."""
        code = validate_error_code(error_code)
        return self._finish_running(
            job_id,
            worker_id,
            JobStatus.FAILED,
            {
                "error_code": code,
                "error_message": clean_error_message(error_message),
                "result": _encode_result(result),
            },
            code,
        )

    def retry(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        error_message: str = "",
        *,
        delay_seconds: float = 0.0,
    ) -> Job:
        """``running`` → ``queued`` after a temporary failure.

        The job is not claimed again for *delay_seconds* (retry back-off).
        Refused when the job has used all its attempts or a cancellation was
        requested; the caller should then :meth:`fail` or
        :meth:`confirm_cancelled` instead.
        """
        code = validate_error_code(error_code)
        message = clean_error_message(error_message)
        delay = _check_delay(delay_seconds)
        with self._write() as conn:
            job = self._get(conn, job_id)
            self._check_owner(job, worker_id, JobStatus.QUEUED)
            if job.cancel_requested:
                raise InvalidTransitionError(
                    "Cancellation was requested; the job cannot be retried"
                )
            if job.attempts >= job.max_attempts:
                raise InvalidTransitionError(
                    f"The job has used all {job.max_attempts} attempt(s)"
                )
            return self._apply(
                conn,
                job,
                JobStatus.QUEUED,
                {
                    "claimed_by": None,
                    "started_at": None,
                    "heartbeat_at": None,
                    "not_before": self._after(delay),
                    "progress": None,
                    "stage": None,
                    "error_code": code,
                    "error_message": message,
                },
                f"retry after {code}" + (f" in {delay:g}s" if delay else ""),
            )

    def confirm_cancelled(
        self, job_id: str, worker_id: str, result: Optional[dict] = None
    ) -> Job:
        """``running`` → ``cancelled`` once the worker has stopped the work.

        *result* records what had already happened (for example a render that
        was published but not validated), so the history stays truthful.
        """
        result_text = _encode_result(result)
        with self._write() as conn:
            job = self._get(conn, job_id)
            self._check_owner(job, worker_id, JobStatus.CANCELLED)
            if not job.cancel_requested:
                raise InvalidTransitionError("No cancellation was requested")
            return self._apply(
                conn,
                job,
                JobStatus.CANCELLED,
                {"finished_at": self._clock(), "result": result_text},
                "cancelled by worker",
            )

    def add_note(self, job_id: str, worker_id: str, note: str) -> None:
        """Append a progress note (e.g. a finished stage) to a running job.

        Only the claiming worker may add notes; the status does not change.
        """
        if not isinstance(note, str) or not note.strip():
            raise InvalidJobError("Note must be a non-empty string")
        note = note[:MAX_NOTE_CHARS]
        with self._write() as conn:
            job = self._get(conn, job_id)
            validate_worker_id(worker_id)
            if job.status is not JobStatus.RUNNING or job.claimed_by != worker_id:
                raise InvalidTransitionError(
                    "Notes can only be added to a job this worker is running"
                )
            now = self._clock()
            conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now, job_id))
            self._event(conn, job_id, job.status, job.status, note, now)

    def heartbeat(self, job_id: str, worker_id: str) -> Job:
        """Record that the claiming worker is alive; returns the current job.

        The returned job's ``cancel_requested`` tells the worker whether to
        stop.  Refused for a job that is not running under *worker_id*.
        """
        validate_worker_id(worker_id)
        with self._write() as conn:
            job = self._get(conn, job_id)
            if job.status is not JobStatus.RUNNING or job.claimed_by != worker_id:
                raise InvalidTransitionError(
                    "Only the worker running a job can send its heartbeat"
                )
            now = self._clock()
            conn.execute(
                "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status = ?",
                (now, job_id, JobStatus.RUNNING.value),
            )
            return self._get(conn, job_id)

    def report_progress(
        self, job_id: str, worker_id: str, *, stage: str, progress: float
    ) -> Job:
        """Record a running job's stage and percentage (0–99).

        Progress never decreases: a lower value keeps the current one.  100
        is only ever recorded by :meth:`complete`.  A change of stage is
        also written to the job's history.  Only the claiming worker may
        report.
        """
        validate_worker_id(worker_id)
        stage = validate_stage(stage)
        value = clean_progress(progress)
        with self._write() as conn:
            job = self._get(conn, job_id)
            if job.status is not JobStatus.RUNNING or job.claimed_by != worker_id:
                raise InvalidTransitionError(
                    "Only the worker running a job can report its progress"
                )
            value = max(value, job.progress or 0.0)
            if value == job.progress and stage == job.stage:
                return job
            now = self._clock()
            conn.execute(
                "UPDATE jobs SET progress = ?, stage = ?, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (value, stage, now, job_id, JobStatus.RUNNING.value),
            )
            if stage != job.stage:
                self._event(conn, job_id, job.status, job.status, f"stage {stage}", now)
            return self._get(conn, job_id)

    # -- recovery ------------------------------------------------------------

    def stale_jobs(
        self, stale_before: str, *, exclude_worker: Optional[str] = None
    ) -> List[Job]:
        """Running jobs whose last heartbeat is older than *stale_before*."""
        sql = (
            "SELECT * FROM jobs WHERE status = ? "
            "AND COALESCE(heartbeat_at, started_at, updated_at) < ? "
        )
        args: list = [JobStatus.RUNNING.value, stale_before]
        if exclude_worker is not None:
            sql += "AND (claimed_by IS NULL OR claimed_by != ?) "
            args.append(exclude_worker)
        with self._read() as conn:
            rows = conn.execute(sql + "ORDER BY seq", args).fetchall()
        return [_row_to_job(r) for r in rows]

    def recover(
        self,
        job_id: str,
        *,
        stale_before: str,
        requeue: bool,
        error_code: str = "interrupted",
        error_message: str = "",
        result: Optional[dict] = None,
        delay_seconds: float = 0.0,
    ) -> Job:
        """Settle a running job whose worker stopped sending heartbeats.

        Re-checked under the write lock: if the job finished or its heartbeat
        is newer than *stale_before*, nothing changes and
        :class:`InvalidTransitionError` is raised.  Then, in order:

        * a requested cancellation → ``cancelled``;
        * *requeue* and attempts left → ``queued`` (after *delay_seconds*);
        * otherwise → ``failed`` with *error_code*.
        """
        code = validate_error_code(error_code)
        message = clean_error_message(error_message)
        result_text = _encode_result(result)
        delay = _check_delay(delay_seconds)
        with self._write() as conn:
            job = self._get(conn, job_id)
            last_seen = job.heartbeat_at or job.started_at or job.updated_at
            if job.status is not JobStatus.RUNNING or last_seen >= stale_before:
                raise InvalidTransitionError(
                    "The job is not running or its worker is still alive"
                )
            owner = job.claimed_by or "unknown worker"
            if job.cancel_requested:
                return self._apply(
                    conn,
                    job,
                    JobStatus.CANCELLED,
                    {"finished_at": self._clock(), "result": result_text},
                    f"recovered from {owner}: cancelled",
                )
            if requeue and job.attempts < job.max_attempts:
                return self._apply(
                    conn,
                    job,
                    JobStatus.QUEUED,
                    {
                        "claimed_by": None,
                        "started_at": None,
                        "heartbeat_at": None,
                        "not_before": self._after(delay),
                        "progress": None,
                        "stage": None,
                        "error_code": code,
                        "error_message": message,
                    },
                    f"recovered from {owner}: requeued after {code}",
                )
            return self._apply(
                conn,
                job,
                JobStatus.FAILED,
                {
                    "finished_at": self._clock(),
                    "error_code": code,
                    "error_message": message,
                    "result": result_text,
                },
                f"recovered from {owner}: {code}",
            )

    def find(self, id_or_prefix: str) -> Job:
        """A job by full id or by a unique prefix of at least 6 characters."""
        if not isinstance(id_or_prefix, str) or not _ID_PREFIX_RE.match(id_or_prefix):
            raise JobNotFoundError("Give a job id (or at least its first 6 characters)")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE id LIKE ? ORDER BY seq LIMIT 2",
                (id_or_prefix.lower() + "%",),
            ).fetchall()
        if not rows:
            raise JobNotFoundError(f"No job with id {id_or_prefix!r}")
        if len(rows) > 1:
            raise JobNotFoundError(
                f"{id_or_prefix!r} matches more than one job; give more characters"
            )
        return _row_to_job(rows[0])

    def time_ago(self, seconds: float) -> str:
        """The store clock's current time minus *seconds*, as stored."""
        return _shift(self._clock(), -float(seconds))

    def _after(self, seconds: float) -> Optional[str]:
        return _shift(self._clock(), seconds) if seconds > 0 else None

    # -- notifications (outbox) ----------------------------------------------

    def notification(self, job_id: str) -> Optional[JobNotification]:
        """The notification owed for a finished job (``None`` if not final)."""
        with self._read() as conn:
            self._get(conn, job_id)
            row = conn.execute(
                _NOTIFICATION_SELECT + "WHERE n.job_id = ?", (job_id,)
            ).fetchone()
        return _row_to_notification(row) if row is not None else None

    def claim_notifications(
        self, worker_id: str, *, lease_before: str, limit: int = 20
    ) -> List[JobNotification]:
        """Atomically take due notifications for sending, oldest first.

        Due means ``pending`` (and past its retry back-off), or ``sending``
        with a claim older than *lease_before*: its sender died between
        claiming and recording the result, so it may or may not have been
        shown; it is sent again (at-least-once, see jobs.md).  One that has
        used all its attempts is given up instead.  Concurrent claimers in
        any thread or process always receive different rows.
        """
        validate_worker_id(worker_id)
        claimed: List[JobNotification] = []
        with self._write() as conn:
            now = self._clock()
            rows = conn.execute(
                "SELECT job_id, state, attempts FROM job_notifications "
                "WHERE (state = ? AND (not_before IS NULL OR not_before <= ?)) "
                "OR (state = ? AND claimed_at < ?) "
                "ORDER BY created_at, rowid LIMIT ?",
                (
                    NotificationState.PENDING.value,
                    now,
                    NotificationState.SENDING.value,
                    lease_before,
                    max(0, int(limit)),
                ),
            ).fetchall()
            for row in rows:
                if row["attempts"] >= MAX_NOTIFICATION_ATTEMPTS:
                    conn.execute(
                        "UPDATE job_notifications SET state = ?, last_error = ?, "
                        "claimed_by = NULL, claimed_at = NULL, updated_at = ? "
                        "WHERE job_id = ? AND state = ?",
                        (
                            NotificationState.FAILED.value,
                            "abandoned",
                            now,
                            row["job_id"],
                            row["state"],
                        ),
                    )
                    continue
                conn.execute(
                    "UPDATE job_notifications SET state = ?, claimed_by = ?, "
                    "claimed_at = ?, attempts = attempts + 1, not_before = NULL, "
                    "updated_at = ? WHERE job_id = ? AND state = ?",
                    (
                        NotificationState.SENDING.value,
                        worker_id,
                        now,
                        now,
                        row["job_id"],
                        row["state"],
                    ),
                )
                claimed.append(
                    _row_to_notification(
                        conn.execute(
                            _NOTIFICATION_SELECT + "WHERE n.job_id = ?",
                            (row["job_id"],),
                        ).fetchone()
                    )
                )
        return claimed

    def notification_delivered(self, job_id: str, worker_id: str) -> bool:
        """Record that *worker_id* showed the notification it claimed.

        Returns ``False`` (and changes nothing) if the claim is no longer
        this worker's, for example because its lease expired.
        """
        validate_worker_id(worker_id)
        with self._write() as conn:
            now = self._clock()
            cursor = conn.execute(
                "UPDATE job_notifications SET state = ?, delivered_at = ?, "
                "claimed_at = NULL, last_error = NULL, updated_at = ? "
                "WHERE job_id = ? AND state = ? AND claimed_by = ?",
                (
                    NotificationState.DELIVERED.value,
                    now,
                    now,
                    job_id,
                    NotificationState.SENDING.value,
                    worker_id,
                ),
            )
            return cursor.rowcount == 1

    def notification_failed(
        self,
        job_id: str,
        worker_id: str,
        error_code: str,
        *,
        retryable: bool = True,
        delay_seconds: float = NOTIFICATION_RETRY_SECONDS,
    ) -> Optional[JobNotification]:
        """Record a failed attempt: retry later if allowed, otherwise give up.

        The job is never touched.  Returns the updated notification, or
        ``None`` if the claim is no longer this worker's.
        """
        validate_worker_id(worker_id)
        code = validate_error_code(error_code)
        delay = _check_delay(delay_seconds)
        with self._write() as conn:
            row = conn.execute(
                "SELECT attempts FROM job_notifications "
                "WHERE job_id = ? AND state = ? AND claimed_by = ?",
                (job_id, NotificationState.SENDING.value, worker_id),
            ).fetchone()
            if row is None:
                return None
            retry = retryable and row["attempts"] < MAX_NOTIFICATION_ATTEMPTS
            state = NotificationState.PENDING if retry else NotificationState.FAILED
            conn.execute(
                "UPDATE job_notifications SET state = ?, last_error = ?, "
                "claimed_at = NULL, not_before = ?, updated_at = ? "
                "WHERE job_id = ?",
                (
                    state.value,
                    code,
                    self._after(delay) if retry else None,
                    self._clock(),
                    job_id,
                ),
            )
            return _row_to_notification(
                conn.execute(
                    _NOTIFICATION_SELECT + "WHERE n.job_id = ?", (job_id,)
                ).fetchone()
            )

    # -- user operations -----------------------------------------------------

    def cancel(self, job_id: str) -> Job:
        """Cancel a job.

        A queued job becomes ``cancelled`` at once.  A running job keeps
        running with ``cancel_requested`` set until its worker stops and calls
        :meth:`confirm_cancelled`.  Final jobs cannot be cancelled.
        """
        with self._write() as conn:
            job = self._get(conn, job_id)
            if job.status is JobStatus.QUEUED:
                return self._apply(
                    conn,
                    job,
                    JobStatus.CANCELLED,
                    {"cancel_requested": 1, "finished_at": self._clock()},
                    "cancelled while queued",
                )
            check_transition(job.status, JobStatus.CANCELLED)
            if job.cancel_requested:
                return job
            now = self._clock()
            conn.execute(
                "UPDATE jobs SET cancel_requested = 1, updated_at = ? "
                "WHERE id = ? AND status = ?",
                (now, job_id, job.status.value),
            )
            self._event(
                conn, job_id, job.status, job.status, "cancellation requested", now
            )
            return self._get(conn, job_id)

    # -- internals -----------------------------------------------------------

    def _finish_running(
        self,
        job_id: str,
        worker_id: str,
        target: JobStatus,
        fields: dict,
        note: str,
    ) -> Job:
        with self._write() as conn:
            job = self._get(conn, job_id)
            self._check_owner(job, worker_id, target)
            return self._apply(
                conn, job, target, {**fields, "finished_at": self._clock()}, note
            )

    @staticmethod
    def _check_owner(job: Job, worker_id: str, target: JobStatus) -> None:
        validate_worker_id(worker_id)
        check_transition(job.status, target)
        if job.claimed_by != worker_id:
            raise InvalidTransitionError("The job is claimed by a different worker")

    def _apply(
        self,
        conn: sqlite3.Connection,
        job: Job,
        target: JobStatus,
        fields: dict,
        note: str,
    ) -> Job:
        """Change status from the state that was checked, and log it."""
        check_transition(job.status, target)
        now = self._clock()
        assignments = {"status": target.value, "updated_at": now, **fields}
        columns = ", ".join(f"{name} = ?" for name in assignments)
        cursor = conn.execute(
            f"UPDATE jobs SET {columns} WHERE id = ? AND status = ?",
            (*assignments.values(), job.id, job.status.value),
        )
        if cursor.rowcount != 1:  # pragma: no cover - guarded by the write lock
            raise InvalidTransitionError("The job changed during the update")
        self._event(conn, job.id, job.status, target, note, now)
        if target.is_final:
            # The outbox row commits (or rolls back) with the final status.
            # OR IGNORE: a final job is never re-finished, but should that
            # ever happen the first outcome's notification is kept.
            conn.execute(
                "INSERT OR IGNORE INTO job_notifications "
                "(job_id, outcome, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (job.id, target.value, NotificationState.PENDING.value, now, now),
            )
        return self._get(conn, job.id)

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        job_id: str,
        from_status: Optional[JobStatus],
        to_status: JobStatus,
        note: str,
        at: str,
    ) -> None:
        conn.execute(
            "INSERT INTO job_events (job_id, at, from_status, to_status, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                job_id,
                at,
                from_status.value if from_status else None,
                to_status.value,
                note,
            ),
        )

    @staticmethod
    def _fetch(conn: sqlite3.Connection, job_id: Any) -> Optional[sqlite3.Row]:
        if not isinstance(job_id, str):
            return None
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def _get(self, conn: sqlite3.Connection, job_id: Any) -> Job:
        row = self._fetch(conn, job_id)
        if row is None:
            raise JobNotFoundError(f"No job with id {job_id!r}")
        return _row_to_job(row)

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise _wrap(exc) from None
            try:
                yield self._conn
            except BaseException as exc:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise _wrap(exc) from None
                raise
            else:
                try:
                    self._conn.execute("COMMIT")
                except sqlite3.Error as exc:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.execute("ROLLBACK")
                    raise _wrap(exc) from None

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
            except sqlite3.Error as exc:
                raise _wrap(exc) from None

    def _read_one(self, sql: str) -> tuple:
        with self._read() as conn:
            return tuple(conn.execute(sql).fetchone())

    def _initialise(self) -> None:
        conn = self._conn
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            # Identify the file before changing anything about it.
            _check_identity(conn)
            # WAL lets status reads proceed while a worker writes.  Some
            # filesystems (network shares) cannot do WAL; SQLite then keeps
            # its rollback journal, which is still correct, only slower.
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.DatabaseError:
            raise JobStoreError("The file is not a JARVIS job database") from None
        if str(mode).lower() != "wal":
            logger.warning("Job database is using journal mode %s, not WAL", mode)
        with self._write() as conn:
            # Checked again under the write lock: another process may have
            # created the schema since the first check.
            version = _check_identity(conn)
            if version == 0:
                for statement in _SCHEMA_V1:
                    conn.execute(statement)
                version = 1
            while version < SCHEMA_VERSION:
                if version not in _MIGRATIONS:  # pragma: no cover - none yet
                    raise SchemaVersionError(
                        f"No upgrade path from job schema version {version}"
                    )
                for statement in _MIGRATIONS[version]:
                    conn.execute(statement)
                version += 1
            # PRAGMA does not accept bound parameters; both values are ints.
            conn.execute(f"PRAGMA application_id = {int(APPLICATION_ID)}")
            conn.execute(f"PRAGMA user_version = {int(version)}")


def _check_identity(conn: sqlite3.Connection) -> int:
    """Return the schema version, refusing foreign or newer databases."""
    app_id = conn.execute("PRAGMA application_id").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if app_id not in (0, APPLICATION_ID) or (
        app_id == 0 and (version != 0 or _has_tables(conn))
    ):
        raise JobStoreError("The file is not a JARVIS job database")
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"The job database uses schema version {version}, but this "
            f"JARVIS supports up to {SCHEMA_VERSION}. Update JARVIS; the "
            "database was not changed."
        )
    return version


def _prepare_file(path: Path) -> None:
    """Refuse odd targets and create a new database file readable only by you."""
    try:
        if path.is_symlink():
            raise JobStoreError("The job database path must not be a symbolic link")
        if path.exists() and not path.is_file():
            raise JobStoreError("The job database path is not a regular file")
        if not path.parent.is_dir():
            raise JobStoreError("The job database folder does not exist")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        os.close(fd)
    except OSError as exc:
        raise JobStoreError(
            f"Cannot create the job database ({exc.strerror or type(exc).__name__})"
        ) from None


def _has_tables(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1"
        ).fetchone()
        is not None
    )


def _wrap(exc: sqlite3.Error) -> JobStoreError:
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
        return JobStoreError("The job database is busy; try again")
    return JobStoreError(f"Job database error ({type(exc).__name__})")


def _shift(timestamp: str, seconds: float) -> str:
    moved = datetime.fromisoformat(timestamp) + timedelta(seconds=seconds)
    return moved.isoformat(timespec="milliseconds")


def _check_delay(delay_seconds: Any) -> float:
    if (
        isinstance(delay_seconds, bool)
        or not isinstance(delay_seconds, (int, float))
        or not 0 <= delay_seconds <= MAX_RETRY_DELAY_SECONDS
    ):
        raise InvalidJobError(
            f"Retry delay must be 0–{MAX_RETRY_DELAY_SECONDS:g} seconds"
        )
    return float(delay_seconds)


def _encode_result(result: Optional[dict]) -> Optional[str]:
    if result is None:
        return None
    return encode_json_object(result, "result", MAX_RESULT_BYTES)


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        kind=row["kind"],
        status=JobStatus(row["status"]),
        payload=json.loads(row["payload"]),
        parent_id=row["parent_id"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        cancel_requested=bool(row["cancel_requested"]),
        claimed_by=row["claimed_by"],
        result=json.loads(row["result"]) if row["result"] is not None else None,
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        heartbeat_at=row["heartbeat_at"],
        not_before=row["not_before"],
        progress=row["progress"],
        stage=row["stage"],
    )


_NOTIFICATION_SELECT = (
    "SELECT n.*, j.kind AS kind, j.error_code AS error_code "
    "FROM job_notifications n JOIN jobs j ON j.id = n.job_id "
)


def _row_to_notification(row: sqlite3.Row) -> JobNotification:
    return JobNotification(
        job_id=row["job_id"],
        kind=row["kind"],
        outcome=JobStatus(row["outcome"]),
        error_code=row["error_code"],
        state=NotificationState(row["state"]),
        attempts=row["attempts"],
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
        not_before=row["not_before"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        delivered_at=row["delivered_at"],
    )


def open_job_store(
    settings: Optional[JobSettings] = None, *, config_path: Optional[Path] = None
) -> JobStore:
    """Open the store configured in ``[jarvis.jobs]``.

    The default location's ``jarvis/`` folder is created (mode 0700) inside
    the OpenJarvis home.  A custom ``database_path`` must be in an existing
    folder, so a path on an unplugged drive fails instead of creating
    folders under ``/Volumes`` (as for ``output_root``, D11).
    """
    if settings is None:
        settings = JobSettings.load(config_path)
    path = settings.database_file()
    if settings.uses_default_path:
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise JobStoreError(
                "Cannot create the job database folder "
                f"({exc.strerror or type(exc).__name__})"
            ) from None
    return JobStore(path, default_max_attempts=settings.max_attempts)


__all__ = [
    "APPLICATION_ID",
    "JobStore",
    "JobStoreError",
    "MAX_NOTIFICATION_ATTEMPTS",
    "NOTIFICATION_RETRY_SECONDS",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "open_job_store",
]
