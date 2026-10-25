"""SRT subtitles: strict parsing, sanitising, and a transcription hook.

Supplied ``.srt`` files are untrusted.  They are parsed here and rewritten as
clean SRT before FFmpeg sees them, so FFmpeg never reads the user's file
directly and styling/override codes (``{\\...}`` blocks, HTML-like tags) that
libass would interpret are removed.  Cue times are relative to the
**rendered output**, not to any source clip.

Automatic captions are not part of the render pipeline yet.  The extension
point is :func:`transcribe_to_srt`: give it any OpenJarvis
:class:`~openjarvis.speech._stubs.SpeechBackend` (for example the local
``faster-whisper`` backend once its model is installed) and audio bytes, and
it returns SRT text that can be saved to the workspace and referenced from a
plan's ``subtitles.source``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

MAX_SUBTITLE_BYTES = 1024 * 1024
MAX_CUES = 5000
MAX_LINES_PER_CUE = 4
MAX_LINE_CHARS = 200

_TIMING = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})(?:\s+.*)?"
)
_OVERRIDE = re.compile(r"\{[^}]*\}")
_TAG = re.compile(r"</?[A-Za-z][^>]{0,40}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class SubtitleError(ValueError):
    """The subtitle file cannot be used."""


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def _time(h: str, m: str, s: str, ms: str) -> float:
    minutes, seconds = int(m), int(s)
    if minutes >= 60 or seconds >= 60:
        raise ValueError("minutes/seconds out of range")
    return int(h) * 3600 + minutes * 60 + seconds + int(ms.ljust(3, "0")) / 1000.0


def clean_text(text: str) -> str:
    """Plain text only: no override blocks, tags or control characters."""
    text = _OVERRIDE.sub("", text)
    text = _TAG.sub("", text)
    text = _CONTROL.sub("", text)
    lines = [line.strip()[:MAX_LINE_CHARS] for line in text.split("\n")]
    return "\n".join(line for line in lines if line)[: MAX_LINE_CHARS * 4]


def parse_srt(data: bytes) -> List[Cue]:
    """Parse SRT bytes (UTF-8, optional BOM).  Raises :class:`SubtitleError`."""
    if len(data) > MAX_SUBTITLE_BYTES:
        raise SubtitleError("Subtitle file is larger than 1 MB")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SubtitleError("Subtitle file is not valid UTF-8") from None
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cues: List[Cue] = []
    for number, block in enumerate(re.split(r"\n[ \t]*\n", text.strip()), 1):
        lines = [line for line in block.split("\n")]
        if not block.strip():
            continue
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            raise SubtitleError(f"Subtitle block {number} has no timing line")
        match = _TIMING.fullmatch(lines[0].strip())
        if not match:
            raise SubtitleError(f"Subtitle block {number} has an invalid timing line")
        try:
            start = _time(*match.groups()[:4])
            end = _time(*match.groups()[4:])
        except ValueError:
            raise SubtitleError(
                f"Subtitle block {number} has an invalid time"
            ) from None
        if end <= start:
            raise SubtitleError(f"Subtitle block {number} ends before it starts")
        body = clean_text("\n".join(lines[1:]))
        if not body:
            continue
        body = "\n".join(body.split("\n")[:MAX_LINES_PER_CUE])
        cues.append(Cue(start=start, end=end, text=body))
        if len(cues) > MAX_CUES:
            raise SubtitleError(f"More than {MAX_CUES} subtitle cues")
    if not cues:
        raise SubtitleError("Subtitle file contains no cues")
    return cues


def fit_to_duration(cues: Iterable[Cue], duration: float) -> Tuple[List[Cue], int]:
    """Drop cues starting after *duration* and trim ones running past it."""
    kept: List[Cue] = []
    dropped = 0
    for cue in cues:
        if cue.start >= duration:
            dropped += 1
            continue
        kept.append(Cue(cue.start, min(cue.end, duration), cue.text))
    return kept, dropped


def _stamp(seconds: float) -> str:
    millis = int(round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def to_srt(cues: Iterable[Cue]) -> str:
    blocks = [
        f"{index}\n{_stamp(cue.start)} --> {_stamp(cue.end)}\n{cue.text}\n"
        for index, cue in enumerate(cues, 1)
    ]
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# Transcription extension point
# ---------------------------------------------------------------------------


def segments_to_cues(segments: Iterable[Any]) -> List[Cue]:
    """Convert OpenJarvis speech ``Segment`` objects (text/start/end) to cues."""
    cues: List[Cue] = []
    for segment in segments:
        text = clean_text(str(getattr(segment, "text", "") or ""))
        start = float(getattr(segment, "start", 0.0) or 0.0)
        end = float(getattr(segment, "end", 0.0) or 0.0)
        if text and end > start >= 0:
            cues.append(Cue(start, end, text))
    return cues


def transcribe_to_srt(
    backend: Any,
    audio: bytes,
    *,
    format: str = "wav",
    language: Optional[str] = None,
) -> str:
    """Transcribe *audio* with an OpenJarvis ``SpeechBackend`` and return SRT.

    This does not download models or call paid services by itself; whatever
    *backend* is passed decides that.  Raises :class:`SubtitleError` when the
    backend returns no timed segments.
    """
    result = backend.transcribe(audio, format=format, language=language)
    cues = segments_to_cues(getattr(result, "segments", []) or [])
    if not cues:
        raise SubtitleError("The speech backend returned no timed segments")
    return to_srt(cues)


__all__ = [
    "Cue",
    "MAX_SUBTITLE_BYTES",
    "SubtitleError",
    "clean_text",
    "fit_to_duration",
    "parse_srt",
    "segments_to_cues",
    "to_srt",
    "transcribe_to_srt",
]
