"""Agent tools for render jobs: ``media_job_submit``, ``media_job_status``,
``media_job_revise``.

They are thin front ends to the Phase 3 job system, the same code paths as
the ``jobs`` CLI: plans are checked by ``render_job.make_payload`` /
``revision_payload`` and queued in the job store; the **worker** renders,
validates, reuses, resumes and notifies.  Nothing here renders, touches the
media workspace or the output folder, or changes a job's status.

Permissions (DECISIONS.md D33):

* ``media_job_submit`` / ``media_job_revise``: ``file:read``,
  ``file:write`` and ``schedule:create``.  The queued job will read the
  workspace and write a render, so the agent needs the same file
  capabilities as ``media_render`` (queueing must not launder them), plus
  ``schedule:create`` because the work runs later.  They also honour
  ``[jarvis.media_render] require_confirmation``.
* ``media_job_status``: ``file:read`` only (it reads the job database).

Results are JSON in ``ToolResult.content``.  They never contain absolute
paths, the plan, payloads or full error messages unfiltered: error messages
and history notes are passed through ``notify.clean_text`` (paths removed,
length bounded).  Config problems are reported at call time (D3).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from jarvis_ext.config import ExtensionConfigError, load_extension_section
from jarvis_ext.jobs.models import (
    AmbiguousJobError,
    Job,
    JobError,
    JobNotFoundError,
)
from jarvis_ext.jobs.settings import JobSettings
from jarvis_ext.notify import clean_text
from jarvis_ext.video.settings import CONFIG_SECTION as RENDER_SECTION
from jarvis_ext.video.settings import MediaRenderSettings
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

logger = logging.getLogger(__name__)

SUBMIT_TOOL = "media_job_submit"
STATUS_TOOL = "media_job_status"
REVISE_TOOL = "media_job_revise"

MAX_PLAN_JSON_BYTES = 256 * 1024
MAX_MESSAGE_CHARS = 500
MAX_HISTORY = 12
MAX_WARNINGS = 5
RECENT_JOBS = 10

QUEUE_CAPABILITIES = ["file:read", "file:write", "schedule:create"]
STATUS_CAPABILITIES = ["file:read"]

_NEXT_STEP = {
    "queued": "Waiting for the job worker ('python -m jarvis_ext jobs worker'). "
    "Check again with media_job_status.",
    "running": "The worker is running it. Check again with media_job_status.",
    "succeeded": "Ready: present the output and its preview to the user.",
    "failed": "Explain the error to the user. To try a changed plan, use "
    "media_job_revise with a complete plan.",
    "cancelled": "Cancelled. Use media_job_revise to queue a new version.",
}

_PLAN_PROPERTY = {
    "type": "object",
    "description": (
        "A complete edit plan, exactly as for media_render: output_name, "
        "clips [{source, start, end}] and optional preset, fit, video, audio, "
        "subtitles, branding. Paths are relative to the media workspace."
    ),
}


class _Refused(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class _JobToolBase(BaseTool):
    """Config loading (at construction, reported at call time) and results."""

    is_local = True
    tool_id = ""

    def __init__(
        self,
        *,
        config_path: Optional[Path] = None,
        job_settings: Optional[JobSettings] = None,
    ) -> None:
        self._config_path = config_path
        self._config_error = ""
        self._render_settings = MediaRenderSettings()
        try:
            if job_settings is None:
                job_settings = JobSettings.load(config_path)
            self._render_settings = MediaRenderSettings.from_mapping(
                load_extension_section(RENDER_SECTION, config_path)
            )
        except ExtensionConfigError as exc:
            self._config_error = str(exc)
        self._job_settings = job_settings or JobSettings()

    # -- helpers ------------------------------------------------------------

    def _open_store(self):
        if self._config_error:
            raise _Refused("config_error", f"Configuration error: {self._config_error}")
        from jarvis_ext.jobs.store import open_job_store

        return open_job_store(self._job_settings)

    def _ok(self, payload: Dict[str, Any]) -> ToolResult:
        return self._result({"status": "ok", **payload}, True)

    def _failure(self, status: str, message: str) -> ToolResult:
        return self._result(
            {"status": status, "error": clean_text(message, MAX_MESSAGE_CHARS)},
            False,
        )

    def _result(self, payload: Dict[str, Any], success: bool) -> ToolResult:
        return ToolResult(
            tool_name=self.tool_id,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            success=success,
            metadata={"status": payload.get("status")},
        )

    def _run(self, action) -> ToolResult:
        try:
            return self._ok(action())
        except _Refused as exc:
            return self._failure(exc.status, exc.message)
        except AmbiguousJobError as exc:
            return self._failure("ambiguous_job", str(exc))
        except JobNotFoundError as exc:
            return self._failure("not_found", str(exc))
        except ExtensionConfigError as exc:
            return self._failure("config_error", f"Configuration error: {exc}")
        except JobError as exc:
            # Store errors are path-free by design (D3); cleaned again anyway.
            return self._failure("job_store_error", str(exc))

    @staticmethod
    def _job_id(params: Mapping[str, Any], *, required: bool = True) -> Optional[str]:
        value = params.get("job_id")
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value.strip():
            raise _Refused(
                "invalid_arguments",
                "job_id must be a job id (or its first 6+ characters)",
            )
        return value.strip()

    @staticmethod
    def _plan(params: Mapping[str, Any]) -> Any:
        plan = params.get("plan")
        if isinstance(plan, str):
            if len(plan.encode("utf-8")) > MAX_PLAN_JSON_BYTES:
                raise _Refused("invalid_plan", "Plan is too large")
            try:
                plan = json.loads(plan)
            except ValueError as exc:
                raise _Refused(
                    "invalid_plan", f"Plan is not valid JSON: {exc}"
                ) from None
        if not isinstance(plan, dict) or not plan:
            raise _Refused("invalid_plan", "plan must be a complete edit plan object")
        return plan

    @staticmethod
    def _reuse(params: Mapping[str, Any]) -> bool:
        reuse = params.get("reuse", True)
        if not isinstance(reuse, bool):
            raise _Refused("invalid_arguments", "reuse must be true or false")
        return reuse

    def _queue_spec(
        self, name: str, description: str, extra: Dict[str, Any]
    ) -> ToolSpec:
        properties = {
            **extra,
            "plan": _PLAN_PROPERTY,
            "reuse": {
                "type": "boolean",
                "description": (
                    "Default true: if an identical earlier job's output is "
                    "verified intact, use it instead of rendering. false "
                    "always renders a new version."
                ),
            },
        }
        return ToolSpec(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": [*extra, "plan"],
            },
            category="media",
            timeout_seconds=30.0,
            requires_confirmation=self._render_settings.require_confirmation,
            required_capabilities=list(QUEUE_CAPABILITIES),
        )


def _queued(job: Job, payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job.id,
        "kind": job.kind,
        "job_status": job.status.value,
        "parent_id": job.parent_id,
        "output_name": payload["plan"].get("output_name"),
        "reuse": payload.get("reuse", True),
        "next_step": _NEXT_STEP["queued"],
    }


class MediaJobSubmitTool(_JobToolBase):
    """Queue a ``video.render`` job for the worker."""

    tool_id = SUBMIT_TOOL

    @property
    def spec(self) -> ToolSpec:
        return self._queue_spec(
            SUBMIT_TOOL,
            (
                "Queue a video render job from a complete edit plan. The job "
                "worker renders it, validates it and makes a preview, so long "
                "renders do not block the conversation. Returns a job_id; "
                "follow it with media_job_status. The plan is checked before "
                "it is queued. Output is a new version (e.g. reel_v002.mp4); "
                "nothing is overwritten."
            ),
            {},
        )

    def execute(self, **params: Any) -> ToolResult:
        def action() -> Dict[str, Any]:
            from jarvis_ext.video.render_job import KIND, RevisionError, make_payload

            plan = self._plan(params)
            reuse = self._reuse(params)
            if self._config_error:
                raise _Refused(
                    "config_error", f"Configuration error: {self._config_error}"
                )
            try:
                payload = make_payload(plan, reuse=reuse, config_path=self._config_path)
            except RevisionError as exc:
                raise _Refused(exc.code, str(exc)) from None
            with self._open_store() as store:
                job = store.submit(KIND, payload)
            return _queued(job, payload)

        return self._run(action)


class MediaJobReviseTool(_JobToolBase):
    """Queue a revision (child job with a complete replacement plan)."""

    tool_id = REVISE_TOOL

    @property
    def spec(self) -> ToolSpec:
        return self._queue_spec(
            REVISE_TOOL,
            (
                "Queue a revision of a finished render job: a new job, linked "
                "to it, with a complete replacement edit plan (not a partial "
                "change). It keeps the original output_name (may be left out) "
                "so the result is the next version; earlier versions and the "
                "original job are never changed. Returns the new job_id."
            ),
            {
                "job_id": {
                    "type": "string",
                    "description": "The job to revise (its id or first 6+ characters).",
                }
            },
        )

    def execute(self, **params: Any) -> ToolResult:
        def action() -> Dict[str, Any]:
            from jarvis_ext.video.render_job import (
                KIND,
                RevisionError,
                revision_payload,
            )

            job_id = self._job_id(params)
            plan = self._plan(params)
            reuse = self._reuse(params)
            with self._open_store() as store:
                parent = store.find(job_id)
                try:
                    payload = revision_payload(
                        parent, plan, reuse=reuse, config_path=self._config_path
                    )
                except RevisionError as exc:
                    raise _Refused(exc.code, str(exc)) from None
                job = store.submit(KIND, payload, parent_id=parent.id)
            return _queued(job, payload)

        return self._run(action)


class MediaJobStatusTool(_JobToolBase):
    """Report a job's status, progress, result and notification."""

    tool_id = STATUS_TOOL

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=STATUS_TOOL,
            description=(
                "Show a render job: status, stage, progress, attempts, parent "
                "job, output and whether it is ready for review (preview and "
                "thumbnail), reuse or resume details, error code and a short "
                "history. Without job_id, lists the 10 most recent jobs. Only "
                "call a video finished when ready_for_review is true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id or its first 6+ characters.",
                    }
                },
                "required": [],
            },
            category="media",
            timeout_seconds=15.0,
            required_capabilities=list(STATUS_CAPABILITIES),
        )

    def execute(self, **params: Any) -> ToolResult:
        def action() -> Dict[str, Any]:
            job_id = self._job_id(params, required=False)
            with self._open_store() as store:
                if job_id is None:
                    return {
                        "jobs": [
                            _brief(job) for job in store.list_jobs(limit=RECENT_JOBS)
                        ]
                    }
                job = store.find(job_id)
                events = store.events(job.id)
                notification = store.notification(job.id)
            return describe_job(job, events, notification)

        return self._run(action)


def _brief(job: Job) -> Dict[str, Any]:
    result = job.result if isinstance(job.result, dict) else {}
    return {
        "job_id": job.id,
        "kind": job.kind,
        "job_status": job.status.value,
        "progress": job.progress,
        "parent_id": job.parent_id,
        "output": _safe_name(result.get("output")),
        "ready_for_review": result.get("ready_for_review") is True,
        "created_at": job.created_at,
    }


def describe_job(job: Job, events: List[Any], notification: Any) -> Dict[str, Any]:
    """A concise, path-free view of a job for an agent."""
    result = job.result if isinstance(job.result, dict) else {}
    validate = (
        result.get("validate") if isinstance(result.get("validate"), dict) else {}
    )
    render = result.get("render") if isinstance(result.get("render"), dict) else {}
    plan = job.payload.get("plan") if isinstance(job.payload, dict) else None
    data: Dict[str, Any] = {
        "job": {
            "job_id": job.id,
            "kind": job.kind,
            "job_status": job.status.value,
            "stage": job.stage,
            "progress": job.progress,
            "attempts": job.attempts,
            "max_attempts": job.max_attempts,
            "parent_id": job.parent_id,
            "output_name": _safe_name(plan.get("output_name"))
            if isinstance(plan, dict)
            else None,
            "cancel_requested": job.cancel_requested,
            "retry_not_before": job.not_before,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        },
        "result": {
            "output": _safe_name(result.get("output")),
            "ready_for_review": result.get("ready_for_review") is True,
            "preview": _safe_name(validate.get("preview")),
            "thumbnail": _safe_name(validate.get("thumbnail")),
            "reused_from": result.get("reused_from"),
            "resumed": result.get("resumed") is True,
            "validation_status": validate.get("status"),
        },
        "error": None,
        "notification": None,
        "history": [
            {
                "at": e.at,
                "status": e.to_status.value,
                "note": clean_text(e.note, 160),
            }
            for e in events[-MAX_HISTORY:]
        ],
        "next_step": _NEXT_STEP.get(job.status.value, ""),
    }
    if job.error_code:
        failed = [
            str(c)
            for c in (render.get("failed_checks") or [])
            + (validate.get("failed_checks") or [])
        ][:10]
        warnings = (validate.get("warnings") or render.get("warnings") or [])[
            :MAX_WARNINGS
        ]
        data["error"] = {
            "code": job.error_code,
            "message": clean_text(job.error_message or "", MAX_MESSAGE_CHARS),
            "failed_checks": [clean_text(c, 64) for c in failed],
            "warnings": [clean_text(w, 200) for w in warnings],
        }
    if notification is not None:
        data["notification"] = {
            "state": notification.state.value,
            "outcome": notification.outcome.value,
            "attempts": notification.attempts,
            "last_error": notification.last_error,
        }
    return data


def _safe_name(value: Any) -> Optional[str]:
    """An output-folder name such as ``reel_v001.mp4`` or
    ``review/x.jpg``; anything else (absolute, odd) is withheld."""
    if not isinstance(value, str) or not value:
        return None
    parts = value.split("/")
    if (
        value.startswith(("/", "~", "."))
        or "\\" in value
        or len(parts) > 2
        or (len(parts) == 2 and parts[0] != "review")
        or any(not p or p.startswith(".") for p in parts)
    ):
        return None
    return value[:200]


__all__ = [
    "MediaJobReviseTool",
    "MediaJobStatusTool",
    "MediaJobSubmitTool",
    "QUEUE_CAPABILITIES",
    "REVISE_TOOL",
    "STATUS_CAPABILITIES",
    "STATUS_TOOL",
    "SUBMIT_TOOL",
    "describe_job",
]
