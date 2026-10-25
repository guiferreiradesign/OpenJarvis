"""``[jarvis.media_render]`` settings shared by the render and validate tools.

::

    [jarvis.media_render]
    output_root = "/Users/me/Movies/JARVIS-Renders"   # required, must exist
    assets_root = "/Users/me/Movies/JARVIS-Assets"    # optional, branding PNGs

The output folder is the **only** place these tools write.  It must be
separate from the source workspace (``[jarvis.media_workspace] root``): the
two may not be the same folder or contain each other.  This is checked by
device/inode, so a different spelling of the same folder is still caught.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from jarvis_ext._workspace import same_or_inside
from jarvis_ext.config import ExtensionConfigError
from jarvis_ext.video.plan import (
    BUILTIN_PRESETS,
    OutputPreset,
    PlanError,
    PlanLimits,
    preset_from_mapping,
)

CONFIG_SECTION = "media_render"

# (default, minimum, maximum) for numeric settings.
LIMITS: Dict[str, Tuple[float, float, float]] = {
    "render_timeout_seconds": (900.0, 10.0, 7200.0),
    "validate_timeout_seconds": (300.0, 10.0, 3600.0),
    "max_clips": (20, 1, 100),
    "max_output_seconds": (600.0, 1.0, 3600.0),
    "duration_tolerance_seconds": (0.25, 0.01, 5.0),
    "preview_height": (640, 144, 1080),
}


@dataclass(frozen=True)
class MediaRenderSettings:
    """Validated ``[jarvis.media_render]`` settings."""

    output_root: Optional[Path] = None
    assets_root: Optional[Path] = None
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    default_preset: str = "instagram_reel"
    require_confirmation: bool = False
    render_timeout_seconds: float = 900.0
    validate_timeout_seconds: float = 300.0
    max_clips: int = 20
    max_output_seconds: float = 600.0
    duration_tolerance_seconds: float = 0.25
    preview_height: int = 640
    presets: Mapping[str, OutputPreset] = field(
        default_factory=lambda: dict(BUILTIN_PRESETS)
    )

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "MediaRenderSettings":
        known = {
            "output_root",
            "assets_root",
            "ffmpeg_path",
            "ffprobe_path",
            "default_preset",
            "require_confirmation",
            "presets",
            *LIMITS,
        }
        unknown = sorted(set(section) - known)
        if unknown:
            raise ExtensionConfigError(
                f"Unknown [jarvis.{CONFIG_SECTION}] keys: {', '.join(unknown)}"
            )
        values: Dict[str, Any] = {}
        for key in ("output_root", "assets_root"):
            raw = section.get(key, "")
            if not isinstance(raw, str):
                raise ExtensionConfigError(f"media_render.{key} must be a string")
            values[key] = _validate_dir(key, raw) if raw.strip() else None
        if (
            values["output_root"] is not None
            and values["assets_root"] is not None
            and _overlap(values["output_root"], values["assets_root"])
        ):
            raise ExtensionConfigError(
                "media_render.assets_root and output_root must be separate folders"
            )

        for key in ("ffmpeg_path", "ffprobe_path", "default_preset"):
            raw = section.get(key, cls.__dataclass_fields__[key].default)
            if not isinstance(raw, str):
                raise ExtensionConfigError(f"media_render.{key} must be a string")
            values[key] = raw.strip()

        if "require_confirmation" in section:
            if not isinstance(section["require_confirmation"], bool):
                raise ExtensionConfigError(
                    "media_render.require_confirmation must be true or false"
                )
            values["require_confirmation"] = section["require_confirmation"]

        for key, (default, low, high) in LIMITS.items():
            raw = section.get(key, default)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise ExtensionConfigError(f"media_render.{key} must be a number")
            if isinstance(default, int) and not float(raw).is_integer():
                raise ExtensionConfigError(f"media_render.{key} must be an integer")
            if not math.isfinite(raw) or not low <= raw <= high:
                raise ExtensionConfigError(
                    f"media_render.{key} must be between {low:g} and {high:g}"
                )
            values[key] = int(raw) if isinstance(default, int) else float(raw)

        presets = dict(BUILTIN_PRESETS)
        raw_presets = section.get("presets", {})
        if not isinstance(raw_presets, Mapping):
            raise ExtensionConfigError("media_render.presets must be a table")
        for name, raw in raw_presets.items():
            if not isinstance(raw, Mapping):
                raise ExtensionConfigError(
                    f"media_render.presets.{name} must be a table"
                )
            if name in BUILTIN_PRESETS:
                raise ExtensionConfigError(
                    f"media_render.presets.{name} would replace a built-in preset"
                )
            try:
                presets[name] = preset_from_mapping(name, raw)
            except PlanError as exc:
                raise ExtensionConfigError(f"media_render: {exc}") from None
        values["presets"] = presets
        if values["default_preset"] not in presets:
            raise ExtensionConfigError(
                "media_render.default_preset must name a known preset"
            )
        return cls(**values)

    @property
    def plan_limits(self) -> PlanLimits:
        return PlanLimits(
            presets=self.presets,
            default_preset=self.default_preset,
            max_clips=self.max_clips,
            max_output_seconds=self.max_output_seconds,
            default_tolerance_seconds=self.duration_tolerance_seconds,
        )


def _validate_dir(key: str, raw: str) -> Path:
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ExtensionConfigError(f"media_render.{key} must be an absolute path")
    resolved = path.resolve()
    if resolved == Path(resolved.anchor):
        raise ExtensionConfigError(f"media_render.{key} cannot be a filesystem root")
    try:
        home = Path.home().resolve()
    except (KeyError, RuntimeError):
        home = None
    if home is not None and resolved == home:
        raise ExtensionConfigError(
            f"media_render.{key} cannot be your whole home directory; "
            "point it at a dedicated folder"
        )
    return resolved


def _overlap(a: Path, b: Path) -> bool:
    return same_or_inside(a, b) or same_or_inside(b, a)


def check_separation(output_root: Path, workspace_root: Optional[Path]) -> None:
    """Refuse an output folder that is, contains or lies inside the workspace.

    Called at run time (not only at config load) because folders can be
    created, moved or remounted after the config was read.
    """
    if workspace_root is not None and _overlap(output_root, workspace_root):
        raise ExtensionConfigError(
            "media_render.output_root must be outside the media workspace "
            "(and must not contain it)"
        )


__all__ = [
    "CONFIG_SECTION",
    "LIMITS",
    "MediaRenderSettings",
    "check_separation",
]
