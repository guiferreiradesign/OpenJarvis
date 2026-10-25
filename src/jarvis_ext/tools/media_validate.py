"""``media_validate``: prove a rendered video is usable, then make review files.

Given a published render (``reel_v001.mp4``) and its record
(``reel_v001.render.json``, written by ``media_render``), the tool checks:

* the file is a regular, non-empty file whose SHA-256 still matches the
  record (so it is the complete file the render produced, not a truncated or
  replaced one);
* ffprobe can read it, and duration, dimensions, frame rate, codecs, pixel
  format, audio and subtitle track match the plan;
* every frame decodes (``ffmpeg -xerror -f null``).

Only when **all** checks pass does it create a low-resolution preview and a
JPEG thumbnail in ``<output>/review/`` (versioned, never overwritten) and
report ``ready_for_review: true``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from jarvis_ext.tools._video_base import Refused, VideoToolBase, sha256_file
from jarvis_ext.tools.media_workspace import probe_media
from jarvis_ext.video.checks import (
    Expectations,
    all_passed,
    check_media,
    make_check,
)
from jarvis_ext.video.ffmpeg import (
    LOG_FILENAME,
    PREVIEW_FILENAME,
    THUMBNAIL_FILENAME,
    build_decode_check_command,
    build_preview_command,
    build_thumbnail_command,
    log_tail,
    minimal_env,
)
from jarvis_ext.video.output import (
    RECORD_SUFFIX,
    REVIEW_DIRNAME,
    OutputError,
    OutputFolder,
    parse_render_name,
)
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import ToolSpec

TOOL_NAME = "media_validate"
MAX_RECORD_BYTES = 1024 * 1024
_JPEG_MAGIC = b"\xff\xd8\xff"


class MediaValidateTool(VideoToolBase):
    """Validate a rendered file and create its preview and thumbnail."""

    tool_id = TOOL_NAME
    status_key = "validation_status"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=TOOL_NAME,
            description=(
                "Check that a video produced by media_render is complete and "
                "matches its plan (duration, resolution, frame rate, codecs, "
                "audio, full decode), then create a low-resolution preview and "
                "a thumbnail for review. Only report a video as ready when this "
                "returns ready_for_review: true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "output": {
                        "type": "string",
                        "description": (
                            "File name of the render in the output folder, "
                            "e.g. 'reel_v001.mp4'."
                        ),
                    },
                    "preview": {
                        "type": "boolean",
                        "description": "Create preview and thumbnail (default true).",
                    },
                    "thumbnail_at_seconds": {
                        "type": "number",
                        "description": "Thumbnail time (default: middle of video).",
                    },
                },
                "required": ["output"],
            },
            category="media",
            timeout_seconds=3 * self._settings.validate_timeout_seconds + 60.0,
            required_capabilities=["file:read", "file:write"],
        )

    # -- inputs -------------------------------------------------------------

    @staticmethod
    def _parse_params(params: Mapping[str, Any]) -> Tuple[str, bool, Optional[float]]:
        output = params.get("output")
        if not isinstance(output, str) or not output.strip():
            raise Refused("invalid_arguments", "output must be a file name")
        output = output.strip()
        if parse_render_name(output) is None:
            raise Refused(
                "invalid_arguments",
                "output must be a render file name such as 'reel_v001.mp4'",
            )
        preview = params.get("preview", True)
        if not isinstance(preview, bool):
            raise Refused("invalid_arguments", "preview must be true or false")
        at = params.get("thumbnail_at_seconds")
        if at is not None:
            if isinstance(at, bool) or not isinstance(at, (int, float)) or at < 0:
                raise Refused("invalid_arguments", "thumbnail_at_seconds must be >= 0")
            at = float(at)
        return output, preview, at

    @staticmethod
    def _read_record(folder: OutputFolder, output: str) -> Mapping[str, Any]:
        name = output[: -len(".mp4")] + RECORD_SUFFIX
        try:
            path = folder.resolve_child(name)
        except FileNotFoundError:
            raise Refused(
                "no_record",
                f"No render record ({name}) found; only files produced by "
                "media_render can be validated.",
            ) from None
        except OutputError as exc:
            raise Refused("no_record", f"Render record unusable: {exc}") from None
        if path.stat().st_size > MAX_RECORD_BYTES:
            raise Refused("no_record", "Render record is too large")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise Refused("no_record", "Render record is not readable JSON") from None
        if not isinstance(record, dict) or not isinstance(record.get("plan"), dict):
            raise Refused("no_record", "Render record is malformed")
        return record

    # -- helpers ------------------------------------------------------------

    def _run(self, cmd: List[str], temp_dir: Path, timeout: float) -> Tuple[int, str]:
        """Run an FFmpeg job; ``(code, error)`` with ``code -1`` on timeout."""
        log = temp_dir / LOG_FILENAME
        try:
            code = self._process_runner(
                cmd, cwd=temp_dir, timeout=timeout, log_path=log, env=minimal_env()
            )
        except subprocess.TimeoutExpired:
            return -1, f"timed out after {timeout:g}s"
        except OSError as exc:
            return -2, f"could not start ffmpeg ({type(exc).__name__})"
        return code, ""

    # -- entry point --------------------------------------------------------

    def execute(self, **params: Any) -> ToolResult:
        warnings: List[str] = []
        try:
            folder, _ = self._preflight()
            output, want_preview, thumb_at = self._parse_params(params)
            try:
                path = folder.resolve_child(output)
            except FileNotFoundError:
                raise Refused(
                    "not_found", f"{output} is not in the output folder"
                ) from None
            except OutputError as exc:
                raise Refused("invalid_arguments", str(exc)) from None
            record = self._read_record(folder, output)
            try:
                expected = Expectations.from_plan_dict(record["plan"])
            except ValueError as exc:
                raise Refused("no_record", str(exc)) from None
            ffmpeg, ffprobe = self._find_tools()
        except Refused as exc:
            return self._failure(exc, warnings)

        stem = output[: -len(".mp4")]
        timeout = self._settings.validate_timeout_seconds
        temp_dir: Optional[Path] = None
        review: Dict[str, Any] = {"preview": None, "thumbnail": None}
        try:
            size = path.stat().st_size
            recorded = record.get("output")
            if not isinstance(recorded, dict):
                recorded = {}
            size_ok = size == recorded.get("size_bytes")
            hash_ok = size_ok and sha256_file(path) == recorded.get("sha256")
            if hash_ok:
                actual = "match"
            elif size_ok:
                actual = "checksum differs"
            else:
                actual = f"{size} bytes, record says {recorded.get('size_bytes')}"
            checks = [
                make_check(
                    "matches_render_record",
                    hash_ok,
                    "size and SHA-256 from the render record",
                    actual,
                )
            ]
            meta = probe_media(ffprobe, path, folder.root, timeout, self._runner)
            checks += check_media(meta, size, expected)

            temp_dir = folder.make_temp_dir()
            if all_passed(checks):
                code, error = self._run(
                    build_decode_check_command(ffmpeg, path), temp_dir, timeout
                )
                detail = error or log_tail(
                    temp_dir / LOG_FILENAME, redact=self._redactions(folder, temp_dir)
                )
                checks.append(
                    make_check(
                        "decodes_cleanly",
                        code == 0,
                        "every frame decodes",
                        "ok" if code == 0 else (detail or f"exit status {code}"),
                    )
                )
            else:
                warnings.append("Full decode skipped because earlier checks failed.")

            passed = all_passed(checks)
            if passed and want_preview:
                review = self._make_review_files(
                    folder,
                    temp_dir,
                    path,
                    stem,
                    ffmpeg,
                    ffprobe,
                    expected,
                    meta.get("duration_seconds") or expected.duration,
                    thumb_at,
                    warnings,
                )
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

        review_ok = not want_preview or (
            review["preview"] is not None and review["thumbnail"] is not None
        )
        ready = passed and review_ok
        payload = {
            "validation_status": "passed" if passed else "failed",
            "ready_for_review": ready,
            "output": output,
            "checks": checks,
            "preview": review["preview"],
            "thumbnail": review["thumbnail"],
            "expected": expected.to_dict(),
            "warnings": warnings,
        }
        if passed and not review_ok:
            payload["validation_status"] = "passed_without_review_files"
        return self._result(payload, success=ready)

    def _make_review_files(
        self,
        folder: OutputFolder,
        temp_dir: Path,
        source: Path,
        stem: str,
        ffmpeg: str,
        ffprobe: str,
        expected: Expectations,
        duration: float,
        thumb_at: Optional[float],
        warnings: List[str],
    ) -> Dict[str, Optional[str]]:
        result: Dict[str, Optional[str]] = {"preview": None, "thumbnail": None}
        timeout = self._settings.validate_timeout_seconds
        height = min(self._settings.preview_height, expected.height)
        review_dir = folder.subdir(REVIEW_DIRNAME)

        code, error = self._run(
            build_preview_command(
                ffmpeg, source, height=height, has_audio=expected.audio
            ),
            temp_dir,
            timeout,
        )
        preview = temp_dir / PREVIEW_FILENAME
        if code == 0 and preview.is_file():
            meta = probe_media(ffprobe, preview, folder.root, timeout, self._runner)
            video = meta.get("video") or {}
            if meta.get("status") == "ok" and video.get("height") == height:
                final, _ = folder.publish_versioned(
                    preview, review_dir, f"{stem}_preview", ".mp4"
                )
                result["preview"] = folder.relative(final)
            else:
                warnings.append("The preview could not be verified; not published.")
        else:
            warnings.append(f"Preview generation failed ({error or f'status {code}'}).")

        if thumb_at is None:
            thumb_at = duration / 2.0
        if thumb_at >= duration:
            warnings.append("thumbnail_at_seconds is past the end; using the middle.")
            thumb_at = duration / 2.0
        code, error = self._run(
            build_thumbnail_command(ffmpeg, source, at_seconds=thumb_at, height=height),
            temp_dir,
            timeout,
        )
        thumb = temp_dir / THUMBNAIL_FILENAME
        if code == 0 and thumb.is_file() and os.path.getsize(thumb) > 0:
            with open(thumb, "rb") as fh:
                magic = fh.read(3)
            if magic == _JPEG_MAGIC:
                final, _ = folder.publish_versioned(
                    thumb, review_dir, f"{stem}_thumb", ".jpg"
                )
                result["thumbnail"] = folder.relative(final)
            else:
                warnings.append("The thumbnail is not a JPEG; not published.")
        else:
            warnings.append(
                f"Thumbnail generation failed ({error or f'status {code}'})."
            )
        return result


__all__ = ["MediaValidateTool", "TOOL_NAME"]
