"""Tests for the VoiceStudio speech-output provider.

Every request goes to an ``httpx.MockTransport``; no VoiceStudio is needed.
Request and error shapes follow what ``jarvis/voicestudio-plan.md`` verified
against VoiceStudio v0.5.7.  Nothing here makes a real network call.
"""

from __future__ import annotations

import io
import json
import wave
from typing import Any, Callable, List

import httpx
import pytest

from jarvis_ext.config import ExtensionConfigError
from jarvis_ext.speech.errors import (
    SpeechInputError,
    SpeechResponseError,
    SpeechTimeoutError,
    SpeechUnavailableError,
)
from jarvis_ext.speech.settings import VoiceStudioSettings
from jarvis_ext.speech.voicestudio import (
    VoiceStudioTTSBackend,
    normalize_for_speech,
)
from openjarvis.speech.tts import TTSBackend


def wav_bytes(seconds: float = 0.5, rate: int = 24000, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * int(rate * seconds) * channels)
    return buf.getvalue()


class FakeVoiceStudio:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.speech: Callable[[httpx.Request], httpx.Response] = lambda r: (
            httpx.Response(
                200, content=wav_bytes(), headers={"content-type": "audio/wav"}
            )
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/audio/voices":
            return httpx.Response(
                200, json={"voices": [{"id": "alloy"}, {"id": "bm_george"}]}
            )
        if request.url.path == "/v1/audio/speech":
            return self.speech(request)
        return httpx.Response(404, json={})

    def speech_bodies(self) -> List[dict]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path == "/v1/audio/speech"
        ]


@pytest.fixture
def vs() -> FakeVoiceStudio:
    return FakeVoiceStudio()


def backend(vs: FakeVoiceStudio, **settings: Any) -> VoiceStudioTTSBackend:
    return VoiceStudioTTSBackend(
        VoiceStudioSettings.from_mapping(settings),
        transport=httpx.MockTransport(vs.handler),
        sleep=lambda seconds: None,
    )


def raising(exc: Exception) -> VoiceStudioTTSBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return VoiceStudioTTSBackend(
        VoiceStudioSettings(), transport=httpx.MockTransport(handler)
    )


class TestSettings:
    def test_defaults_are_kokoro_george_british_english(self):
        s = VoiceStudioSettings()
        assert (s.base_url, s.model, s.voice, s.language) == (
            "http://127.0.0.1:3900",
            "mlx-audio",
            "bm_george",
            "British English",
        )
        assert VoiceStudioSettings.from_mapping({}) == s

    def test_configurable_from_the_config_file(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            '[jarvis.voicestudio]\nbase_url = "http://localhost:3901"\n'
            'voice = "bm_lewis"\nlanguage = "American English"\n',
            encoding="utf-8",
        )
        tts = VoiceStudioTTSBackend(config_path=path)
        assert tts.settings.base_url == "http://localhost:3901"
        assert tts.settings.voice == "bm_lewis"
        assert tts.settings.model == "mlx-audio"

    @pytest.mark.parametrize(
        "bad",
        [
            {"base_url": "https://example.com:3900"},
            {"base_url": "http://192.168.1.5:3900"},
            {"base_url": "http://127.0.0.1"},
            {"voice": ""},
            {"model": ""},
            {"max_text_chars": 5000},
            {"speed": 0},
            {"timeout_seconds": 0},
            {"unknown": 1},
        ],
    )
    def test_invalid_settings_are_errors(self, bad):
        with pytest.raises(ExtensionConfigError):
            VoiceStudioSettings.from_mapping(bad)


class TestSynthesize:
    def test_is_an_upstream_tts_backend_and_constructing_sends_nothing(self, vs):
        tts = backend(vs)
        assert isinstance(tts, TTSBackend)
        assert tts.backend_id == "voicestudio"
        assert vs.requests == []

    def test_sends_model_voice_language_and_format_explicitly(self, vs):
        backend(vs).synthesize("Good evening, sir.")
        (request,) = [r for r in vs.requests if r.url.path.endswith("/speech")]
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:3900/v1/audio/speech"
        assert vs.speech_bodies() == [
            {
                "model": "mlx-audio",
                "input": "Good evening, sir.",
                "voice": "bm_george",
                "language": "British English",
                "response_format": "wav",
                "speed": 1.0,
            }
        ]

    def test_every_request_repeats_the_voice(self, vs):
        tts = backend(vs)
        tts.synthesize("One.")
        tts.synthesize("Two.")
        assert [b["voice"] for b in vs.speech_bodies()] == ["bm_george"] * 2
        assert [b["model"] for b in vs.speech_bodies()] == ["mlx-audio"] * 2

    def test_other_formats_are_still_requested_as_wav(self, vs):
        result = backend(vs).synthesize("Hi.", output_format="mp3")
        assert vs.speech_bodies()[0]["response_format"] == "wav"
        assert result.format == "wav"

    def test_configured_and_per_call_overrides(self, vs):
        tts = backend(vs, voice="bm_lewis", language="American English", speed=1.2)
        tts.synthesize("Hi.")
        tts.synthesize("Hi.", voice_id="bf_emma", speed=0.9)
        first, second = vs.speech_bodies()
        assert (first["voice"], first["language"], first["speed"]) == (
            "bm_lewis",
            "American English",
            1.2,
        )
        assert (second["voice"], second["speed"]) == ("bf_emma", 0.9)

    def test_result_uses_the_wav_header_not_an_assumed_rate(self, vs):
        vs.speech = lambda r: httpx.Response(
            200, content=wav_bytes(seconds=0.5, rate=22050, channels=2)
        )
        result = backend(vs).synthesize("Hi.")
        assert result.sample_rate == 22050
        assert result.duration_seconds == pytest.approx(0.5)
        assert result.metadata["channels"] == 2
        assert result.audio[:4] == b"RIFF"
        assert result.voice_id == "bm_george"

    def test_jarvis_is_spoken_as_a_word(self, vs):
        backend(vs).synthesize("J.A.R.V.I.S. is online. JARVIS, report.")
        assert vs.speech_bodies()[0]["input"] == "Jarvis is online. Jarvis, report."

    def test_empty_and_oversized_text_never_reach_the_service(self, vs):
        tts = backend(vs, max_text_chars=10)
        with pytest.raises(SpeechInputError):
            tts.synthesize("   ")
        with pytest.raises(SpeechInputError):
            tts.synthesize("x" * 11)
        assert vs.requests == []


class TestFailures:
    def test_service_not_running_is_a_clear_error(self):
        with pytest.raises(SpeechUnavailableError, match="VoiceStudio is not running"):
            raising(httpx.ConnectError("refused")).synthesize("Hi.")

    def test_connect_timeout_means_not_running(self):
        with pytest.raises(SpeechUnavailableError):
            raising(httpx.ConnectTimeout("slow")).synthesize("Hi.")

    def test_slow_generation_is_a_timeout_error(self):
        with pytest.raises(SpeechTimeoutError, match="did not answer within 180"):
            raising(httpx.ReadTimeout("slow")).synthesize("Hi.")

    def test_cold_start_uses_the_long_request_timeout(self, vs):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.extensions["timeout"])
            return httpx.Response(200, content=wav_bytes())

        tts = VoiceStudioTTSBackend(
            VoiceStudioSettings(), transport=httpx.MockTransport(handler)
        )
        tts.synthesize("Hi.")
        assert seen["read"] == 180.0
        assert seen["connect"] == 2.0

    @pytest.mark.parametrize(
        "content",
        [b"", b"not audio at all", b"RIFF\x00\x00\x00\x00WAVEjunk", b"ID3\x04mp3data"],
    )
    def test_invalid_audio_is_rejected(self, vs, content):
        vs.speech = lambda r: httpx.Response(200, content=content)
        with pytest.raises(SpeechResponseError):
            backend(vs).synthesize("Hi.")

    def test_empty_wav_is_rejected(self, vs):
        vs.speech = lambda r: httpx.Response(200, content=wav_bytes(seconds=0))
        with pytest.raises(SpeechResponseError, match="empty"):
            backend(vs).synthesize("Hi.")

    def test_licence_not_accepted_is_reported_without_a_fallback(self, vs):
        vs.speech = lambda r: httpx.Response(
            403, json={"error": {"message": "Licence not accepted", "code": "x"}}
        )
        with pytest.raises(SpeechUnavailableError, match="Licence not accepted"):
            backend(vs).synthesize("Hi.")

    def test_busy_service_is_retried_once_then_reported(self, vs):
        calls = []

        def speech(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(503, headers={"Retry-After": "2"}, json={})

        vs.speech = speech
        with pytest.raises(SpeechUnavailableError, match="busy"):
            backend(vs).synthesize("Hi.")
        assert len(calls) == 2

    def test_busy_then_ready_succeeds(self, vs):
        answers = iter([httpx.Response(429, json={}), None])

        def speech(request: httpx.Request) -> httpx.Response:
            return next(answers) or httpx.Response(200, content=wav_bytes())

        vs.speech = speech
        assert backend(vs).synthesize("Hi.").format == "wav"

    def test_model_error_is_a_response_error(self, vs):
        vs.speech = lambda r: httpx.Response(
            400, json={"error": {"message": "model_not_found"}}
        )
        with pytest.raises(SpeechResponseError, match="HTTP 400: model_not_found"):
            backend(vs).synthesize("Hi.")

    def test_server_error_is_a_response_error(self, vs):
        vs.speech = lambda r: httpx.Response(500, text="boom")
        with pytest.raises(SpeechResponseError, match="HTTP 500"):
            backend(vs).synthesize("Hi.")


class TestHealthAndVoices:
    def test_health_is_true_only_for_200_and_never_raises(self, vs):
        assert backend(vs).health() is True
        assert raising(httpx.ConnectError("refused")).health() is False
        down = VoiceStudioTTSBackend(
            VoiceStudioSettings(),
            transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        )
        assert down.health() is False

    def test_health_does_not_generate_audio(self, vs):
        backend(vs).health()
        assert [r.url.path for r in vs.requests] == ["/health"]

    def test_available_voices(self, vs):
        assert backend(vs).available_voices() == ["alloy", "bm_george"]
        assert raising(httpx.ConnectError("refused")).available_voices() == []


class TestNoCloudFallback:
    def test_only_the_configured_local_host_is_contacted(self, vs):
        tts = backend(vs)
        tts.health()
        tts.synthesize("Hi.")
        assert {(r.url.host, r.url.port) for r in vs.requests} == {("127.0.0.1", 3900)}

    def test_failure_makes_no_other_request(self, vs):
        vs.speech = lambda r: httpx.Response(500, text="boom")
        with pytest.raises(SpeechResponseError):
            backend(vs).synthesize("Hi.")
        assert {r.url.host for r in vs.requests} == {"127.0.0.1"}

    def test_discovery_with_voicestudio_down_never_builds_a_cloud_backend(
        self, monkeypatch
    ):
        import openjarvis.speech  # noqa: F401
        from openjarvis.core.registry import TTSRegistry
        from openjarvis.speech._tts_discovery import get_tts_backend

        cloud = ("openai_tts", "cartesia", "elevenlabs")
        built: list[str] = []

        class Fake:
            def __init__(self, key: str) -> None:
                self.backend_id = key

            def health(self) -> bool:
                return self.backend_id in cloud  # cloud "healthy": key present

        def get(key):
            def make():
                built.append(key)
                if key == "voicestudio":
                    return VoiceStudioTTSBackend(
                        VoiceStudioSettings(),
                        transport=httpx.MockTransport(
                            lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))
                        ),
                    )
                return Fake(key)

            return make

        monkeypatch.setattr(TTSRegistry, "contains", lambda key: True)
        monkeypatch.setattr(TTSRegistry, "get", get)
        assert get_tts_backend("voicestudio") is None
        assert "voicestudio" in built
        assert not set(built) & set(cloud)


class TestNormalizeForSpeech:
    @pytest.mark.parametrize(
        ("text", "spoken"),
        [
            ("I am JARVIS.", "I am Jarvis."),
            ("jarvis", "Jarvis"),
            ("J.A.R.V.I.S. is online.", "Jarvis is online."),
            ("Hello, J.A.R.V.I.S.", "Hello, Jarvis."),
            ("J-A-R-V-I-S", "Jarvis"),
            ("JARVIS's voice", "Jarvis's voice"),
            ("OpenJarvis and Jarvises", "OpenJarvis and Jarvises"),
            ("No name here.", "No name here."),
        ],
    )
    def test_cases(self, text, spoken):
        assert normalize_for_speech(text) == spoken
