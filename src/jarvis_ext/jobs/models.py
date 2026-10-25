"""The job model and its status rules, independent of what a job does.

A job is a unit of long-running work: a *kind* (``"video.render"``, later
other skills' kinds), an opaque JSON *payload* the matching runner
understands, and a status that only moves along these edges::

    queued ──claim──► running ──► succeeded
       │                 │  ├───► failed
       │                 │  └───► cancelled   (after a cancel request)
       │                 └──retry──► queued   (only while attempts < max_attempts)
       └──cancel──► cancelled

``succeeded``, ``failed`` and ``cancelled`` are final.  A revision is a new
job with ``parent_id`` set; finished jobs are never reopened.

Nothing here knows about video, FFmpeg or any particular skill.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, FrozenSet, Mapping, Optional

# D23: at most two attempts, and only for failures a runner classifies as
# temporary.  The store enforces the ceiling; runners decide what to retry.
MAX_ATTEMPTS = 2

MAX_KIND_LENGTH = 64
MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_ERROR_MESSAGE_CHARS = 4000
MAX_WORKER_ID_LENGTH = 128

# Dotted lower-case names, e.g. "video.render" or "research.report".
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STAGE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# Progress is a percentage of the whole job.  While a job runs it stays
# below 100; only a successful completion records 100.
MAX_RUNNING_PROGRESS = 99.0


class JobError(Exception):
    """Base class for job model and store errors."""


class InvalidJobError(JobError, ValueError):
    """A job field (kind, payload, result, error, worker id) is invalid."""


class InvalidTransitionError(JobError):
    """The requested status change is not allowed from the job's state."""


class JobNotFoundError(JobError):
    """No job has the given id."""


class JobStatus(str, Enum):
    """Lifecycle states shared by every job kind."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_final(self) -> bool:
        return self in FINAL_STATUSES


FINAL_STATUSES: FrozenSet[JobStatus] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
)

TRANSITIONS: Mapping[JobStatus, FrozenSet[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.QUEUED,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


def check_transition(current: JobStatus, target: JobStatus) -> None:
    """Raise :class:`InvalidTransitionError` unless *current* → *target* is allowed."""
    if target not in TRANSITIONS[current]:
        raise InvalidTransitionError(
            f"A {current.value} job cannot become {target.value}"
        )


def validate_kind(kind: Any) -> str:
    if (
        not isinstance(kind, str)
        or len(kind) > MAX_KIND_LENGTH
        or not _KIND_RE.match(kind)
    ):
        raise InvalidJobError(
            "Job kind must be a dotted lower-case name such as 'video.render' "
            f"(at most {MAX_KIND_LENGTH} characters)"
        )
    return kind


def validate_error_code(code: Any) -> str:
    if not isinstance(code, str) or not _ERROR_CODE_RE.match(code):
        raise InvalidJobError(
            "Error code must be a lower-case identifier such as 'ffmpeg_failed'"
        )
    return code


def validate_worker_id(worker_id: Any) -> str:
    if (
        not isinstance(worker_id, str)
        or not worker_id.strip()
        or len(worker_id) > MAX_WORKER_ID_LENGTH
        or not worker_id.isprintable()
    ):
        raise InvalidJobError(
            f"Worker id must be 1–{MAX_WORKER_ID_LENGTH} printable characters"
        )
    return worker_id


def validate_stage(stage: Any) -> str:
    if not isinstance(stage, str) or not _STAGE_RE.match(stage):
        raise InvalidJobError("Stage must be a short lower-case name such as 'render'")
    return stage


def clean_progress(progress: Any) -> float:
    """A running job's percentage: finite, clamped to 0–``MAX_RUNNING_PROGRESS``."""
    if isinstance(progress, bool) or not isinstance(progress, (int, float)):
        raise InvalidJobError("Progress must be a number")
    if progress != progress or progress in (float("inf"), float("-inf")):
        raise InvalidJobError("Progress must be a finite number")
    return round(min(max(float(progress), 0.0), MAX_RUNNING_PROGRESS), 1)


def clean_error_message(message: Any) -> str:
    if not isinstance(message, str):
        raise InvalidJobError("Error message must be a string")
    return message[:MAX_ERROR_MESSAGE_CHARS]


def encode_json_object(value: Any, label: str, max_bytes: int) -> str:
    """Serialise a JSON object deterministically, refusing anything lossy.

    Only plain JSON types are accepted (no tuples, NaN, non-string keys), so
    what a runner reads back is exactly what was submitted.
    """
    if not isinstance(value, dict):
        raise InvalidJobError(f"Job {label} must be a JSON object")
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidJobError(
            f"Job {label} must contain only JSON values (no NaN or infinity)"
        ) from None
    # Tuples become lists and integer keys become strings; refuse both.
    if json.loads(text) != value:
        raise InvalidJobError(f"Job {label} must contain only plain JSON types")
    if len(text.encode("utf-8")) > max_bytes:
        raise InvalidJobError(f"Job {label} is larger than {max_bytes} bytes")
    return text


@dataclass(frozen=True)
class Job:
    """A job as stored; returned by every store operation."""

    id: str
    kind: str
    status: JobStatus
    payload: Dict[str, Any]
    parent_id: Optional[str]
    attempts: int
    max_attempts: int
    cancel_requested: bool
    claimed_by: Optional[str]
    result: Optional[Dict[str, Any]]
    error_code: Optional[str]
    error_message: Optional[str]
    created_at: str
    updated_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    heartbeat_at: Optional[str] = None
    not_before: Optional[str] = None
    progress: Optional[float] = None
    stage: Optional[str] = None

    @property
    def is_final(self) -> bool:
        return self.status.is_final

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status.value,
            "payload": self.payload,
            "parent_id": self.parent_id,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "cancel_requested": self.cancel_requested,
            "claimed_by": self.claimed_by,
            "result": self.result,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
            "not_before": self.not_before,
            "progress": self.progress,
            "stage": self.stage,
        }


class NotificationState(str, Enum):
    """Delivery state of a finished job's notification (the outbox row)."""

    PENDING = "pending"  # waiting to be sent (or to be retried)
    SENDING = "sending"  # claimed by a worker; recovered if it never finishes
    DELIVERED = "delivered"
    FAILED = "failed"  # gave up; the job itself is unaffected


@dataclass(frozen=True)
class JobNotification:
    """The notification owed for one finished job.

    Created in the same transaction as the job's final status, at most once
    per job (final states are never left).  ``kind`` and ``error_code`` are
    copied from the job when read, so a notifier needs nothing else.
    """

    job_id: str
    kind: str
    outcome: JobStatus
    error_code: Optional[str]
    state: NotificationState
    attempts: int
    claimed_by: Optional[str]
    claimed_at: Optional[str]
    not_before: Optional[str]
    last_error: Optional[str]
    created_at: str
    updated_at: str
    delivered_at: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "state": self.state.value,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "retry_not_before": self.not_before,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
        }


@dataclass(frozen=True)
class JobEvent:
    """One entry in a job's append-only history."""

    job_id: str
    at: str
    from_status: Optional[JobStatus]
    to_status: JobStatus
    note: str


__all__ = [
    "FINAL_STATUSES",
    "InvalidJobError",
    "InvalidTransitionError",
    "Job",
    "JobError",
    "JobEvent",
    "JobNotFoundError",
    "JobNotification",
    "JobStatus",
    "MAX_ATTEMPTS",
    "NotificationState",
    "TRANSITIONS",
    "check_transition",
    "encode_json_object",
]
