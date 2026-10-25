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
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from jarvis_ext.jobs.runner import JobCancelled, JobContext, JobOutcome
from openjarvis.core.types import ToolResult

KIND = "video.render"

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
        if render_tool is None or validate_tool is None:
            from jarvis_ext.tools.media_render import MediaRenderTool
            from jarvis_ext.tools.media_validate import MediaValidateTool

            render_tool = render_tool or MediaRenderTool(config_path=config_path)
            validate_tool = validate_tool or MediaValidateTool(config_path=config_path)
        self._render = render_tool
        self._validate = validate_tool

    def run(self, context: JobContext) -> JobOutcome:
        plan, problem = read_payload(context.job.payload)
        if plan is None:
            return JobOutcome.failed("invalid_payload", problem)

        if context.cancel_requested():
            return JobOutcome.cancelled(
                {"stage": "before_render", "output": None, "ready_for_review": False}
            )

        # A JobCancelled raised during the render propagates: the render tool
        # has then stopped FFmpeg and removed its temp folder, so nothing was
        # published and the runner records the cancellation.
        render = _report(self._render.execute(plan=plan))
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

        context.note(f"rendered {file_name}")
        if context.cancel_requested():
            result["stage"] = "after_render"
            result["note"] = (
                f"{file_name} was published but not validated; it is not ready "
                "for review."
            )
            return JobOutcome.cancelled(result)

        try:
            validation = _report(self._validate.execute(output=file_name))
        except JobCancelled:
            result["stage"] = "during_validation"
            result["note"] = (
                f"{file_name} was published but not validated; it is not ready "
                "for review."
            )
            return JobOutcome.cancelled(result)
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
    "RENDER_PERMANENT",
    "RENDER_RETRYABLE",
    "VideoRenderHandler",
    "read_payload",
]
