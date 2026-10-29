"""Tests for converting browser recordings to WAV (``jarvis_ext.speech.convert``).

FFmpeg is faked except in the one test that is skipped when it is not installed.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, List

import pytest

from jarvis_ext.speech import convert
from jarvis_ext.speech.convert import (
    AudioTooLarge,
    UnsupportedAudioFormat,
    detect_format,
    prepare_for_transcription,
)
from jarvis_ext.speech.errors import (
    SpeechInputError,
    SpeechResponseError,
    SpeechTimeoutError,
    SpeechUnavailableError,
)

WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
OGG = b"OggS" + b"\x00" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def wav_bytes(seconds: float = 0.2, rate: int = 16000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * int(rate * seconds) * channels)
    return buf.getvalue()


class FakeFfmpeg:
    """Stands in for subprocess.run; writes the output file like FFmpeg would."""

    def __init__(self, *, output: bytes | None = None, returncode: int = 0) -> None:
        self.output = wav_bytes() if output is None else output
        self.returncode = returncode
        self.calls: List[dict[str, Any]] = []
        self.dirs: List[Path] = []
        self.exc: Exception | None = None

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        source, target = Path(command[command.index("-i") + 1]), Path(command[-1])
        self.dirs.append(source.parent)
        assert source.read_bytes()  # the upload was written for FFmpeg
        if self.exc is not None:
            raise self.exc
        if self.returncode == 0:
            target.write_bytes(self.output)
        return subprocess.CompletedProcess(
            command, self.returncode, b"", b"Invalid data found when processing input"
        )


def prepare(data: bytes, fake: FakeFfmpeg, **kwargs: Any):
    return prepare_for_transcription(
        data, ffmpeg="/usr/bin/ffmpeg", run=fake, **kwargs
    )


class TestDetect:
    @pytest.mark.parametrize(
        ("data", "kind"),
        [
            (wav_bytes(), "wav"),
            (WEBM, "webm"),
            (OGG, "ogg"),
            (MP4, "mp4"),
            (b"ID3\x04mp3", None),
            (b"%PDF-1.7", None),
            (b"", None),
        ],
    )
    def test_format_comes_from_the_bytes(self, data, kind):
        assert detect_format(data) == kind


class TestWavPassThrough:
    def test_valid_wav_is_returned_unchanged_without_ffmpeg(self):
        data = wav_bytes(rate=44100, channels=2)
        fake = FakeFfmpeg()
        result = prepare(data, fake)
        assert result.wav is data
        assert not result.converted and result.source_format == "wav"
        assert fake.calls == []

    def test_does_not_need_ffmpeg_to_be_installed(self):
        result = prepare_for_transcription(wav_bytes(), which=lambda name: None)
        assert result.wav and not result.converted

    def test_broken_wav_header_is_rejected(self):
        with pytest.raises(SpeechInputError):
            prepare(b"RIFF\x00\x00\x00\x00WAVEjunkjunk", FakeFfmpeg())

    def test_wav_with_no_samples_is_rejected(self):
        with pytest.raises(SpeechInputError, match="no audio"):
            prepare(wav_bytes(seconds=0), FakeFfmpeg())


class TestConversion:
    @pytest.mark.parametrize(("data", "kind"), [(WEBM, "webm"), (OGG, "ogg"), (MP4, "mp4")])
    def test_browser_formats_are_converted(self, data, kind):
        fake = FakeFfmpeg(output=wav_bytes(seconds=1.0))
        result = prepare(data, fake)
        assert result.converted and result.source_format == kind
        info = wave.open(io.BytesIO(result.wav))
        assert (info.getframerate(), info.getnchannels(), info.getsampwidth()) == (
            16000,
            1,
            2,
        )

    def test_ffmpeg_is_called_safely(self):
        fake = FakeFfmpeg()
        prepare(WEBM, fake, timeout_seconds=7)
        (call,) = fake.calls
        command = call["command"]
        assert isinstance(command, list) and command[0] == "/usr/bin/ffmpeg"
        assert call.get("shell") is None  # never a shell
        assert call["timeout"] == 7 and call["stdin"] == subprocess.DEVNULL
        pairs = dict(zip(command, command[1:]))
        assert pairs["-ar"] == "16000" and pairs["-ac"] == "1"
        assert pairs["-c:a"] == "pcm_s16le" and pairs["-protocol_whitelist"] == "file"
        assert pairs["-f"] in ("matroska,webm", "wav")  # forced demuxer, wav out
        assert "-nostdin" in command and "-map" in command
        assert not any(part.startswith(("http", "tcp", "pipe")) for part in command)

    def test_temp_files_are_removed_after_success(self):
        fake = FakeFfmpeg()
        prepare(WEBM, fake)
        assert fake.dirs and not any(d.exists() for d in fake.dirs)

    @pytest.mark.parametrize(
        "setup",
        [
            {"returncode": 1},
            {"output": b"not a wav"},
        ],
    )
    def test_temp_files_are_removed_after_failure(self, setup):
        fake = FakeFfmpeg(**setup)
        with pytest.raises((SpeechInputError, SpeechResponseError)):
            prepare(WEBM, fake)
        assert not any(d.exists() for d in fake.dirs)

    def test_temp_files_are_removed_after_timeout(self):
        fake = FakeFfmpeg()
        fake.exc = subprocess.TimeoutExpired("ffmpeg", 1)
        with pytest.raises(SpeechTimeoutError, match="longer than"):
            prepare(WEBM, fake, timeout_seconds=1)
        assert not any(d.exists() for d in fake.dirs)

    def test_damaged_audio_is_a_clear_input_error(self):
        with pytest.raises(SpeechInputError, match="could not be decoded"):
            prepare(WEBM, FakeFfmpeg(returncode=1))

    def test_unreadable_output_is_a_response_error(self):
        with pytest.raises(SpeechResponseError):
            prepare(WEBM, FakeFfmpeg(output=b"garbage"))

    def test_empty_output_means_no_audio(self):
        with pytest.raises(SpeechInputError, match="no audio"):
            prepare(WEBM, FakeFfmpeg(output=wav_bytes(seconds=0)))

    def test_over_long_output_is_refused(self):
        with pytest.raises(SpeechInputError, match="longer than"):
            prepare(WEBM, FakeFfmpeg(output=wav_bytes(seconds=3)), max_seconds=1)

    def test_missing_ffmpeg_is_unavailable(self):
        with pytest.raises(SpeechUnavailableError, match="ffmpeg is not installed"):
            prepare_for_transcription(WEBM, which=lambda name: None)

    def test_ffmpeg_that_cannot_start_is_unavailable(self):
        fake = FakeFfmpeg()
        fake.exc = PermissionError("denied")
        with pytest.raises(SpeechUnavailableError):
            prepare(WEBM, fake)


class TestRefusals:
    def test_empty_recording(self):
        with pytest.raises(SpeechInputError, match="empty"):
            prepare(b"", FakeFfmpeg())

    @pytest.mark.parametrize("data", [b"ID3\x04mp3data", b"%PDF-1.7 ...", b"hello"])
    def test_unsupported_formats(self, data):
        fake = FakeFfmpeg()
        with pytest.raises(UnsupportedAudioFormat):
            prepare(data, fake)
        assert fake.calls == []

    def test_name_and_content_type_are_not_trusted(self):
        # There is no filename parameter at all: only the bytes decide.
        with pytest.raises(UnsupportedAudioFormat):
            prepare(b"MZ\x90\x00 not audio even if called a.wav", FakeFfmpeg())

    def test_oversized_upload_is_refused_before_ffmpeg(self):
        fake = FakeFfmpeg()
        with pytest.raises(AudioTooLarge):
            prepare(WEBM + b"\x00" * 2048, fake, max_bytes=1024)
        assert fake.calls == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_converts_a_generated_webm(tmp_path):
    """One real FFmpeg run on a tone it generates itself (no network, no model)."""
    source = tmp_path / "tone.webm"
    made = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=1:sample_rate=48000", "-ac", "2",
            "-c:a", "libopus", str(source),
        ],
        capture_output=True,
        timeout=30,
    )
    if made.returncode != 0:
        pytest.skip("this ffmpeg has no libopus encoder")
    before = set(Path(convert.tempfile.gettempdir()).glob("jarvis-stt-*"))
    result = prepare_for_transcription(source.read_bytes())
    after = set(Path(convert.tempfile.gettempdir()).glob("jarvis-stt-*"))
    assert result.converted and result.source_format == "webm"
    with wave.open(io.BytesIO(result.wav)) as wav:
        assert (wav.getframerate(), wav.getnchannels()) == (16000, 1)
        assert 0.8 < wav.getnframes() / 16000 < 1.3
    assert after == before  # nothing left behind


def test_garbage_with_webm_magic_fails_cleanly_with_real_ffmpeg():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    with pytest.raises(SpeechInputError, match="could not be decoded"):
        prepare_for_transcription(WEBM)
