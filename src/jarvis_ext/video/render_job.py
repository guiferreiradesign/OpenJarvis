"""Job handler for ``video.render``: render a plan, then validate the result.

The handler adds no rendering or checking of its own.  It calls the Phase 2
tools exactly as an agent or the CLI would (``media_render``, then
``media_validate``) and translates their reports into a job outcome.

Payload::

    {"plan": { ...an EditPlan object, see video-pipeline.md... }}

Outcomes (DECISIONS.md D23, D27, D28):

* **succeeded** only when the render succeeded *and* validation passed with
  ``ready_for_review: true`` for that same file (preview and thumbnail made).
* **retry** for render failures that may pass on a second try
  (:data:`RENDER_RETRYABLE`).  The store still enforces the attempt limit.
* **failed** for everything else: permanent render failures (including
  ``timeout`` and ``failed_checks``), every validation failure, and any
  report the handler cannot interpret.  Validation failures are never
  retried, because a retry would render a second version.
* **cancelled** when a cancellation was requested before rendering, or
  after rendering but before validation.  A render published before the
  cancellation is listed in the result as *not validated*; it is never
  reported as a finished, validated output.

Progress (D30): stage ``render`` goes from 0 to :data:`RENDER_SHARE` (95 %)
following FFmpeg's encoded time against the plan's total duration; stage
``validate`` holds at 95 % (validation reports no measurable progress); the
store records 100 % only when the job succeeds.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from jarvis_ext.jobs.models import Job, JobEvent, JobStatus
from jarvis_ext.jobs.runner import (
    STOP_CANCEL,
    STOP_SHUTDOWN,
    JobCancelled,
    JobContext,
    JobOutcome,
    WorkerInterrupted,
    current_stop_reason,
)
from jarvis_ext.video.ffmpeg import (
    PROGRESS_FILENAME,
    ProcessStopped,
    ProgressReader,
    run_process_until,
)
from jarvis_ext.video.output import TMP_DIRNAME, OutputFolder
from jarvis_ext.video.plan import PlanError, parse_timestamp
from openjarvis.core.types import ToolResult

KIND = "video.render"
RENDER_SHARE = 95.0
STAGE_RENDER = "render"
STAGE_VALIDATE = "validate"
RENDER_STARTED_NOTE = "render started"
RENDERED_NOTE_PREFIX = "rendered "
_TEMP_PREFIX = "job-"  # OutputFolder.make_temp_dir's mkdtemp prefix

logger = logging.getLogger(__name__)
# The running render's progress callback (set by the handler, read by
# stoppable_run_process in the same thread).
_render_progress = threading.local()

# Render statuses worth one more attempt: a folder that was briefly
# unavailable (drive unplugged, network volume), FFmpeg exiting with an
# error, or a filesystem error while publishing.  Everything else is either
# deterministic (bad plan, missing FFmpeg, failed checks) or excluded by
# D23 (timeout).
RENDER_RETRYABLE = frozenset(
    {"workspace_unavailable", "output_unavailable", "ffmpeg_failed", "output_error"}
)

RENDER_PERMANENT = frozenset(
    {
        "config_error",
        "not_configured",
        "invalid_plan",
        "access_denied",
        "invalid_media",
        "ffmpeg_missing",
        "ffmpeg_unusable",
        "unsupported",
        "timeout",
        "failed_checks",
    }
)

_MAX_WARNINGS = 50
_STATUS_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_MAX_WARNING_CHARS = 500


class VideoRenderHandler:
    """Runs ``video.render`` jobs through the existing render and validate tools."""

    kind = KIND

    def __init__(
        self,
        *,
        render_tool: Any = None,
        validate_tool: Any = None,
        config_path: Optional[Path] = None,
    ) -> None:
        self._config_path = config_path
        if render_tool is None or validate_tool is None:
            from jarvis_ext.tools.media_render import MediaRenderTool
            from jarvis_ext.tools.media_validate import MediaValidateTool

            # Same tools as the CLI and agents use, with FFmpeg run through
            # stoppable_run_process so cancellation and shutdown can stop it.
            render_tool = render_tool or MediaRenderTool(
                config_path=config_path,
                process_runner=stoppable_run_process,
                report_progress=True,
            )
            validate_tool = validate_tool or MediaValidateTool(
                config_path=config_path, process_runner=stoppable_run_process
            )
        self._render = render_tool
        self._validate = validate_tool

    def run(self, context: JobContext) -> JobOutcome:
        plan, problem = read_payload(context.job.payload)
        if plan is None:
            return JobOutcome.failed("invalid_payload", problem)

        stop = context.stop_reason(fresh=True)
        if stop == STOP_CANCEL:
            return JobOutcome.cancelled(
                {"stage": "before_render", "output": None, "ready_for_review": False}
            )
        if stop == STOP_SHUTDOWN:
            return JobOutcome.retry(
                "interrupted", "The worker stopped before rendering started"
            )

        context.note(RENDER_STARTED_NOTE)
        context.progress(STAGE_RENDER, 0.0)
        expected = expected_duration(plan)

        def on_encoded(seconds: float) -> None:
            if expected:
                share = min(seconds / expected, 1.0) * RENDER_SHARE
                context.progress(STAGE_RENDER, share)

        _render_progress.callback = on_encoded
        try:
            render = _report(self._render.execute(plan=plan))
        except JobCancelled:
            # FFmpeg was stopped inside the render tool, which then removed
            # its temp folder: nothing was published.
            return JobOutcome.cancelled(
                {"stage": "during_render", "output": None, "ready_for_review": False}
            )
        except WorkerInterrupted:
            return JobOutcome.retry(
                "interrupted",
                "The worker was stopped during the render; nothing was published",
                {"stage": "during_render", "output": None, "ready_for_review": False},
            )
        finally:
            _render_progress.callback = None
        render_status = render.get("render_status")
        output = render.get("output") if isinstance(render.get("output"), dict) else {}
        file_name = output.get("file")
        render_ok = (
            render.get("_success") is True
            and render_status == "succeeded"
            and _plain_mp4_name(file_name)
        )
        result: Dict[str, Any] = {
            "output": file_name if render_ok else None,
            "ready_for_review": False,
            "render": _render_summary(render),
            "validate": None,
        }
        if not render_ok:
            message = _message(render, "The render did not succeed")
            if render.get("_success") is True or render_status == "succeeded":
                # Claimed success without a usable file name, or the
                # reverse: never treated as a render.
                return JobOutcome.failed(
                    "unexpected_result",
                    "media_render returned an inconsistent report",
                    result,
                )
            code = _code("render", render_status)
            if render_status in RENDER_RETRYABLE:
                return JobOutcome.retry(code, message, result)
            return JobOutcome.failed(code, message, result)

        context.note(f"{RENDERED_NOTE_PREFIX}{file_name}")
        context.progress(STAGE_VALIDATE, RENDER_SHARE)
        unvalidated = (
            f"{file_name} was published but not validated; it is not ready for review."
        )
        stop = context.stop_reason(fresh=True)
        if stop is not None:
            result["stage"] = "after_render"
            result["note"] = unvalidated
            if stop == STOP_CANCEL:
                return JobOutcome.cancelled(result)
            return JobOutcome.failed("validate_interrupted", unvalidated, result)

        try:
            validation = _report(self._validate.execute(output=file_name))
        except (JobCancelled, WorkerInterrupted) as exc:
            result["stage"] = "during_validation"
            result["note"] = unvalidated
            if isinstance(exc, JobCancelled):
                return JobOutcome.cancelled(result)
            # Not retried: a retry would render a second version.
            return JobOutcome.failed("validate_interrupted", unvalidated, result)
        status = validation.get("validation_status")
        result["validate"] = _validate_summary(validation)
        validated = (
            validation.get("_success") is True
            and status == "passed"
            and validation.get("ready_for_review") is True
            and validation.get("output") == file_name
            and isinstance(validation.get("preview"), str)
            and isinstance(validation.get("thumbnail"), str)
        )
        if validated:
            result["ready_for_review"] = True
            context.note(f"validated {file_name}")
            return JobOutcome.succeeded(result)
        if validation.get("_success") is True or status == "passed":
            return JobOutcome.failed(
                "unexpected_result",
                "media_validate returned an inconsistent report",
                result,
            )
        return JobOutcome.failed(
            _code("validate", status),
            _message(validation, "Validation did not pass"),
            result,
        )

    # -- worker hooks ----------------------------------------------------------

    def recovery(self, job: Job, events: List[JobEvent]) -> Tuple[bool, str, str, dict]:
        """How to settle this job after its worker died: ``(requeue, code,
        message, result)``.  A cancellation request is honoured by the store
        before this is applied.

        If the history shows a published render, the job is *not* re-run (a
        re-run would publish a second version); the file is reported as not
        validated.  Otherwise no render was recorded as published, and the
        job may be retried within its attempt limit.
        """
        rendered = [
            e.note[len(RENDERED_NOTE_PREFIX) :]
            for e in events
            if e.from_status is JobStatus.RUNNING
            and e.note.startswith(RENDERED_NOTE_PREFIX)
        ]
        if rendered:
            name = rendered[-1]
            return (
                False,
                "validate_interrupted",
                f"The worker stopped after {name} was published; it was not "
                "validated and is not ready for review.",
                {"output": name, "ready_for_review": False, "stage": "after_render"},
            )
        return (
            True,
            "interrupted",
            "The worker stopped before a render was published",
            {"output": None, "ready_for_review": False},
        )

    def cleanup(self) -> List[str]:
        """Remove abandoned temp folders in the output folder (see
        :func:`remove_abandoned_temp_dirs`).  Call only while no job runs."""
        from jarvis_ext.config import ExtensionConfigError, load_extension_section
        from jarvis_ext.video.settings import CONFIG_SECTION, MediaRenderSettings

        try:
            settings = MediaRenderSettings.from_mapping(
                load_extension_section(CONFIG_SECTION, self._config_path)
            )
        except ExtensionConfigError:
            return []
        if settings.output_root is None:
            return []
        return remove_abandoned_temp_dirs(
            settings.output_root, older_than_seconds=abandoned_after(settings)
        )


def stoppable_run_process(
    cmd: List[str],
    *,
    cwd: Path,
    timeout: float,
    log_path: Path,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """The tools' ``process_runner`` hook, stoppable by the running job.

    While a job renders with ``report_progress``, it also follows FFmpeg's
    ``-progress`` file and passes the encoded time to the handler.
    Outside a job it behaves like ``run_process``.  Inside one, a cancel
    request or a forced worker stop terminates FFmpeg's process group (the
    existing ``_stop``: SIGTERM, then SIGKILL) and raises
    :class:`JobCancelled` or :class:`WorkerInterrupted`.  The render tool's
    cleanup then removes its temp folder, so nothing is published.
    """
    on_encoded = getattr(_render_progress, "callback", None)
    reader = (
        ProgressReader(Path(cwd) / PROGRESS_FILENAME)
        if on_encoded is not None and "-progress" in cmd
        else None
    )

    def should_stop() -> Optional[str]:
        if reader is not None:
            seconds = reader.poll()
            if seconds is not None:
                try:
                    on_encoded(seconds)
                except Exception:  # progress must never break a render
                    logger.warning("Render progress callback failed", exc_info=True)
        return current_stop_reason()

    try:
        return run_process_until(
            cmd,
            cwd=cwd,
            timeout=timeout,
            log_path=log_path,
            env=env,
            should_stop=should_stop,
        )
    except ProcessStopped as exc:
        if exc.reason == STOP_CANCEL:
            raise JobCancelled() from None
        raise WorkerInterrupted() from None


def expected_duration(plan: Mapping[str, Any]) -> Optional[float]:
    """Total output duration of a raw plan (sum of clip lengths), or ``None``.

    Uses the plan module's own timestamp parser; the render tool still
    validates the plan in full.
    """
    try:
        total = sum(
            parse_timestamp(clip["end"]) - parse_timestamp(clip["start"])
            for clip in plan["clips"]
        )
    except (KeyError, TypeError, ValueError, PlanError):
        return None
    return total if total > 0 else None


def abandoned_after(settings: Any) -> float:
    """Age after which a temp folder cannot belong to a live tool call.

    A render runs one FFmpeg (stopped at ``render_timeout_seconds``); a
    validation at most three (decode, preview, thumbnail), each stopped at
    ``validate_timeout_seconds``.  Ten minutes are added for probing and
    publishing.
    """
    return (
        settings.render_timeout_seconds + 4 * settings.validate_timeout_seconds + 600.0
    )


def remove_abandoned_temp_dirs(
    output_root: Path,
    *,
    older_than_seconds: float,
    now: Optional[float] = None,
) -> List[str]:
    """Delete ``<output_root>/.jarvis-tmp/job-*`` folders nothing has touched
    for *older_than_seconds*; return their names.

    Only folders the render/validate tools create are considered: plain
    directories (never symlinks) named ``job-*`` directly inside
    ``.jarvis-tmp``.  A folder counts as abandoned only when it **and
    everything in it** are older than the limit, so a folder an orphaned
    FFmpeg is still writing to is kept.  Deletion goes through
    ``OutputFolder.remove_temp_dir``, which refuses anything else.
    Renders, records and ``review/`` files are never candidates.
    """
    current = time.time() if now is None else now
    tmp_root = output_root / TMP_DIRNAME
    try:
        info = os.lstat(tmp_root)
    except OSError:
        return []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return []
    folder = OutputFolder(output_root)
    removed = []
    for entry in sorted(tmp_root.iterdir()):
        try:
            entry_info = os.lstat(entry)
        except OSError:
            continue
        if (
            not entry.name.startswith(_TEMP_PREFIX)
            or stat.S_ISLNK(entry_info.st_mode)
            or not stat.S_ISDIR(entry_info.st_mode)
        ):
            continue
        newest = _newest_mtime(entry, entry_info.st_mtime)
        if newest is None or current - newest < older_than_seconds:
            continue
        folder.remove_temp_dir(entry)
        if not os.path.lexists(entry):
            removed.append(entry.name)
    return removed


def _newest_mtime(directory: Path, own_mtime: float) -> Optional[float]:
    newest = own_mtime
    try:
        for root, dirs, files in os.walk(directory, followlinks=False):
            for name in dirs + files:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
                except OSError:
                    return None  # changing under us: not abandoned
    except OSError:
        return None
    return newest


def read_payload(payload: Mapping[str, Any]) -> Tuple[Optional[dict], str]:
    """Return ``(plan, "")`` or ``(None, reason)``; the plan itself is checked
    by ``media_render`` before anything is written."""
    if not isinstance(payload, Mapping):
        return None, "The payload must be a JSON object"
    unknown = sorted(set(payload) - {"plan"})
    if unknown:
        return None, f"Unknown payload keys: {', '.join(unknown)}"
    plan = payload.get("plan")
    if not isinstance(plan, dict) or not plan:
        return None, "The payload needs a 'plan' object (an EditPlan)"
    return plan, ""


def _report(tool_result: Any) -> Dict[str, Any]:
    """Parse a tool's JSON report; anything unreadable becomes an empty report."""
    if not isinstance(tool_result, ToolResult):
        return {"_success": False, "error": "The tool returned no ToolResult"}
    try:
        data = json.loads(tool_result.content)
    except (TypeError, ValueError):
        return {"_success": False, "error": "The tool's report is not JSON"}
    if not isinstance(data, dict):
        return {"_success": False, "error": "The tool's report is not an object"}
    data["_success"] = tool_result.success is True
    return data


def _plain_mp4_name(name: Any) -> bool:
    return (
        isinstance(name, str)
        and name.endswith(".mp4")
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
    )


def _code(stage: str, status: Any) -> str:
    """``render_ffmpeg_failed``, ``validate_failed``; unknown → ``*_unexpected``."""
    if not isinstance(status, str):
        known = False
    elif stage == "render":
        known = status in RENDER_RETRYABLE | RENDER_PERMANENT
    else:
        known = _STATUS_RE.match(status) is not None
    return f"{stage}_{status}" if known else f"{stage}_unexpected"


def _message(report: Mapping[str, Any], default: str) -> str:
    error = report.get("error")
    return error if isinstance(error, str) and error else default


def _warnings(report: Mapping[str, Any]) -> List[str]:
    raw = report.get("warnings")
    if not isinstance(raw, list):
        return []
    return [str(w)[:_MAX_WARNING_CHARS] for w in raw[:_MAX_WARNINGS]]


def _failed_checks(report: Mapping[str, Any]) -> List[str]:
    checks = report.get("checks") or report.get("prepublish_checks") or []
    if not isinstance(checks, list):
        return []
    return [
        str(c.get("check"))
        for c in checks
        if isinstance(c, dict) and c.get("passed") is False
    ]


def _render_summary(report: Mapping[str, Any]) -> Dict[str, Any]:
    output = report.get("output")
    keep = ("file", "version", "size_bytes", "sha256", "duration_seconds",
            "width", "height", "frame_rate", "has_audio")  # fmt: skip
    return {
        "status": report.get("render_status"),
        "output": (
            {k: output.get(k) for k in keep} if isinstance(output, dict) else None
        ),
        "record": report.get("record"),
        "ffmpeg_version": report.get("ffmpeg_version"),
        "elapsed_seconds": report.get("elapsed_seconds"),
        "failed_checks": _failed_checks(report),
        "warnings": _warnings(report),
    }


def _validate_summary(report: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "status": report.get("validation_status"),
        "ready_for_review": report.get("ready_for_review") is True,
        "preview": report.get("preview"),
        "thumbnail": report.get("thumbnail"),
        "failed_checks": _failed_checks(report),
        "warnings": _warnings(report),
    }


__all__ = [
    "KIND",
    "RENDER_SHARE",
    "expected_duration",
    "abandoned_after",
    "remove_abandoned_temp_dirs",
    "stoppable_run_process",
    "RENDER_PERMANENT",
    "RENDER_RETRYABLE",
    "VideoRenderHandler",
    "read_payload",
]
