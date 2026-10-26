"""Tests for the MLX Whisper speech-to-text provider and WAV decoding.

MLX is never imported: the provider is given fake ``mlx_whisper`` /
``mlx.core`` modules, so these tests need no Apple Silicon, no model
download and no microphone.
"""

from __future__ import annotations

import io
import subprocess
import threading
import time
import wave
from types import SimpleNamespace
from typing import Any, Dict, List

import numpy as np
import pytest

from jarvis_ext.speech import audio as audio_mod
from jarvis_ext.speech import mlx_whisper as mw
from jarvis_ext.speech.errors import (
    SpeechError,
    SpeechInputError,
    SpeechUnavailableError,
)
from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend, platform_supported
from jarvis_ext.speech.settings import DEFAULT_INITIAL_PROMPT, SpeechInputSettings
from openjarvis.speech._stubs import SpeechBackend


def make_wav(
    samples: List[int], *, rate: int = 16000, width: int = 2, channels: int = 1
) -> bytes:
    """Build a PCM WAV from interleaved integer samples."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(rate)
        if width == 1:
            data = bytes(s + 128 for s in samples)
        else:
            data = b"".join(s.to_bytes(width, "little", signed=True) for s in samples)
        wav.writeframes(data)
    return buf.getvalue()


def speech_wav(seconds: float = 0.5) -> bytes:
    n = int(16000 * seconds)
    tone = (np.sin(np.arange(n) / 8.0) * 8000).astype(int).tolist()
    return make_wav(tone)


class FakeMlx:
    """Stand-ins for ``mlx_whisper``, ``mlx_whisper.transcribe`` and ``mlx.core``."""

    def __init__(self, *, with_holder: bool = True) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.model_loads: List[tuple] = []
        self.result: Any = {
            "text": "  Hello JARVIS, this is Voicebox speaking.  ",
            "language": "en",
            "segments": [{"text": " Hello JARVIS,", "start": 0.0, "end": 1.2}],
        }
        self.error: Exception | None = None
        self.load_error: Exception | None = None
        self.active = 0
        self.max_active = 0
        self.imports: List[str] = []
        fake = self

        class ModelHolder:
            @classmethod
            def get_model(cls, model_path, dtype):
                if fake.load_error is not None:
                    raise fake.load_error
                fake.model_loads.append((model_path, dtype))
                return object()

        def transcribe(audio, **kwargs):
            fake.active += 1
            fake.max_active = max(fake.max_active, fake.active)
            try:
                time.sleep(0.001)
                fake.calls.append({"audio": audio, **kwargs})
                if fake.error is not None:
                    raise fake.error
                return fake.result
            finally:
                fake.active -= 1

        self.modules = {
            "mlx_whisper": SimpleNamespace(transcribe=transcribe),
            "mlx_whisper.transcribe": SimpleNamespace(
                **({"ModelHolder": ModelHolder} if with_holder else {})
            ),
            "mlx.core": SimpleNamespace(float16="float16", float32="float32"),
        }

    def load(self, name: str):
        self.imports.append(name)
        try:
            return self.modules[name]
        except KeyError:
            raise ImportError(f"No module named {name!r}") from None


@pytest.fixture
def fake() -> FakeMlx:
    return FakeMlx()


@pytest.fixture(autouse=True)
def _mlx_installed(monkeypatch):
    """Pretend mlx-whisper is installed unless a test says otherwise."""
    monkeypatch.setattr(mw, "_module_present", lambda name: True)


def backend(fake: FakeMlx, **settings: Any) -> MlxWhisperBackend:
    return MlxWhisperBackend(
        SpeechInputSettings.from_mapping(settings),
        module_loader=fake.load,
        platform_check=lambda: True,
    )


class TestInterface:
    def test_is_an_upstream_speech_backend(self, fake):
        stt = backend(fake)
        assert isinstance(stt, SpeechBackend)
        assert stt.backend_id == "mlx-whisper"
        assert stt.supported_formats() == ["wav"]

    def test_constructing_loads_nothing(self, fake):
        stt = backend(fake)
        assert fake.imports == []
        assert not stt.is_loaded
        assert stt.health() is True
        assert fake.imports == []  # health never imports or loads

    def test_default_settings_come_from_config(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('[jarvis.speech_input]\nlanguage = "de"\n', encoding="utf-8")
        assert MlxWhisperBackend(config_path=path).settings.language == "de"


class TestTranscription:
    def test_passes_model_language_and_prompt(self, fake):
        stt = backend(fake)
        result = stt.transcribe(speech_wav(), format="wav")
        call = fake.calls[-1]
        assert call["path_or_hf_repo"] == "mlx-community/whisper-base-mlx"
        assert call["language"] == "en"
        assert call["initial_prompt"] == DEFAULT_INITIAL_PROMPT
        assert call["verbose"] is None
        assert result.text == "Hello JARVIS, this is Voicebox speaking."
        assert result.language == "en"
        assert result.duration_seconds == pytest.approx(0.5)
        assert result.segments[0].text == "Hello JARVIS,"
        assert result.segments[0].end == 1.2

    def test_configured_values_are_passed(self, fake):
        stt = backend(
            fake,
            model="mlx-community/whisper-small-mlx",
            language="pt",
            initial_prompt="Lisboa",
        )
        stt.transcribe(speech_wav())
        call = fake.calls[-1]
        assert call["path_or_hf_repo"] == "mlx-community/whisper-small-mlx"
        assert call["language"] == "pt"
        assert call["initial_prompt"] == "Lisboa"
        assert fake.model_loads == [("mlx-community/whisper-small-mlx", "float16")]

    def test_language_override_and_auto_detect(self, fake):
        stt = backend(fake, language="", initial_prompt="")
        stt.transcribe(speech_wav())
        assert fake.calls[-1]["language"] is None
        assert fake.calls[-1]["initial_prompt"] is None
        stt.transcribe(speech_wav(), language="FR")
        assert fake.calls[-1]["language"] == "fr"

    def test_audio_reaches_whisper_as_a_float_array(self, fake):
        # A path would make mlx_whisper start ffmpeg; the array must not.
        backend(fake).transcribe(speech_wav())
        sent = fake.calls[-1]["audio"]
        assert isinstance(sent, np.ndarray)
        assert sent.dtype == np.float32
        assert sent.ndim == 1 and len(sent) == 8000

    def test_raw_transcript_is_returned_without_refinement(self, fake):
        fake.result = {"text": " um, jarvis  render the reel ", "segments": []}
        result = backend(fake).transcribe(speech_wav())
        assert result.text == "um, jarvis  render the reel"


class TestModelReuse:
    def test_model_is_loaded_once_for_many_requests(self, fake):
        stt = backend(fake)
        for _ in range(5):
            stt.transcribe(speech_wav())
        assert fake.model_loads == [("mlx-community/whisper-base-mlx", "float16")]
        assert len(fake.calls) == 5
        assert stt.stats()["load_count"] == 1
        assert stt.stats()["transcriptions"] == 5

    def test_explicit_load_then_warm_requests(self, fake):
        stt = backend(fake)
        assert stt.load() >= 0.0
        assert stt.is_loaded
        assert stt.load() == 0.0  # already loaded: no second load
        stt.transcribe(speech_wav())
        assert len(fake.model_loads) == 1
        assert stt.last_timing.cold is False
        assert stt.last_timing.load_seconds == 0.0

    def test_cold_and_warm_timings_are_separate(self, fake):
        stt = backend(fake)
        stt.transcribe(speech_wav())
        cold = stt.last_timing
        assert cold.cold is True
        assert stt.stats()["load_seconds"] == round(cold.load_seconds, 4)
        stt.transcribe(speech_wav())
        warm = stt.last_timing
        assert warm.cold is False
        assert warm.load_seconds == 0.0
        assert warm.inference_seconds >= 0.0
        stats = stt.stats()
        assert stats["last_cold"] is False
        assert stats["loaded"] is True

    def test_no_subprocess_per_request(self, fake, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("a subprocess was started")

        monkeypatch.setattr(subprocess, "Popen", forbidden)
        monkeypatch.setattr(subprocess, "run", forbidden)
        stt = backend(fake)
        for _ in range(3):
            stt.transcribe(speech_wav())
        assert len(fake.calls) == 3

    def test_requests_are_serialised(self, fake):
        stt = backend(fake)
        wav = speech_wav(0.1)
        threads = [
            threading.Thread(target=stt.transcribe, args=(wav,)) for _ in range(6)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(fake.calls) == 6
        assert fake.max_active == 1
        assert len(fake.model_loads) == 1

    def test_without_model_holder_loads_by_warming_up(self):
        fake = FakeMlx(with_holder=False)
        stt = backend(fake)
        stt.load()
        assert len(fake.calls) == 1  # one warm-up call on silence
        assert fake.calls[0]["path_or_hf_repo"] == "mlx-community/whisper-base-mlx"
        stt.transcribe(speech_wav())
        assert len(fake.calls) == 2


class TestErrors:
    def test_transcription_error_propagates_as_speech_error(self, fake):
        fake.error = RuntimeError("metal exploded at /Users/me/secret")
        with pytest.raises(SpeechError) as info:
            backend(fake).transcribe(speech_wav())
        assert "RuntimeError" in str(info.value)
        assert "/Users" not in str(info.value)
        assert isinstance(info.value.__cause__, RuntimeError)

    def test_unexpected_result_is_an_error(self, fake):
        fake.result = ["not", "a", "dict"]
        with pytest.raises(SpeechError):
            backend(fake).transcribe(speech_wav())

    def test_load_failure_is_unavailable_and_can_be_retried(self, fake):
        fake.load_error = OSError("download failed")
        stt = backend(fake)
        with pytest.raises(SpeechUnavailableError) as info:
            stt.transcribe(speech_wav())
        assert "OSError" in str(info.value)
        assert not stt.is_loaded
        fake.load_error = None
        stt.transcribe(speech_wav())
        assert stt.is_loaded

    def test_local_model_path_is_not_shown(self, fake):
        fake.load_error = OSError("missing")
        stt = backend(fake, model="/Users/me/models/whisper-base")
        with pytest.raises(SpeechUnavailableError) as info:
            stt.load()
        assert "/Users/me" not in str(info.value)
        assert "whisper-base" in str(info.value)
        assert stt.stats()["model"] == "whisper-base"

    def test_missing_dependency(self, fake, monkeypatch):
        monkeypatch.setattr(mw, "_module_present", lambda name: False)
        stt = backend(fake)
        assert stt.health() is False
        assert "not installed" in stt.unavailable_reason()
        with pytest.raises(SpeechUnavailableError) as info:
            stt.transcribe(speech_wav())
        assert "jarvis/speech.md" in str(info.value)
        assert fake.imports == []

    def test_import_failure_is_unavailable(self):
        stt = MlxWhisperBackend(
            SpeechInputSettings(),
            module_loader=lambda name: (_ for _ in ()).throw(ImportError(name)),
            platform_check=lambda: True,
        )
        with pytest.raises(SpeechUnavailableError):
            stt.load()

    def test_unsupported_platform(self, fake):
        stt = MlxWhisperBackend(
            SpeechInputSettings(), module_loader=fake.load, platform_check=lambda: False
        )
        assert stt.health() is False
        assert "Apple Silicon" in stt.unavailable_reason()
        with pytest.raises(SpeechUnavailableError):
            stt.transcribe(speech_wav())
        assert fake.imports == []

    @pytest.mark.parametrize(
        ("system", "machine", "expected"),
        [
            ("darwin", "arm64", True),
            ("darwin", "x86_64", False),
            ("linux", "arm64", False),
            ("win32", "AMD64", False),
        ],
    )
    def test_platform_supported(self, monkeypatch, system, machine, expected):
        monkeypatch.setattr(mw.sys, "platform", system)
        monkeypatch.setattr(mw.platform, "machine", lambda: machine)
        assert platform_supported() is expected

    def test_non_wav_format_is_refused(self, fake):
        with pytest.raises(SpeechInputError):
            backend(fake).transcribe(b"ID3...", format="mp3")
        assert fake.calls == []

    def test_too_long_audio_is_refused_before_loading(self, fake):
        stt = backend(fake, max_audio_seconds=1)
        with pytest.raises(SpeechInputError):
            stt.transcribe(speech_wav(1.5))
        assert fake.model_loads == []

    @pytest.mark.parametrize("data", [b"", b"not a wav file", make_wav([])])
    def test_unusable_audio_is_refused(self, fake, data):
        with pytest.raises(SpeechInputError):
            backend(fake).transcribe(data)
        assert fake.calls == []


class TestDecodeWav:
    def test_16k_mono_is_passed_through_exactly(self):
        samples = [0, 16384, -16384, 32767, -32768]
        out = audio_mod.decode_wav(make_wav(samples))
        assert out.dtype == np.float32
        np.testing.assert_array_equal(out, np.array(samples, dtype=np.float32) / 32768)

    def test_stereo_is_averaged(self):
        out = audio_mod.decode_wav(make_wav([16384, 0, -16384, -16384], channels=2))
        np.testing.assert_allclose(out, [0.25, -0.5])

    @pytest.mark.parametrize(
        ("width", "peak"), [(1, 127), (3, (1 << 23) - 1), (4, (1 << 31) - 1)]
    )
    def test_other_sample_widths(self, width, peak):
        out = audio_mod.decode_wav(make_wav([0, peak, -peak], width=width))
        np.testing.assert_allclose(out, [0.0, 1.0, -1.0], atol=1e-2)

    def test_other_rates_are_resampled(self):
        pytest.importorskip("scipy")
        tone = (np.sin(np.arange(4800) / 10.0) * 10000).astype(int).tolist()
        out = audio_mod.decode_wav(make_wav(tone, rate=48000))
        assert len(out) == 1600
        assert out.dtype == np.float32

    def test_resampling_without_scipy_is_a_clear_error(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_scipy(name, *args, **kwargs):
            if name.startswith("scipy"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_scipy)
        with pytest.raises(SpeechInputError) as info:
            audio_mod.decode_wav(make_wav([0] * 480, rate=48000))
        assert "16 kHz" in str(info.value)

    def test_wav_info(self):
        info = audio_mod.read_wav_info(make_wav([0] * 24000, rate=24000))
        assert (info.channels, info.sample_width, info.sample_rate) == (1, 2, 24000)
        assert info.duration_seconds == 1.0
