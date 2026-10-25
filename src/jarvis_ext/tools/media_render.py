"""``media_render``: execute an ``EditPlan`` with FFmpeg, deterministically.

The tool does exactly what the plan says (trim, order, fit to the preset,
encode, optional subtitles and logo) and makes no creative decisions.

Flow
----
1. Validate the plan's shape and values (:mod:`jarvis_ext.video.plan`).
2. Check the output folder exists and is separate from the workspace.
3. Resolve every source through the Phase 1 path rules and probe it with
   the Phase 1 hardened ffprobe; reject times beyond a clip's end.
4. Render into a private temp directory under ``<output>/.jarvis-tmp``.
5. Probe the result and check it against the plan **before** it gets a name.
6. Publish it as ``<output_name>_vNNN.mp4`` (never overwriting) plus a
   ``.render.json`` record, and remove the temp directory.

A successful render is *not* the same as a validated one: the result always
says ``ready_for_review: false`` and points to ``media_validate``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from jarvis_ext._workspace import AccessDenied, resolve_relative
from jarvis_ext.tools._video_base import Refused, VideoToolBase, sha256_file
from jarvis_ext.tools.media_workspace import probe_media
from jarvis_ext.video.checks import Expectations, all_passed, check_media, failed_names
from jarvis_ext.video.ffmpeg import (
    LOG_FILENAME,
    RENDER_FILENAME,
    SUBTITLE_FILENAME,
    ClipInput,
    LogoInput,
    build_render_command,
    log_tail,
    minimal_env,
    png_dimensions,
)
from jarvis_ext.video.output import RECORD_SUFFIX, OutputError
from jarvis_ext.video.plan import EditPlan, PlanError, parse_edit_plan
from jarvis_ext.video.subtitles import (
    MAX_SUBTITLE_BYTES,
    SubtitleError,
    fit_to_duration,
    parse_srt,
    to_srt,
)
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import ToolSpec

TOOL_NAME = "media_render"
RECORD_VERSION = 1

MAX_PLAN_JSON_BYTES = 256 * 1024
MAX_LOGO_BYTES = 10 * 1024 * 1024
MAX_LOGO_PIXELS = 8192
# Allow tiny container/stream rounding when comparing an out-point with the
# probed source duration.
_END_EPSILON = 0.001
_HIGH_BIT_DEPTH = ("10", "12", "16")


class MediaRenderTool(VideoToolBase):
    """Render a validated ``EditPlan`` into the configured output folder."""

    tool_id = TOOL_NAME
    status_key = "render_status"

    @property
    def spec(self) -> ToolSpec:
        presets = ", ".join(sorted(self._settings.presets))
        return ToolSpec(
            name=TOOL_NAME,
            description=(
                "Render a video exactly as described by an edit plan: trims "
                "clips from the media workspace, joins them in list order, fits "
                "them to the output preset (crop or pad), encodes H.264/AAC MP4, "
                "and optionally adds an .srt subtitle file and a PNG logo. "
                "Writes a new versioned file (e.g. reel_v001.mp4) to the output "
                "folder; never changes or overwrites existing files. After a "
                "successful render call media_validate before presenting the "
                "video. Paths are relative to the media workspace (clips, "
                "subtitles) or the assets folder (logo)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "object",
                        "description": (
                            "Edit plan. Required: output_name (lowercase "
                            "letters, digits, '_' '-'), clips (list of "
                            "{source, start, end}; times in seconds or "
                            "'HH:MM:SS.mmm'). Optional: preset "
                            f"({presets}; default {self._settings.default_preset}),"
                            " fit ('crop' | 'pad'), video {frame_rate, crf, "
                            "x264_preset}, audio {mode: 'keep' | 'none', "
                            "bitrate_kbps}, subtitles {source: '.srt', mode: "
                            "'embed' | 'burn', language}, branding {asset: "
                            "'.png', position, width_fraction, margin_px, "
                            "opacity}, duration_tolerance_seconds."
                        ),
                    },
                },
                "required": ["plan"],
            },
            category="media",
            # Source probes + render + pre-publish probe, with headroom, so
            # the executor never gives up while a render is still publishing.
            timeout_seconds=self._settings.render_timeout_seconds
            + self._workspace.probe_budget_seconds
            + self._settings.validate_timeout_seconds
            + 60.0,
            requires_confirmation=self._settings.require_confirmation,
            required_capabilities=["file:read", "file:write"],
        )

    # -- plan ---------------------------------------------------------------

    def _read_plan(self, params: Mapping[str, Any]) -> EditPlan:
        raw = params.get("plan")
        if isinstance(raw, str):
            if len(raw.encode("utf-8")) > MAX_PLAN_JSON_BYTES:
                raise Refused("invalid_plan", "Plan is too large")
            try:
                raw = json.loads(raw)
            except ValueError as exc:
                raise Refused(
                    "invalid_plan", f"Plan is not valid JSON: {exc}"
                ) from None
        if raw is None:
            raise Refused("invalid_plan", "A plan is required")
        try:
            return parse_edit_plan(raw, self._settings.plan_limits)
        except PlanError as exc:
            raise Refused("invalid_plan", f"Invalid plan: {exc}") from None

    # -- sources ------------------------------------------------------------

    def _resolve_workspace_file(self, root: Path, subpath: str, label: str) -> Path:
        try:
            real, display = resolve_relative(
                root, subpath, follow_symlinks=self._workspace.follow_symlinks
            )
        except AccessDenied as exc:
            raise Refused("access_denied", f"{label}: Access denied: {exc}") from None
        except FileNotFoundError:
            raise Refused(
                "invalid_plan", f"{label}: not found in media workspace: {subpath}"
            ) from None
        except (OSError, RuntimeError) as exc:
            raise Refused(
                "invalid_plan",
                f"{label}: cannot access {subpath}: {type(exc).__name__}",
            ) from None
        if not self._workspace.include_hidden and any(
            part.startswith(".") for part in display.parts
        ):
            raise Refused("access_denied", f"{label}: hidden files are not used")
        try:
            info = os.stat(real)
        except OSError:
            raise Refused("invalid_plan", f"{label}: cannot read {subpath}") from None
        if not stat.S_ISREG(info.st_mode):
            raise Refused("invalid_plan", f"{label}: not a regular file: {subpath}")
        return real

    def _resolve_clips(
        self, plan: EditPlan, root: Path, ffprobe: str, warnings: List[str]
    ) -> List[ClipInput]:
        probes: Dict[Path, Dict[str, Any]] = {}
        resolved: List[ClipInput] = []
        deadline = time.monotonic() + self._workspace.probe_budget_seconds
        for index, clip in enumerate(plan.clips):
            label = f"clips[{index}].source"
            real = self._resolve_workspace_file(root, clip.source, label)
            if real.suffix.lower() not in self._workspace.video_extensions:
                raise Refused(
                    "invalid_plan", f"{label}: not a video file: {clip.source}"
                )
            if real not in probes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Refused(
                        "timeout", "Inspecting the source clips took too long"
                    )
                probes[real] = probe_media(
                    ffprobe,
                    real,
                    root,
                    min(self._workspace.ffprobe_timeout_seconds, remaining),
                    self._runner,
                )
                video = probes[real].get("video") or {}
                pix = video.get("pixel_format") or ""
                if any(depth in pix for depth in _HIGH_BIT_DEPTH):
                    warnings.append(
                        f"{clip.source}: {pix} source (likely HDR); converted to "
                        "8-bit SDR without tone mapping, colours may look flat."
                    )
            meta = probes[real]
            if meta.get("status") != "ok":
                detail = meta.get("error") or meta.get("status")
                raise Refused(
                    "invalid_media", f"{label}: cannot be read as video ({detail})"
                )
            if "video" not in meta:
                raise Refused("invalid_media", f"{label}: has no video stream")
            duration = meta.get("duration_seconds")
            if duration is None:
                raise Refused("invalid_media", f"{label}: duration is unknown")
            if clip.end > duration + _END_EPSILON:
                raise Refused(
                    "invalid_plan",
                    f"clips[{index}].end ({clip.end:g}s) is beyond the end of "
                    f"{clip.source} ({duration:g}s)",
                )
            has_audio = "audio" in meta
            if plan.audio_mode == "keep" and not has_audio:
                warnings.append(f"{clip.source} has no audio; silence is used.")
            resolved.append(
                ClipInput(
                    path=real,
                    start=clip.start,
                    duration=clip.duration,
                    has_audio=has_audio,
                )
            )
        return resolved

    def _load_subtitles(
        self, plan: EditPlan, root: Path, warnings: List[str]
    ) -> Optional[str]:
        if plan.subtitles is None:
            return None
        source = plan.subtitles.source
        real = self._resolve_workspace_file(root, source, "subtitles.source")
        if real.stat().st_size > MAX_SUBTITLE_BYTES:
            raise Refused("invalid_plan", "subtitles.source is larger than 1 MB")
        try:
            with open(real, "rb") as fh:
                cues = parse_srt(fh.read(MAX_SUBTITLE_BYTES + 1))
        except SubtitleError as exc:
            raise Refused("invalid_plan", f"subtitles.source: {exc}") from None
        except OSError:
            raise Refused("invalid_plan", "subtitles.source cannot be read") from None
        cues, dropped = fit_to_duration(cues, plan.expected_duration)
        if dropped:
            warnings.append(
                f"{dropped} subtitle cue(s) start after the end of the video and "
                "were dropped (subtitle times are relative to the output)."
            )
        if not cues:
            raise Refused("invalid_plan", "No subtitle cue falls inside the video")
        return to_srt(cues)

    def _load_logo(self, plan: EditPlan) -> Optional[LogoInput]:
        if plan.branding is None:
            return None
        assets = self._settings.assets_root
        if assets is None:
            raise Refused(
                "not_configured",
                "Branding needs [jarvis.media_render] assets_root in config.toml.",
            )
        if not assets.is_dir():
            raise Refused(
                "invalid_plan", "The configured assets folder does not exist."
            )
        try:
            real, display = resolve_relative(
                assets, plan.branding.asset, where="assets folder"
            )
        except AccessDenied as exc:
            raise Refused(
                "access_denied", f"branding.asset: Access denied: {exc}"
            ) from None
        except (FileNotFoundError, OSError, RuntimeError):
            raise Refused(
                "invalid_plan", f"branding.asset not found: {plan.branding.asset}"
            ) from None
        if any(part.startswith(".") for part in display.parts):
            raise Refused("access_denied", "branding.asset: hidden files are not used")
        try:
            info = os.stat(real)
            if not stat.S_ISREG(info.st_mode):
                raise Refused("invalid_plan", "branding.asset is not a regular file")
            if info.st_size > MAX_LOGO_BYTES:
                raise Refused("invalid_plan", "branding.asset is larger than 10 MB")
            with open(real, "rb") as fh:
                header = fh.read(32)
        except OSError:
            raise Refused("invalid_plan", "branding.asset cannot be read") from None
        dims = png_dimensions(header)
        if dims is None:
            raise Refused("invalid_plan", "branding.asset is not a PNG image")
        if max(dims) > MAX_LOGO_PIXELS:
            raise Refused("invalid_plan", "branding.asset is larger than 8192 pixels")
        return LogoInput(path=real, width=dims[0], height=dims[1])

    # -- entry point --------------------------------------------------------

    def execute(self, **params: Any) -> ToolResult:
        started = time.monotonic()
        warnings: List[str] = []
        try:
            folder, root = self._preflight()
            plan = self._read_plan(params)
            ffmpeg, ffprobe = self._find_tools()
            capabilities = self._ffmpeg_capabilities(ffmpeg)
            missing = capabilities.missing_for(plan)
            if missing:
                hint = ""
                if "subtitles" in missing:
                    hint = (
                        " Subtitle burn-in needs an FFmpeg built with libass; "
                        "use subtitles.mode 'embed' instead."
                    )
                raise Refused(
                    "unsupported",
                    "This FFmpeg build lacks: " + ", ".join(missing) + "." + hint,
                )
            clips = self._resolve_clips(plan, root, ffprobe, warnings)
            subtitles = self._load_subtitles(plan, root, warnings)
            logo = self._load_logo(plan)
        except Refused as exc:
            return self._failure(exc, warnings)

        temp_dir: Optional[Path] = None
        try:
            temp_dir = folder.make_temp_dir()
            if subtitles is not None:
                (temp_dir / SUBTITLE_FILENAME).write_text(subtitles, encoding="utf-8")
            cmd = build_render_command(ffmpeg, plan, clips, logo=logo)
            try:
                code = self._process_runner(
                    cmd,
                    cwd=temp_dir,
                    timeout=self._settings.render_timeout_seconds,
                    log_path=temp_dir / LOG_FILENAME,
                    env=minimal_env(),
                )
            except subprocess.TimeoutExpired:
                raise Refused(
                    "timeout",
                    "Render exceeded render_timeout_seconds "
                    f"({self._settings.render_timeout_seconds:g}s) and was stopped.",
                ) from None
            except OSError as exc:
                raise Refused(
                    "ffmpeg_failed",
                    f"ffmpeg could not be started ({type(exc).__name__})",
                ) from None
            if code != 0:
                detail = log_tail(
                    temp_dir / LOG_FILENAME, redact=self._redactions(folder, temp_dir)
                )
                raise Refused(
                    "ffmpeg_failed",
                    f"ffmpeg exited with status {code}"
                    + (f": {detail}" if detail else ""),
                )

            rendered = temp_dir / RENDER_FILENAME
            expected = Expectations.from_plan_dict(plan.to_dict())
            try:
                size = rendered.stat().st_size
            except OSError:
                raise Refused(
                    "ffmpeg_failed", "ffmpeg reported success but wrote no file"
                )
            meta = probe_media(
                ffprobe,
                rendered,
                folder.root,
                self._settings.validate_timeout_seconds,
                self._runner,
            )
            checks = check_media(meta, size, expected)
            if not all_passed(checks):
                raise Refused(
                    "failed_checks",
                    "The rendered file does not match the plan ("
                    + ", ".join(failed_names(checks))
                    + "); it was not published.",
                    checks=checks,
                )

            checksum = sha256_file(rendered)
            final, version = folder.publish_versioned(
                rendered, folder.root, plan.output_name, ".mp4"
            )
            record_name = final.stem + RECORD_SUFFIX
            output = {
                "file": final.name,
                "version": version,
                "size_bytes": size,
                "sha256": checksum,
                "duration_seconds": meta.get("duration_seconds"),
                "width": meta["video"].get("width"),
                "height": meta["video"].get("height"),
                "frame_rate": meta["video"].get("frame_rate"),
                "has_audio": "audio" in meta,
            }
            record = {
                "record_version": RECORD_VERSION,
                "tool": TOOL_NAME,
                "created": datetime.now(timezone.utc)
                .replace(microsecond=0)
                .isoformat(),
                "output": output,
                "plan": plan.to_dict(),
                "preset": plan.preset.to_dict(),
                "ffmpeg_version": capabilities.version,
                "prepublish_checks": checks,
                "warnings": warnings,
            }
            try:
                folder.write_record(temp_dir, final.with_name(record_name), record)
            except (OSError, OutputError):
                warnings.append(
                    "The render record could not be written; media_validate "
                    "needs it to check this file."
                )
                record_name = None
        except Refused as exc:
            return self._failure(exc, warnings)
        except OutputError as exc:
            return self._failure(Refused("output_error", str(exc)), warnings)
        except OSError as exc:
            return self._failure(
                Refused("output_error", f"Output folder error: {type(exc).__name__}"),
                warnings,
            )
        finally:
            folder.remove_temp_dir(temp_dir)

        payload = {
            "render_status": "succeeded",
            "validation_status": "not_validated",
            "ready_for_review": False,
            "next_step": f"Call media_validate with output='{final.name}'.",
            "output": output,
            "record": record_name,
            "settings": plan.to_dict(),
            "prepublish_checks": checks,
            "ffmpeg_version": capabilities.version,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "warnings": warnings,
        }
        return self._result(payload, success=True)


__all__ = ["MediaRenderTool", "TOOL_NAME"]
