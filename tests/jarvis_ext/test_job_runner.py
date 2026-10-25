"""The single-job runner and the ``video.render`` handler.

Three groups, by what actually runs:

* ``TestRunNext`` and ``TestVideoRenderHandler``: fake handlers / fake
  tools only.  No FFmpeg, no render tool code.
* ``TestHandlerWithRenderTool``: the real ``media_render`` tool with fake
  FFmpeg/ffprobe (``FakeRunner``/``FakeProcess``).
* ``TestRealFfmpegRunner``: real FFmpeg end to end, on generated media in a
  temporary folder (skipped when FFmpeg is not installed).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from jarvis_ext.jobs import InvalidTransitionError, JobStatus, JobStore
from jarvis_ext.jobs.runner import (
    JobCancelled,
    JobContext,
    JobOutcome,
    record_outcome,
    run_next,
)
from jarvis_ext.video.ffmpeg import run_process
from jarvis_ext.video.render_job import (
    KIND,
    RENDER_PERMANENT,
    RENDER_RETRYABLE,
    VideoRenderHandler,
)
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
    "clips": [{"source": "Shoot/a.mp4", "start": 1, "end": 4}],
}

needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"
)


@pytest.fixture
def store(tmp_path: Path):
    with JobStore(tmp_path / "jobs.db") as s:
        yield s


# -- fakes --------------------------------------------------------------------


def tool_result(report: Any, success: Optional[bool] = None) -> ToolResult:
    if success is None:
        success = bool(report.get("ready_for_review")) or (
            report.get("render_status") == "succeeded"
        )
    content = report if isinstance(report, str) else json.dumps(report)
    return ToolResult(tool_name="fake", content=content, success=success)


def render_ok(file: str = "reel_v001.mp4", **extra: Any) -> Dict[str, Any]:
    return {
        "render_status": "succeeded",
        "validation_status": "not_validated",
        "ready_for_review": False,
        "output": {"file": file, "version": 1, "size_bytes": 10, "sha256": "ab"},
        "record": "reel_v001.render.json",
        "prepublish_checks": [{"check": "dimensions", "passed": True}],
        "ffmpeg_version": "9.0.2",
        "elapsed_seconds": 1.0,
        "warnings": [],
        **extra,
    }


def render_failed(status: str) -> Dict[str, Any]:
    return {"render_status": status, "error": f"{status} happened", "warnings": []}


def validate_ok(file: str = "reel_v001.mp4", **extra: Any) -> Dict[str, Any]:
    return {
        "validation_status": "passed",
        "ready_for_review": True,
        "output": file,
        "checks": [{"check": "decodes_cleanly", "passed": True}],
        "preview": "review/reel_v001_preview_v001.mp4",
        "thumbnail": "review/reel_v001_thumb_v001.jpg",
        "warnings": [],
        **extra,
    }


class FakeTool:
    """Returns queued results in order; an item may be a callable or exception."""

    def __init__(self, *results: Any) -> None:
        self.results: List[Any] = list(results)
        self.calls: List[Dict[str, Any]] = []

    def execute(self, **params: Any) -> Any:
        self.calls.append(params)
        item = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(**params)
        if isinstance(item, dict):
            return tool_result(item)
        return item


def handler(render: Any, validate: Any = None) -> VideoRenderHandler:
    return VideoRenderHandler(
        render_tool=render if isinstance(render, FakeTool) else FakeTool(render),
        validate_tool=(
            validate
            if isinstance(validate, FakeTool)
            else FakeTool(validate if validate is not None else validate_ok())
        ),
    )


def run_video(store: JobStore, h: VideoRenderHandler):
    return run_next(store, WORKER, {KIND: h})


class Fixed:
    """A generic handler returning a fixed outcome (or running a function)."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.contexts: List[JobContext] = []

    def run(self, context: JobContext) -> Any:
        self.contexts.append(context)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(context)
        return self.outcome


# -- generic runner (fake handlers) -------------------------------------------


class TestRunNext:
    def test_nothing_queued(self, store):
        assert run_next(store, WORKER, {"test.echo": Fixed(None)}) is None

    def test_no_handlers_claims_nothing(self, store):
        job = store.submit("test.echo", {})
        assert run_next(store, WORKER, {}) is None
        assert store.get(job.id).status is JobStatus.QUEUED

    def test_only_handled_kinds_are_claimed(self, store):
        other = store.submit("research.report", {})
        mine = store.submit("test.echo", {})
        done = run_next(store, WORKER, {"test.echo": Fixed(JobOutcome.succeeded())})
        assert done.id == mine.id
        assert store.get(other.id).status is JobStatus.QUEUED

    def test_runs_one_job_only(self, store):
        store.submit("test.echo", {})
        second = store.submit("test.echo", {})
        run_next(store, WORKER, {"test.echo": Fixed(JobOutcome.succeeded())})
        assert store.get(second.id).status is JobStatus.QUEUED

    def test_success_records_result(self, store):
        job = store.submit("test.echo", {"x": 1})
        h = Fixed(JobOutcome.succeeded({"answer": 42}))
        done = run_next(store, WORKER, {"test.echo": h})
        assert done.status is JobStatus.SUCCEEDED
        assert done.result == {"answer": 42}
        assert h.contexts[0].job.id == job.id
        assert h.contexts[0].job.payload == {"x": 1}

    def test_failure_records_code_message_and_result(self, store):
        store.submit("test.echo", {})
        out = JobOutcome.failed("bad_input", "nope", {"partial": True})
        done = run_next(store, WORKER, {"test.echo": Fixed(out)})
        assert done.status is JobStatus.FAILED
        assert (done.error_code, done.error_message) == ("bad_input", "nope")
        assert done.result == {"partial": True}

    def test_exception_fails_without_leaking_details(self, store):
        store.submit("test.echo", {})
        boom = RuntimeError("/Users/someone/secret/path exploded")
        done = run_next(store, WORKER, {"test.echo": Fixed(boom)})
        assert done.status is JobStatus.FAILED
        assert done.error_code == "runner_error"
        assert "RuntimeError" in done.error_message
        assert "/Users" not in done.error_message

    @pytest.mark.parametrize("value", [None, "ok", {"action": "succeeded"}, True])
    def test_non_outcome_is_never_success(self, store, value):
        store.submit("test.echo", {})
        done = run_next(store, WORKER, {"test.echo": Fixed(value)})
        assert done.status is JobStatus.FAILED
        assert done.error_code == "unexpected_result"

    def test_invalid_outcome_built_by_handler(self, store):
        store.submit("test.echo", {})
        done = run_next(
            store,
            WORKER,
            {"test.echo": Fixed(lambda ctx: JobOutcome.failed("Not A Code"))},
        )
        assert done.status is JobStatus.FAILED
        assert done.error_code == "runner_error"

    def test_unknown_action_is_refused(self):
        with pytest.raises(ValueError):
            JobOutcome("done")

    def test_unstorable_success_result_fails(self, store):
        store.submit("test.echo", {})
        out = JobOutcome.succeeded({"bad": float("nan")})
        done = run_next(store, WORKER, {"test.echo": Fixed(out)})
        assert done.status is JobStatus.FAILED
        assert done.error_code == "invalid_result"
        assert done.result is None

    def test_retry_then_attempt_limit(self, store):
        job = store.submit("test.echo", {})
        h = {"test.echo": Fixed(JobOutcome.retry("flaky", "try again"))}
        first = run_next(store, WORKER, h)
        assert first.status is JobStatus.QUEUED and first.attempts == 1
        assert first.error_code == "flaky"
        second = run_next(store, WORKER, h)
        assert second.id == job.id
        assert second.status is JobStatus.FAILED and second.attempts == 2
        assert second.error_code == "flaky"
        assert "not retried" in second.error_message
        assert run_next(store, WORKER, h) is None

    def test_single_attempt_is_not_retried(self, store):
        store.submit("test.echo", {}, max_attempts=1)
        done = run_next(store, WORKER, {"test.echo": Fixed(JobOutcome.retry("x"))})
        assert done.status is JobStatus.FAILED and done.attempts == 1

    def test_retry_after_cancel_request_fails(self, store):
        store.submit("test.echo", {})

        def cancel_then_retry(ctx):
            store.cancel(ctx.job.id)
            return JobOutcome.retry("flaky", "boom")

        done = run_next(store, WORKER, {"test.echo": Fixed(cancel_then_retry)})
        assert done.status is JobStatus.FAILED
        assert "cancellation was requested" in done.error_message

    def test_handler_sees_cancel_request(self, store):
        store.submit("test.echo", {})
        seen = []

        def check(ctx):
            seen.append(ctx.cancel_requested())
            store.cancel(ctx.job.id)
            seen.append(ctx.cancel_requested())
            return JobOutcome.cancelled({"stage": "x"})

        done = run_next(store, WORKER, {"test.echo": Fixed(check)})
        assert seen == [False, True]
        assert done.status is JobStatus.CANCELLED
        assert done.result == {"stage": "x"}

    def test_unrequested_cancellation_is_not_recorded_as_cancelled(self, store):
        store.submit("test.echo", {})
        done = run_next(
            store, WORKER, {"test.echo": Fixed(JobOutcome.cancelled({"a": 1}))}
        )
        assert done.status is JobStatus.FAILED
        assert done.error_code == "unexpected_result"

    def test_job_cancelled_exception(self, store):
        store.submit("test.echo", {})

        def stop(ctx):
            store.cancel(ctx.job.id)
            ctx.note("stage one done")
            raise JobCancelled()

        done = run_next(store, WORKER, {"test.echo": Fixed(stop)})
        assert done.status is JobStatus.CANCELLED
        assert done.result == {"stopped": True, "notes": ["stage one done"]}

    @pytest.mark.parametrize(
        "outcome", [JobOutcome.succeeded({"ok": 1}), JobOutcome.failed("boom")]
    )
    def test_real_outcome_wins_over_cancel_request(self, store, outcome):
        store.submit("test.echo", {})

        def finish(ctx):
            store.cancel(ctx.job.id)
            return outcome

        done = run_next(store, WORKER, {"test.echo": Fixed(finish)})
        assert done.status.value == outcome.action
        assert done.cancel_requested

    def test_notes_go_to_history(self, store):
        job = store.submit("test.echo", {})

        def noisy(ctx):
            ctx.note("stage A finished")
            return JobOutcome.succeeded()

        run_next(store, WORKER, {"test.echo": Fixed(noisy)})
        notes = [(e.from_status, e.to_status, e.note) for e in store.events(job.id)]
        assert (JobStatus.RUNNING, JobStatus.RUNNING, "stage A finished") in notes

    def test_add_note_requires_owner(self, store):
        job = store.submit("test.echo", {})
        store.claim_next(WORKER)
        with pytest.raises(InvalidTransitionError):
            store.add_note(job.id, "someone-else", "x")

    def test_outcome_for_another_workers_job_is_refused(self, store):
        store.submit("test.echo", {})
        job = store.claim_next("worker-2")
        with pytest.raises(InvalidTransitionError):
            record_outcome(store, job, WORKER, JobOutcome.succeeded())
        assert store.get(job.id).status is JobStatus.RUNNING

    def test_keyboard_interrupt_leaves_job_running(self, store):
        job = store.submit("test.echo", {})
        with pytest.raises(KeyboardInterrupt):
            run_next(store, WORKER, {"test.echo": Fixed(KeyboardInterrupt())})
        assert store.get(job.id).status is JobStatus.RUNNING


# -- video.render handler (fake tools) ----------------------------------------


class TestVideoRenderHandler:
    def test_success(self, store):
        job = store.submit(KIND, {"plan": PLAN})
        render, validate = FakeTool(render_ok()), FakeTool(validate_ok())
        done = run_video(store, handler(render, validate))
        assert done.status is JobStatus.SUCCEEDED
        assert done.result["output"] == "reel_v001.mp4"
        assert done.result["ready_for_review"] is True
        assert done.result["render"]["output"]["sha256"] == "ab"
        assert done.result["validate"]["preview"].startswith("review/")
        assert render.calls == [{"plan": PLAN}]
        assert validate.calls == [{"output": "reel_v001.mp4"}]
        notes = [e.note for e in store.events(job.id)]
        assert "rendered reel_v001.mp4" in notes
        assert "validated reel_v001.mp4" in notes

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"plan": "not an object"},
            {"plan": {}},
            {"plan": []},
            {"plan": PLAN, "priority": 1},
            {"edit_plan": PLAN},
        ],
    )
    def test_invalid_payload_never_renders(self, store, payload):
        store.submit(KIND, payload)
        render = FakeTool(render_ok())
        done = run_video(store, handler(render))
        assert done.status is JobStatus.FAILED
        assert done.error_code == "invalid_payload"
        assert render.calls == []

    def test_other_kinds_are_not_claimed(self, store):
        job = store.submit("video.publish", {"plan": PLAN})
        assert run_video(store, handler(render_ok())) is None
        assert store.get(job.id).status is JobStatus.QUEUED

    @pytest.mark.parametrize("status", sorted(RENDER_RETRYABLE))
    def test_retryable_render_failures(self, store, status):
        store.submit(KIND, {"plan": PLAN})
        validate = FakeTool(validate_ok())
        done = run_video(store, handler(render_failed(status), validate))
        assert done.status is JobStatus.QUEUED
        assert done.error_code == f"render_{status}"
        assert done.error_message == f"{status} happened"
        assert validate.calls == []

    @pytest.mark.parametrize("status", sorted(RENDER_PERMANENT))
    def test_permanent_render_failures(self, store, status):
        store.submit(KIND, {"plan": PLAN})
        validate = FakeTool(validate_ok())
        done = run_video(store, handler(render_failed(status), validate))
        assert done.status is JobStatus.FAILED and done.attempts == 1
        assert done.error_code == f"render_{status}"
        assert done.result["output"] is None
        assert done.result["ready_for_review"] is False
        assert validate.calls == []

    def test_timeout_and_failed_checks_are_permanent(self):
        assert {"timeout", "failed_checks"} <= RENDER_PERMANENT
        assert not RENDER_PERMANENT & RENDER_RETRYABLE

    def test_unknown_render_status_is_permanent(self, store):
        store.submit(KIND, {"plan": PLAN})
        done = run_video(store, handler(render_failed("brand_new_status")))
        assert done.status is JobStatus.FAILED
        assert done.error_code == "render_unexpected"

    def test_failed_checks_are_listed(self, store):
        store.submit(KIND, {"plan": PLAN})
        report = render_failed("failed_checks")
        report["checks"] = [
            {"check": "dimensions", "passed": False},
            {"check": "codec", "passed": True},
        ]
        done = run_video(store, handler(report))
        assert done.result["render"]["failed_checks"] == ["dimensions"]

    def test_retry_limit_renders_twice_at_most(self, store):
        store.submit(KIND, {"plan": PLAN})
        render = FakeTool(render_failed("ffmpeg_failed"))
        h = handler(render)
        assert run_video(store, h).status is JobStatus.QUEUED
        done = run_video(store, h)
        assert done.status is JobStatus.FAILED and done.attempts == 2
        assert done.error_code == "render_ffmpeg_failed"
        assert run_video(store, h) is None
        assert len(render.calls) == 2

    def test_retry_then_success(self, store):
        store.submit(KIND, {"plan": PLAN})
        render = FakeTool(render_failed("output_unavailable"), render_ok())
        h = handler(render)
        run_video(store, h)
        done = run_video(store, h)
        assert done.status is JobStatus.SUCCEEDED and done.attempts == 2

    @pytest.mark.parametrize(
        "status",
        [
            "failed",
            "passed_without_review_files",
            "not_found",
            "no_record",
            "invalid_arguments",
            "config_error",
            "not_configured",
            "workspace_unavailable",
            "output_unavailable",
            "output_error",
            "ffmpeg_missing",
            "ffmpeg_unusable",
        ],
    )
    def test_validation_failures_are_final_and_keep_the_render(self, store, status):
        store.submit(KIND, {"plan": PLAN})
        report = {
            "validation_status": status,
            "ready_for_review": False,
            "output": "reel_v001.mp4",
            "error": f"{status}!",
            "checks": [{"check": "decodes_cleanly", "passed": False}],
            "warnings": [],
        }
        render = FakeTool(render_ok())
        done = run_video(store, handler(render, tool_result(report, False)))
        assert done.status is JobStatus.FAILED and done.attempts == 1
        assert done.error_code == f"validate_{status}"
        assert done.result["output"] == "reel_v001.mp4"
        assert done.result["ready_for_review"] is False
        assert len(render.calls) == 1

    @pytest.mark.parametrize(
        "render_report",
        [
            tool_result(render_failed("ffmpeg_failed"), success=True),
            tool_result(render_ok(), success=False),
            tool_result(render_ok(file="../escape.mp4")),
            tool_result(render_ok(file=".hidden.mp4")),
            tool_result(render_ok(file="reel_v001.mov")),
            tool_result(render_ok(file=None)),
            tool_result({**render_ok(), "output": "reel_v001.mp4"}),
            tool_result("not json", success=True),
            tool_result("[1, 2]", success=True),
            None,
        ],
    )
    def test_inconsistent_render_reports_never_succeed(self, store, render_report):
        store.submit(KIND, {"plan": PLAN})
        validate = FakeTool(validate_ok())
        done = run_video(store, handler(FakeTool(render_report), validate))
        assert done.status in (JobStatus.FAILED, JobStatus.QUEUED)
        assert done.status is not JobStatus.SUCCEEDED
        assert validate.calls == []

    @pytest.mark.parametrize(
        "validate_report",
        [
            tool_result({**validate_ok(), "ready_for_review": False}, success=True),
            tool_result(validate_ok(), success=False),
            tool_result(validate_ok(file="other_v001.mp4")),
            tool_result({**validate_ok(), "preview": None}),
            tool_result({**validate_ok(), "thumbnail": None}),
            tool_result({**validate_ok(), "validation_status": "failed"}, True),
            tool_result("garbage", success=True),
            None,
        ],
    )
    def test_inconsistent_validation_never_succeeds(self, store, validate_report):
        store.submit(KIND, {"plan": PLAN})
        done = run_video(store, handler(render_ok(), FakeTool(validate_report)))
        assert done.status is JobStatus.FAILED
        assert done.result["ready_for_review"] is False

    def test_render_tool_exception(self, store):
        store.submit(KIND, {"plan": PLAN})
        done = run_video(store, handler(FakeTool(OSError("disk"))))
        assert done.status is JobStatus.FAILED
        assert done.error_code == "runner_error"

    def test_validate_tool_exception_is_not_success(self, store):
        store.submit(KIND, {"plan": PLAN})
        done = run_video(store, handler(render_ok(), FakeTool(ValueError("x"))))
        assert done.status is JobStatus.FAILED
        assert done.error_code == "runner_error"

    def test_long_warnings_are_trimmed(self, store):
        store.submit(KIND, {"plan": PLAN})
        report = render_ok(warnings=["w" * 5000] * 500)
        done = run_video(store, handler(report))
        warnings = done.result["render"]["warnings"]
        assert len(warnings) == 50 and all(len(w) == 500 for w in warnings)

    # -- cancellation (D27) ----------------------------------------------------

    def test_cancel_before_render(self, store):
        job = store.submit(KIND, {"plan": PLAN})
        render = FakeTool(render_ok())
        inner = handler(render)

        class CancelFirst:
            def run(self, ctx):
                store.cancel(ctx.job.id)
                return inner.run(ctx)

        done = run_next(store, WORKER, {KIND: CancelFirst()})
        assert done.id == job.id
        assert done.status is JobStatus.CANCELLED
        assert done.result["output"] is None
        assert render.calls == []

    def test_cancel_after_render_is_not_validated_or_ready(self, store):
        job = store.submit(KIND, {"plan": PLAN})

        def render_then_cancel(**params):
            store.cancel(job.id)
            return render_ok()

        validate = FakeTool(validate_ok())
        done = run_video(store, handler(FakeTool(render_then_cancel), validate))
        assert done.status is JobStatus.CANCELLED
        assert done.result["stage"] == "after_render"
        assert done.result["ready_for_review"] is False
        assert done.result["validate"] is None
        assert "not validated" in done.result["note"]
        assert validate.calls == []

    def test_job_cancelled_during_render_publishes_nothing(self, store):
        job = store.submit(KIND, {"plan": PLAN})

        def stopped(**params):
            store.cancel(job.id)
            raise JobCancelled()

        validate = FakeTool(validate_ok())
        done = run_video(store, handler(FakeTool(stopped), validate))
        assert done.status is JobStatus.CANCELLED
        assert validate.calls == []
        assert "output" not in done.result or done.result.get("output") is None

    def test_job_cancelled_during_validation(self, store):
        job = store.submit(KIND, {"plan": PLAN})

        def stopped(**params):
            store.cancel(job.id)
            raise JobCancelled()

        done = run_video(store, handler(render_ok(), FakeTool(stopped)))
        assert done.status is JobStatus.CANCELLED
        assert done.result["stage"] == "during_validation"
        assert done.result["ready_for_review"] is False

    def test_validation_that_finishes_wins_over_cancel(self, store):
        job = store.submit(KIND, {"plan": PLAN})

        def validate_then_cancelled(**params):
            store.cancel(job.id)
            return validate_ok()

        done = run_video(store, handler(render_ok(), FakeTool(validate_then_cancelled)))
        assert done.status is JobStatus.SUCCEEDED
        assert done.cancel_requested


# -- real media_render tool, fake FFmpeg --------------------------------------


@pytest.fixture
def dirs(tmp_path: Path):
    workspace = tmp_path / "media"
    (workspace / "Shoot").mkdir(parents=True)
    (workspace / "Shoot" / "a.mp4").write_bytes(b"source a")
    output = tmp_path / "renders"
    output.mkdir()
    return workspace, output


def _fake_ffprobe() -> FakeRunner:
    return FakeRunner(
        {
            "a.mp4": probe_payload(duration=10.0),
            "render.mp4": probe_payload(width=1080, height=1920, duration=3.0),
        }
    )


def _published(output: Path) -> List[str]:
    return sorted(
        p.name for p in output.iterdir() if p.is_file() and p.name != ".DS_Store"
    )


def _temp_left(output: Path) -> List[str]:
    tmp = output / ".jarvis-tmp"
    return sorted(p.name for p in tmp.iterdir()) if tmp.exists() else []


class TestHandlerWithRenderTool:
    def _handler(self, dirs, process: Callable, validate: Any = None):
        workspace, output = dirs
        render = make_render_tool(
            workspace, output, runner=_fake_ffprobe(), process_runner=process
        )
        return VideoRenderHandler(
            render_tool=render,
            validate_tool=FakeTool(validate or validate_ok()),
        )

    def test_success_flows_through(self, store, dirs):
        _, output = dirs
        store.submit(KIND, {"plan": PLAN})
        done = run_video(store, self._handler(dirs, FakeProcess()))
        assert done.status is JobStatus.SUCCEEDED
        assert done.result["output"] == "reel_v001.mp4"
        assert "reel_v001.mp4" in _published(output)

    def test_ffmpeg_error_is_retried_and_publishes_nothing(self, store, dirs):
        _, output = dirs
        store.submit(KIND, {"plan": PLAN})
        done = run_video(store, self._handler(dirs, FakeProcess(1)))
        assert done.status is JobStatus.QUEUED
        assert done.error_code == "render_ffmpeg_failed"
        assert _published(output) == []
        assert _temp_left(output) == []

    def test_invalid_plan_is_permanent(self, store, dirs):
        _, output = dirs
        store.submit(KIND, {"plan": {**PLAN, "clips": [{"source": "../x.mp4"}]}})
        process = FakeProcess()
        done = run_video(store, self._handler(dirs, process))
        assert done.status is JobStatus.FAILED
        assert done.error_code == "render_invalid_plan"
        assert process.calls == []
        assert _published(output) == []

    def test_cancel_while_ffmpeg_runs_publishes_nothing(self, store, dirs):
        _, output = dirs
        job = store.submit(KIND, {"plan": PLAN})

        def stopped(cmd, cwd):
            # What the Step 3 worker's process control will do.
            store.cancel(job.id)
            (cwd / "render.mp4").write_bytes(b"half a file")
            raise JobCancelled()

        done = run_video(store, self._handler(dirs, FakeProcess(stopped)))
        assert done.status is JobStatus.CANCELLED
        assert _published(output) == []
        assert _temp_left(output) == []

    def test_existing_outputs_are_not_overwritten(self, store, dirs):
        _, output = dirs
        (output / "reel_v001.mp4").write_bytes(b"earlier render")
        store.submit(KIND, {"plan": PLAN})
        done = run_video(
            store, self._handler(dirs, FakeProcess(), validate_ok("reel_v002.mp4"))
        )
        assert done.result["output"] == "reel_v002.mp4"
        assert (output / "reel_v001.mp4").read_bytes() == b"earlier render"


# -- real FFmpeg ----------------------------------------------------------------


@needs_ffmpeg
class TestRealFfmpegRunner:
    @pytest.fixture
    def real(self, tmp_path):
        workspace = tmp_path / "workspace"
        output = tmp_path / "renders"
        assets = tmp_path / "assets"
        output.mkdir()
        make_test_media(workspace, assets)
        return workspace, output

    def _handler(self, real, process_runner=None):
        workspace, output = real
        render = make_render_tool(
            workspace, output, runner=subprocess.run, process_runner=process_runner
        )
        render._which = shutil.which
        validate = make_validate_tool(workspace, output, runner=subprocess.run)
        validate._which = shutil.which
        return VideoRenderHandler(render_tool=render, validate_tool=validate)

    PLAN = {
        "output_name": "job_reel",
        "clips": [
            {"source": "Shoot/landscape.mp4", "start": 0.5, "end": 2.0},
            {"source": "Shoot/portrait silent.mov", "start": 0, "end": 1.0},
        ],
        "video": {"x264_preset": "ultrafast"},
    }

    def test_job_renders_and_validates(self, store, real):
        workspace, output = real
        before = digest_tree(workspace)
        first = store.submit(KIND, {"plan": self.PLAN})
        done = run_video(store, self._handler(real))
        assert done.id == first.id
        assert done.status is JobStatus.SUCCEEDED, done.to_dict()
        assert done.result["output"] == "job_reel_v001.mp4"
        assert done.result["ready_for_review"] is True
        assert done.result["validate"]["status"] == "passed"
        for rel in (
            "job_reel_v001.mp4",
            done.result["validate"]["preview"],
            done.result["validate"]["thumbnail"],
        ):
            assert (output / rel).is_file()
        probe = json.loads(
            subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_streams", str(output / "job_reel_v001.mp4")],
                capture_output=True, check=True,
            ).stdout
        )  # fmt: skip
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        assert (video["width"], video["height"]) == (1080, 1920)

        # A second job for the same plan gets a new version; v001 is untouched.
        v1 = (output / "job_reel_v001.mp4").read_bytes()
        store.submit(KIND, {"plan": self.PLAN})
        again = run_video(store, self._handler(real))
        assert again.result["output"] == "job_reel_v002.mp4"
        assert (output / "job_reel_v001.mp4").read_bytes() == v1
        assert digest_tree(workspace) == before
        assert _temp_left(output) == []

    def test_cancel_stops_real_ffmpeg_and_publishes_nothing(self, store, real):
        workspace, output = real
        before = digest_tree(workspace)
        job = store.submit(
            KIND, {"plan": {**self.PLAN, "video": {"x264_preset": "veryslow"}}}
        )

        def cancel_soon(cmd, *, cwd, timeout, log_path, env):
            # Start the real FFmpeg, stop it almost at once the way Step 3
            # will (run_process kills the process group), then report it.
            store.cancel(job.id)
            try:
                return run_process(
                    cmd, cwd=cwd, timeout=0.05, log_path=log_path, env=env
                )
            except subprocess.TimeoutExpired:
                raise JobCancelled() from None

        done = run_video(store, self._handler(real, process_runner=cancel_soon))
        assert done.status is JobStatus.CANCELLED, done.to_dict()
        assert not list(output.glob("*.mp4"))
        assert not list(output.glob("*.render.json"))
        assert _temp_left(output) == []
        assert digest_tree(workspace) == before
