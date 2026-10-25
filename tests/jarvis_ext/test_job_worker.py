"""Phase 3 step 3: store v2 (heartbeat, back-off, recovery), stoppable
process control, the worker, temp-folder cleanup and the ``jobs`` CLI.

What actually runs, by group:

* ``TestStoreV2``, ``TestRunnerStep3``, ``TestHandlerStops``,
  ``TestTempCleanup``, ``TestWorker``, ``TestJobsCli``: fakes only (fake
  handlers/tools, an injected clock); no FFmpeg.
* ``TestStoppableProcess``, ``TestLockProcesses``, ``TestCliSignals``: real
  OS processes (Python children, a real CLI worker, real signals); no FFmpeg.
* ``TestRealFfmpegWorker``: real FFmpeg on generated clips in a temporary
  folder (skipped without FFmpeg).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from jarvis_ext.__main__ import main
from jarvis_ext.jobs import (
    InvalidJobError,
    InvalidTransitionError,
    JobNotFoundError,
    JobSettings,
    JobStatus,
    JobStore,
)
from jarvis_ext.jobs.runner import (
    JobContext,
    JobOutcome,
    WorkerInterrupted,
    current_stop_reason,
    run_next,
)
from jarvis_ext.jobs.store import _MIGRATIONS, _SCHEMA_V1, APPLICATION_ID
from jarvis_ext.jobs.worker import Worker, WorkerLock, WorkerLockedError
from jarvis_ext.video.ffmpeg import ProcessStopped, run_process, run_process_until
from jarvis_ext.video.render_job import (
    KIND,
    VideoRenderHandler,
    abandoned_after,
    remove_abandoned_temp_dirs,
    stoppable_run_process,
)
from jarvis_ext.video.settings import MediaRenderSettings
from openjarvis.core.types import ToolResult

from .video_fakes import HAVE_FFMPEG, digest_tree, make_test_media

WORKER = "worker-1"
PLAN = {
    "output_name": "reel",
    "clips": [{"source": "Shoot/a.mp4", "start": 1, "end": 4}],
}
FAST = {
    "retry_delay_seconds": 0,
    "poll_seconds": 0.1,
    "heartbeat_seconds": 0.05,
    "stale_after_seconds": 1,
}

needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"
)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


class FakeClock:
    """An injectable store clock (UTC, millisecond ISO strings)."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.now.isoformat(timespec="milliseconds")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def wait_for(predicate: Callable[[], Any], timeout: float = 30.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, clock: FakeClock):
    with JobStore(tmp_path / "jobs.db", clock=clock) as s:
        yield s


def tool_result(report: Dict[str, Any], success: bool) -> ToolResult:
    return ToolResult(tool_name="fake", content=json.dumps(report), success=success)


RENDER_OK = tool_result(
    {
        "render_status": "succeeded",
        "output": {"file": "reel_v001.mp4", "version": 1},
        "warnings": [],
    },
    True,
)
VALIDATE_OK = tool_result(
    {
        "validation_status": "passed",
        "ready_for_review": True,
        "output": "reel_v001.mp4",
        "preview": "review/p.mp4",
        "thumbnail": "review/t.jpg",
        "checks": [],
        "warnings": [],
    },
    True,
)


class FakeTool:
    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.calls = 0

    def execute(self, **params: Any) -> Any:
        self.calls += 1
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        if callable(self.behaviour):
            return self.behaviour(**params)
        return self.behaviour


class Recorder:
    """A generic handler: records jobs, optionally blocks or runs a function."""

    def __init__(self, action: Optional[Callable[[JobContext], Any]] = None):
        self.action = action
        self.seen: List[str] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def run(self, context: JobContext) -> JobOutcome:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.seen.append(context.job.id)
            if self.action is not None:
                outcome = self.action(context)
                if outcome is not None:
                    return outcome
            return JobOutcome.succeeded({"ok": True})
        finally:
            with self.lock:
                self.active -= 1


def make_worker(store: JobStore, tmp_path: Path, handlers, **settings) -> Worker:
    return Worker(
        store,
        handlers,
        lock_path=tmp_path / "jobs.db.worker.lock",
        settings=JobSettings.from_mapping({**FAST, **settings}),
        worker_id="this-worker",
    )


def _running_elsewhere(store: JobStore, *, worker: str = "dead-worker", **kw):
    job = store.submit(kw.pop("kind", KIND), {"plan": PLAN}, **kw)
    claimed = store.claim_next(worker)
    assert claimed.id == job.id
    return claimed


# -- store schema v2 ----------------------------------------------------------


class TestStoreV2:
    def test_version_1_database_is_upgraded(self, tmp_path):
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        for statement in _SCHEMA_V1:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO jobs (id, kind, status, payload, max_attempts, "
            "created_at, updated_at, started_at, claimed_by, attempts) "
            "VALUES (?, 'video.render', 'running', '{}', 2, ?, ?, ?, 'old', 1)",
            ("a" * 32, *["2026-01-01T00:00:00.000+00:00"] * 3),
        )
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        with JobStore(path) as upgraded:
            assert upgraded.schema_version == 4 == max(_MIGRATIONS) + 1
            (job,) = upgraded.list_jobs()
            assert job.heartbeat_at is None and job.not_before is None
            # A pre-upgrade running job (no heartbeat) counts as stale.
            assert upgraded.stale_jobs(upgraded.time_ago(0))[0].id == job.id

    def test_claim_records_heartbeat(self, store, clock):
        store.submit(KIND, {})
        job = store.claim_next(WORKER)
        assert job.heartbeat_at == clock() == job.started_at

    def test_retry_back_off(self, store, clock):
        store.submit(KIND, {})
        job = store.claim_next(WORKER)
        queued = store.retry(job.id, WORKER, "ffmpeg_failed", delay_seconds=60)
        assert queued.status is JobStatus.QUEUED
        assert queued.not_before == (clock.now + timedelta(seconds=60)).isoformat(
            timespec="milliseconds"
        )
        assert "in 60s" in store.events(job.id)[-1].note
        clock.advance(59.9)
        assert store.claim_next(WORKER) is None
        clock.advance(0.1)
        again = store.claim_next(WORKER)
        assert again.id == job.id and again.attempts == 2
        assert again.not_before is None

    def test_back_off_does_not_block_other_jobs(self, store):
        first = store.submit(KIND, {})
        second = store.submit(KIND, {})
        store.retry(store.claim_next(WORKER).id, WORKER, "x", delay_seconds=60)
        assert store.claim_next(WORKER).id == second.id
        assert store.get(first.id).status is JobStatus.QUEUED

    @pytest.mark.parametrize("delay", [-1, True, "5", 86401, float("nan")])
    def test_invalid_delay(self, store, delay):
        store.submit(KIND, {})
        job = store.claim_next(WORKER)
        with pytest.raises(InvalidJobError):
            store.retry(job.id, WORKER, "x", delay_seconds=delay)
        assert store.get(job.id).status is JobStatus.RUNNING

    def test_heartbeat(self, store, clock):
        store.submit(KIND, {})
        job = store.claim_next(WORKER)
        clock.advance(5)
        beat = store.heartbeat(job.id, WORKER)
        assert beat.heartbeat_at == clock() and not beat.cancel_requested
        store.cancel(job.id)
        assert store.heartbeat(job.id, WORKER).cancel_requested
        with pytest.raises(InvalidTransitionError):
            store.heartbeat(job.id, "someone-else")
        store.confirm_cancelled(job.id, WORKER)
        with pytest.raises(InvalidTransitionError):
            store.heartbeat(job.id, WORKER)

    def test_stale_jobs(self, store, clock):
        old = _running_elsewhere(store)
        clock.advance(100)
        fresh = _running_elsewhere(store, worker="live-worker")
        mine = _running_elsewhere(store, worker=WORKER)
        clock.advance(10)
        stale = store.stale_jobs(store.time_ago(60), exclude_worker=WORKER)
        assert [j.id for j in stale] == [old.id]
        assert fresh.id and mine.id

    def test_recover_requeues_with_back_off(self, store, clock):
        job = _running_elsewhere(store)
        clock.advance(300)
        settled = store.recover(
            job.id,
            stale_before=store.time_ago(120),
            requeue=True,
            error_message="worker died",
            delay_seconds=30,
        )
        assert settled.status is JobStatus.QUEUED and settled.attempts == 1
        assert settled.error_code == "interrupted"
        assert settled.claimed_by is None and settled.not_before is not None
        assert store.events(job.id)[-1].note.startswith("recovered from dead-worker")

    def test_recover_never_exceeds_attempts(self, store, clock):
        job = _running_elsewhere(store)
        store.retry(job.id, "dead-worker", "x")
        store.claim_next("dead-worker")
        clock.advance(300)
        settled = store.recover(job.id, stale_before=store.time_ago(120), requeue=True)
        assert settled.status is JobStatus.FAILED and settled.attempts == 2

    def test_recover_honours_cancellation(self, store, clock):
        job = _running_elsewhere(store)
        store.cancel(job.id)
        clock.advance(300)
        settled = store.recover(
            job.id, stale_before=store.time_ago(120), requeue=True, result={"a": 1}
        )
        assert settled.status is JobStatus.CANCELLED
        assert settled.result == {"a": 1}

    def test_recover_fail(self, store, clock):
        job = _running_elsewhere(store)
        clock.advance(300)
        settled = store.recover(
            job.id,
            stale_before=store.time_ago(120),
            requeue=False,
            error_code="validate_interrupted",
            result={"output": "reel_v001.mp4"},
        )
        assert settled.status is JobStatus.FAILED
        assert settled.error_code == "validate_interrupted"
        assert settled.result == {"output": "reel_v001.mp4"}

    def test_recover_refuses_live_or_finished_jobs(self, store, clock):
        job = _running_elsewhere(store)
        clock.advance(300)
        stale_before = store.time_ago(120)
        store.heartbeat(job.id, "dead-worker")  # it was alive after all
        with pytest.raises(InvalidTransitionError):
            store.recover(job.id, stale_before=stale_before, requeue=True)
        assert store.get(job.id).status is JobStatus.RUNNING
        store.complete(job.id, "dead-worker")
        clock.advance(300)
        with pytest.raises(InvalidTransitionError):
            store.recover(job.id, stale_before=store.time_ago(120), requeue=True)
        assert store.get(job.id).status is JobStatus.SUCCEEDED

    def test_find(self, store, monkeypatch):
        ids = iter(["abcdef" + "0" * 26, "abcdef" + "1" * 26, "123456" + "0" * 26])

        class FakeUuid:
            def __init__(self) -> None:
                self.hex = next(ids)

        monkeypatch.setattr("jarvis_ext.jobs.store.uuid.uuid4", FakeUuid)
        a = store.submit(KIND, {})
        store.submit(KIND, {})
        c = store.submit(KIND, {})
        assert store.find(c.id).id == c.id
        assert store.find("123456").id == c.id
        assert store.find("ABCDEF0").id == a.id
        with pytest.raises(JobNotFoundError, match="more than one"):
            store.find("abcdef")
        for bad in ("abc", "zzzzzz", "", "abcdef%", None):
            with pytest.raises(JobNotFoundError):
                store.find(bad)
        with pytest.raises(JobNotFoundError):
            store.find("fedcba")


# -- runner additions ---------------------------------------------------------


class TestRunnerStep3:
    def test_retry_outcome_uses_back_off(self, store, clock):
        job = store.submit("test.echo", {})
        handlers = {"test.echo": Recorder(lambda c: JobOutcome.retry("flaky"))}
        first = run_next(store, WORKER, handlers, retry_delay_seconds=60)
        assert first.status is JobStatus.QUEUED and first.not_before is not None
        assert run_next(store, WORKER, handlers, retry_delay_seconds=60) is None
        clock.advance(60)
        done = run_next(store, WORKER, handlers, retry_delay_seconds=60)
        assert done.id == job.id
        assert done.status is JobStatus.FAILED and done.attempts == 2

    def test_heartbeats_while_running(self, tmp_path):
        with JobStore(tmp_path / "jobs.db") as store:
            job = store.submit("test.echo", {})
            beats = []

            def slow(context):
                first = store.get(job.id).heartbeat_at
                beats.append(
                    wait_for(
                        lambda: store.get(job.id).heartbeat_at != first, timeout=10
                    )
                )

            run_next(
                store, WORKER, {"test.echo": Recorder(slow)}, heartbeat_seconds=0.05
            )
            assert beats == [True]
            assert store.get(job.id).status is JobStatus.SUCCEEDED

    def test_stop_reason(self, store):
        assert current_stop_reason() is None
        job = store.submit("test.echo", {})
        shutdown = threading.Event()
        seen = []

        def look(context):
            seen.append(current_stop_reason())
            store.cancel(job.id)
            seen.append(context.stop_reason(fresh=True))
            shutdown.set()
            seen.append(current_stop_reason())
            return JobOutcome.cancelled()

        run_next(store, WORKER, {"test.echo": Recorder(look)}, shutdown=shutdown)
        assert seen == [None, "cancel", "shutdown"]
        assert current_stop_reason() is None

    def test_worker_interrupted_in_generic_handler_fails(self, store):
        store.submit("test.echo", {})

        def interrupted(context):
            raise WorkerInterrupted()

        done = run_next(store, WORKER, {"test.echo": Recorder(interrupted)})
        assert done.status is JobStatus.FAILED and done.error_code == "interrupted"


# -- stoppable process control (real child processes, not FFmpeg) -------------


CHILD = textwrap.dedent(
    """
    import os, sys, time
    open(sys.argv[1], "w").write(str(os.getpid()))
    time.sleep(float(sys.argv[2]))
    """
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); _stop reaps, so treat it as gone.
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True)
    return bool(out.stdout.strip()) and not out.stdout.strip().startswith(b"Z")


@posix_only
class TestStoppableProcess:
    def _cmd(self, tmp_path: Path, seconds: float) -> List[str]:
        return [sys.executable, "-c", CHILD, str(tmp_path / "pid"), str(seconds)]

    def test_completes_normally(self, tmp_path):
        code = run_process_until(
            self._cmd(tmp_path, 0),
            cwd=tmp_path,
            timeout=30,
            log_path=tmp_path / "log",
            should_stop=lambda: None,
        )
        assert code == 0

    def test_stop_request_kills_the_process(self, tmp_path):
        started = time.monotonic()
        with pytest.raises(ProcessStopped) as info:
            run_process_until(
                self._cmd(tmp_path, 60),
                cwd=tmp_path,
                timeout=60,
                log_path=tmp_path / "log",
                should_stop=lambda: "cancel" if (tmp_path / "pid").exists() else None,
                poll_seconds=0.05,
            )
        assert info.value.reason == "cancel"
        assert time.monotonic() - started < 20
        pid = int((tmp_path / "pid").read_text())
        assert not _alive(pid)

    def test_timeout_still_applies(self, tmp_path):
        with pytest.raises(subprocess.TimeoutExpired):
            run_process_until(
                self._cmd(tmp_path, 60),
                cwd=tmp_path,
                timeout=0.5,
                log_path=tmp_path / "log",
                should_stop=lambda: None,
            )
        assert not _alive(int(wait_for(lambda: (tmp_path / "pid").read_text())))

    def test_outside_a_job_behaves_like_run_process(self, tmp_path):
        args = dict(cwd=tmp_path, timeout=30, log_path=tmp_path / "log")
        assert stoppable_run_process(self._cmd(tmp_path, 0), **args) == 0
        assert run_process(self._cmd(tmp_path, 0), **args) == 0


# -- video handler: shutdown and recovery decisions (fake tools) --------------


def _video_handler(render: Any, validate: Any = VALIDATE_OK) -> VideoRenderHandler:
    return VideoRenderHandler(
        render_tool=FakeTool(render), validate_tool=FakeTool(validate)
    )


class TestHandlerStops:
    def test_shutdown_before_render_requeues_without_rendering(self, store):
        store.submit(KIND, {"plan": PLAN})
        h = _video_handler(RENDER_OK)
        stop = threading.Event()
        stop.set()
        done = run_next(store, WORKER, {KIND: h}, shutdown=stop)
        assert done.status is JobStatus.QUEUED and done.error_code == "interrupted"
        assert h._render.calls == 0

    def test_interrupted_during_render_is_retried(self, store):
        store.submit(KIND, {"plan": PLAN})
        done = run_next(store, WORKER, {KIND: _video_handler(WorkerInterrupted())})
        assert done.status is JobStatus.QUEUED
        assert done.error_code == "interrupted"
        assert "nothing was published" in done.error_message

    def test_interrupted_during_validation_is_final(self, store):
        store.submit(KIND, {"plan": PLAN})
        done = run_next(
            store, WORKER, {KIND: _video_handler(RENDER_OK, WorkerInterrupted())}
        )
        assert done.status is JobStatus.FAILED
        assert done.error_code == "validate_interrupted"
        assert done.result["output"] == "reel_v001.mp4"
        assert done.result["ready_for_review"] is False

    def test_shutdown_after_render_is_final(self, store):
        store.submit(KIND, {"plan": PLAN})
        stop = threading.Event()

        def render(**params):
            stop.set()
            return RENDER_OK

        h = _video_handler(render)
        done = run_next(store, WORKER, {KIND: h}, shutdown=stop)
        assert done.status is JobStatus.FAILED
        assert done.error_code == "validate_interrupted"
        assert h._validate.calls == 0

    def test_recovery_decision(self, store):
        h = _video_handler(RENDER_OK)
        job = _running_elsewhere(store)
        store.add_note(job.id, "dead-worker", "render started")
        requeue, code, _, result = h.recovery(job, store.events(job.id))
        assert (requeue, code, result["output"]) == (True, "interrupted", None)
        store.add_note(job.id, "dead-worker", "rendered reel_v003.mp4")
        requeue, code, message, result = h.recovery(job, store.events(job.id))
        assert (requeue, code) == (False, "validate_interrupted")
        assert result == {
            "output": "reel_v003.mp4",
            "ready_for_review": False,
            "stage": "after_render",
        }
        assert "not ready for review" in message

    def test_default_tools_use_stoppable_process_control(self, tmp_path):
        h = VideoRenderHandler(config_path=tmp_path / "missing.toml")
        assert h._render._process_runner is stoppable_run_process
        assert h._validate._process_runner is stoppable_run_process


# -- temp-folder cleanup (real filesystem, no FFmpeg) -------------------------


def _age(path: Path, seconds_ago: float) -> None:
    then = time.time() - seconds_ago
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(root, name), (then, then), follow_symlinks=False)
    os.utime(path, (then, then), follow_symlinks=False)


@posix_only
class TestTempCleanup:
    @pytest.fixture
    def output(self, tmp_path):
        out = tmp_path / "renders"
        (out / ".jarvis-tmp").mkdir(parents=True)
        (out / "review").mkdir()
        (out / "reel_v001.mp4").write_bytes(b"render")
        (out / "reel_v001.render.json").write_text("{}")
        (out / "review" / "reel_v001_thumb_v001.jpg").write_bytes(b"jpg")
        return out

    def test_only_old_job_dirs_are_removed(self, output, tmp_path):
        tmp = output / ".jarvis-tmp"
        old = tmp / "job-old123"
        (old / "sub").mkdir(parents=True)
        (old / "render.mp4").write_bytes(b"partial")
        _age(old, 7200)
        recent = tmp / "job-new456"
        recent.mkdir()
        (recent / "render.mp4").write_bytes(b"x")
        busy = tmp / "job-busy789"
        busy.mkdir()
        (busy / "render.mp4").write_bytes(b"x")
        _age(busy, 7200)
        os.utime(busy / "render.mp4")  # an orphaned FFmpeg still writing
        other = tmp / "notes"
        other.mkdir()
        _age(other, 7200)
        stray = tmp / "job-file"
        stray.write_bytes(b"not a dir")
        _age(stray, 7200)
        precious = tmp_path / "precious"
        precious.mkdir()
        (precious / "keep.mov").write_bytes(b"footage")
        _age(precious, 7200)
        (tmp / "job-link").symlink_to(precious)
        before = digest_tree(precious)

        removed = remove_abandoned_temp_dirs(output, older_than_seconds=3600)

        assert removed == ["job-old123"]
        assert not old.exists()
        for kept in (recent, busy, other, stray, tmp / "job-link"):
            assert os.path.lexists(kept)
        assert digest_tree(precious) == before
        assert (output / "reel_v001.mp4").read_bytes() == b"render"
        assert (output / "reel_v001.render.json").exists()
        assert (output / "review" / "reel_v001_thumb_v001.jpg").exists()

    def test_symlinked_tmp_root_is_ignored(self, tmp_path):
        out = tmp_path / "renders"
        out.mkdir()
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "job-x").mkdir(parents=True)
        _age(elsewhere / "job-x", 7200)
        (out / ".jarvis-tmp").symlink_to(elsewhere)
        assert remove_abandoned_temp_dirs(out, older_than_seconds=1) == []
        assert (elsewhere / "job-x").exists()

    def test_missing_folder(self, tmp_path):
        assert remove_abandoned_temp_dirs(tmp_path / "gone", older_than_seconds=1) == []

    def test_threshold_outlasts_any_live_call(self, tmp_path):
        settings = MediaRenderSettings.from_mapping(
            {"output_root": str(tmp_path), "render_timeout_seconds": 900,
             "validate_timeout_seconds": 300}
        )  # fmt: skip
        assert abandoned_after(settings) == 900 + 4 * 300 + 600

    def test_handler_cleanup_uses_configured_output(self, output, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            f'[jarvis.media_render]\noutput_root = "{output}"\n'
            "render_timeout_seconds = 10\nvalidate_timeout_seconds = 10\n"
        )
        old = output / ".jarvis-tmp" / "job-abc"
        old.mkdir()
        _age(old, 10 + 40 + 600 + 5)
        h = VideoRenderHandler(config_path=config)
        assert h.cleanup() == ["job-abc"]
        assert VideoRenderHandler(config_path=tmp_path / "none.toml").cleanup() == []


# -- worker (fake handlers) ---------------------------------------------------


class TestWorker:
    def test_once_runs_one_job(self, store, tmp_path):
        first = store.submit("test.echo", {})
        second = store.submit("test.echo", {})
        rec = Recorder()
        report = make_worker(store, tmp_path, {"test.echo": rec}).run(once=True)
        assert rec.seen == [first.id]
        assert [p["id"] for p in report["processed"]] == [first.id]
        assert report["processed"][0]["status"] == "succeeded"
        assert store.get(second.id).status is JobStatus.QUEUED

    def test_once_with_nothing_queued(self, store, tmp_path):
        report = make_worker(store, tmp_path, {"test.echo": Recorder()}).run(once=True)
        assert report["processed"] == [] and report["stopped"] == "idle"

    def test_loop_runs_jobs_in_order_one_at_a_time(self, tmp_path):
        with JobStore(tmp_path / "jobs.db") as store:
            ids = [store.submit("test.echo", {"i": i}).id for i in range(4)]
            rec = Recorder(lambda c: time.sleep(0.02))
            worker = make_worker(store, tmp_path, {"test.echo": rec})
            thread = threading.Thread(target=worker.run)
            thread.start()
            wait_for(lambda: len(rec.seen) == 4 and rec.active == 0)
            late = store.submit("test.echo", {}).id
            wait_for(lambda: late in rec.seen and rec.active == 0)
            worker.stop()
            thread.join(10)
            assert not thread.is_alive()
            assert rec.seen == ids + [late]
            assert rec.max_active == 1
            assert all(j.status is JobStatus.SUCCEEDED for j in store.list_jobs())

    def test_graceful_stop_finishes_current_job(self, tmp_path):
        with JobStore(tmp_path / "jobs.db") as store:
            first = store.submit("test.echo", {}).id
            second = store.submit("test.echo", {}).id
            release = threading.Event()
            rec = Recorder(lambda c: release.wait(10) and None)
            worker = make_worker(store, tmp_path, {"test.echo": rec})
            thread = threading.Thread(target=worker.run)
            thread.start()
            wait_for(lambda: rec.seen)
            worker.stop()
            release.set()
            thread.join(10)
            assert store.get(first).status is JobStatus.SUCCEEDED
            assert store.get(second).status is JobStatus.QUEUED

    def test_forced_stop_interrupts_current_job(self, tmp_path):
        with JobStore(tmp_path / "jobs.db") as store:
            job = store.submit("test.echo", {}).id

            def until_stopped(context):
                wait_for(lambda: current_stop_reason() == "shutdown", timeout=10)
                raise WorkerInterrupted()

            rec = Recorder(until_stopped)
            worker = make_worker(store, tmp_path, {"test.echo": rec})
            thread = threading.Thread(target=worker.run)
            thread.start()
            wait_for(lambda: rec.seen)
            worker.stop(force=True)
            thread.join(10)
            assert store.get(job).status is JobStatus.FAILED
            assert store.get(job).error_code == "interrupted"

    def test_second_worker_is_refused(self, tmp_path):
        with JobStore(tmp_path / "jobs.db") as store:
            store.submit("test.echo", {})
            release = threading.Event()
            rec = Recorder(lambda c: release.wait(10) and None)
            first = make_worker(store, tmp_path, {"test.echo": rec})
            thread = threading.Thread(target=first.run)
            thread.start()
            wait_for(lambda: rec.seen)
            with JobStore(tmp_path / "jobs.db") as other_store:
                second = make_worker(other_store, tmp_path, {"test.echo": Recorder()})
                with pytest.raises(WorkerLockedError):
                    second.run(once=True)
            release.set()
            first.stop()
            thread.join(10)

    def test_recovery_on_start(self, store, clock, tmp_path):
        requeue = _running_elsewhere(store)
        store.add_note(requeue.id, "dead-worker", "render started")
        rendered = _running_elsewhere(store)
        store.add_note(rendered.id, "dead-worker", "rendered reel_v002.mp4")
        cancelled = _running_elsewhere(store)
        store.cancel(cancelled.id)
        exhausted = _running_elsewhere(store, max_attempts=1)
        other_kind = _running_elsewhere(store, kind="research.report")
        clock.advance(600)
        live = _running_elsewhere(store, worker="live-worker")
        h = _video_handler(RENDER_OK)
        worker = make_worker(store, tmp_path, {KIND: h}, stale_after_seconds=120)
        report = worker.run(once=True)

        states = {j["id"]: j["status"] for j in report["recovered"]}
        assert states == {
            requeue.id: "queued",
            rendered.id: "failed",
            cancelled.id: "cancelled",
            exhausted.id: "failed",
            other_kind.id: "failed",
        }
        assert store.get(rendered.id).error_code == "validate_interrupted"
        assert store.get(rendered.id).result["output"] == "reel_v002.mp4"
        assert store.get(live.id).status is JobStatus.RUNNING
        # The requeued job ran afterwards in the same --once pass (no back-off
        # with FAST settings), as its second and last attempt.
        assert [p["id"] for p in report["processed"]] == [requeue.id]
        assert store.get(requeue.id).attempts == 2
        assert h._render.calls == 1

    def test_own_running_job_is_never_recovered(self, store, clock, tmp_path):
        mine = _running_elsewhere(store, worker="this-worker")
        clock.advance(600)
        make_worker(store, tmp_path, {KIND: _video_handler(RENDER_OK)}).run(once=True)
        assert store.get(mine.id).status is JobStatus.RUNNING

    def test_cleanup_runs_at_start_and_hourly(self, store, tmp_path):
        class Cleaner(Recorder):
            calls = 0

            def cleanup(self):
                Cleaner.calls += 1
                return ["job-x"] if Cleaner.calls == 1 else []

        ticks = iter([0.0, 10.0, 4000.0])
        worker = Worker(
            store,
            {"test.echo": Cleaner()},
            lock_path=tmp_path / "lock",
            settings=JobSettings.from_mapping(FAST),
            monotonic=lambda: next(ticks),
        )
        report = worker.run(once=True)
        assert report["cleaned_temp_dirs"] == ["job-x"]
        worker._cleanup(report)  # 10 s later: not due
        assert Cleaner.calls == 1
        worker._cleanup(report)  # over an hour later
        assert Cleaner.calls == 2

    def test_lock_file_symlink_is_refused(self, tmp_path):
        target = tmp_path / "target"
        target.write_text("")
        (tmp_path / "lock").symlink_to(target)
        with pytest.raises(Exception, match="symbolic link"):
            WorkerLock(tmp_path / "lock").acquire()


@posix_only
class TestLockProcesses:
    HOLDER = textwrap.dedent(
        """
        import sys, time
        from jarvis_ext.jobs.worker import WorkerLock
        lock = WorkerLock(sys.argv[1]); lock.acquire()
        print("locked", flush=True)
        time.sleep(60)
        """
    )

    def test_lock_is_released_when_holder_is_killed(self, tmp_path):
        lock_path = tmp_path / "lock"
        proc = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER, str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "locked"
            with pytest.raises(WorkerLockedError):
                WorkerLock(lock_path).acquire()
        finally:
            proc.kill()
            proc.wait(10)
        lock = WorkerLock(lock_path)
        lock.acquire()  # a crashed worker never blocks the next one
        lock.release()


# -- CLI (fakes / no FFmpeg) --------------------------------------------------


def write_config(tmp_path: Path, **sections: Dict[str, Any]) -> Path:
    jobs = {"database_path": str(tmp_path / "jobs.db"), **FAST}
    lines = ["[jarvis.jobs]"] + [f"{k} = {json.dumps(v)}" for k, v in jobs.items()]
    for name, values in sections.items():
        lines.append(f"[jarvis.{name}]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in values.items()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def cli(capsys, *args: str) -> tuple:
    code = main(["jobs", *args])
    out, err = capsys.readouterr()
    return code, out, err


class TestJobsCli:
    @pytest.fixture
    def config(self, tmp_path):
        return write_config(tmp_path)

    @pytest.fixture
    def plan_file(self, tmp_path):
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(PLAN))
        return path

    def test_submit_list_show_cancel(self, capsys, config, plan_file, tmp_path):
        code, out, _ = cli(capsys, "submit", str(plan_file), "--config", str(config))
        assert code == 0
        submitted = json.loads(out)
        assert submitted["status"] == "queued" and submitted["kind"] == KIND
        job_id = submitted["id"]

        code, out, _ = cli(capsys, "list", "--config", str(config))
        assert [j["id"] for j in json.loads(out)["jobs"]] == [job_id]
        code, out, _ = cli(
            capsys, "list", "--status", "failed", "--config", str(config)
        )
        assert json.loads(out)["jobs"] == []

        code, out, _ = cli(capsys, "show", job_id[:8], "--config", str(config))
        shown = json.loads(out)
        assert shown["job"]["payload"] == {"plan": PLAN}
        assert shown["events"][0]["note"] == "submitted"

        code, out, _ = cli(capsys, "cancel", job_id[:8], "--config", str(config))
        assert code == 0 and json.loads(out)["status"] == "cancelled"
        code, out, err = cli(capsys, "cancel", job_id, "--config", str(config))
        assert code == 1 and "cannot become cancelled" in err
        for text in (out, err):
            assert str(tmp_path) not in text

    def test_submit_from_stdin(self, capsys, config, monkeypatch):
        monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps(PLAN)))
        code, out, _ = cli(capsys, "submit", "-", "--config", str(config))
        assert code == 0 and json.loads(out)["status"] == "queued"

    @pytest.mark.parametrize(
        "content,message",
        [
            ("{not json", "not valid JSON"),
            ("[]", "plan"),
            ("{}", "plan"),
            ('"x"', "plan"),
        ],
    )
    def test_submit_rejects_bad_plans(self, capsys, config, tmp_path, content, message):
        bad = tmp_path / "bad.json"
        bad.write_text(content)
        code, out, err = cli(capsys, "submit", str(bad), "--config", str(config))
        assert code == 1 and message in err
        code, out, _ = cli(capsys, "list", "--config", str(config))
        assert json.loads(out)["jobs"] == []

    def test_submit_missing_file(self, capsys, config, tmp_path):
        code, _, err = cli(
            capsys, "submit", str(tmp_path / "nope.json"), "--config", str(config)
        )
        assert code == 1 and "Cannot read plan" in err

    def test_cancel_running_is_a_request(self, capsys, config, plan_file, tmp_path):
        cli(capsys, "submit", str(plan_file), "--config", str(config))
        with JobStore(tmp_path / "jobs.db") as store:
            job = store.claim_next("w")
        code, out, _ = cli(capsys, "cancel", job.id, "--config", str(config))
        result = json.loads(out)
        assert result["status"] == "running" and result["cancel_requested"]
        assert "Cancellation requested" in result["message"]

    @pytest.mark.parametrize("job_id", ["abc", "0123456789", "not-hex!"])
    def test_show_unknown(self, capsys, config, job_id):
        code, out, err = cli(capsys, "show", job_id, "--config", str(config))
        assert code == 1 and out == "" and err

    @pytest.mark.parametrize(
        "args",
        [
            ["list", "--limit", "0"],
            ["list", "--status", "done"],
            ["submit"],
            ["show"],
            ["frobnicate"],
            [],
        ],
    )
    def test_invalid_arguments(self, capsys, args):
        with pytest.raises(SystemExit) as info:
            main(["jobs", *args])
        assert info.value.code == 2

    def test_config_error(self, capsys, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text("[jarvis.jobs]\nworkers = 4\n")
        code, _, err = cli(capsys, "list", "--config", str(config))
        assert code == 1 and "Unknown [jarvis.jobs] keys" in err

    def test_worker_once_without_media_config(self, capsys, config, plan_file):
        cli(capsys, "submit", str(plan_file), "--config", str(config))
        code, out, err = cli(capsys, "worker", "--once", "--config", str(config))
        assert code == 0
        (processed,) = json.loads(out)["processed"]
        assert processed["status"] == "failed"
        assert processed["error_code"] == "render_not_configured"

    def test_worker_refuses_when_locked(self, capsys, config, tmp_path):
        settings = JobSettings.load(config)
        with WorkerLock(settings.lock_file()):
            code, out, err = cli(capsys, "worker", "--once", "--config", str(config))
        assert code == 1 and "already running" in err
        assert str(tmp_path) not in err


@posix_only
class TestCliSignals:
    def test_sigterm_stops_an_idle_worker_and_lock_is_exclusive(self, tmp_path):
        config = write_config(tmp_path)
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "jarvis_ext",
                "jobs",
                "worker",
                "--config",
                str(config),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            lock = JobSettings.load(config).lock_file()
            wait_for(
                lambda: lock.exists() and lock.read_text().strip() == str(proc.pid)
            )
            second = subprocess.run(
                [sys.executable, "-m", "jarvis_ext", "jobs", "worker", "--once",
                 "--config", str(config)],
                capture_output=True, text=True, timeout=60,
            )  # fmt: skip
            assert second.returncode == 1
            assert "already running" in second.stderr
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == 0, err
        assert json.loads(out)["stopped"] == "requested"
        assert "Stopping after the current job" in err


# -- real FFmpeg --------------------------------------------------------------


def _ffmpeg_running(marker: Path) -> bool:
    """An FFmpeg encoder (not ffprobe) reading files under *marker* is running."""
    pattern = f"ffmpeg .*{re.escape(str(marker))}"
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return any(pid.strip() for pid in out.stdout.splitlines())


@needs_ffmpeg
@posix_only
class TestRealFfmpegWorker:
    @pytest.fixture
    def env(self, tmp_path):
        workspace = tmp_path / "workspace"
        output = tmp_path / "renders"
        output.mkdir()
        make_test_media(workspace, tmp_path / "assets")
        config = write_config(
            tmp_path,
            media_workspace={"root": str(workspace)},
            media_render={"output_root": str(output)},
        )
        yield workspace, output, config
        subprocess.run(["pkill", "-f", str(workspace)], capture_output=True)

    def plan(self, preset: str = "ultrafast") -> Dict[str, Any]:
        return {
            "output_name": "worker_reel",
            "clips": [
                {"source": "Shoot/landscape.mp4", "start": 0, "end": 4},
                {"source": "Shoot/portrait silent.mov", "start": 0, "end": 3},
            ],
            "video": {"x264_preset": preset},
        }

    def test_cli_submit_worker_show(self, capsys, env, tmp_path):
        workspace, output, config = env
        before = digest_tree(workspace)
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(self.plan()))
        # Step 6: the identical second job reuses v001 (validated again);
        # --no-reuse renders v002.
        first_id = None
        for expected, extra in (
            ("worker_reel_v001.mp4", []),
            ("worker_reel_v001.mp4", []),
            ("worker_reel_v002.mp4", ["--no-reuse"]),
        ):
            code, out, _ = cli(
                capsys, "submit", str(plan), *extra, "--config", str(config)
            )
            job_id = json.loads(out)["id"]
            first_id = first_id or job_id
            if first_id != job_id:
                v1 = (output / "worker_reel_v001.mp4").read_bytes()
            code, out, err = cli(capsys, "worker", "--once", "--config", str(config))
            assert code == 0, err
            code, out, _ = cli(capsys, "show", job_id, "--config", str(config))
            job = json.loads(out)["job"]
            assert job["status"] == "succeeded", job
            assert job["result"]["output"] == expected
            assert job["result"]["ready_for_review"] is True
            reused = job["result"].get("reused_from")
            assert reused == (first_id if job_id != first_id and not extra else None)
        assert (output / "worker_reel_v001.mp4").read_bytes() == v1
        assert digest_tree(workspace) == before
        assert not any((output / ".jarvis-tmp").iterdir())

    def _start_worker(self, config: Path):
        settings = JobSettings.load(config)
        store = JobStore(settings.database_file())
        worker = Worker(
            store,
            {KIND: VideoRenderHandler(config_path=config)},
            lock_path=settings.lock_file(),
            settings=settings,
        )
        thread = threading.Thread(target=worker.run)
        thread.start()
        return store, worker, thread

    def test_cancel_stops_ffmpeg_mid_render(self, env):
        workspace, output, config = env
        store, worker, thread = self._start_worker(config)
        try:
            job = store.submit(KIND, {"plan": self.plan("veryslow")})
            wait_for(lambda: _ffmpeg_running(workspace))
            with JobStore(JobSettings.load(config).database_file()) as user:
                assert user.cancel(job.id).status is JobStatus.RUNNING
            done = wait_for(lambda: (j := store.get(job.id)).is_final and j, timeout=60)
            assert done.status is JobStatus.CANCELLED
            assert done.result["output"] is None
            assert not _ffmpeg_running(workspace)
            assert not list(output.glob("*.mp4"))
            assert not list(output.glob("*.render.json"))
            assert not any((output / ".jarvis-tmp").iterdir())
        finally:
            worker.stop(force=True)
            thread.join(60)
            store.close()

    def test_forced_shutdown_stops_ffmpeg_and_requeues(self, env):
        workspace, output, config = env
        store, worker, thread = self._start_worker(config)
        try:
            job = store.submit(KIND, {"plan": self.plan("veryslow")})
            wait_for(lambda: _ffmpeg_running(workspace))
            worker.stop(force=True)
            thread.join(60)
            assert not thread.is_alive()
            settled = store.get(job.id)
            assert settled.status is JobStatus.QUEUED
            assert settled.error_code == "interrupted" and settled.attempts == 1
            assert not _ffmpeg_running(workspace)
            assert not list(output.glob("*.mp4"))
        finally:
            worker.stop(force=True)
            thread.join(60)
            store.close()

    def test_killed_worker_is_recovered_without_duplicates(self, env):
        workspace, output, config = env
        settings = JobSettings.load(config)
        with JobStore(settings.database_file()) as store:
            job = store.submit(KIND, {"plan": self.plan("veryslow")})
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "jarvis_ext",
                "jobs",
                "worker",
                "--config",
                str(config),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for(lambda: _ffmpeg_running(workspace))
            proc.kill()  # SIGKILL: no cleanup, no outcome recorded
            proc.wait(30)
        finally:
            if proc.poll() is None:
                proc.kill()
        # FFmpeg runs in its own session, so it outlives a SIGKILLed worker
        # (a documented limitation); stop it here so the test is quick.
        subprocess.run(["pkill", "-f", str(workspace)], capture_output=True)
        wait_for(lambda: not _ffmpeg_running(workspace))
        with JobStore(settings.database_file()) as store:
            assert store.get(job.id).status is JobStatus.RUNNING
            time.sleep(settings.stale_after_seconds + 0.2)
            worker = Worker(
                store,
                {KIND: VideoRenderHandler(config_path=config)},
                lock_path=settings.lock_file(),
                settings=settings,
            )
            report = worker.run(once=True)
            assert [r["id"] for r in report["recovered"]] == [job.id]
            done = store.get(job.id)
        assert done.status is JobStatus.SUCCEEDED, done.to_dict()
        assert done.attempts == 2
        assert sorted(p.name for p in output.glob("*.mp4")) == ["worker_reel_v001.mp4"]
