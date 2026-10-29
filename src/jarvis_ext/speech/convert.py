"""Turn an uploaded recording into the 16 kHz mono WAV the local recogniser reads.

Browsers record WebM/Opus (Chrome, Edge), Ogg/Opus (Firefox) or MP4/AAC
(Safari); ``MlxWhisperBackend`` takes PCM WAV only.  ``prepare_for_transcription``
returns WAV bytes ready for ``backend.transcribe(..., format="wav")``:

* A valid PCM WAV is passed through untouched (no FFmpeg, no copy).
* WebM, Ogg and MP4 containers go through the installed ``ffmpeg``.
* Anything else is refused.  The format is decided from the file's first bytes,
  never from the file name or content type, which the client controls.

FFmpeg is run without a shell, with a fixed argument list, the demuxer forced to
the detected container (it never probes for other formats), only local files
allowed, a hard wall-clock timeout and a cap on the decoded length.  The upload
and the result live in a private temporary directory that is removed on every
path; nothing is kept.  Nothing here touches the network.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from jarvis_ext.speech.audio import WHISPER_SAMPLE_RATE, read_wav_info
from jarvis_ext.speech.errors import (
    SpeechInputError,
    SpeechResponseError,
    SpeechTimeoutError,
    SpeechUnavailableError,
)

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # a 2 minute Opus recording is about 1 MB
MAX_AUDIO_SECONDS = 120.0
CONVERT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = int(MAX_AUDIO_SECONDS * WHISPER_SAMPLE_RATE * 2) + 1_000_000

_NO_FFMPEG = (
    "ffmpeg is not installed, so browser recordings cannot be converted; "
    "install it (brew install ffmpeg) or send a WAV file"
)


class UnsupportedAudioFormat(SpeechInputError):
    """The upload is not a WAV, WebM, Ogg or MP4 recording."""


class AudioTooLarge(SpeechInputError):
    """The upload is bigger than the size limit."""


@dataclass(frozen=True)
class PreparedAudio:
    """WAV bytes for the recogniser, and whether FFmpeg was needed."""

    wav: bytes
    converted: bool
    source_format: str  # "wav", "webm", "ogg" or "mp4"


def detect_format(data: bytes) -> Optional[str]:
    """Name the container from its leading bytes, or None if unsupported."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"\x1a\x45\xdf\xa3":  # EBML: WebM (or Matroska)
        return "webm"
    if data[:4] == b"OggS":
        return "ogg"
    if data[4:8] == b"ftyp":
        return "mp4"
    return None


# ffmpeg demuxer name for each supported container.
_DEMUXER = {"webm": "matroska,webm", "ogg": "ogg", "mp4": "mov,mp4,m4a,3gp,3g2,mj2"}


def prepare_for_transcription(
    data: bytes,
    *,
    max_bytes: int = MAX_UPLOAD_BYTES,
    max_seconds: float = MAX_AUDIO_SECONDS,
    timeout_seconds: float = CONVERT_TIMEOUT_SECONDS,
    ffmpeg: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[bytes]"] = subprocess.run,
) -> PreparedAudio:
    """Return *data* as WAV for the local recogniser.

    Raises :class:`SpeechInputError` (empty, too large, malformed or silent
    audio), :class:`UnsupportedAudioFormat`, :class:`SpeechTimeoutError`,
    :class:`SpeechUnavailableError` (no FFmpeg) or
    :class:`SpeechResponseError` (FFmpeg failed on audio that looked valid).
    """
    if not data:
        raise SpeechInputError("The recording is empty")
    if len(data) > max_bytes:
        raise AudioTooLarge(
            f"The recording is larger than the {max_bytes // (1024 * 1024)} MB limit"
        )
    kind = detect_format(data)
    if kind is None:
        raise UnsupportedAudioFormat(
            "Unsupported audio format; send a WAV, WebM, Ogg or MP4 recording"
        )
    if kind == "wav":
        info = read_wav_info(data)  # SpeechInputError if the header is broken
        if info.frames == 0:
            raise SpeechInputError("The recording contains no audio")
        return PreparedAudio(wav=data, converted=False, source_format="wav")

    exe = ffmpeg or which("ffmpeg")
    if not exe:
        raise SpeechUnavailableError(_NO_FFMPEG)

    with tempfile.TemporaryDirectory(prefix="jarvis-stt-") as tmp:  # mode 0700
        source = Path(tmp) / "in.bin"
        target = Path(tmp) / "out.wav"
        source.write_bytes(data)
        command = [
            exe,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-f",
            _DEMUXER[kind],
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
            "-sn",
            "-dn",
            "-t",
            f"{max_seconds + 1:g}",
            "-ac",
            "1",
            "-ar",
            str(WHISPER_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(target),
        ]
        try:
            completed = run(  # no shell; argv list only
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SpeechTimeoutError(
                f"Converting the recording took longer than {timeout_seconds:g} s"
            ) from exc
        except OSError as exc:
            raise SpeechUnavailableError(_NO_FFMPEG) from exc
        if completed.returncode != 0 or not target.exists():
            logger.warning(
                "ffmpeg could not convert a %s upload: %s",
                kind,
                (completed.stderr or b"")[-300:].decode("utf-8", "replace").strip(),
            )
            raise SpeechInputError(
                "The recording could not be decoded; it may be damaged or "
                "contain no audio"
            )
        if target.stat().st_size > MAX_OUTPUT_BYTES:
            raise SpeechResponseError("The converted recording is unexpectedly large")
        wav = target.read_bytes()
    try:
        info = read_wav_info(wav)
    except SpeechInputError as exc:
        raise SpeechResponseError("Conversion produced unreadable audio") from exc
    if info.frames == 0:
        raise SpeechInputError("The recording contains no audio")
    if info.duration_seconds > max_seconds:
        raise SpeechInputError(f"The recording is longer than {max_seconds:g} s")
    return PreparedAudio(wav=wav, converted=True, source_format=kind)


__all__ = [
    "CONVERT_TIMEOUT_SECONDS",
    "MAX_AUDIO_SECONDS",
    "MAX_UPLOAD_BYTES",
    "AudioTooLarge",
    "PreparedAudio",
    "UnsupportedAudioFormat",
    "detect_format",
    "prepare_for_transcription",
]
