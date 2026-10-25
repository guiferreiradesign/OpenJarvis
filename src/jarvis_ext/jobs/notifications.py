"""Deliver the notifications the job store owes for finished jobs.

The store writes one ``job_notifications`` row (the outbox) in the same
transaction as a job's final status.  :func:`deliver_notifications` sends
the due rows through a :class:`~jarvis_ext.notify.Notifier`:

1. claim due rows (``pending`` → ``sending``) in one write transaction;
2. call the notifier, outside any transaction;
3. record ``delivered``, or a failed attempt (retried later, or given up).

A crash between 2 and 3 leaves the row ``sending``; once its lease expires
it is claimed and sent again.  Delivery is therefore **at least once**, with
a duplicate possible only if the process dies in the moment between showing
a notification and recording it.  Recording first would instead lose that
notification; for "your render is done" a rare repeat is the better failure.

Nothing here can change a job: notifier errors, database errors and bugs
in a notifier are logged (with a short code, never the text) and counted.
Messages are built only from validated identifiers (kind, short id,
outcome, error code), never from error messages, payloads or results, so
they cannot carry paths, plan contents or credentials.
"""

from __future__ import annotations

import logging
from typing import Dict

from jarvis_ext.jobs.models import (
    JobError,
    JobNotification,
    JobStatus,
    NotificationState,
)
from jarvis_ext.jobs.store import JobStore
from jarvis_ext.notify import Notification, NotificationError, Notifier

logger = logging.getLogger(__name__)

# A claimed notification is presumed abandoned after this long.  Kept well
# above the osascript timeout so a slow but live send is never duplicated.
MIN_LEASE_SECONDS = 60.0
SHORT_ID_CHARS = 8

_VERBS = {
    JobStatus.SUCCEEDED: "succeeded",
    JobStatus.FAILED: "failed",
    JobStatus.CANCELLED: "was cancelled",
}


def job_notification(record: JobNotification) -> Notification:
    """The title and message for a finished job (safe, bounded text)."""
    outcome = record.outcome
    short_id = record.job_id[:SHORT_ID_CHARS]
    message = f"{record.kind} job {short_id} {_VERBS[outcome]}"
    if outcome is JobStatus.FAILED and record.error_code:
        message += f" ({record.error_code})"
    return Notification(title=f"JARVIS: job {outcome.value}", message=message + ".")


def new_counts() -> Dict[str, int]:
    return {"delivered": 0, "retrying": 0, "failed": 0}


def deliver_notifications(
    store: JobStore,
    notifier: Notifier,
    worker_id: str,
    *,
    lease_seconds: float = MIN_LEASE_SECONDS,
    limit: int = 20,
) -> Dict[str, int]:
    """Send due notifications once each; returns counts.  Never raises."""
    counts = new_counts()
    try:
        claimed = store.claim_notifications(
            worker_id, lease_before=store.time_ago(lease_seconds), limit=limit
        )
    except JobError as exc:
        logger.warning("Could not read pending notifications: %s", exc)
        return counts
    for record in claimed:
        short_id = record.job_id[:SHORT_ID_CHARS]
        try:
            notifier.send(job_notification(record))
        except NotificationError as exc:
            code, retryable = exc.code, exc.retryable
        except Exception as exc:
            # A notifier bug must not stop the worker; the class name is
            # enough to diagnose it and cannot leak notification data.
            code, retryable = "notifier_error", True
            logger.warning(
                "Notifier %s raised %s for job %s",
                getattr(notifier, "name", "?"),
                type(exc).__name__,
                short_id,
            )
        else:
            try:
                if store.notification_delivered(record.job_id, worker_id):
                    counts["delivered"] += 1
                else:
                    logger.warning(
                        "Notification for job %s was sent after its claim expired",
                        short_id,
                    )
            except JobError as exc:
                logger.warning(
                    "Notification for job %s was sent but not recorded: %s",
                    short_id,
                    exc,
                )
            continue
        try:
            updated = store.notification_failed(
                record.job_id, worker_id, code, retryable=retryable
            )
        except JobError as exc:
            logger.warning(
                "Failed notification for job %s not recorded: %s", short_id, exc
            )
            continue
        if updated is not None and updated.state is NotificationState.PENDING:
            counts["retrying"] += 1
            logger.warning(
                "Notification for job %s failed (%s); will retry", short_id, code
            )
        else:
            counts["failed"] += 1
            logger.warning(
                "Notification for job %s failed (%s); giving up", short_id, code
            )
    return counts


__all__ = [
    "MIN_LEASE_SECONDS",
    "deliver_notifications",
    "job_notification",
    "new_counts",
]
