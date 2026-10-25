"""Phase 3 step 5: notifications for finished jobs.

Everything here uses fakes: recording notifiers, an injected store clock
and a fake process runner for ``osascript``.  An autouse fixture replaces
the notifier module's default runner with one that fails the test, so no
automated run can ever show a real desktop notification.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from jarvis_ext import notify
from jarvis_ext.__main__ import main
from jarvis_ext.config import ExtensionConfigError
from jarvis_ext.jobs import (
    JobSettings,
    JobStatus,
    JobStore,
    NotificationState,
)
from jarvis_ext.jobs.notifications import (
    MIN_LEASE_SECONDS,
    deliver_notifications,
    job_notification,
)
from jarvis_ext.jobs.runner import JobContext, JobOutcome
from jarvis_ext.jobs.store import (
    _MIGRATIONS,
    _SCHEMA_V1,
    APPLICATION_ID,
    MAX_NOTIFICATION_ATTEMPTS,
    NOTIFICATION_RETRY_SECONDS,
    SCHEMA_VERSION,
    JobStoreError,
)
from jarvis_ext.jobs.worker import Worker
from jarvis_ext.notify import (
    APPLESCRIPT,
    LogNotifier,
    MacOSNotifier,
    Notification,
    NotificationError,
    clean_text,
    make_notifier,
)

WORKER = "worker-1"
KIND = "test.echo"
FAST = {
    "retry_delay_seconds": 0,
    "poll_seconds": 0.1,
    "heartbeat_seconds": 0.05,
    "stale_after_seconds": 1,
}
HOSTILE = (
    '-x" & (do shell script "rm -rf ~") & "\n\x07 /Users/me/Footage/raw.mov '
    "~/secret.txt ./plan.json C:\\Users\\me end"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.now.isoformat(timespec="milliseconds")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingNotifier:
    name = "recording"

    def __init__(self, error: Any = None) -> None:
        self.sent: List[Notification] = []
        self.error = error
        self.lock = threading.Lock()

    def send(self, notification: Notification) -> None:
        with self.lock:
            self.sent.append(notification)
        if self.error is not None:
            raise self.error


class FakeRunner:
    def __init__(self, returncode: int = 0, error: Any = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.returncode = returncode
        self.error = error

    def __call__(self, cmd: List[str], **kwargs: Any) -> Any:
        self.calls.append({"cmd": cmd, **kwargs})
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(cmd, self.returncode, b"", b"secret err")


@pytest.fixture(autouse=True)
def no_real_osascript(monkeypatch):
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a test tried to run the real osascript")

    monkeypatch.setattr(notify, "_default_runner", refuse)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock):
    with JobStore(tmp_path / "jobs.db", clock=clock) as s:
        yield s


def finished(store: JobStore, status: JobStatus = JobStatus.SUCCEEDED, **kw):
    job = store.submit(kw.pop("kind", KIND), {})
    store.claim_next(WORKER)
    if status is JobStatus.SUCCEEDED:
        return store.complete(job.id, WORKER, {"output": "reel_v001.mp4"})
    if status is JobStatus.FAILED:
        return store.fail(
            job.id,
            WORKER,
            kw.pop("code", "ffmpeg_failed"),
            kw.pop("message", "FFmpeg could not read /Users/me/Footage/raw.mov"),
        )
    store.cancel(job.id)
    return store.confirm_cancelled(job.id, WORKER)


def make_worker(store: JobStore, tmp_path: Path, handlers, notifier=None, **kw):
    return Worker(
        store,
        handlers,
        lock_path=tmp_path / "jobs.db.worker.lock",
        settings=JobSettings.from_mapping({**FAST, **kw}),
        worker_id="this-worker",
        notifier=notifier,
    )


class Echo:
    def __init__(self, outcome: JobOutcome = JobOutcome.succeeded({"ok": True})):
        self.outcome = outcome

    def run(self, context: JobContext) -> JobOutcome:
        return self.outcome


# -- notification text ----------------------------------------------------------


class TestText:
    def test_clean_text_removes_control_paths_and_bounds_length(self):
        text = clean_text(HOSTILE, 60)
        assert len(text) <= 60
        assert "\n" not in text and "\x07" not in text
        for secret in ("/Users", "Footage", "raw.mov", "secret.txt", "plan.json"):
            assert secret not in text
        assert not text.startswith("-")

    def test_clean_text_keeps_ordinary_words(self):
        assert clean_text("video.render job 1a2b3c4d failed (ffmpeg_failed).", 240) == (
            "video.render job 1a2b3c4d failed (ffmpeg_failed)."
        )
        assert clean_text("1/2 done", 40) == "1/2 done"

    def test_notification_cleans_itself(self):
        n = Notification(title="\n\n", message=HOSTILE * 10)
        assert n.title == "JARVIS"
        assert len(n.message) <= notify.MAX_MESSAGE_CHARS
        assert "/Users" not in n.message and "\x07" not in n.message

    @pytest.mark.parametrize(
        "status, title, message",
        [
            (
                JobStatus.SUCCEEDED,
                "JARVIS: job succeeded",
                "video.render job {id} succeeded.",
            ),
            (
                JobStatus.FAILED,
                "JARVIS: job failed",
                "video.render job {id} failed (ffmpeg_failed).",
            ),
            (
                JobStatus.CANCELLED,
                "JARVIS: job cancelled",
                "video.render job {id} was cancelled.",
            ),
        ],
    )
    def test_job_messages(self, store, status, title, message):
        job = finished(store, status, kind="video.render")
        n = job_notification(store.notification(job.id))
        assert n.title == title
        assert n.message == message.format(id=job.id[:8])

    def test_failure_message_never_includes_error_text_or_results(self, store):
        job = finished(
            store,
            JobStatus.FAILED,
            code="source_missing",
            message="Missing /Users/me/Footage/raw.mov; API_KEY=abc",
        )
        n = job_notification(store.notification(job.id))
        text = n.title + n.message
        for secret in ("/Users", "raw.mov", "API_KEY", "abc", job.id[8:]):
            assert secret not in text
        assert "source_missing" in n.message


# -- the outbox in the store ----------------------------------------------------


class TestOutbox:
    @pytest.mark.parametrize(
        "status", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED]
    )
    def test_every_final_status_owes_one_notification(self, store, status):
        job = finished(store, status)
        record = store.notification(job.id)
        assert record.outcome is status
        assert record.state is NotificationState.PENDING
        assert record.attempts == 0
        assert record.created_at == job.finished_at

    def test_cancel_while_queued_and_recovery_owe_notifications(self, store, clock):
        queued = store.submit(KIND, {})
        store.cancel(queued.id)
        assert store.notification(queued.id).outcome is JobStatus.CANCELLED

        stale = store.submit(KIND, {}, max_attempts=1)
        store.claim_next("dead-worker")
        clock.advance(300)
        store.recover(stale.id, stale_before=store.time_ago(120), requeue=True)
        assert store.notification(stale.id).outcome is JobStatus.FAILED

    def test_non_final_changes_owe_nothing(self, store):
        job = store.submit(KIND, {})
        assert store.notification(job.id) is None
        store.claim_next(WORKER)
        store.report_progress(job.id, WORKER, stage="render", progress=40)
        store.add_note(job.id, WORKER, "rendered")
        store.heartbeat(job.id, WORKER)
        store.cancel(job.id)  # only a request while running
        assert store.notification(job.id) is None
        retry = store.submit(KIND, {})
        store.claim_next(WORKER)  # the first job is still running
        assert store.notification(retry.id) is None

    def test_retry_owes_nothing_until_final(self, store):
        job = store.submit(KIND, {})
        store.claim_next(WORKER)
        store.retry(job.id, WORKER, "temporary")
        assert store.notification(job.id) is None
        store.claim_next(WORKER)
        store.fail(job.id, WORKER, "temporary")
        assert store.notification(job.id).outcome is JobStatus.FAILED

    def test_refused_final_change_creates_nothing(self, store):
        job = store.submit(KIND, {})
        store.claim_next(WORKER)
        with pytest.raises(Exception):
            store.complete(job.id, "someone-else")
        assert store.notification(job.id) is None

    def test_claim_is_once_during_polling(self, store):
        job = finished(store)
        lease = store.time_ago(MIN_LEASE_SECONDS)
        (claimed,) = store.claim_notifications(WORKER, lease_before=lease)
        assert claimed.job_id == job.id and claimed.state is NotificationState.SENDING
        assert claimed.attempts == 1
        for _ in range(5):
            assert store.claim_notifications(WORKER, lease_before=lease) == []
        assert store.notification_delivered(job.id, WORKER)
        assert store.notification(job.id).state is NotificationState.DELIVERED
        assert store.claim_notifications(WORKER, lease_before=lease) == []

    def test_only_the_claimer_records_delivery(self, store):
        job = finished(store)
        store.claim_notifications(WORKER, lease_before=store.time_ago(60))
        assert not store.notification_delivered(job.id, "other")
        assert store.notification_failed(job.id, "other", "timeout") is None
        assert store.notification(job.id).state is NotificationState.SENDING

    def test_abandoned_claim_is_recovered_after_its_lease(self, store, clock):
        job = finished(store)
        store.claim_notifications("dead-worker", lease_before=store.time_ago(60))
        clock.advance(59)
        assert store.claim_notifications(WORKER, lease_before=store.time_ago(60)) == []
        clock.advance(2)
        (again,) = store.claim_notifications(WORKER, lease_before=store.time_ago(60))
        assert again.job_id == job.id and again.claimed_by == WORKER
        assert again.attempts == 2

    def test_failed_attempts_back_off_then_give_up(self, store, clock):
        job = finished(store)
        lease = store.time_ago
        for attempt in range(1, MAX_NOTIFICATION_ATTEMPTS):
            (claimed,) = store.claim_notifications(WORKER, lease_before=lease(60))
            assert claimed.attempts == attempt
            record = store.notification_failed(job.id, WORKER, "timeout")
            assert record.state is NotificationState.PENDING
            assert record.last_error == "timeout"
            clock.advance(NOTIFICATION_RETRY_SECONDS - 1)
            assert store.claim_notifications(WORKER, lease_before=lease(60)) == []
            clock.advance(1)
        store.claim_notifications(WORKER, lease_before=lease(60))
        record = store.notification_failed(job.id, WORKER, "timeout")
        assert record.state is NotificationState.FAILED
        clock.advance(3600)
        assert store.claim_notifications(WORKER, lease_before=lease(60)) == []
        assert store.get(job.id).status is JobStatus.SUCCEEDED

    def test_non_retryable_failure_gives_up_at_once(self, store):
        job = finished(store)
        store.claim_notifications(WORKER, lease_before=store.time_ago(60))
        record = store.notification_failed(
            job.id, WORKER, "unsupported_platform", retryable=False
        )
        assert record.state is NotificationState.FAILED and record.attempts == 1

    def test_repeatedly_abandoned_claim_is_given_up(self, store, clock):
        job = finished(store)
        for _ in range(MAX_NOTIFICATION_ATTEMPTS):
            store.claim_notifications("dead", lease_before=store.time_ago(60))
            clock.advance(61)
        assert store.claim_notifications(WORKER, lease_before=store.time_ago(60)) == []
        record = store.notification(job.id)
        assert record.state is NotificationState.FAILED
        assert record.last_error == "abandoned"

    def test_concurrent_claimers_never_share_a_notification(self, tmp_path):
        path = tmp_path / "jobs.db"
        with JobStore(path) as setup:
            ids = {finished(setup).id for _ in range(30)}
        notifier = RecordingNotifier()
        barrier = threading.Barrier(6)

        def deliver(n: int) -> None:
            with JobStore(path) as own:
                barrier.wait()
                for _ in range(10):
                    deliver_notifications(own, notifier, f"w{n}", limit=3)

        threads = [threading.Thread(target=deliver, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sent = [n.message.split()[2] for n in notifier.sent]
        assert sorted(sent) == sorted(i[:8] for i in ids)
        with JobStore(path) as check:
            assert all(
                check.notification(i).state is NotificationState.DELIVERED for i in ids
            )

    def test_notification_survives_reopening(self, tmp_path):
        path = tmp_path / "jobs.db"
        with JobStore(path) as first:
            job = finished(first)
        with JobStore(path) as second:
            assert second.notification(job.id).state is NotificationState.PENDING


# -- schema upgrade -------------------------------------------------------------


def _make_v3(path: Path) -> None:
    conn = sqlite3.connect(path)
    for statement in _SCHEMA_V1:
        conn.execute(statement)
    for version in (1, 2):
        for statement in _MIGRATIONS[version]:
            conn.execute(statement)
    now = "2026-01-01T00:00:00.000+00:00"
    conn.execute(
        "INSERT INTO jobs (id, kind, status, payload, max_attempts, created_at, "
        "updated_at, finished_at, attempts) "
        "VALUES (?, 'video.render', 'succeeded', '{}', 2, ?, ?, ?, 1)",
        ("a" * 32, now, now, now),
    )
    conn.execute(
        "INSERT INTO jobs (id, kind, status, payload, max_attempts, created_at, "
        "updated_at, started_at, claimed_by, attempts, progress, stage) "
        "VALUES (?, 'video.render', 'running', '{}', 2, ?, ?, ?, 'old', 1, 40, "
        "'render')",
        ("b" * 32, now, now, now),
    )
    conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()


class TestSchemaV4:
    def test_version_3_database_is_upgraded_without_old_notifications(self, tmp_path):
        path = tmp_path / "old.db"
        _make_v3(path)
        with JobStore(path) as upgraded:
            assert upgraded.schema_version == SCHEMA_VERSION == 4
            assert max(_MIGRATIONS) + 1 == SCHEMA_VERSION
            # Work finished before the upgrade is not announced now.
            assert upgraded.notification("a" * 32) is None
            lease = upgraded.time_ago(60)
            assert upgraded.claim_notifications(WORKER, lease_before=lease) == []
            # A job running across the upgrade owes one when it finishes.
            old = upgraded.get("b" * 32)
            assert old.progress == 40 and old.stage == "render"
            upgraded.fail(old.id, "old", "interrupted")
            assert upgraded.notification(old.id).outcome is JobStatus.FAILED

    def test_upgrade_is_idempotent_on_reopen(self, tmp_path):
        path = tmp_path / "old.db"
        _make_v3(path)
        JobStore(path).close()
        with JobStore(path) as again:
            assert again.schema_version == 4
            assert len(again.list_jobs()) == 2

    def test_outbox_rejects_bad_values(self, tmp_path):
        path = tmp_path / "jobs.db"
        with JobStore(path) as s:
            job = s.submit(KIND, {})
        conn = sqlite3.connect(path)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO job_notifications (job_id, outcome, state, "
                "created_at, updated_at) VALUES (?, 'running', 'pending', 'x', 'x')",
                (job.id,),
            )
        conn.close()


# -- delivery -------------------------------------------------------------------


class TestDeliver:
    def test_delivers_each_outcome_once(self, store):
        jobs = [finished(store, s) for s in JobStatus if s.is_final]
        notifier = RecordingNotifier()
        assert deliver_notifications(store, notifier, WORKER) == {
            "delivered": 3,
            "retrying": 0,
            "failed": 0,
        }
        assert [n.title for n in notifier.sent] == [
            "JARVIS: job succeeded",
            "JARVIS: job failed",
            "JARVIS: job cancelled",
        ]
        assert deliver_notifications(store, notifier, WORKER)["delivered"] == 0
        assert len(notifier.sent) == 3
        assert all(
            store.notification(j.id).state is NotificationState.DELIVERED for j in jobs
        )

    def test_notifier_error_is_retried_and_job_unchanged(self, store, clock):
        job = finished(store)
        failing = RecordingNotifier(
            NotificationError("timeout", "slow", retryable=True)
        )
        assert deliver_notifications(store, failing, WORKER)["retrying"] == 1
        assert store.get(job.id).status is JobStatus.SUCCEEDED
        clock.advance(NOTIFICATION_RETRY_SECONDS)
        working = RecordingNotifier()
        assert deliver_notifications(store, working, WORKER)["delivered"] == 1

    def test_non_retryable_error_gives_up(self, store):
        job = finished(store)
        failing = RecordingNotifier(
            NotificationError("unsupported_platform", "no", retryable=False)
        )
        assert deliver_notifications(store, failing, WORKER)["failed"] == 1
        assert store.notification(job.id).last_error == "unsupported_platform"

    def test_notifier_bug_is_contained_and_logged_safely(self, store, caplog):
        job = finished(store)
        broken = RecordingNotifier(RuntimeError("/Users/me/secret exploded"))
        with caplog.at_level(logging.WARNING):
            counts = deliver_notifications(store, broken, WORKER)
        assert counts["retrying"] == 1
        assert store.notification(job.id).last_error == "notifier_error"
        assert "RuntimeError" in caplog.text
        assert "/Users/me" not in caplog.text
        assert store.get(job.id).status is JobStatus.SUCCEEDED

    def test_database_error_is_contained(self, store, monkeypatch):
        finished(store)

        def busy(*args: Any, **kwargs: Any) -> Any:
            raise JobStoreError("The job database is busy; try again")

        monkeypatch.setattr(store, "claim_notifications", busy)
        notifier = RecordingNotifier()
        assert deliver_notifications(store, notifier, WORKER)["delivered"] == 0
        assert notifier.sent == []

    def test_unrecorded_delivery_is_logged_not_raised(self, store, monkeypatch, caplog):
        finished(store)

        def busy(*args: Any, **kwargs: Any) -> Any:
            raise JobStoreError("The job database is busy; try again")

        monkeypatch.setattr(store, "notification_delivered", busy)
        with caplog.at_level(logging.WARNING):
            counts = deliver_notifications(store, RecordingNotifier(), WORKER)
        assert counts == {"delivered": 0, "retrying": 0, "failed": 0}
        assert "not recorded" in caplog.text

    def test_log_notifier_writes_the_message(self, store, caplog):
        job = finished(store, JobStatus.FAILED)
        with caplog.at_level(logging.INFO, logger="jarvis_ext.notify"):
            deliver_notifications(store, LogNotifier(), WORKER)
        assert f"test.echo job {job.id[:8]} failed (ffmpeg_failed)." in caplog.text
        assert "raw.mov" not in caplog.text


# -- the worker -----------------------------------------------------------------


class TestWorkerNotifications:
    def test_default_notifier_is_log(self, store, tmp_path):
        worker = make_worker(store, tmp_path, {KIND: Echo()})
        assert isinstance(worker.notifier, LogNotifier)

    @pytest.mark.parametrize(
        "outcome, status",
        [
            (JobOutcome.succeeded({"ok": True}), JobStatus.SUCCEEDED),
            (JobOutcome.failed("bad_plan", "/Users/me/x"), JobStatus.FAILED),
        ],
    )
    def test_worker_notifies_after_a_job(self, store, tmp_path, outcome, status):
        job = store.submit(KIND, {})
        notifier = RecordingNotifier()
        worker = make_worker(store, tmp_path, {KIND: Echo(outcome)}, notifier)
        report = worker.run(once=True)
        assert store.get(job.id).status is status
        assert len(notifier.sent) == 1
        assert notifier.sent[0].title == f"JARVIS: job {status.value}"
        assert report["notifications"]["delivered"] == 1

    def test_cancelled_job_is_notified(self, store, tmp_path):
        job = store.submit(KIND, {})
        store.cancel(job.id)
        notifier = RecordingNotifier()
        make_worker(store, tmp_path, {KIND: Echo()}, notifier).run(once=True)
        assert [n.title for n in notifier.sent] == ["JARVIS: job cancelled"]

    def test_polling_does_not_repeat(self, store, tmp_path):
        store.submit(KIND, {})
        notifier = RecordingNotifier()
        worker = make_worker(store, tmp_path, {KIND: Echo()}, notifier)
        worker.run(once=True)
        for _ in range(3):
            make_worker(store, tmp_path, {KIND: Echo()}, notifier).run(once=True)
        assert len(notifier.sent) == 1

    def test_failing_notifier_never_fails_the_job_or_worker(self, store, tmp_path):
        first = store.submit(KIND, {})
        second = store.submit(KIND, {})
        notifier = RecordingNotifier(RuntimeError("boom"))
        worker = make_worker(store, tmp_path, {KIND: Echo()}, notifier)
        threading.Timer(1.0, worker.stop).start()
        report = worker.run()
        assert [p["id"] for p in report["processed"]] == [first.id, second.id]
        assert store.get(first.id).status is JobStatus.SUCCEEDED
        assert store.get(second.id).status is JobStatus.SUCCEEDED
        assert report["notifications"]["delivered"] == 0
        assert report["notifications"]["retrying"] == 2

    def test_restart_recovers_a_notification_left_sending(self, store, tmp_path, clock):
        job = finished(store)
        # A previous worker claimed it and died before recording the result.
        store.claim_notifications("dead-worker", lease_before=store.time_ago(60))
        notifier = RecordingNotifier()
        make_worker(store, tmp_path, {KIND: Echo()}, notifier).run(once=True)
        assert notifier.sent == []  # its lease has not expired yet
        clock.advance(MIN_LEASE_SECONDS + 1)
        report = make_worker(store, tmp_path, {KIND: Echo()}, notifier).run(once=True)
        assert len(notifier.sent) == 1 and report["notifications"]["delivered"] == 1
        record = store.notification(job.id)
        assert record.state is NotificationState.DELIVERED
        assert record.attempts == 2

    def test_restart_delivers_pending_notification(self, store, tmp_path):
        # The worker died after the job finished but before notifying.
        job = finished(store)
        notifier = RecordingNotifier()
        make_worker(store, tmp_path, {}, notifier).run(once=True)
        assert len(notifier.sent) == 1
        assert store.notification(job.id).state is NotificationState.DELIVERED

    def test_lease_is_never_shorter_than_the_minimum(self, store, tmp_path, clock):
        finished(store)
        store.claim_notifications("slow-worker", lease_before=store.time_ago(60))
        clock.advance(5)  # stale_after_seconds = 1, but the lease is 60
        notifier = RecordingNotifier()
        make_worker(store, tmp_path, {}, notifier).run(once=True)
        assert notifier.sent == []


# -- macOS backend --------------------------------------------------------------


@pytest.fixture
def osascript(tmp_path: Path) -> str:
    path = tmp_path / "osascript"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return str(path)


def mac(osascript: str, runner: Any) -> MacOSNotifier:
    return MacOSNotifier(osascript=osascript, runner=runner, platform="darwin")


class TestMacOS:
    def test_arguments_without_shell_or_interpolation(self, osascript, monkeypatch):
        monkeypatch.setenv("JARVIS_TEST_SECRET", "hunter2")
        runner = FakeRunner()
        n = Notification(title='Done" & quit & "', message=HOSTILE)
        mac(osascript, runner).send(n)
        (call,) = runner.calls
        cmd = call["cmd"]
        assert isinstance(cmd, list)
        assert cmd[0] == osascript
        script = cmd[1 : 1 + 2 * len(APPLESCRIPT)]
        assert script[0::2] == ["-e"] * len(APPLESCRIPT)
        assert tuple(script[1::2]) == APPLESCRIPT  # fixed, never formatted
        assert cmd[1 + 2 * len(APPLESCRIPT) :] == ["--", n.title, n.message]
        assert "shell" not in call or call["shell"] is False
        assert call["stdin"] is subprocess.DEVNULL
        assert call["timeout"] == notify.OSASCRIPT_TIMEOUT_SECONDS
        assert call["check"] is False
        assert set(call["env"]) <= {"PATH", "HOME"}
        assert "hunter2" not in json.dumps(call["env"])
        assert "/Users/me" not in n.message and not n.message.startswith("-")

    def test_success_logs(self, osascript, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis_ext.notify"):
            mac(osascript, FakeRunner()).send(Notification("T", "M"))
        assert "Notification (macOS): T — M" in caplog.text

    @pytest.mark.parametrize(
        "runner, code, retryable",
        [
            (FakeRunner(returncode=1), "osascript_failed", True),
            (FakeRunner(error=subprocess.TimeoutExpired("x", 10)), "timeout", True),
            (FakeRunner(error=FileNotFoundError()), "osascript_missing", False),
            (FakeRunner(error=PermissionError()), "osascript_unavailable", True),
        ],
    )
    def test_failures(self, osascript, runner, code, retryable, caplog):
        with caplog.at_level(logging.INFO, logger="jarvis_ext.notify"):
            with pytest.raises(NotificationError) as info:
                mac(osascript, runner).send(Notification("T", "M"))
        assert info.value.code == code and info.value.retryable is retryable
        assert "secret err" not in caplog.text and "secret err" not in str(info.value)

    def test_unsupported_platform(self, osascript):
        runner = FakeRunner()
        notifier = MacOSNotifier(osascript=osascript, runner=runner, platform="linux")
        with pytest.raises(NotificationError) as info:
            notifier.send(Notification("T", "M"))
        assert info.value.code == "unsupported_platform"
        assert info.value.retryable is False
        assert runner.calls == []
        with pytest.raises(NotificationError):
            make_notifier("macos", platform="linux")

    def test_missing_osascript(self, tmp_path):
        runner = FakeRunner()
        missing = str(tmp_path / "nope")
        with pytest.raises(NotificationError) as info:
            mac(missing, runner).send(Notification("T", "M"))
        assert info.value.code == "osascript_missing"
        assert runner.calls == []
        with pytest.raises(NotificationError):
            make_notifier("macos", osascript=missing, platform="darwin")

    def test_not_executable_osascript(self, tmp_path):
        path = tmp_path / "osascript"
        path.write_text("")
        path.chmod(0o644)
        with pytest.raises(NotificationError):
            mac(str(path), FakeRunner()).check()

    def test_delivery_through_macos_records_failure(self, store, osascript):
        job = finished(store)
        notifier = mac(osascript, FakeRunner(returncode=1))
        assert deliver_notifications(store, notifier, WORKER)["retrying"] == 1
        assert store.notification(job.id).last_error == "osascript_failed"
        assert store.get(job.id).status is JobStatus.SUCCEEDED

    def test_make_notifier(self, osascript):
        assert isinstance(make_notifier(), LogNotifier)
        assert isinstance(make_notifier("log"), LogNotifier)
        notifier = make_notifier(
            "macos", osascript=osascript, runner=FakeRunner(), platform="darwin"
        )
        assert isinstance(notifier, MacOSNotifier)
        with pytest.raises(ValueError):
            make_notifier("slack")


# -- configuration and CLI ------------------------------------------------------


class TestSettings:
    def test_default_is_log(self):
        assert JobSettings().notifications == "log"
        assert JobSettings.from_mapping({}).notifications == "log"

    def test_macos_is_accepted(self):
        assert JobSettings.from_mapping({"notifications": "macos"}).notifications == (
            "macos"
        )

    @pytest.mark.parametrize("value", ["slack", "", "MACOS", 1, True, ["log"]])
    def test_invalid_backend_is_refused(self, value):
        with pytest.raises(ExtensionConfigError, match="notifications"):
            JobSettings.from_mapping({"notifications": value})


def write_config(tmp_path: Path, **jobs: Any) -> Path:
    values = {"database_path": str(tmp_path / "jobs.db"), **FAST, **jobs}
    lines = ["[jarvis.jobs]"] + [f"{k} = {json.dumps(v)}" for k, v in values.items()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestCli:
    def test_show_includes_notification(self, tmp_path, capsys):
        config = write_config(tmp_path)
        with JobStore(tmp_path / "jobs.db") as s:
            job = finished(s)
        assert main(["jobs", "show", job.id[:8], "--config", str(config)]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["notification"]["state"] == "pending"
        assert data["notification"]["outcome"] == "succeeded"

    def test_show_without_notification(self, tmp_path, capsys):
        config = write_config(tmp_path)
        with JobStore(tmp_path / "jobs.db") as s:
            job = s.submit(KIND, {})
        assert main(["jobs", "show", job.id, "--config", str(config)]) == 0
        assert json.loads(capsys.readouterr().out)["notification"] is None

    def test_worker_with_macos_on_unsupported_platform_fails_clearly(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(notify.sys, "platform", "linux")
        config = write_config(tmp_path, notifications="macos")
        assert main(["jobs", "worker", "--once", "--config", str(config)]) == 1
        err = capsys.readouterr().err
        assert "notifications = 'macos' cannot be used" in err
        assert "only available on macOS" in err
        assert str(tmp_path) not in err

    def test_worker_with_macos_without_osascript_fails_clearly(
        self, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setattr(notify, "DEFAULT_OSASCRIPT", str(tmp_path / "missing"))
        config = write_config(tmp_path, notifications="macos")
        assert main(["jobs", "worker", "--once", "--config", str(config)]) == 1
        err = capsys.readouterr().err
        assert "osascript" in err and str(tmp_path) not in err

    def test_worker_with_macos_delivers_through_osascript(
        self, tmp_path, capsys, monkeypatch, osascript
    ):
        runner = FakeRunner()
        monkeypatch.setattr(notify.sys, "platform", "darwin")
        monkeypatch.setattr(notify, "DEFAULT_OSASCRIPT", osascript)
        monkeypatch.setattr(notify, "_default_runner", runner)
        config = write_config(tmp_path, notifications="macos")
        with JobStore(tmp_path / "jobs.db") as s:
            job = finished(s)
        assert main(["jobs", "worker", "--once", "--config", str(config)]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["notifications"]["delivered"] == 1
        (call,) = runner.calls
        assert call["cmd"][-2:] == [
            "JARVIS: job succeeded",
            f"test.echo job {job.id[:8]} succeeded.",
        ]
