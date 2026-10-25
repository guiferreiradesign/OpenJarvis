"""Checks that a rendered file is what its plan asked for.

Pure functions over ``parse_ffprobe_output`` results, used twice:

* by ``media_render`` on the temp file **before** publishing it, so a broken
  render never receives a final name, and
* by ``media_validate`` on the published file, together with a full decode
  pass and a checksum comparison against the render record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Mapping

FRAME_RATE_TOLERANCE = 0.05


@dataclass(frozen=True)
class Expectations:
    width: int
    height: int
    frame_rate: float
    duration: float
    tolerance: float
    audio: bool
    subtitle_track: bool = False
    video_codec: str = "h264"
    audio_codec: str = "aac"
    pixel_format: str = "yuv420p"
    audio_sample_rate: int = 48000

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_plan_dict(cls, plan: Mapping[str, Any]) -> "Expectations":
        """Rebuild expectations from a render record's ``plan`` (untrusted)."""
        try:
            video = plan["video"]
            audio = plan["audio"]
            subtitles = plan.get("subtitles")
            values = cls(
                width=int(video["width"]),
                height=int(video["height"]),
                frame_rate=float(video["frame_rate"]),
                duration=float(plan["expected_duration_seconds"]),
                tolerance=float(plan["duration_tolerance_seconds"]),
                audio=audio["mode"] == "keep",
                subtitle_track=bool(subtitles) and subtitles.get("mode") == "embed",
                audio_sample_rate=int(audio.get("sample_rate", 48000)),
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ValueError("Render record is incomplete or malformed") from None
        if not (
            0 < values.width <= 8192
            and 0 < values.height <= 8192
            and 0 < values.frame_rate <= 240
            and 0 < values.duration <= 24 * 3600
            and 0 < values.tolerance <= 5
        ):
            raise ValueError("Render record has out-of-range values")
        return values


def make_check(name: str, passed: bool, expected: Any, actual: Any) -> Dict[str, Any]:
    """One entry of a check list."""
    return {
        "check": name,
        "passed": bool(passed),
        "expected": expected,
        "actual": actual,
    }


def check_media(
    meta: Mapping[str, Any], size_bytes: int, expected: Expectations
) -> List[Dict[str, Any]]:
    """Return one entry per check; the file is usable only if all pass."""
    checks = [make_check("non_empty", size_bytes > 0, "> 0 bytes", size_bytes)]
    status = meta.get("status")
    checks.append(make_check("probe", status == "ok", "ok", status))
    if status != "ok":
        return checks

    container = meta.get("container") or ""
    checks.append(
        make_check("container", "mp4" in container.split(","), "mp4", container)
    )

    duration = meta.get("duration_seconds")
    checks.append(
        make_check(
            "duration",
            duration is not None
            and abs(duration - expected.duration) <= expected.tolerance,
            f"{expected.duration:.3f}s ± {expected.tolerance:g}s",
            duration,
        )
    )

    # The container reports its longest stream, so a video track that stops
    # early (e.g. a truncated source, padded audio) must be checked on its own.
    for kind in ("video", "audio"):
        stream = meta.get(kind)
        if not stream or (kind == "audio" and not expected.audio):
            continue
        length = stream.get("duration_seconds")
        checks.append(
            make_check(
                f"{kind}_duration",
                length is not None
                and abs(length - expected.duration) <= expected.tolerance,
                f"{expected.duration:.3f}s ± {expected.tolerance:g}s",
                length,
            )
        )

    video = meta.get("video")
    checks.append(make_check("video_stream", video is not None, "present", bool(video)))
    if video:
        dims = (video.get("width"), video.get("height"))
        checks.append(
            make_check(
                "dimensions",
                dims == (expected.width, expected.height)
                and video.get("rotation", 0) == 0,
                f"{expected.width}x{expected.height}",
                f"{dims[0]}x{dims[1]}"
                + (f" rotated {video['rotation']}" if video.get("rotation") else ""),
            )
        )
        rate = video.get("frame_rate")
        checks.append(
            make_check(
                "frame_rate",
                rate is not None
                and abs(rate - expected.frame_rate) <= FRAME_RATE_TOLERANCE,
                expected.frame_rate,
                rate,
            )
        )
        checks.append(
            make_check(
                "video_codec",
                video.get("codec") == expected.video_codec,
                expected.video_codec,
                video.get("codec"),
            )
        )
        checks.append(
            make_check(
                "pixel_format",
                video.get("pixel_format") == expected.pixel_format,
                expected.pixel_format,
                video.get("pixel_format"),
            )
        )

    audio = meta.get("audio")
    if expected.audio:
        checks.append(
            make_check("audio_stream", audio is not None, "present", bool(audio))
        )
        if audio:
            checks.append(
                make_check(
                    "audio_codec",
                    audio.get("codec") == expected.audio_codec,
                    expected.audio_codec,
                    audio.get("codec"),
                )
            )
            checks.append(
                make_check(
                    "audio_sample_rate",
                    audio.get("sample_rate") == expected.audio_sample_rate,
                    expected.audio_sample_rate,
                    audio.get("sample_rate"),
                )
            )
    else:
        checks.append(make_check("audio_stream", audio is None, "absent", bool(audio)))

    subtitle_count = (meta.get("stream_counts") or {}).get("subtitle", 0)
    if expected.subtitle_track:
        checks.append(
            make_check("subtitle_track", subtitle_count >= 1, ">= 1", subtitle_count)
        )
    return checks


def all_passed(checks: List[Mapping[str, Any]]) -> bool:
    return bool(checks) and all(item["passed"] for item in checks)


def failed_names(checks: List[Mapping[str, Any]]) -> List[str]:
    return [item["check"] for item in checks if not item["passed"]]


__all__ = [
    "Expectations",
    "FRAME_RATE_TOLERANCE",
    "all_passed",
    "check_media",
    "failed_names",
    "make_check",
]
