"""Decode recorded WAV audio into the waveform Whisper expects, in process.

Whisper models take mono float32 samples at 16 kHz.  ``mlx_whisper`` can read
files itself, but it does so by starting an ``ffmpeg`` subprocess for every
call; decoding here keeps each utterance inside the running process.

Only uncompressed PCM WAV is accepted (8, 16, 24 or 32 bit, any channel count
and sample rate), which is what microphones and the Voicebox service produce.
Channels are averaged; other rates are resampled with a polyphase filter
(``scipy.signal.resample_poly``; SciPy is already a dependency of
``mlx-whisper``).  Integer scaling is exact, so 16 kHz mono input reaches the
model unchanged apart from the conversion to float.
"""

from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass
from typing import Any

from jarvis_ext.speech.errors import SpeechInputError

WHISPER_SAMPLE_RATE = 16000


@dataclass(frozen=True)
class WavInfo:
    """Header facts about a WAV recording."""

    channels: int
    sample_width: int  # bytes per sample
    sample_rate: int
    frames: int

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0


def read_wav_info(data: bytes) -> WavInfo:
    """Return the header of a PCM WAV file, or raise :class:`SpeechInputError`."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            return WavInfo(
                channels=wav.getnchannels(),
                sample_width=wav.getsampwidth(),
                sample_rate=wav.getframerate(),
                frames=wav.getnframes(),
            )
    except (wave.Error, EOFError) as exc:
        raise SpeechInputError("Audio must be an uncompressed PCM WAV file") from exc


def decode_wav(data: bytes, *, max_seconds: float = math.inf) -> Any:
    """Return *data* as a mono float32 NumPy array at 16 kHz.

    Raises :class:`SpeechInputError` for anything that is not a usable PCM
    WAV recording, or one longer than *max_seconds*.
    """
    import numpy as np

    if not data:
        raise SpeechInputError("Audio is empty")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
            if rate <= 0 or channels <= 0:
                raise SpeechInputError("Audio has an invalid WAV header")
            if frames / rate > max_seconds:
                raise SpeechInputError(
                    f"Audio is longer than the {max_seconds:g} s limit"
                )
            raw = wav.readframes(frames)
    except (wave.Error, EOFError) as exc:
        raise SpeechInputError("Audio must be an uncompressed PCM WAV file") from exc

    usable = len(raw) - len(raw) % (width * channels)
    raw = raw[:usable]
    if not raw:
        raise SpeechInputError("Audio contains no samples")

    if width == 1:  # 8-bit WAV is unsigned
        samples = (
            np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0
        ) / 128.0
    elif width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        triplets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = triplets[:, 0] | (triplets[:, 1] << 8) | (triplets[:, 2] << 16)
        values = np.where(values >= 1 << 23, values - (1 << 24), values)
        samples = values.astype(np.float32) / float(1 << 23)
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / float(1 << 31)
    else:
        raise SpeechInputError(f"Unsupported WAV sample width: {width} bytes")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1, dtype=np.float32)
    if rate != WHISPER_SAMPLE_RATE:
        samples = _resample(samples, rate, WHISPER_SAMPLE_RATE)
    return np.ascontiguousarray(samples, dtype=np.float32)


def _resample(samples: Any, rate: int, target: int) -> Any:
    try:
        from scipy.signal import resample_poly
    except ImportError as exc:
        raise SpeechInputError(
            f"Audio at {rate} Hz needs SciPy to resample to {target} Hz; "
            "install the speech extra or record at 16 kHz"
        ) from exc
    common = math.gcd(rate, target)
    return resample_poly(samples, target // common, rate // common).astype("float32")


__all__ = ["WHISPER_SAMPLE_RATE", "WavInfo", "decode_wav", "read_wav_info"]
