"""Record one utterance from a local microphone with FFmpeg (macOS AVFoundation).

``record_utterance`` captures 16 kHz mono 16-bit WAV, the format the local
recogniser reads, and returns its bytes.  It is push-to-talk: recording starts
when called and ends when ``stop_requested()`` turns true (the CLI uses the
Enter key), when ``max_seconds`` is reached, or on cancellation.  Nothing
listens in the background.

The microphone is found by name, never by a fixed index: on this Mac index 0
is a virtual "ZoomAudioDevice", and the real microphone is index 1 (the order
changes with the installed apps).  ``list_audio_devices`` reads FFmpeg's own
device list, which only enumerates devices and does not open the microphone.

Safety: FFmpeg runs without a shell with an argument list built from an
integer device index; the recording is written to a private temporary
directory that is deleted on success, failure, timeout and Ctrl-C; the audio
never leaves the process and nothing here touches the network.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional

from jarvis_ext.speech.audio import WHISPER_SAMPLE_RATE, read_wav_info
from jarvis_ext.speech.errors import (
    SpeechError,
    SpeechInputError,
    SpeechTimeoutError,
    SpeechUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_SECONDS = 30.0
MAX_ALLOWED_SECONDS = 120.0
START_TIMEOUT_SECONDS = 10.0  # ffmpeg must be recording by then
STOP_GRACE_SECONDS = 5.0  # time ffmpeg gets to finish the file after "q"
LIST_TIMEOUT_SECONDS = 10.0
# Below this RMS (fraction of full scale, about -54 dBFS) nobody was speaking.
SILENCE_RMS = 0.002
POLL_SECONDS = 0.05

_NO_FFMPEG = "ffmpeg is not installed; install it with 'brew install ffmpeg'"
_PERMISSION_HELP = (
    "macOS blocked microphone access. Open System Settings > Privacy & "
    "Security > Microphone, allow the app you run JARVIS from (Terminal, "
    "iTerm, Claude ...), then try again"
)
_DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")
_PERMISSION_WORDS = re.compile(
    r"not authori[sz]ed|permission|denied|privacy|tcc", re.IGNORECASE
)


class CaptureError(SpeechError):
    """Recording failed (FFmpeg error, no output, device trouble)."""


class MicrophonePermissionError(SpeechUnavailableError):
    """macOS did not allow microphone access."""


class NoMicrophoneError(SpeechUnavailableError):
    """No usable microphone was found."""


class SilentRecordingError(SpeechInputError):
    """The recording holds no speech."""


class CaptureCancelled(SpeechError):
    """The user cancelled (Ctrl-C) before the recording was complete."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str


@dataclass(frozen=True)
class Recording:
    wav: bytes
    device: AudioDevice
    seconds: float
    rms: float  # 0..1 of full scale


def find_ffmpeg(
    configured: str = "", which: Callable[[str], Optional[str]] = shutil.which
) -> str:
    exe = which(configured or "ffmpeg")
    if not exe:
        raise SpeechUnavailableError(_NO_FFMPEG)
    return exe


def list_audio_devices(
    ffmpeg: str,
    *,
    run: Callable[..., "subprocess.CompletedProcess[bytes]"] = subprocess.run,
) -> List[AudioDevice]:
    """AVFoundation audio input devices, as FFmpeg numbers them right now."""
    try:
        done = run(
            [ffmpeg, "-hide_banner", "-nostdin", "-f", "avfoundation"]
            + ["-list_devices", "true", "-i", ""],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=LIST_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CaptureError("Listing audio devices timed out") from exc
    except OSError as exc:
        raise SpeechUnavailableError(_NO_FFMPEG) from exc
    return parse_audio_devices((done.stderr or b"").decode("utf-8", "replace"))


def parse_audio_devices(text: str) -> List[AudioDevice]:
    devices: List[AudioDevice] = []
    in_audio = False
    for line in text.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if in_audio:
            match = _DEVICE_LINE.search(line)
            if match:
                devices.append(AudioDevice(int(match.group(1)), match.group(2)))
    return devices


def select_device(devices: List[AudioDevice], preference: str = "") -> AudioDevice:
    """Pick the microphone: *preference* (a name fragment or index), else the
    first device whose name says "microphone".  Never just index 0."""
    if not devices:
        raise NoMicrophoneError(
            "No audio input devices were found; connect a microphone"
        )
    wanted = (preference or "").strip()
    if wanted:
        if wanted.isdigit():
            for device in devices:
                if device.index == int(wanted):
                    return device
        else:
            folded = wanted.casefold()
            for device in devices:
                if folded in device.name.casefold():
                    return device
        names = ", ".join(f"[{d.index}] {d.name}" for d in devices)
        raise NoMicrophoneError(f"No microphone matches '{wanted}'. Found: {names}")
    for device in devices:
        if "microphone" in device.name.casefold():
            return device
    names = ", ".join(f"[{d.index}] {d.name}" for d in devices)
    raise NoMicrophoneError(
        f"No built-in microphone was found ({names}); choose one with --device"
    )


def measure_rms(wav: bytes) -> float:
    """RMS level of 16-bit WAV *wav* as a fraction of full scale."""
    import numpy as np

    info = read_wav_info(wav)
    if info.frames == 0:
        return 0.0
    import io
    import wave

    with wave.open(io.BytesIO(wav), "rb") as handle:
        raw = handle.readframes(handle.getnframes())
    samples = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2")
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean((samples.astype(np.float64) / 32768.0) ** 2)))


def record_utterance(
    *,
    device_preference: str = "",
    max_seconds: float = DEFAULT_MAX_SECONDS,
    stop_requested: Callable[[], bool] = lambda: False,
    ffmpeg: Optional[str] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    run: Callable[..., "subprocess.CompletedProcess[bytes]"] = subprocess.run,
    popen: Callable[..., Any] = subprocess.Popen,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_start: Optional[Callable[[AudioDevice], None]] = None,
) -> Recording:
    """Record from the microphone and return 16 kHz mono WAV.

    Raises :class:`SpeechUnavailableError` (no FFmpeg),
    :class:`NoMicrophoneError`, :class:`MicrophonePermissionError`,
    :class:`CaptureError`, :class:`SpeechTimeoutError` (FFmpeg never started
    or never finished), :class:`SilentRecordingError` and
    :class:`CaptureCancelled`.
    """
    if not 0 < max_seconds <= MAX_ALLOWED_SECONDS:
        raise SpeechInputError(
            f"max_seconds must be between 0 and {MAX_ALLOWED_SECONDS:g}"
        )
    exe = ffmpeg or find_ffmpeg(which=which)
    device = select_device(list_audio_devices(exe, run=run), device_preference)

    with tempfile.TemporaryDirectory(prefix="jarvis-mic-") as tmp:  # mode 0700
        target = Path(tmp) / "utterance.wav"
        command = [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "avfoundation",
            "-i",
            f":{int(device.index)}",
            "-t",
            f"{max_seconds:g}",
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
            proc = popen(  # no shell; argv list only
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise SpeechUnavailableError(_NO_FFMPEG) from exc

        started = clock()
        announced = False
        try:
            while proc.poll() is None:
                elapsed = clock() - started
                if not announced and (target.exists() or elapsed > 0.3):
                    announced = True
                    if on_start is not None:
                        on_start(device)
                if not target.exists() and elapsed > START_TIMEOUT_SECONDS:
                    _stop(proc, sleep, clock)
                    raise SpeechTimeoutError(
                        "The microphone did not start recording in time"
                    )
                if elapsed > max_seconds + STOP_GRACE_SECONDS + START_TIMEOUT_SECONDS:
                    _stop(proc, sleep, clock)
                    raise SpeechTimeoutError("Recording did not finish in time")
                if stop_requested():
                    _stop(proc, sleep, clock)
                    break
                sleep(POLL_SECONDS)
        except KeyboardInterrupt as exc:
            _stop(proc, sleep, clock, hard=True)
            raise CaptureCancelled("Recording cancelled") from exc
        except BaseException:
            _stop(proc, sleep, clock, hard=True)
            raise

        stderr = _read_stderr(proc)
        if not target.exists() or target.stat().st_size == 0:
            raise _capture_failure(stderr, proc.returncode)
        wav = target.read_bytes()

    try:
        info = read_wav_info(wav)
    except SpeechInputError as exc:
        if _PERMISSION_WORDS.search(stderr):
            raise MicrophonePermissionError(_PERMISSION_HELP) from exc
        raise CaptureError("The microphone produced an unreadable recording") from exc
    if info.frames == 0:
        if _PERMISSION_WORDS.search(stderr):
            raise MicrophonePermissionError(_PERMISSION_HELP)
        raise SilentRecordingError("The recording is empty; nothing was captured")
    rms = measure_rms(wav)
    if rms == 0.0:
        raise SilentRecordingError(
            "The recording is completely silent. The microphone may be muted or "
            f"blocked. {_PERMISSION_HELP}"
        )
    if rms < SILENCE_RMS:
        raise SilentRecordingError("No speech was heard; try again, closer to the mic")
    return Recording(
        wav=wav, device=device, seconds=info.duration_seconds, rms=round(rms, 5)
    )


def _stop(
    proc: Any,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
    *,
    hard: bool = False,
) -> None:
    """Ask FFmpeg to finish the file ("q"); terminate it if it does not."""
    if proc.poll() is not None:
        return
    if not hard:
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        deadline = clock() + STOP_GRACE_SECONDS
        while proc.poll() is None and clock() < deadline:
            sleep(POLL_SECONDS)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


def _read_stderr(proc: Any) -> str:
    try:
        data = proc.stderr.read() if proc.stderr else b""
    except (OSError, ValueError):
        data = b""
    return (data or b"").decode("utf-8", "replace")[-500:].strip()


def _capture_failure(stderr: str, returncode: Optional[int]) -> SpeechError:
    if _PERMISSION_WORDS.search(stderr):
        return MicrophonePermissionError(_PERMISSION_HELP)
    logger.warning("ffmpeg capture failed (%s): %s", returncode, stderr)
    detail = " ".join(stderr.split())[:160]
    return CaptureError(
        "Recording failed" + (f": {detail}" if detail else "") + ". If macOS asked "
        "for microphone access, allow it and try again"
    )


__all__ = [
    "AudioDevice",
    "CaptureCancelled",
    "CaptureError",
    "MicrophonePermissionError",
    "NoMicrophoneError",
    "Recording",
    "SilentRecordingError",
    "find_ffmpeg",
    "list_audio_devices",
    "measure_rms",
    "parse_audio_devices",
    "record_utterance",
    "select_device",
]
