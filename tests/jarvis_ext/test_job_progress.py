"""Phase 3 step 4: progress reporting.

What actually runs, by group:

* ``TestProgressReader``, ``TestRenderCommand``, ``TestStoreProgress``,
  ``TestContextProgress``, ``TestHandlerProgress``, ``TestCliProgress``:
  fakes only (fake FFmpeg output, fake tools, an injected clock or a
  fake render process runner); no FFmpeg.
* ``TestStoppableProgress``: a real child process (Python, not FFmpeg)
  writing FFmpeg-style progress files; real process stopping.
* ``TestRealFfmpegProgress``: real FFmpeg on generated clips in a
  temporary folder (skipped without FFmpeg).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from jarvis_ext.__main__ import main
from jarvis_ext.jobs import InvalidJobError, InvalidTransitionError, JobStatus, JobStore
from jarvis_ext.jobs.runner import (
    JOB_PROGRESS_EVENT,
    JobCancelled,
    JobContext,
    JobOutcome,
    run_next,
)
from jarvis_ext.video import render_job
from jarvis_ext.video.ffmpeg import PROGRESS_FILENAME, ProgressReader
from jarvis_ext.video.render_job import (
    KIND,
    RENDER_SHARE,
    VideoRenderHandler,
    expected_duration,
    stoppable_run_process,
)
from openjarvis.core.events import EventBus
from openjarvis.core.types import ToolResult

from .video_fakes import (
    HAVE_FFMPEG,
    FakeProcess,
    FakeRunner,
    digest_tree,
    make_render_tool,
    make_test_media,
    make_validate_tool,
    probe_payload,
)

WORKER = "worker-1"
PLAN = {
    "output_name": "reel",
    "clips": [
        {"source": "Shoot/a.mp4", "start": 0, "end": 6},
        {"source": "Shoot/b.mov", "start": "00:00:01.0", "end": "00:00:05.0"},
    ],
}  # 10 s of output

needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"
)
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        return self.now.isoformat(timespec="milliseconds")

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def store(tmp_path: Path):
    with JobStore(tmp_path / "jobs.db", clock=FakeClock()) as s:
        yield s


@pytest.fixture
def bus() -> EventBus:
    return EventBus(record_history=True)


def events(bus: EventBus) -> List[Dict[str, Any]]:
    return [e.data for e in bus.history if e.event_type == JOB_PROGRESS_EVENT]


def block(us: Any, *, out_time: str = "", state: str = "continue") -> bytes:
    lines = ["frame=10", "fps=0.00", f"out_time_us={us}", f"out_time_ms={us}"]
    if out_time:
        lines.append(f"out_time={out_time}")
    lines += ["speed=1x", f"progress={state}"]
    return ("\n".join(lines) + "\n").encode()


# -- FFmpeg progress parsing (fake output) ------------------------------------


class TestProgressReader:
    def reader(self) -> ProgressReader:
        return ProgressReader(Path("/nonexistent/progress.txt"))

    def test_complete_blocks(self):
        r = self.reader()
        r.feed(block(1_500_000) + block(3_250_000, state="end"))
        assert r.out_time == 3.25 and r.ended

    def test_out_time_fallback_and_na(self):
        r = self.reader()
        r.feed(block("N/A"))
        assert r.out_time is None
        r.feed(block("N/A", out_time="00:01:02.500000"))
        assert r.out_time == 62.5

    def test_negative_start_time_is_zero(self):
        r = self.reader()
        r.feed(block("-33000", out_time="-00:00:00.033000"))
        assert r.out_time == 0.0

    def test_partial_lines_wait_for_the_rest(self):
        r = self.reader()
        data = block(2_000_000)
        r.feed(data[:25])
        assert r.out_time is None
        r.feed(data[25:-3])
        assert r.out_time is None  # "progress=" line not complete yet
        r.feed(data[-3:])
        assert r.out_time == 2.0

    def test_block_without_progress_line_is_not_counted(self):
        r = self.reader()
        r.feed(b"out_time_us=5000000\nspeed=1x\n")
        assert r.out_time is None

    @pytest.mark.parametrize(
        "junk",
        [
            b"garbage\n=\n==\nout_time_us=abc\nout_time=soon\nprogress=continue\n",
            b"\xff\xfe\x00binary\nprogress=continue\n",
            b"x" * 10_000,
            b"\n\n\n",
        ],
    )
    def test_malformed_input_is_ignored(self, junk):
        r = self.reader()
        r.feed(block(1_000_000))
        r.feed(junk)
        r.feed(b"\n" + block(2_000_000))
        assert r.out_time == 2.0

    def test_duplicated_and_out_of_order_blocks_never_go_back(self):
        r = self.reader()
        seen = []
        for us in (1_000_000, 3_000_000, 3_000_000, 2_000_000, 0, 4_000_000):
            r.feed(block(us))
            seen.append(r.out_time)
        assert seen == [1.0, 3.0, 3.0, 3.0, 3.0, 4.0]

    def test_polls_a_growing_file(self, tmp_path):
        path = tmp_path / PROGRESS_FILENAME
        r = ProgressReader(path)
        assert r.poll() is None  # not created yet (FFmpeg still starting)
        path.write_bytes(block(1_000_000))
        assert r.poll() == 1.0
        assert r.poll() == 1.0  # nothing new (a delayed update)
        with open(path, "ab") as fh:
            fh.write(block(2_500_000)[:10])
        assert r.poll() == 1.0
        with open(path, "ab") as fh:
            fh.write(block(2_500_000)[10:])
        assert r.poll() == 2.5

    def test_replaced_file_is_reread_without_going_back(self, tmp_path):
        path = tmp_path / PROGRESS_FILENAME
        r = ProgressReader(path)
        path.write_bytes(block(4_000_000) + block(5_000_000))
        assert r.poll() == 5.0
        path.write_bytes(block(1_000_000))  # shorter: a new file
        assert r.poll() == 5.0
        with open(path, "ab") as fh:
            fh.write(block(6_000_000))
        assert r.poll() == 6.0


# -- render command compatibility (fake FFmpeg) -------------------------------


@pytest.fixture
def dirs(tmp_path):
    workspace = tmp_path / "media"
    (workspace / "Shoot").mkdir(parents=True)
    (workspace / "Shoot" / "a.mp4").write_bytes(b"a")
    (workspace / "Shoot" / "b.mov").write_bytes(b"b")
    output = tmp_path / "renders"
    output.mkdir()
    return workspace, output


def fake_ffprobe() -> FakeRunner:
    return FakeRunner(
        {
            "a.mp4": probe_payload(duration=10.0),
            "b.mov": probe_payload(duration=10.0),
            "render.mp4": probe_payload(width=1080, height=1920, duration=10.0),
        }
    )


class TestRenderCommand:
    def _command(self, dirs, **tool_kw) -> List[str]:
        workspace, output = dirs
        process = FakeProcess()
        tool = make_render_tool(
            workspace, output, runner=fake_ffprobe(), process_runner=process
        )
        for key, value in tool_kw.items():
            setattr(tool, key, value)
        result = tool.execute(plan=PLAN)
        assert result.success, result.content
        return process.calls[0]["cmd"]

    def test_default_command_has_no_progress(self, dirs):
        cmd = self._command(dirs)
        assert "-progress" not in cmd

    def test_progress_adds_exactly_one_global_option(self, dirs):
        plain = self._command(dirs)
        with_progress = self._command(dirs, _report_progress=True)
        i = with_progress.index("-progress")
        assert with_progress[i : i + 2] == ["-progress", f"file:{PROGRESS_FILENAME}"]
        assert with_progress[:i] == plain[:i] == [
            "/usr/bin/ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-v", "error",
        ]  # fmt: skip
        assert with_progress[:i] + with_progress[i + 2 :] == plain

    def test_tool_default_is_off(self, dirs):
        workspace, output = dirs
        tool = make_render_tool(workspace, output)
        assert tool._report_progress is False


# -- store (fake clock) -------------------------------------------------------


class TestStoreProgress:
    def _running(self, store):
        store.submit(KIND, {})
        return store.claim_next(WORKER)

    def test_new_and_claimed_jobs(self, store):
        queued = store.submit(KIND, {})
        assert queued.progress is None and queued.stage is None
        job = store.claim_next(WORKER)
        assert job.progress == 0.0 and job.stage is None

    def test_monotonic_and_capped(self, store):
        job = self._running(store)
        values = []
        for value in (10, 35.56, 20, 35.5, 120, 99.5):
            values.append(
                store.report_progress(
                    job.id, WORKER, stage="render", progress=value
                ).progress
            )
        assert values == [10.0, 35.6, 35.6, 35.6, 99.0, 99.0]

    def test_stage_changes_are_in_history(self, store):
        job = self._running(store)
        store.report_progress(job.id, WORKER, stage="render", progress=0)
        store.report_progress(job.id, WORKER, stage="render", progress=50)
        store.report_progress(job.id, WORKER, stage="validate", progress=40)
        current = store.get(job.id)
        assert (current.stage, current.progress) == ("validate", 50.0)
        notes = [e.note for e in store.events(job.id) if e.note.startswith("stage ")]
        assert notes == ["stage render", "stage validate"]

    def test_only_100_on_success(self, store):
        job = self._running(store)
        store.report_progress(job.id, WORKER, stage="validate", progress=99.9)
        assert store.get(job.id).progress == 99.0
        assert store.complete(job.id, WORKER).progress == 100.0

    @pytest.mark.parametrize("final", ["fail", "cancel"])
    def test_failed_or_cancelled_keep_last_progress(self, store, final):
        job = self._running(store)
        store.report_progress(job.id, WORKER, stage="render", progress=42)
        if final == "fail":
            done = store.fail(job.id, WORKER, "render_ffmpeg_failed")
        else:
            store.cancel(job.id)
            done = store.confirm_cancelled(job.id, WORKER)
        assert (done.progress, done.stage) == (42.0, "render")

    def test_requeue_starts_the_next_attempt_from_zero(self, store):
        job = self._running(store)
        store.report_progress(job.id, WORKER, stage="render", progress=60)
        queued = store.retry(job.id, WORKER, "ffmpeg_failed")
        assert queued.progress is None and queued.stage is None
        assert store.claim_next(WORKER).progress == 0.0

    def test_owner_and_status_checks(self, store):
        queued = store.submit(KIND, {})
        with pytest.raises(InvalidTransitionError):
            store.report_progress(queued.id, WORKER, stage="render", progress=1)
        job = store.claim_next(WORKER)
        with pytest.raises(InvalidTransitionError):
            store.report_progress(job.id, "other", stage="render", progress=1)

    @pytest.mark.parametrize(
        "stage,progress",
        [("Render", 1), ("", 1), ("x" * 40, 1), ("render", float("nan")),
         ("render", float("inf")), ("render", True), ("render", "5")],
    )  # fmt: skip
    def test_invalid_values(self, store, stage, progress):
        job = self._running(store)
        with pytest.raises(InvalidJobError):
            store.report_progress(job.id, WORKER, stage=stage, progress=progress)


# -- runner context: throttling, events, failures (fakes) ---------------------


class Handler:
    def __init__(self, action):
        self.action = action

    def run(self, context: JobContext) -> JobOutcome:
        return self.action(context) or JobOutcome.succeeded()


class TestContextProgress:
    def test_throttled_by_step(self, store, bus):
        store.submit("test.echo", {})

        def many(ctx):
            for i in range(200):
                ctx.progress("work", i * 0.25)  # 0 … 49.75 in tiny steps

        run_next(
            store, WORKER, {"test.echo": Handler(many)}, bus=bus, progress_interval=0
        )
        progress = [e["progress"] for e in events(bus) if e["status"] == "running"]
        # claim (0), then roughly one event per full point, not 200
        assert 45 <= len(progress) <= 52
        assert progress == sorted(progress)

    def test_throttled_by_time(self, store, bus):
        store.submit("test.echo", {})

        def many(ctx):
            for i in range(50):
                ctx.progress("work", i * 2)

        run_next(
            store, WORKER, {"test.echo": Handler(many)}, bus=bus, progress_interval=60
        )
        running = [e for e in events(bus) if e["status"] == "running"]
        # The claim, then the first report (a stage change); nothing else
        # within the minute.
        assert [(e["stage"], e["progress"]) for e in running] == [
            (None, 0.0),
            ("work", 0.0),
        ]

    def test_stage_change_is_always_reported(self, store, bus):
        store.submit("test.echo", {})

        def stages(ctx):
            ctx.progress("a", 10)
            ctx.progress("b", 10)
            ctx.progress("c", 10)

        run_next(
            store, WORKER, {"test.echo": Handler(stages)}, bus=bus, progress_interval=60
        )
        assert [e["stage"] for e in events(bus)] == [None, "a", "b", "c", "c"]

    def test_never_decreases_and_never_hits_100_while_running(self, store, bus):
        job = store.submit("test.echo", {})
        seen = []

        def bumpy(ctx):
            for value in (30, 10, 100, 150, 5):
                ctx.progress("work", value)
                seen.append(store.get(job.id).progress)

        run_next(
            store, WORKER, {"test.echo": Handler(bumpy)}, bus=bus, progress_interval=0
        )
        assert seen == [30.0, 30.0, 99.0, 99.0, 99.0]
        assert store.get(job.id).progress == 100.0
        assert events(bus)[-1] == {
            "job_id": job.id,
            "kind": "test.echo",
            "status": "succeeded",
            "stage": "work",
            "progress": 100.0,
            "attempt": 1,
        }

    def test_bad_values_are_ignored(self, store, bus):
        job = store.submit("test.echo", {})

        def bad(ctx):
            ctx.progress("Bad Stage", 10)
            ctx.progress("work", float("nan"))
            ctx.progress("work", "50")
            ctx.progress("work", 20)

        done = run_next(
            store, WORKER, {"test.echo": Handler(bad)}, bus=bus, progress_interval=0
        )
        assert done.status is JobStatus.SUCCEEDED
        assert [e.note for e in store.events(job.id)].count("stage work") == 1

    def test_failed_job_is_not_reported_complete(self, store, bus):
        store.submit("test.echo", {})

        def fail(ctx):
            ctx.progress("work", 80)
            return JobOutcome.failed("boom")

        done = run_next(
            store, WORKER, {"test.echo": Handler(fail)}, bus=bus, progress_interval=0
        )
        assert done.progress == 80.0
        assert events(bus)[-1]["status"] == "failed"
        assert all(e["progress"] != 100.0 for e in events(bus))

    def test_broken_bus_never_affects_the_job(self, store):
        class Broken:
            calls = 0

            def publish(self, *a, **k):
                Broken.calls += 1
                raise RuntimeError("bus down")

        store.submit("test.echo", {})
        done = run_next(
            store,
            WORKER,
            {"test.echo": Handler(lambda c: c.progress("work", 50))},
            bus=Broken(),
            progress_interval=0,
        )
        assert done.status is JobStatus.SUCCEEDED
        assert done.progress == 100.0 and Broken.calls == 3

    def test_failing_subscriber_never_affects_the_job(self, store):
        real_bus = EventBus()

        def explode(event):
            raise ValueError("subscriber bug")

        real_bus.subscribe(JOB_PROGRESS_EVENT, explode)
        store.submit("test.echo", {})
        done = run_next(
            store,
            WORKER,
            {"test.echo": Handler(lambda c: c.progress("work", 50))},
            bus=real_bus,
            progress_interval=0,
        )
        assert done.status is JobStatus.SUCCEEDED

    def test_store_failure_during_progress_is_not_fatal(self, store, bus, monkeypatch):
        store.submit("test.echo", {})

        def flaky(*a, **k):
            from jarvis_ext.jobs.store import JobStoreError

            raise JobStoreError("The job database is busy; try again")

        def work(ctx):
            monkeypatch.setattr(store, "report_progress", flaky)
            ctx.progress("work", 50)
            monkeypatch.undo()

        done = run_next(
            store, WORKER, {"test.echo": Handler(work)}, bus=bus, progress_interval=0
        )
        assert done.status is JobStatus.SUCCEEDED

    def test_events_contain_no_paths_or_payload(self, store, bus, tmp_path):
        store.submit("test.echo", {"secret_path": str(tmp_path)})
        run_next(store, WORKER, {"test.echo": Handler(lambda c: None)}, bus=bus)
        assert events(bus)
        for data in events(bus):
            assert set(data) == {
                "job_id",
                "kind",
                "status",
                "stage",
                "progress",
                "attempt",
            }
            assert str(tmp_path) not in json.dumps(data)

    def test_default_bus_is_the_openjarvis_bus(self, store):
        from openjarvis.core.events import get_event_bus

        seen = []
        get_event_bus().subscribe(JOB_PROGRESS_EVENT, lambda e: seen.append(e.data))
        store.submit("test.echo", {})
        run_next(store, WORKER, {"test.echo": Handler(lambda c: None)})
        assert [d["status"] for d in seen] == ["running", "succeeded"]


# -- video handler (fake tools) -----------------------------------------------


def tool_result(report: Dict[str, Any], success: bool) -> ToolResult:
    return ToolResult(tool_name="fake", content=json.dumps(report), success=success)


RENDER_OK = tool_result(
    {"render_status": "succeeded", "output": {"file": "reel_v001.mp4"}, "warnings": []},
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


class FakeEncodingRender:
    """A render tool that reports FFmpeg-style progress, then a result."""

    def __init__(self, times, result: Any = RENDER_OK, raise_after=None):
        self.times, self.result, self.raise_after = times, result, raise_after

    def execute(self, **params):
        callback = render_job._render_progress.callback
        for t in self.times:
            callback(t)
        if self.raise_after is not None:
            raise self.raise_after
        return self.result


class FakeTool:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def execute(self, **params):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def run_video(store, bus, render, validate=VALIDATE_OK, plan=PLAN):
    store.submit(KIND, {"plan": plan})
    handler = VideoRenderHandler(
        render_tool=render,
        validate_tool=validate
        if isinstance(validate, FakeTool)
        else FakeTool(validate),
    )
    return run_next(store, WORKER, {KIND: handler}, bus=bus, progress_interval=0)


class TestHandlerProgress:
    def test_expected_duration(self):
        assert expected_duration(PLAN) == 10.0
        assert expected_duration({"clips": [{"start": "00:01:00", "end": 61.5}]}) == 1.5
        for bad in ({}, {"clips": "x"}, {"clips": [{"start": 1}]},
                    {"clips": [{"start": "soon", "end": 2}]},
                    {"clips": [{"start": 5, "end": 5}]}):  # fmt: skip
            assert expected_duration(bad) is None

    def test_success_path(self, store, bus):
        done = run_video(store, bus, FakeEncodingRender([1, 2.5, 5, 4, 10, 12]))
        assert done.status is JobStatus.SUCCEEDED and done.progress == 100.0
        trail = [(e["status"], e["stage"], e["progress"]) for e in events(bus)]
        assert trail == [
            ("running", None, 0.0),
            ("running", "render", 0.0),
            ("running", "render", 9.5),
            ("running", "render", 23.8),
            ("running", "render", 47.5),
            ("running", "render", RENDER_SHARE),  # 10 s of 10 s; 12 s capped
            ("running", "validate", RENDER_SHARE),
            ("succeeded", "validate", 100.0),
        ]
        assert render_job._render_progress.callback is None

    def test_100_only_after_validation(self, store, bus):
        seen = []

        class Validate(FakeTool):
            def execute(self, **params):
                seen.append(store.list_jobs()[0].progress)
                return super().execute(**params)

        run_video(store, bus, FakeEncodingRender([10]), Validate(VALIDATE_OK))
        assert seen == [RENDER_SHARE]

    def test_unknown_duration_keeps_render_at_zero(self, store, bus):
        plan = {
            "output_name": "x",
            "clips": [{"source": "a.mp4", "start": "?", "end": 1}],
        }
        done = run_video(store, bus, FakeEncodingRender([1, 2, 3]), plan=plan)
        render = [e["progress"] for e in events(bus) if e["stage"] == "render"]
        assert render == [0.0]
        assert done.status is JobStatus.SUCCEEDED

    def test_validation_failure_stays_below_100(self, store, bus):
        failed = tool_result(
            {"validation_status": "failed", "ready_for_review": False,
             "output": "reel_v001.mp4", "checks": [], "warnings": []},
            False,
        )  # fmt: skip
        done = run_video(store, bus, FakeEncodingRender([10]), failed)
        assert done.status is JobStatus.FAILED
        assert (done.stage, done.progress) == ("validate", RENDER_SHARE)
        assert events(bus)[-1]["status"] == "failed"

    def test_render_failure_keeps_render_progress(self, store, bus):
        failed = tool_result({"render_status": "failed_checks", "warnings": []}, False)
        done = run_video(store, bus, FakeEncodingRender([4], result=failed))
        assert done.status is JobStatus.FAILED
        assert (done.stage, done.progress) == ("render", 38.0)

    def test_cancel_mid_render_keeps_partial_progress(self, store, bus):
        done = run_video(
            store, bus, FakeEncodingRender([3], raise_after=JobCancelled())
        )
        assert done.status is JobStatus.FAILED  # no cancel was requested
        store.submit(KIND, {"plan": PLAN})
        job_id = store.list_jobs()[0].id

        class CancelThenStop(FakeEncodingRender):
            def execute(self, **params):
                render_job._render_progress.callback(3)
                store.cancel(job_id)
                raise JobCancelled()

        handler = VideoRenderHandler(
            render_tool=CancelThenStop([]), validate_tool=FakeTool(VALIDATE_OK)
        )
        done = run_next(store, WORKER, {KIND: handler}, bus=bus, progress_interval=0)
        assert done.status is JobStatus.CANCELLED
        assert (done.stage, done.progress) == ("render", 28.5)

    def test_interrupted_job_is_requeued_without_stale_progress(self, store, bus):
        from jarvis_ext.jobs.runner import WorkerInterrupted

        done = run_video(
            store, bus, FakeEncodingRender([5], raise_after=WorkerInterrupted())
        )
        assert done.status is JobStatus.QUEUED
        assert done.progress is None and done.stage is None
        assert events(bus)[-1]["status"] == "queued"

    def test_default_tools_report_progress(self, tmp_path):
        handler = VideoRenderHandler(config_path=tmp_path / "none.toml")
        assert handler._render._report_progress is True


# -- stoppable runner + progress file (real child process, not FFmpeg) --------


WRITER = textwrap.dedent(
    """
    import sys, time
    blocks = [int(x) for x in sys.argv[1].split(",")]
    with open("progress.txt", "w") as fh:
        for us in blocks:
            fh.write(f"frame=1\\nout_time_us={us}\\nprogress=continue\\n")
            fh.flush()
            time.sleep(0.15)
        time.sleep(float(sys.argv[2]))
    """
)


@posix_only
class TestStoppableProgress:
    def _cmd(self, blocks: str, linger: float, *, flag: bool = True) -> List[str]:
        return [sys.executable, "-c", WRITER, blocks, str(linger)] + (
            ["-progress"] if flag else []
        )

    def test_progress_reaches_the_callback(self, tmp_path):
        seen = []
        render_job._render_progress.callback = seen.append
        try:
            code = stoppable_run_process(
                self._cmd("500000,1000000,2000000", 0.6),
                cwd=tmp_path,
                timeout=30,
                log_path=tmp_path / "log",
            )
        finally:
            render_job._render_progress.callback = None
        assert code == 0
        assert seen and seen == sorted(seen) and seen[-1] == 2.0

    def test_no_reader_without_flag_or_callback(self, tmp_path):
        seen = []
        render_job._render_progress.callback = seen.append
        try:
            stoppable_run_process(
                self._cmd("1000000", 0.5, flag=False),
                cwd=tmp_path, timeout=30, log_path=tmp_path / "log",
            )  # fmt: skip
        finally:
            render_job._render_progress.callback = None
        assert seen == []
        assert (
            stoppable_run_process(
                self._cmd("1000000", 0.0),
                cwd=tmp_path,
                timeout=30,
                log_path=tmp_path / "l",
            )
            == 0
        )

    def test_callback_errors_do_not_stop_the_process(self, tmp_path):
        def broken(seconds):
            raise RuntimeError("callback bug")

        render_job._render_progress.callback = broken
        try:
            code = stoppable_run_process(
                self._cmd("1000000", 0.5), cwd=tmp_path, timeout=30,
                log_path=tmp_path / "log",
            )  # fmt: skip
        finally:
            render_job._render_progress.callback = None
        assert code == 0

    def test_cancel_with_progress_enabled_stops_the_process(self, store, bus, tmp_path):
        job = store.submit("test.echo", {})
        blocks = ",".join(str(i * 500_000) for i in range(1, 400))
        cmd = self._cmd(blocks, 60) + [str(tmp_path)]  # tmp_path: unique marker

        def work(ctx):
            def on_time(seconds):
                ctx.progress("render", seconds)
                if seconds >= 1.0:
                    store.cancel(job.id)

            render_job._render_progress.callback = on_time
            try:
                stoppable_run_process(
                    cmd, cwd=tmp_path, timeout=60, log_path=tmp_path / "log"
                )
            finally:
                render_job._render_progress.callback = None

        done = run_next(
            store, WORKER, {"test.echo": Handler(work)}, bus=bus, progress_interval=0
        )
        assert done.status is JobStatus.CANCELLED
        assert 1.0 <= done.progress < 99
        pids = subprocess.run(
            ["pgrep", "-f", str(tmp_path)], capture_output=True, text=True
        )
        assert pids.stdout.strip() == ""


# -- CLI (fakes) --------------------------------------------------------------


class TestCliProgress:
    def test_list_and_show_include_stage_and_progress(self, capsys, tmp_path):
        db = tmp_path / "jobs.db"
        config = tmp_path / "config.toml"
        config.write_text(f'[jarvis.jobs]\ndatabase_path = "{db}"\n')
        with JobStore(db) as store:
            store.submit(KIND, {"plan": PLAN})
            job = store.claim_next(WORKER)
            store.report_progress(job.id, WORKER, stage="render", progress=37.25)
        assert main(["jobs", "list", "--config", str(config)]) == 0
        (listed,) = json.loads(capsys.readouterr().out)["jobs"]
        assert (listed["stage"], listed["progress"]) == ("render", 37.2)
        assert main(["jobs", "show", job.id[:8], "--config", str(config)]) == 0
        shown = json.loads(capsys.readouterr().out)
        assert (shown["job"]["stage"], shown["job"]["progress"]) == ("render", 37.2)
        assert "stage render" in [e["note"] for e in shown["events"]]


# -- real FFmpeg --------------------------------------------------------------


@needs_ffmpeg
@posix_only
class TestRealFfmpegProgress:
    @pytest.fixture
    def real(self, tmp_path):
        workspace = tmp_path / "workspace"
        output = tmp_path / "renders"
        output.mkdir()
        make_test_media(workspace, tmp_path / "assets")
        yield workspace, output
        subprocess.run(["pkill", "-f", str(workspace)], capture_output=True)

    def _handler(self, real) -> VideoRenderHandler:
        workspace, output = real
        render = make_render_tool(
            workspace, output, runner=subprocess.run,
            process_runner=stoppable_run_process,
        )  # fmt: skip
        render._which = __import__("shutil").which
        render._report_progress = True
        validate = make_validate_tool(
            workspace, output, runner=subprocess.run,
            process_runner=stoppable_run_process,
        )  # fmt: skip
        validate._which = __import__("shutil").which
        return VideoRenderHandler(render_tool=render, validate_tool=validate)

    PLAN = {
        "output_name": "progress_reel",
        "clips": [
            {"source": "Shoot/landscape.mp4", "start": 0, "end": 4},
            {"source": "Shoot/portrait silent.mov", "start": 0, "end": 3},
        ],
        "video": {"x264_preset": "veryslow"},
    }

    def test_real_render_reports_progress(self, store, bus, real):
        workspace, output = real
        before = digest_tree(workspace)
        store.submit(KIND, {"plan": self.PLAN})
        done = run_next(
            store, WORKER, {KIND: self._handler(real)}, bus=bus, progress_interval=0
        )
        assert done.status is JobStatus.SUCCEEDED, done.to_dict()
        assert done.result["ready_for_review"] is True
        trail = [(e["status"], e["stage"], e["progress"]) for e in events(bus)]
        render = [p for s, st, p in trail if st == "render"]
        assert render == sorted(render)
        assert any(0 < p < RENDER_SHARE for p in render), trail  # real mid-encode
        assert trail[-2] == ("running", "validate", RENDER_SHARE)
        assert trail[-1] == ("succeeded", "validate", 100.0)
        assert all(p < 100 for s, st, p in trail[:-1])
        assert (output / "progress_reel_v001.mp4").is_file()
        assert digest_tree(workspace) == before
        assert not any((output / ".jarvis-tmp").iterdir())

    def test_cancel_after_real_progress(self, store, bus, real):
        workspace, output = real
        job = store.submit(KIND, {"plan": self.PLAN})

        def cancel_on_progress(event):
            data = event.data
            running = data["status"] == "running"
            if running and data["stage"] == "render" and data["progress"] > 0:
                with JobStore(store_path) as other:
                    other.cancel(job.id)

        store_path = Path(store._conn.execute("PRAGMA database_list").fetchone()[2])
        bus.subscribe(JOB_PROGRESS_EVENT, cancel_on_progress)
        done = run_next(
            store, WORKER, {KIND: self._handler(real)}, bus=bus, progress_interval=0
        )
        assert done.status is JobStatus.CANCELLED, done.to_dict()
        assert 0 < done.progress < RENDER_SHARE
        assert not list(output.glob("*.mp4"))
        assert not any((output / ".jarvis-tmp").iterdir())
        out = subprocess.run(
            ["pgrep", "-f", f"ffmpeg .*{workspace}"], capture_output=True, text=True
        )
        assert out.stdout.strip() == ""
