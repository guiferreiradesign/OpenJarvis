"""Tests for microphone capture and ``speech-listen`` (no microphone, no model).

FFmpeg and the recogniser are faked.  Nothing opens a real audio device.
"""

from __future__ import annotations

import io
import json
import math
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any, List, Optional

import pytest

from jarvis_ext.__main__ import main
from jarvis_ext.speech import capture
from jarvis_ext.speech import mlx_whisper as mw
from jarvis_ext.speech.capture import (
    AudioDevice,
    CaptureCancelled,
    CaptureError,
    MicrophonePermissionError,
    NoMicrophoneError,
    SilentRecordingError,
    record_utterance,
    select_device,
)
from jarvis_ext.speech.errors import SpeechTimeoutError, SpeechUnavailableError
from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend

# What `ffmpeg -f avfoundation -list_devices true -i ""` prints on this Mac.
DEVICE_LISTING = b"""\
[AVFoundation indev @ 0x1] AVFoundation video devices:
[AVFoundation indev @ 0x1] [0] FaceTime HD Camera
[AVFoundation indev @ 0x1] [1] Capture screen 0
[AVFoundation indev @ 0x1] AVFoundation audio devices:
[AVFoundation indev @ 0x1] [0] ZoomAudioDevice
[AVFoundation indev @ 0x1] [1] MacBook Pro Microphone
[AVFoundation indev @ 0x1] [2] Microsoft Teams Audio
[in#0 @ 0x2] Error opening input: Input/output error
"""


def wav_bytes(seconds: float = 1.0, amplitude: float = 0.3) -> bytes:
    rate = 16000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(
            b"".join(
                struct.pack(
                    "<h", int(32767 * amplitude * math.sin(2 * math.pi * 220 * i / rate))
                )
                for i in range(int(rate * seconds))
            )
        )
    return buf.getvalue()


class FakeProc:
    """A fake ffmpeg process that writes its WAV when told to stop ("q")."""

    instances: List["FakeProc"] = []

    def __init__(self, command, *, wav, stderr=b"", rc=0, finish_on_poll=None, **kw):
        self.command = command
        self.target = Path(command[-1])
        self.dir = self.target.parent
        self.wav, self.rc, self.finish_on_poll = wav, rc, finish_on_poll
        self.returncode: Optional[int] = None
        self.stderr = io.BytesIO(stderr)
        self.polls = 0
        self.terminated = False
        self.stdin = self
        FakeProc.instances.append(self)

    def _finish(self) -> None:
        if self.returncode is None:
            if self.wav is not None:
                self.target.write_bytes(self.wav)
            self.returncode = self.rc

    def write(self, data: bytes) -> None:
        if data.startswith(b"q"):
            self._finish()

    def flush(self) -> None: ...
    def close(self) -> None: ...

    def poll(self):
        self.polls += 1
        if self.finish_on_poll and self.polls >= self.finish_on_poll:
            self._finish()
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None) -> int:
        return self.returncode or 0


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)


def fake_run(command, **kwargs):
    return subprocess.CompletedProcess(command, 1, b"", DEVICE_LISTING)


@pytest.fixture(autouse=True)
def reset():
    FakeProc.instances = []


NO_FILE = object()


def record(wav: Any = None, *, stop_after: int = 2, **overrides: Any):
    clock = Clock()
    polls = {"n": 0}
    proc_kwargs = {
        k: overrides.pop(k) for k in ("stderr", "rc", "finish_on_poll") if k in overrides
    }

    def stop_requested() -> bool:
        polls["n"] += 1
        return polls["n"] > stop_after

    def popen(command, **kw):
        content = wav_bytes() if wav is None else (None if wav is NO_FILE else wav)
        return FakeProc(command, wav=content, **proc_kwargs)

    kwargs = dict(
        ffmpeg="/usr/bin/ffmpeg",
        run=fake_run,
        popen=popen,
        clock=clock,
        sleep=clock.sleep,
        stop_requested=stop_requested,
    )
    kwargs.update(overrides)
    return record_utterance(**kwargs)


class TestDevices:
    def test_listing_is_parsed_and_index_zero_is_not_assumed(self):
        devices = capture.parse_audio_devices(DEVICE_LISTING.decode())
        assert devices == [
            AudioDevice(0, "ZoomAudioDevice"),
            AudioDevice(1, "MacBook Pro Microphone"),
            AudioDevice(2, "Microsoft Teams Audio"),
        ]
        assert select_device(devices) == AudioDevice(1, "MacBook Pro Microphone")

    def test_preference_by_name_fragment_or_index(self):
        devices = capture.parse_audio_devices(DEVICE_LISTING.decode())
        assert select_device(devices, "teams").index == 2
        assert select_device(devices, "0").name == "ZoomAudioDevice"

    def test_unknown_preference_lists_what_exists(self):
        devices = capture.parse_audio_devices(DEVICE_LISTING.decode())
        with pytest.raises(NoMicrophoneError, match="Found: .*MacBook Pro Microphone"):
            select_device(devices, "usb headset")

    def test_no_devices_and_no_microphone(self):
        with pytest.raises(NoMicrophoneError):
            select_device([])
        with pytest.raises(NoMicrophoneError, match="--device"):
            select_device([AudioDevice(0, "ZoomAudioDevice")])


class TestRecord:
    def test_success_records_16k_mono_from_the_real_microphone(self):
        result = record()
        (proc,) = FakeProc.instances
        command = proc.command
        assert command[0] == "/usr/bin/ffmpeg"
        assert command[command.index("-i") + 1] == ":1"  # not :0 (Zoom)
        pairs = dict(zip(command, command[1:]))
        assert pairs["-ar"] == "16000" and pairs["-ac"] == "1"
        assert pairs["-f"] in ("avfoundation", "wav") and pairs["-t"] == "30"
        assert not any(c in " ".join(command) for c in ("http", "tcp://", "rtp"))
        assert result.device.name == "MacBook Pro Microphone"
        assert result.seconds == pytest.approx(1.0)
        with wave.open(io.BytesIO(result.wav)) as handle:
            assert (handle.getframerate(), handle.getnchannels()) == (16000, 1)
        assert result.rms > 0.05

    def test_recording_stops_by_itself_at_the_time_limit(self):
        result = record(stop_after=10**9, finish_on_poll=5, max_seconds=2)
        assert result.seconds == pytest.approx(1.0)
        assert FakeProc.instances[0].command[
            FakeProc.instances[0].command.index("-t") + 1
        ] == "2"

    def test_temp_dir_is_removed_on_success(self):
        record()
        assert not FakeProc.instances[0].dir.exists()

    def test_calls_on_start_with_the_device(self):
        seen = []
        record(on_start=seen.append, stop_after=10)  # past the 0.3 s start-up
        assert seen == [AudioDevice(1, "MacBook Pro Microphone")]

    @pytest.mark.parametrize("seconds", [0, -1, 121])
    def test_limits_are_validated_before_any_process_starts(self, seconds):
        with pytest.raises(Exception, match="max_seconds"):
            record(max_seconds=seconds)
        assert FakeProc.instances == []


class TestFailures:
    def test_silence_is_reported_and_cleaned_up(self):
        with pytest.raises(SilentRecordingError, match="No speech"):
            record(wav=wav_bytes(amplitude=0.0005))
        assert not FakeProc.instances[0].dir.exists()

    def test_digital_silence_hints_at_blocked_microphone(self):
        with pytest.raises(SilentRecordingError, match="Privacy"):
            record(wav=wav_bytes(amplitude=0.0))

    def test_empty_recording(self):
        with pytest.raises(SilentRecordingError, match="empty"):
            record(wav=wav_bytes(seconds=0))

    def test_permission_denied_has_actionable_help(self):
        with pytest.raises(MicrophonePermissionError, match="System Settings"):
            record(wav=NO_FILE, rc=1, stderr=b"Could not start: not authorized")
        assert not FakeProc.instances[0].dir.exists()

    def test_ffmpeg_error_without_output(self):
        def popen(command, **kw):
            return FakeProc(command, wav=None, rc=1, stderr=b"Input/output error")

        clock = Clock()
        with pytest.raises(CaptureError, match="Recording failed: Input/output"):
            record_utterance(
                ffmpeg="/usr/bin/ffmpeg", run=fake_run, popen=popen, clock=clock,
                sleep=clock.sleep, stop_requested=lambda: True,
            )

    def test_unreadable_output(self):
        with pytest.raises(CaptureError, match="unreadable"):
            record(wav=b"not a wav")

    def test_device_that_never_starts_times_out_and_is_stopped(self):
        clock = Clock()

        class Stuck(FakeProc):
            def write(self, data):  # ignores "q"
                pass

        def popen(command, **kw):
            return Stuck(command, wav=None)

        with pytest.raises(SpeechTimeoutError, match="did not start"):
            record_utterance(
                ffmpeg="/usr/bin/ffmpeg", run=fake_run, popen=popen, clock=clock,
                sleep=clock.sleep,
            )
        proc = FakeProc.instances[0]
        assert proc.terminated and not proc.dir.exists()

    def test_ctrl_c_cancels_stops_ffmpeg_and_cleans_up(self):
        def interrupt() -> bool:
            raise KeyboardInterrupt

        with pytest.raises(CaptureCancelled):
            record(stop_requested=interrupt)
        proc = FakeProc.instances[0]
        assert proc.terminated and not proc.dir.exists()

    def test_missing_ffmpeg(self):
        with pytest.raises(SpeechUnavailableError, match="ffmpeg is not installed"):
            record_utterance(ffmpeg=None, which=lambda name: None)

    def test_ffmpeg_that_cannot_start(self):
        def popen(command, **kw):
            raise PermissionError("denied")

        with pytest.raises(SpeechUnavailableError):
            record_utterance(ffmpeg="/usr/bin/ffmpeg", run=fake_run, popen=popen)


# -- the CLI ------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def recogniser(monkeypatch):
    from tests.jarvis_ext.test_speech_mlx_whisper import FakeMlx

    fake = FakeMlx()
    original = MlxWhisperBackend.__init__

    def init(self, *args, **kwargs):
        original(
            self, *args, module_loader=fake.load, platform_check=lambda: True, **kwargs
        )

    monkeypatch.setattr(mw, "_module_present", lambda name: True)
    monkeypatch.setattr(MlxWhisperBackend, "__init__", init)
    return fake


def fake_recording(seconds: float = 1.0) -> capture.Recording:
    return capture.Recording(
        wav=wav_bytes(seconds),
        device=AudioDevice(1, "MacBook Pro Microphone"),
        seconds=seconds,
        rms=0.2,
    )


class TestListenCommand:
    def test_records_then_transcribes_locally(
        self, config, recogniser, monkeypatch, capsys
    ):
        calls = []

        def fake_record(**kwargs):
            calls.append(kwargs)
            return fake_recording()

        monkeypatch.setattr(capture, "record_utterance", fake_record)
        assert main(["speech-listen", "--config", str(config)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["text"] == "Hello JARVIS, this is Voicebox speaking."
        assert out["device"] == "MacBook Pro Microphone"
        assert calls[0]["max_seconds"] == 30.0
        assert calls[0]["device_preference"] == ""
        assert len(recogniser.model_loads) == 1

    def test_options_and_settings_are_passed_through(
        self, tmp_path, recogniser, monkeypatch
    ):
        config = tmp_path / "config.toml"
        config.write_text('[jarvis.speech_input]\nmicrophone = "Teams"\n')
        seen = {}

        def fake_record(**kwargs):
            seen.update(kwargs)
            return fake_recording()

        monkeypatch.setattr(capture, "record_utterance", fake_record)
        assert main(["speech-listen", "--max-seconds", "500", "--config", str(config)]) == 0
        assert seen["device_preference"] == "Teams"
        assert seen["max_seconds"] == 120.0  # capped by max_audio_seconds
        seen.clear()
        main(["speech-listen", "--device", "1", "--config", str(config)])
        assert seen["device_preference"] == "1"

    def test_no_local_recogniser_means_no_recording_and_no_cloud(
        self, config, monkeypatch, capsys
    ):
        monkeypatch.setattr(mw, "_module_present", lambda name: False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
        monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-not-real")

        def forbidden(**kwargs):
            raise AssertionError("the microphone must not be opened")

        monkeypatch.setattr(capture, "record_utterance", forbidden)
        monkeypatch.setattr(capture, "list_audio_devices", forbidden)
        assert main(["speech-listen", "--config", str(config)]) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "mlx-whisper is not installed" in captured.err
        assert "Nothing was recorded" in captured.err
        assert "sk-test" not in captured.err

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (MicrophonePermissionError("blocked: System Settings"), "System Settings"),
            (NoMicrophoneError("No audio input devices"), "No audio input"),
            (SilentRecordingError("No speech was heard"), "No speech"),
            (SpeechTimeoutError("did not start"), "did not start"),
            (CaptureError("Recording failed"), "Recording failed"),
        ],
    )
    def test_capture_failures_are_clear_and_exit_1(
        self, config, recogniser, monkeypatch, capsys, error, message
    ):
        def failing(**kwargs):
            raise error

        monkeypatch.setattr(capture, "record_utterance", failing)
        assert main(["speech-listen", "--config", str(config)]) == 1
        captured = capsys.readouterr()
        assert captured.out == "" and message in captured.err
        assert recogniser.model_loads == []  # nothing was transcribed

    def test_ctrl_c_exits_130_without_transcribing(
        self, config, recogniser, monkeypatch, capsys
    ):
        def cancelled(**kwargs):
            raise CaptureCancelled("Recording cancelled")

        monkeypatch.setattr(capture, "record_utterance", cancelled)
        assert main(["speech-listen", "--config", str(config)]) == 130
        assert "Cancelled" in capsys.readouterr().err
        assert recogniser.model_loads == []

    def test_empty_transcript_is_an_error(self, config, recogniser, monkeypatch, capsys):
        recogniser.result = {"text": "  ", "language": "en", "segments": []}
        monkeypatch.setattr(capture, "record_utterance", lambda **k: fake_recording())
        assert main(["speech-listen", "--config", str(config)]) == 1
        assert "Nothing was recognised" in capsys.readouterr().err
