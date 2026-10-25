"""Job management shared by every JARVIS skill.

A job records long-running work from submission to a final status.  This
package only stores jobs and enforces their status rules; what a job *does*
is decided by the runner for its ``kind`` (the first will be
``video.render``, added in Phase 3 step 2).  See ``jarvis/jobs.md``.
"""

from __future__ import annotations

from jarvis_ext.jobs.models import (
    FINAL_STATUSES,
    MAX_ATTEMPTS,
    TRANSITIONS,
    InvalidJobError,
    InvalidTransitionError,
    Job,
    JobError,
    JobEvent,
    JobNotFoundError,
    JobStatus,
)
from jarvis_ext.jobs.settings import JobSettings
from jarvis_ext.jobs.store import (
    SCHEMA_VERSION,
    JobStore,
    JobStoreError,
    SchemaVersionError,
    open_job_store,
)

__all__ = [
    "FINAL_STATUSES",
    "InvalidJobError",
    "InvalidTransitionError",
    "Job",
    "JobError",
    "JobEvent",
    "JobNotFoundError",
    "JobSettings",
    "JobStatus",
    "JobStore",
    "JobStoreError",
    "MAX_ATTEMPTS",
    "SCHEMA_VERSION",
    "SchemaVersionError",
    "TRANSITIONS",
    "open_job_store",
]
