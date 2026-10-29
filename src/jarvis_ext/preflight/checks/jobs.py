"""Group C: the job manager, in an isolated temporary database.

Nothing here reads or writes the real ``jobs.db`` or processes a real user
project.  Jobs of a fake kind exercise the lifecycle rules; one job runs the
real ``video.render`` handler on the synthetic fixture.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from jarvis_ext.jobs import InvalidTransitionError, JobStatus, JobStore
from jarvis_ext.jobs.models import MAX_PAYLOAD_BYTES, InvalidJobError, JobNotFoundError
from jarvis_ext.jobs.runner import JobContext, JobOutcome, run_next
from jarvis_ext.preflight.checks.media import ensure_fixture
from jarvis_ext.preflight.core import (
    Blocked,
    Context,
    Outcome,
    check,
    failed,
    passed,
)
from jarvis_ext.preflight.fixtures import PLAN, digest_tree
from openjarvis.core.types import ToolResult

WORKER = "preflight-worker"
OTHER_WORKER = "preflight-other-worker"
FAKE = "preflight.fake"


class _Fixed:
    """A handler that returns a fixed value, or calls a function."""

    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome

    def run(self, context: JobContext) -> Any:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        if callable(self.outcome):
            return self.outcome(context)
        return self.outcome


def _store(ctx: Context, name: str, **kwargs: Any) -> JobStore:
    folder = ctx.workdir / "jobs"
    folder.mkdir(exist_ok=True)
    return JobStore(folder / f"{name}.db", **kwargs)


def _run(store: JobStore, handler: Any, kind: str = FAKE):
    return run_next(store, WORKER, {kind: handler}, bus=None)


def _expect_raises(errors: Any, fn: Callable[[], Any], what: str) -> Optional[str]:
    """Return a problem string unless *fn* raises one of *errors*."""
    try:
        fn()
    except errors:
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{what}: raised {type(exc).__name__}, expected a job error"
    return f"{what}: was accepted"


@check("jobs.create_retrieve", "jobs", "Create and retrieve a job", budget=3)
def create_and_retrieve(ctx: Context) -> Outcome:
    with _store(ctx, "create") as store:
        job = store.submit(FAKE, {"hello": "world"})
        got = store.get(job.id)
        listed = [j.id for j in store.list_jobs(status=JobStatus.QUEUED)]
        events = store.events(job.id)
        prefix = store.find(job.id[:8]).id
        problems = []
        if got.status is not JobStatus.QUEUED or got.attempts != 0:
            problems.append(f"new job is {got.status.value}, attempts {got.attempts}")
        if got.kind != FAKE or got.payload != {"hello": "world"}:
            problems.append("kind/payload did not round-trip")
        if job.id not in listed:
            problems.append("job missing from list_jobs(queued)")
        if prefix != job.id:
            problems.append("find() by id prefix failed")
        if [e.to_status for e in events] != [JobStatus.QUEUED]:
            problems.append("history does not start with 'queued'")
        mode = store.journal_mode
        if problems:
            return failed("; ".join(problems))
    return passed(f"created, read back, listed, found by prefix; SQLite journal={mode}")


@check(
    "jobs.validation", "jobs", "Invalid kind, payload and limits are refused", budget=3
)
def validation(ctx: Context) -> Outcome:
    from jarvis_ext.video.render_job import KIND, read_payload

    with _store(ctx, "validation") as store:
        problems = [
            _expect_raises(
                InvalidJobError, lambda: store.submit("Bad Kind!", {}), "bad kind"
            ),
            _expect_raises(InvalidJobError, lambda: store.submit("", {}), "empty kind"),
            _expect_raises(
                InvalidJobError, lambda: store.submit(FAKE, [1, 2]), "list payload"
            ),  # type: ignore[arg-type]
            _expect_raises(
                InvalidJobError,
                lambda: store.submit(FAKE, {"x": "y" * (MAX_PAYLOAD_BYTES + 1)}),
                "oversize payload",
            ),
            _expect_raises(
                InvalidJobError,
                lambda: store.submit(FAKE, {"x": float("nan")}),
                "non-JSON payload",
            ),
            _expect_raises(
                InvalidJobError,
                lambda: store.submit(FAKE, {}, max_attempts=99),
                "max_attempts 99",
            ),
            _expect_raises(
                JobNotFoundError,
                lambda: store.submit(FAKE, {}, parent_id="0" * 32),
                "unknown parent",
            ),
        ]
        stored = len(store.list_jobs())
    problems = [p for p in problems if p]
    if stored:
        problems.append(f"{stored} job(s) were stored despite being invalid")
    # The video.render payload rules.
    for label, payload in (
        ("no plan", {}),
        ("empty plan", {"plan": {}}),
        ("unknown key", {"plan": {"a": 1}, "bogus": 1}),
        ("non-bool reuse", {"plan": {"a": 1}, "reuse": "yes"}),
    ):
        plan, problem = read_payload(payload)
        if plan is not None or not problem:
            problems.append(f"video.render payload '{label}' was accepted")
    plan, problem = read_payload({"plan": PLAN})
    if plan is None:
        problems.append(f"a valid {KIND} payload was refused: {problem}")
    if problems:
        return failed("; ".join(problems))
    return passed(
        "7 invalid submissions and 4 invalid video.render payloads refused; none stored"
    )


@check("jobs.transitions", "jobs", "State transitions and ownership rules", budget=3)
def transitions(ctx: Context) -> Outcome:
    problems: List[str] = []
    with _store(ctx, "transitions") as store:
        job = store.submit(FAKE, {})
        # queued -> succeeded is not allowed; only a claimed job can finish.
        err = _expect_raises(
            InvalidTransitionError,
            lambda: store.complete(job.id, WORKER),
            "complete while queued",
        )
        problems.append(err)
        running = store.claim_next(WORKER, kinds=[FAKE])
        if (
            running is None
            or running.status is not JobStatus.RUNNING
            or running.attempts != 1
        ):
            return failed("claim_next did not move the job to running (attempts=1)")
        if store.claim_next(OTHER_WORKER, kinds=[FAKE]) is not None:
            problems.append("a running job was claimed a second time")
        problems.append(
            _expect_raises(
                (InvalidTransitionError, InvalidJobError),
                lambda: store.complete(job.id, OTHER_WORKER),
                "complete by a worker that does not own the job",
            )
        )
        done = store.complete(job.id, WORKER, {"answer": 42})
        if done.status is not JobStatus.SUCCEEDED or done.result != {"answer": 42}:
            problems.append("complete() did not record success and its result")
        if done.progress != 100.0:
            problems.append(f"success progress is {done.progress}, expected 100")
        for label, fn in (
            ("fail after success", lambda: store.fail(job.id, WORKER, "late")),
            ("complete twice", lambda: store.complete(job.id, WORKER)),
            ("cancel a finished job", lambda: store.cancel(job.id)),
            ("retry after success", lambda: store.retry(job.id, WORKER, "late")),
        ):
            problems.append(_expect_raises(InvalidTransitionError, fn, label))
        history = [e.to_status.value for e in store.events(job.id)]
        if history != ["queued", "running", "succeeded"]:
            problems.append(f"history is {history}")
    problems = [p for p in problems if p]
    if problems:
        return failed("; ".join(problems))
    return passed(
        "queued->running->succeeded recorded; illegal moves and non-owners refused"
    )


@check("jobs.failure_recorded", "jobs", "Failures are recorded as failures", budget=3)
def failure_recorded(ctx: Context) -> Outcome:
    cases = [
        (
            "handler reports failure",
            JobOutcome.failed("bad_input", "nope"),
            "bad_input",
        ),
        ("handler raises", RuntimeError("boom"), "runner_error"),
        ("handler returns a non-outcome", "all good", "unexpected_result"),
        (
            "success with an unstorable result",
            JobOutcome.succeeded({"bad": float("nan")}),
            "invalid_result",
        ),
    ]
    problems: List[str] = []
    with _store(ctx, "failures") as store:
        for label, outcome, code in cases:
            store.submit(FAKE, {})
            done = _run(store, _Fixed(outcome))
            if done is None or done.status is not JobStatus.FAILED:
                problems.append(
                    f"{label}: ended {done.status.value if done else 'unclaimed'}, "
                    "expected failed"
                )
            elif done.error_code != code:
                problems.append(
                    f"{label}: error_code {done.error_code!r}, expected {code!r}"
                )
    if problems:
        return failed("; ".join(problems))
    return passed(
        f"{len(cases)} kinds of bad outcome all ended 'failed' with the right "
        "error code"
    )


@check("jobs.retry", "jobs", "Retry runs once more, then stops", budget=3)
def retry_behaviour(ctx: Context) -> Outcome:
    problems: List[str] = []
    retry = _Fixed(JobOutcome.retry("flaky", "try again"))
    with _store(ctx, "retry") as store:
        job = store.submit(FAKE, {})
        first = _run(store, retry)
        if first is None or first.status is not JobStatus.QUEUED or first.attempts != 1:
            problems.append("first temporary failure was not re-queued")
        second = _run(store, retry)
        if (
            second is None
            or second.id != job.id
            or second.status is not JobStatus.FAILED
        ):
            problems.append("the attempt limit did not end in 'failed'")
        elif "not retried" not in second.error_message:
            problems.append("final failure does not say it was not retried")
        if _run(store, retry) is not None:
            problems.append("a finished job was claimed again")
        store.submit(FAKE, {}, max_attempts=1)
        single = _run(store, retry)
        if single is None or single.status is not JobStatus.FAILED:
            problems.append("max_attempts=1 job was retried")
    if problems:
        return failed("; ".join(problems))
    return passed("temporary failure re-queued once, then failed at the attempt limit")


@check("jobs.cancel", "jobs", "Cancellation, and the real outcome wins", budget=3)
def cancellation(ctx: Context) -> Outcome:
    problems: List[str] = []
    with _store(ctx, "cancel") as store:
        queued = store.submit(FAKE, {})
        gone = store.cancel(queued.id)
        if gone.status is not JobStatus.CANCELLED:
            problems.append("a queued job was not cancelled at once")
        if store.claim_next(WORKER, kinds=[FAKE]) is not None:
            problems.append("a cancelled job was claimed")

        def cancel_then_stop(context: JobContext) -> JobOutcome:
            context.store.cancel(context.job.id)
            if not context.cancel_requested():
                return JobOutcome.failed("cancel_not_seen", "cancel request invisible")
            return JobOutcome.cancelled({"stage": "preflight"})

        store.submit(FAKE, {})
        stopped = _run(store, _Fixed(cancel_then_stop))
        if stopped is None or stopped.status is not JobStatus.CANCELLED:
            problems.append(
                "a running job that honoured cancellation did not end 'cancelled' "
                f"(got {stopped.status.value if stopped else 'unclaimed'})"
            )

        def cancel_but_finish(context: JobContext) -> JobOutcome:
            context.store.cancel(context.job.id)
            return JobOutcome.succeeded({"finished": True})

        store.submit(FAKE, {})
        real = _run(store, _Fixed(cancel_but_finish))
        if (
            real is None
            or real.status is not JobStatus.SUCCEEDED
            or not real.cancel_requested
        ):
            problems.append(
                "work that finished despite a cancel request was not kept as succeeded"
            )

        store.submit(FAKE, {})
        unasked = _run(store, _Fixed(JobOutcome.cancelled({})))
        if unasked is None or unasked.status is not JobStatus.FAILED:
            problems.append(
                "an unrequested 'cancelled' outcome was recorded as cancelled"
            )
    if problems:
        return failed("; ".join(problems))
    return passed(
        "queued cancel immediate; running cancel honoured; finished work stays "
        "succeeded; unrequested cancel -> failed"
    )


def _tool(report: Dict[str, Any], success: bool) -> Any:
    class Tool:
        def execute(self, **params: Any) -> ToolResult:
            return ToolResult(
                tool_name="fake", content=json.dumps(report), success=success
            )

    return Tool()


@check(
    "jobs.no_false_success",
    "jobs",
    "A failed operation never reports success",
    budget=15,
)
def no_false_success(ctx: Context) -> Outcome:
    from jarvis_ext.video.render_job import KIND, VideoRenderHandler

    ok_validate = _tool(
        {
            "validation_status": "passed",
            "ready_for_review": True,
            "output": "x_v001.mp4",
        },
        True,
    )
    bad_render = _tool(
        {"render_status": "invalid_plan", "error": "preflight says no"}, False
    )
    inconsistent = _tool({"render_status": "succeeded"}, True)  # no output file
    bad_validate = _tool(
        {
            "validation_status": "failed",
            "ready_for_review": False,
            "output": "x_v001.mp4",
        },
        False,
    )
    good_render = _tool(
        {
            "render_status": "succeeded",
            "output": {
                "file": "x_v001.mp4",
                "version": 1,
                "size_bytes": 10,
                "sha256": "ab",
            },
            "record": "x_v001.render.json",
        },
        True,
    )
    cases = [
        (
            "render reports failure",
            VideoRenderHandler(render_tool=bad_render, validate_tool=ok_validate),
        ),
        (
            "render claims success with no file",
            VideoRenderHandler(render_tool=inconsistent, validate_tool=ok_validate),
        ),
        (
            "render ok but validation fails",
            VideoRenderHandler(render_tool=good_render, validate_tool=bad_validate),
        ),
    ]
    problems: List[str] = []
    with _store(ctx, "nofalse") as store:
        for label, handler in cases:
            store.submit(KIND, {"plan": PLAN, "reuse": False})
            done = run_next(store, WORKER, {KIND: handler}, bus=None)
            if done is None or done.status is JobStatus.SUCCEEDED:
                problems.append(f"{label}: job was reported SUCCEEDED")
            elif (done.result or {}).get("ready_for_review") is True:
                problems.append(f"{label}: result claims ready_for_review")
        store.submit(KIND, {"plan": {}})
        empty = run_next(
            store,
            WORKER,
            {
                KIND: VideoRenderHandler(
                    render_tool=good_render, validate_tool=ok_validate
                )
            },
            bus=None,
        )
        if (
            empty is None
            or empty.status is not JobStatus.FAILED
            or empty.error_code != "invalid_payload"
        ):
            problems.append("an invalid payload did not fail with invalid_payload")
        # The real tools, a plan whose source file does not exist.
        try:
            have_fixture = bool(ensure_fixture(ctx))
        except Blocked:
            have_fixture = False
        if have_fixture:
            missing = {
                **PLAN,
                "clips": [{"source": "Shoot/does-not-exist.mp4", "start": 0, "end": 1}],
            }
            render, validate = _production_tools(ctx)
            store.submit(KIND, {"plan": missing, "reuse": False})
            real = run_next(
                store,
                WORKER,
                {KIND: VideoRenderHandler(render_tool=render, validate_tool=validate)},
                bus=None,
            )
            if real is None or real.status is JobStatus.SUCCEEDED:
                problems.append(
                    "real tools: a plan with a missing source was reported SUCCEEDED"
                )
            elif real.status is not JobStatus.FAILED:
                problems.append(f"real tools: missing source ended {real.status.value}")
    if problems:
        return failed("; ".join(problems))
    extra = (
        "; real tools refused a missing source"
        if have_fixture
        else (" (real-tool case not run: no fixture)")
    )
    return passed(
        "3 fake-tool failure modes and an invalid payload all ended 'failed'" + extra
    )


def _production_tools(ctx: Context):
    """The tools exactly as the job worker wires them, on the fixture."""
    from jarvis_ext.tools.media_render import MediaRenderTool
    from jarvis_ext.tools.media_validate import MediaValidateTool
    from jarvis_ext.tools.media_workspace import MediaWorkspaceSettings
    from jarvis_ext.video.render_job import stoppable_run_process
    from jarvis_ext.video.settings import MediaRenderSettings

    fixture = ctx.shared["fixture"]
    workspace = MediaWorkspaceSettings.from_mapping(
        {"root": str(fixture.workspace), "ffprobe_path": ctx.shared["ffprobe"]}
    )
    settings = MediaRenderSettings.from_mapping(
        {
            "output_root": str(fixture.output),
            "assets_root": str(fixture.assets),
            "ffmpeg_path": ctx.shared["ffmpeg"],
            "ffprobe_path": ctx.shared["ffprobe"],
        }
    )
    base = {"settings": settings, "workspace_settings": workspace}
    return (
        MediaRenderTool(
            process_runner=stoppable_run_process,
            report_progress=True,
            record_provenance=True,
            **base,
        ),
        MediaValidateTool(process_runner=stoppable_run_process, **base),
    )


@check(
    "jobs.real_pipeline", "jobs", "video.render job through the real tools", budget=60
)
def real_pipeline(ctx: Context) -> Outcome:
    from jarvis_ext.video.render_job import KIND, VideoRenderHandler

    fixture = ensure_fixture(ctx)
    render, validate = _production_tools(ctx)
    handler = VideoRenderHandler(render_tool=render, validate_tool=validate)
    before = digest_tree(fixture.workspace)
    with _store(ctx, "real") as store:
        # A distinct output name, so this job is independent of media.render.
        plan = {**PLAN, "output_name": "preflight_job"}
        store.submit(KIND, {"plan": plan, "reuse": False})
        done = run_next(store, WORKER, {KIND: handler}, bus=None)
        if done is None:
            return failed("the job was not claimed")
        if done.status is not JobStatus.SUCCEEDED:
            reason = f"{done.error_code} {done.error_message}"
            return failed(f"job ended {done.status.value}: {reason}"[:300])
        result = done.result or {}
        validate_info = result.get("validate") or {}
        if (
            result.get("ready_for_review") is not True
            or validate_info.get("status") != "passed"
        ):
            return failed("job SUCCEEDED but is not validated/ready_for_review")
        output = fixture.output / str(result.get("output"))
        if not output.is_file() or output.stat().st_size == 0:
            return failed("job SUCCEEDED but its output file is missing")
        if done.progress != 100.0:
            return failed(f"job SUCCEEDED with progress {done.progress}")
    if digest_tree(fixture.workspace) != before:
        return failed("the job changed the fixture's source files")
    return passed(
        f"{result['output']} rendered + validated through a job; sources unchanged"
    )
