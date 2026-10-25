"""The ``EditPlan``: a complete, validated description of one render.

A plan is plain JSON so that a person, a script or (in a later phase) an LLM
can write it.  :func:`parse_edit_plan` checks its *shape and values* without
touching the filesystem; the render tool then checks paths and media against
the workspace.  Unknown keys are errors at every level, so a typo can never be
silently ignored.

Example::

    {
      "version": 1,
      "output_name": "skyline_reel",
      "preset": "instagram_reel",
      "fit": "crop",
      "clips": [
        {"source": "Skyline/keynote.mp4", "start": "00:00:05.0", "end": "00:00:12.5"},
        {"source": "Skyline/panel.mov", "start": 30, "end": 41.2}
      ],
      "audio": {"mode": "keep"},
      "subtitles": {"source": "Skyline/captions.srt", "mode": "embed"},
      "branding": {"asset": "logo.png", "position": "top-right"}
    }

Clips are rendered in list order.  Times are seconds (number) or
``[[HH:]MM:]SS[.fff]`` strings, relative to each source file.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

PLAN_VERSION = 1

FIT_MODES = ("crop", "pad")
AUDIO_MODES = ("keep", "none")
SUBTITLE_MODES = ("embed", "burn")
BRANDING_POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right", "center")
X264_PRESETS = (
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
)
FRAME_RATES = (24, 25, 30, 50, 60)

MIN_CLIP_SECONDS = 0.1
MAX_TIMESTAMP_SECONDS = 24 * 3600.0
_OUTPUT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
_LANGUAGE = re.compile(r"[a-z]{3}")
_TIME_FIELD = re.compile(r"\d{1,6}")
_SECONDS_FIELD = re.compile(r"\d{1,6}(?:\.\d{1,6})?")


class PlanError(ValueError):
    """The plan is malformed or asks for something unsupported."""


# ---------------------------------------------------------------------------
# Output presets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputPreset:
    """Target format for a render.  All presets produce H.264/AAC in MP4."""

    name: str
    width: int
    height: int
    frame_rate: int
    crf: int = 20
    x264_preset: str = "medium"
    audio_bitrate_kbps: int = 128
    audio_sample_rate: int = 48000
    max_duration_seconds: float = 180.0

    @property
    def aspect_ratio(self) -> str:
        divisor = math.gcd(self.width, self.height)
        return f"{self.width // divisor}:{self.height // divisor}"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["aspect_ratio"] = self.aspect_ratio
        data["container"] = "mp4"
        data["video_codec"] = "h264"
        data["audio_codec"] = "aac"
        return data


BUILTIN_PRESETS: Dict[str, OutputPreset] = {
    # Instagram Reels: 9:16, 1080x1920, up to 3 minutes.
    "instagram_reel": OutputPreset(
        name="instagram_reel", width=1080, height=1920, frame_rate=30
    ),
}

# (minimum, maximum) for preset fields read from config.
PRESET_LIMITS: Dict[str, Tuple[float, float]] = {
    "width": (144, 4096),
    "height": (144, 4096),
    "frame_rate": (24, 60),
    "crf": (14, 32),
    "audio_bitrate_kbps": (64, 320),
    "max_duration_seconds": (1, 3600),
}


def preset_from_mapping(name: str, raw: Mapping[str, Any]) -> OutputPreset:
    """Build a custom preset from ``[jarvis.media_render.presets.<name>]``."""
    if not _OUTPUT_NAME.fullmatch(name):
        raise PlanError(f"Invalid preset name {name!r}")
    known = set(PRESET_LIMITS) | {"x264_preset"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise PlanError(f"Unknown keys in preset {name}: {', '.join(unknown)}")
    missing = [key for key in ("width", "height", "frame_rate") if key not in raw]
    if missing:
        raise PlanError(f"Preset {name} needs {', '.join(missing)}")
    values: Dict[str, Any] = {"name": name}
    for key, (low, high) in PRESET_LIMITS.items():
        if key not in raw:
            continue
        number = _number(raw[key], f"preset {name}.{key}")
        if key != "max_duration_seconds":
            number = _integer(number, f"preset {name}.{key}")
        if not low <= number <= high:
            raise PlanError(f"preset {name}.{key} must be between {low:g} and {high:g}")
        values[key] = number
    if values["width"] % 2 or values["height"] % 2:
        raise PlanError(f"preset {name} width and height must be even")
    if values["frame_rate"] not in FRAME_RATES:
        raise PlanError(f"preset {name}.frame_rate must be one of {FRAME_RATES}")
    if "x264_preset" in raw:
        values["x264_preset"] = _choice(
            raw["x264_preset"], X264_PRESETS, f"preset {name}.x264_preset"
        )
    if "max_duration_seconds" in values:
        values["max_duration_seconds"] = float(values["max_duration_seconds"])
    return OutputPreset(**values)


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    source: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class SubtitleSpec:
    source: str
    mode: str = "embed"
    language: str = "und"


@dataclass(frozen=True)
class BrandingSpec:
    asset: str
    position: str = "top-right"
    width_fraction: float = 0.2
    margin_px: int = 48
    opacity: float = 1.0


@dataclass(frozen=True)
class EditPlan:
    output_name: str
    preset: OutputPreset
    clips: Tuple[Clip, ...]
    fit: str = "crop"
    frame_rate: int = 30
    crf: int = 20
    x264_preset: str = "medium"
    audio_mode: str = "keep"
    audio_bitrate_kbps: int = 128
    subtitles: Optional[SubtitleSpec] = None
    branding: Optional[BrandingSpec] = None
    duration_tolerance_seconds: float = 0.25
    version: int = PLAN_VERSION

    @property
    def width(self) -> int:
        return self.preset.width

    @property
    def height(self) -> int:
        return self.preset.height

    @property
    def expected_duration(self) -> float:
        return round(sum(clip.duration for clip in self.clips), 6)

    def to_dict(self) -> Dict[str, Any]:
        """Normalised plan (times in seconds) for render records."""
        return {
            "version": self.version,
            "output_name": self.output_name,
            "preset": self.preset.name,
            "fit": self.fit,
            "clips": [asdict(clip) for clip in self.clips],
            "video": {
                "width": self.width,
                "height": self.height,
                "aspect_ratio": self.preset.aspect_ratio,
                "frame_rate": self.frame_rate,
                "codec": "h264",
                "crf": self.crf,
                "x264_preset": self.x264_preset,
                "pixel_format": "yuv420p",
                "color": {
                    "range": "tv",
                    "primaries": "bt709",
                    "transfer": "bt709",
                    "space": "bt709",
                },
            },
            "audio": {
                "mode": self.audio_mode,
                "codec": "aac",
                "bitrate_kbps": self.audio_bitrate_kbps,
                "sample_rate": self.preset.audio_sample_rate,
            },
            "subtitles": asdict(self.subtitles) if self.subtitles else None,
            "branding": asdict(self.branding) if self.branding else None,
            "duration_tolerance_seconds": self.duration_tolerance_seconds,
            "expected_duration_seconds": self.expected_duration,
        }


@dataclass(frozen=True)
class PlanLimits:
    """Configured limits a plan is checked against."""

    presets: Mapping[str, OutputPreset] = field(
        default_factory=lambda: dict(BUILTIN_PRESETS)
    )
    default_preset: str = "instagram_reel"
    max_clips: int = 20
    max_output_seconds: float = 600.0
    default_tolerance_seconds: float = 0.25


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanError(f"{name} must be a number")
    if not math.isfinite(value):
        raise PlanError(f"{name} must be finite")
    return value


def _integer(value: Any, name: str) -> int:
    number = _number(value, name)
    if not float(number).is_integer():
        raise PlanError(f"{name} must be a whole number")
    return int(number)


def _ranged(value: Any, name: str, low: float, high: float) -> float:
    number = _number(value, name)
    if not low <= number <= high:
        raise PlanError(f"{name} must be between {low:g} and {high:g}")
    return float(number)


def _choice(value: Any, options: Tuple, name: str) -> Any:
    if value not in options or isinstance(value, bool):
        listed = ", ".join(str(option) for option in options)
        raise PlanError(f"{name} must be one of: {listed}")
    return value


def _string(value: Any, name: str, max_length: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{name} must be a non-empty string")
    if len(value) > max_length:
        raise PlanError(f"{name} is too long")
    if any(ord(ch) < 32 for ch in value):
        raise PlanError(f"{name} contains control characters")
    return value.strip()


def _table(value: Any, name: str, known: Tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PlanError(f"{name} must be an object")
    unknown = sorted(set(value) - set(known))
    if unknown:
        raise PlanError(f"Unknown keys in {name}: {', '.join(map(str, unknown))}")
    return value


def parse_timestamp(value: Any, name: str = "timestamp") -> float:
    """Seconds from a number or a ``[[HH:]MM:]SS[.ffffff]`` string."""
    if isinstance(value, str):
        text = value.strip()
        parts = text.split(":")
        if not text or len(parts) > 3:
            raise PlanError(f"{name}: invalid timestamp {value!r}")
        *leading, seconds_text = parts
        if not _SECONDS_FIELD.fullmatch(seconds_text) or not all(
            _TIME_FIELD.fullmatch(p) for p in leading
        ):
            raise PlanError(f"{name}: invalid timestamp {value!r}")
        seconds = float(seconds_text)
        numbers = [int(p) for p in leading]
        if leading and seconds >= 60:
            raise PlanError(f"{name}: seconds must be below 60 in {value!r}")
        if len(numbers) == 2 and numbers[1] >= 60:
            raise PlanError(f"{name}: minutes must be below 60 in {value!r}")
        total = 0.0
        for number in numbers:
            total = total * 60 + number
        total = total * 60 + seconds if numbers else seconds
    else:
        total = float(_number(value, name))
    if total < 0:
        raise PlanError(f"{name} cannot be negative")
    if total > MAX_TIMESTAMP_SECONDS:
        raise PlanError(f"{name} is beyond 24 hours")
    return round(total, 6)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_PLAN_KEYS = (
    "version",
    "output_name",
    "preset",
    "clips",
    "fit",
    "video",
    "audio",
    "subtitles",
    "branding",
    "duration_tolerance_seconds",
)


def parse_edit_plan(data: Any, limits: Optional[PlanLimits] = None) -> EditPlan:
    """Validate a decoded JSON plan and return an :class:`EditPlan`.

    Raises :class:`PlanError` with a message suitable for the model/user.
    """
    limits = limits or PlanLimits()
    plan = _table(data, "plan", _PLAN_KEYS)

    version = plan.get("version", PLAN_VERSION)
    if isinstance(version, bool) or version != PLAN_VERSION:
        raise PlanError(f"version must be {PLAN_VERSION}")

    output_name = plan.get("output_name")
    if not isinstance(output_name, str) or not _OUTPUT_NAME.fullmatch(output_name):
        raise PlanError(
            "output_name must be 1-48 lowercase letters, digits, '_' or '-', "
            "starting with a letter or digit (e.g. 'skyline_reel')"
        )

    preset_name = plan.get("preset", limits.default_preset)
    if not isinstance(preset_name, str) or preset_name not in limits.presets:
        raise PlanError("preset must be one of: " + ", ".join(sorted(limits.presets)))
    preset = limits.presets[preset_name]

    raw_clips = plan.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise PlanError("clips must be a non-empty list")
    if len(raw_clips) > limits.max_clips:
        raise PlanError(f"At most {limits.max_clips} clips per plan")
    clips = tuple(_parse_clip(raw, index) for index, raw in enumerate(raw_clips))

    fit = _choice(plan.get("fit", "crop"), FIT_MODES, "fit")

    video = _table(plan.get("video", {}), "video", ("frame_rate", "crf", "x264_preset"))
    frame_rate = int(
        _choice(
            video.get("frame_rate", preset.frame_rate), FRAME_RATES, "video.frame_rate"
        )
    )
    crf = _integer(video.get("crf", preset.crf), "video.crf")
    if not 14 <= crf <= 32:
        raise PlanError("video.crf must be between 14 and 32")
    x264_preset = _choice(
        video.get("x264_preset", preset.x264_preset), X264_PRESETS, "video.x264_preset"
    )

    audio = _table(plan.get("audio", {}), "audio", ("mode", "bitrate_kbps"))
    audio_mode = _choice(audio.get("mode", "keep"), AUDIO_MODES, "audio.mode")
    audio_bitrate = _integer(
        audio.get("bitrate_kbps", preset.audio_bitrate_kbps), "audio.bitrate_kbps"
    )
    if not 64 <= audio_bitrate <= 320:
        raise PlanError("audio.bitrate_kbps must be between 64 and 320")

    subtitles = None
    if plan.get("subtitles") is not None:
        raw = _table(plan["subtitles"], "subtitles", ("source", "mode", "language"))
        source = _string(raw.get("source"), "subtitles.source")
        if not source.lower().endswith(".srt"):
            raise PlanError("subtitles.source must be an .srt file")
        language = raw.get("language", "und")
        if not isinstance(language, str) or not _LANGUAGE.fullmatch(language):
            raise PlanError("subtitles.language must be a 3-letter ISO 639-2 code")
        subtitles = SubtitleSpec(
            source=source,
            mode=_choice(raw.get("mode", "embed"), SUBTITLE_MODES, "subtitles.mode"),
            language=language,
        )

    branding = None
    if plan.get("branding") is not None:
        raw = _table(
            plan["branding"],
            "branding",
            ("asset", "position", "width_fraction", "margin_px", "opacity"),
        )
        asset = _string(raw.get("asset"), "branding.asset")
        if not asset.lower().endswith(".png"):
            raise PlanError("branding.asset must be a .png file")
        margin = _integer(raw.get("margin_px", 48), "branding.margin_px")
        if not 0 <= margin <= 400:
            raise PlanError("branding.margin_px must be between 0 and 400")
        branding = BrandingSpec(
            asset=asset,
            position=_choice(
                raw.get("position", "top-right"),
                BRANDING_POSITIONS,
                "branding.position",
            ),
            width_fraction=_ranged(
                raw.get("width_fraction", 0.2), "branding.width_fraction", 0.05, 0.5
            ),
            margin_px=margin,
            opacity=_ranged(raw.get("opacity", 1.0), "branding.opacity", 0.1, 1.0),
        )

    tolerance = _ranged(
        plan.get("duration_tolerance_seconds", limits.default_tolerance_seconds),
        "duration_tolerance_seconds",
        0.01,
        5.0,
    )

    result = EditPlan(
        output_name=output_name,
        preset=preset,
        clips=clips,
        fit=fit,
        frame_rate=frame_rate,
        crf=crf,
        x264_preset=x264_preset,
        audio_mode=audio_mode,
        audio_bitrate_kbps=audio_bitrate,
        subtitles=subtitles,
        branding=branding,
        duration_tolerance_seconds=tolerance,
    )
    total = result.expected_duration
    cap = min(preset.max_duration_seconds, limits.max_output_seconds)
    if total > cap:
        raise PlanError(
            f"Total duration {total:.3f}s exceeds the {cap:g}s limit for "
            f"preset {preset.name}"
        )
    return result


def _parse_clip(raw: Any, index: int) -> Clip:
    name = f"clips[{index}]"
    clip = _table(raw, name, ("source", "start", "end"))
    source = _string(clip.get("source"), f"{name}.source")
    if "start" not in clip or "end" not in clip:
        raise PlanError(f"{name} needs start and end")
    start = parse_timestamp(clip["start"], f"{name}.start")
    end = parse_timestamp(clip["end"], f"{name}.end")
    if end <= start:
        raise PlanError(f"{name}.end must be after start")
    if end - start < MIN_CLIP_SECONDS:
        raise PlanError(f"{name} is shorter than {MIN_CLIP_SECONDS}s")
    return Clip(source=source, start=start, end=end)


__all__ = [
    "AUDIO_MODES",
    "BRANDING_POSITIONS",
    "BUILTIN_PRESETS",
    "BrandingSpec",
    "Clip",
    "EditPlan",
    "FIT_MODES",
    "FRAME_RATES",
    "OutputPreset",
    "PLAN_VERSION",
    "PlanError",
    "PlanLimits",
    "SUBTITLE_MODES",
    "SubtitleSpec",
    "X264_PRESETS",
    "parse_edit_plan",
    "parse_timestamp",
    "preset_from_mapping",
]
