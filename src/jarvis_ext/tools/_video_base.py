"""Shared plumbing for the ``media_render`` and ``media_validate`` tools.

Both read two config sections: ``[jarvis.media_workspace]`` (where sources
live; Phase 1 settings, reused unchanged) and ``[jarvis.media_render]``
(where outputs go).  Both report results as JSON in ``ToolResult.content``,
including failures, and never put absolute paths in that JSON.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from jarvis_ext.config import ExtensionConfigError, load_extension_section
from jarvis_ext.tools.media_workspace import CONFIG_SECTION as WORKSPACE_SECTION
from jarvis_ext.tools.media_workspace import MediaWorkspaceSettings, Runner
from jarvis_ext.video.ffmpeg import (
    FfmpegCapabilities,
    FfmpegError,
    ProcessRunner,
    query_capabilities,
    run_process,
)
from jarvis_ext.video.output import OutputError, OutputFolder
from jarvis_ext.video.settings import (
    CONFIG_SECTION as RENDER_SECTION,
)
from jarvis_ext.video.settings import (
    MediaRenderSettings,
    check_separation,
)
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool

logger = logging.getLogger(__name__)

_FFMPEG_HINT = (
    "Install FFmpeg (e.g. 'brew install ffmpeg') or set "
    "[jarvis.media_render] ffmpeg_path / ffprobe_path."
)


class Refused(Exception):
    """Stop the call with a structured failure."""

    def __init__(self, status: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class VideoToolBase(BaseTool):
    """Config loading, FFmpeg discovery and result formatting."""

    is_local = True
    tool_id = ""
    status_key = "status"

    def __init__(
        self,
        *,
        settings: Optional[MediaRenderSettings] = None,
        workspace_settings: Optional[MediaWorkspaceSettings] = None,
        config_path: Optional[Path] = None,
        runner: Optional[Runner] = None,
        process_runner: Optional[ProcessRunner] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self._config_error = ""
        try:
            if workspace_settings is None:
                workspace_settings = MediaWorkspaceSettings.from_mapping(
                    load_extension_section(WORKSPACE_SECTION, config_path)
                )
            if settings is None:
                settings = MediaRenderSettings.from_mapping(
                    load_extension_section(RENDER_SECTION, config_path)
                )
        except ExtensionConfigError as exc:
            self._config_error = str(exc)
        self._settings = settings or MediaRenderSettings()
        self._workspace = workspace_settings or MediaWorkspaceSettings()
        self._runner: Runner = runner or subprocess.run
        self._process_runner: ProcessRunner = process_runner or run_process
        self._which = which or shutil.which
        self._capabilities: Optional[FfmpegCapabilities] = None

    # -- results ------------------------------------------------------------

    def _result(self, payload: Dict[str, Any], success: bool) -> ToolResult:
        return ToolResult(
            tool_name=self.tool_id,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            success=success,
            metadata={self.status_key: payload.get(self.status_key)},
        )

    def _failure(self, exc: Refused, warnings: Optional[list] = None) -> ToolResult:
        payload: Dict[str, Any] = {
            self.status_key: exc.status,
            "error": exc.message,
            **exc.extra,
            "warnings": list(warnings or []),
        }
        return self._result(payload, success=False)

    # -- preflight ----------------------------------------------------------

    def _preflight(self) -> Tuple[OutputFolder, Path]:
        """Config present, folders exist and are separate.  Raises Refused."""
        if self._config_error:
            raise Refused("config_error", f"Configuration error: {self._config_error}")
        workspace_root = self._workspace.root
        if workspace_root is None:
            raise Refused(
                "not_configured",
                "Media workspace is not configured. Add [jarvis.media_workspace] "
                'root = "/absolute/path" to config.toml.',
            )
        output_root = self._settings.output_root
        if output_root is None:
            raise Refused(
                "not_configured",
                "Output folder is not configured. Add [jarvis.media_render] "
                'output_root = "/absolute/path" (outside the media workspace) '
                "to config.toml.",
            )
        if not workspace_root.is_dir():
            logger.warning("Media workspace root is missing: %s", workspace_root)
            raise Refused(
                "workspace_unavailable",
                "The configured media workspace folder does not exist or is not "
                "a directory (is the drive connected?).",
            )
        folder = OutputFolder(output_root)
        try:
            folder.check_ready()
            check_separation(output_root, workspace_root)
        except OutputError as exc:
            logger.warning("Output folder problem (%s): %s", output_root, exc)
            raise Refused("output_unavailable", str(exc)) from None
        except ExtensionConfigError as exc:
            raise Refused("config_error", f"Configuration error: {exc}") from None
        return folder, workspace_root

    def _find_tools(self) -> Tuple[str, str]:
        ffmpeg = self._which(self._settings.ffmpeg_path or "ffmpeg")
        ffprobe = self._which(
            self._settings.ffprobe_path or self._workspace.ffprobe_path or "ffprobe"
        )
        missing = [
            name
            for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
            if not path
        ]
        if missing:
            raise Refused(
                "ffmpeg_missing", f"{' and '.join(missing)} not found. {_FFMPEG_HINT}"
            )
        assert ffmpeg is not None and ffprobe is not None
        return ffmpeg, ffprobe

    def _ffmpeg_capabilities(self, ffmpeg: str) -> FfmpegCapabilities:
        if self._capabilities is None:
            try:
                self._capabilities = query_capabilities(ffmpeg, self._runner)
            except FfmpegError as exc:
                raise Refused("ffmpeg_unusable", f"{exc}. {_FFMPEG_HINT}") from None
        return self._capabilities

    def _redactions(self, folder: OutputFolder, temp_dir: Optional[Path]) -> dict:
        redact = {str(folder.root): "<output>"}
        if self._workspace.root is not None:
            redact[str(self._workspace.root)] = "<workspace>"
        if self._settings.assets_root is not None:
            redact[str(self._settings.assets_root)] = "<assets>"
        if temp_dir is not None:
            redact[str(temp_dir)] = "<render-dir>"
        return redact


__all__ = ["Refused", "VideoToolBase", "sha256_file"]
