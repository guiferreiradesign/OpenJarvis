"""Tests for the Voicebox speech-output provider.

Every request goes to an ``httpx.MockTransport``; no Voicebox service is
needed.  Request shapes follow Voicebox 0.5.0's OpenAPI schema.
"""

from __future__ import annotations

import io
import json
import wave
from typing import Any, Callable, Dict, List

import httpx
import pytest

from jarvis_ext.speech.errors import (
    SpeechInputError,
    SpeechResponseError,
    SpeechTimeoutError,
    SpeechUnavailableError,
)
from jarvis_ext.speech.settings import SpeechOutputSettings
from jarvis_ext.speech.voicebox import VoiceboxTTSBackend
from openjarvis.speech.tts import TTSBackend, TTSResult

PROFILES = [
    {
        "id": "p-test",
        "name": "Test Voice",
        "preset_engine": "kokoro",
        "preset_voice_id": "am_onyx",
    },
    {
        "id": "p-jarvis",
        "name": "JARVIS",
        "preset_engine": "kokoro",
        "preset_voice_id": "bm_george",
    },
]


def wav_bytes(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeVoicebox:
    """A scripted Voicebox: records requests, answers per path."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self.profiles: Any = PROFILES
        self.audio: bytes = wav_bytes()
        self.generate: Callable[[httpx.Request], httpx.Response] | None = None
        self.health: Dict[str, Any] = {"status": "healthy", "model_loaded": False}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json=self.health)
        if path == "/profiles":
            return httpx.Response(200, json=self.profiles)
        if path == "/generate/stream":
            if self.generate is not None:
                return self.generate(request)
            return httpx.Response(
                200, content=self.audio, headers={"content-type": "audio/wav"}
            )
        return httpx.Response(404, json={"detail": "Not Found"})

    def generate_bodies(self) -> List[Dict[str, Any]]:
        return [
            json.loads(r.content)
            for r in self.requests
            if r.url.path == "/generate/stream"
        ]


@pytest.fixture
def vb() -> FakeVoicebox:
    return FakeVoicebox()


def backend(vb: FakeVoicebox, **settings: Any) -> VoiceboxTTSBackend:
    return VoiceboxTTSBackend(
        SpeechOutputSettings.from_mapping(settings),
        transport=httpx.MockTransport(vb.handler),
    )


def failing(exc: Exception) -> VoiceboxTTSBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return VoiceboxTTSBackend(
        SpeechOutputSettings(), transport=httpx.MockTransport(handler)
    )


class TestInterface:
    def test_is_an_upstream_tts_backend(self, vb):
        tts = backend(vb)
        assert isinstance(tts, TTSBackend)
        assert tts.backend_id == "voicebox"

    def test_constructing_sends_nothing(self, vb):
        backend(vb)
        assert vb.requests == []

    def test_settings_come_from_config(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(
            '[jarvis.speech_output]\nbase_url = "http://localhost:18001"\n'
            'profile = "Test Voice"\n',
            encoding="utf-8",
        )
        tts = VoiceboxTTSBackend(config_path=path)
        assert tts.settings.base_url == "http://localhost:18001"
        assert tts.settings.profile == "Test Voice"


class TestSynthesize:
    def test_request_matches_the_voicebox_schema(self, vb):
        result = backend(vb).synthesize("Hello, sir.")
        assert vb.generate_bodies() == [
            {
                "profile_id": "p-jarvis",  # default: the kokoro bm_george profile
                "text": "Hello, sir.",
                "language": "en",
                "engine": "kokoro",
                "personality": False,
            }
        ]
        request = vb.requests[-1]
        assert request.method == "POST"
        assert str(request.url) == "http://127.0.0.1:17493/generate/stream"
        assert isinstance(result, TTSResult)
        assert result.audio == vb.audio
        assert result.format == "wav"
        assert result.sample_rate == 24000
        assert result.duration_seconds == pytest.approx(0.5)
        assert result.voice_id == "p-jarvis"
        assert result.metadata["profile"] == "JARVIS"
        assert result.metadata["engine"] == "kokoro"

    def test_personality_rewrite_is_never_requested(self, vb):
        backend(vb).synthesize("Exactly these words.")
        assert vb.generate_bodies()[0]["personality"] is False
        assert vb.generate_bodies()[0]["text"] == "Exactly these words."

    @pytest.mark.parametrize(
        "wanted", ["p-test", "Test Voice", "test voice", "am_onyx"]
    )
    def test_voice_id_selects_a_profile(self, vb, wanted):
        result = backend(vb).synthesize("Hi", voice_id=wanted)
        assert vb.generate_bodies()[0]["profile_id"] == "p-test"
        assert result.voice_id == "p-test"

    def test_configured_profile_and_url(self, vb):
        tts = backend(vb, profile="Test Voice", base_url="http://localhost:18000")
        tts.synthesize("Hi")
        assert vb.generate_bodies()[0]["profile_id"] == "p-test"
        assert all(r.url.host == "localhost" for r in vb.requests)
        assert all(r.url.port == 18000 for r in vb.requests)

    def test_configured_engine_and_voice_preference(self, vb):
        backend(vb, voice="am_onyx").synthesize("Hi")
        assert vb.generate_bodies()[0]["profile_id"] == "p-test"

    def test_unknown_profile(self, vb):
        with pytest.raises(SpeechUnavailableError) as info:
            backend(vb).synthesize("Hi", voice_id="Nobody")
        assert "Nobody" in str(info.value)
        assert vb.generate_bodies() == []

    def test_no_matching_profile(self, vb):
        vb.profiles = [PROFILES[0]]
        with pytest.raises(SpeechUnavailableError) as info:
            backend(vb).synthesize("Hi")
        assert "bm_george" in str(info.value)

    def test_profiles_are_cached(self, vb):
        tts = backend(vb)
        tts.synthesize("One")
        tts.synthesize("Two")
        assert [r.url.path for r in vb.requests].count("/profiles") == 1

    def test_speed_and_format_do_not_change_the_request(self, vb):
        result = backend(vb).synthesize("Hi", speed=1.5, output_format="mp3")
        assert set(vb.generate_bodies()[0]) == {
            "profile_id",
            "text",
            "language",
            "engine",
            "personality",
        }
        assert result.format == "wav"

    @pytest.mark.parametrize("text", ["", "   "])
    def test_empty_text_is_refused_locally(self, vb, text):
        with pytest.raises(SpeechInputError):
            backend(vb).synthesize(text)
        assert vb.requests == []

    def test_long_text_is_refused_locally(self, vb):
        with pytest.raises(SpeechInputError):
            backend(vb, max_text_chars=10).synthesize("x" * 11)
        assert vb.requests == []

    def test_no_proxy_from_the_environment(self, vb, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
        monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
        backend(vb).synthesize("Hi")
        assert all(r.url.host == "127.0.0.1" for r in vb.requests)


class TestFailures:
    def test_service_not_running(self):
        tts = failing(httpx.ConnectError("Connection refused"))
        assert tts.health() is False
        assert tts.available_voices() == []
        with pytest.raises(SpeechUnavailableError) as info:
            tts.synthesize("Hi")
        assert "not running" in str(info.value)

    def test_connect_timeout_counts_as_not_running(self):
        with pytest.raises(SpeechUnavailableError):
            failing(httpx.ConnectTimeout("slow")).synthesize("Hi")

    def test_generation_timeout(self, vb):
        def slow(request):
            raise httpx.ReadTimeout("still generating", request=request)

        vb.generate = slow
        with pytest.raises(SpeechTimeoutError) as info:
            backend(vb, timeout_seconds=5).synthesize("Hi")
        assert "5 s" in str(info.value)

    def test_http_error_includes_short_detail(self, vb):
        vb.generate = lambda r: httpx.Response(
            422, json={"detail": "text\n too   long"}
        )
        with pytest.raises(SpeechResponseError) as info:
            backend(vb).synthesize("Hi")
        assert "HTTP 422" in str(info.value)
        assert "text too long" in str(info.value)

    def test_server_error_without_json(self, vb):
        vb.generate = lambda r: httpx.Response(500, content=b"<html>boom</html>")
        with pytest.raises(SpeechResponseError) as info:
            backend(vb).synthesize("Hi")
        assert "HTTP 500" in str(info.value)

    @pytest.mark.parametrize(
        "content",
        [
            b"",
            b'{"status": "generating"}',
            b"RIFF\x00\x00\x00\x00WAVEjunk",
            wav_bytes(0.0),
        ],
    )
    def test_malformed_audio(self, vb, content):
        vb.generate = lambda r: httpx.Response(200, content=content)
        with pytest.raises(SpeechResponseError):
            backend(vb).synthesize("Hi")

    @pytest.mark.parametrize("profiles", [{"not": "a list"}, "nope"])
    def test_malformed_profile_list(self, vb, profiles):
        vb.profiles = profiles
        tts = backend(vb)
        with pytest.raises(SpeechResponseError):
            tts.synthesize("Hi")
        assert tts.available_voices() == []

    def test_profile_entries_without_ids_are_ignored(self, vb):
        vb.profiles = [{"name": "broken"}, 3, PROFILES[1]]
        assert backend(vb).available_voices() == ["JARVIS"]

    def test_profile_list_not_json(self, vb):
        def handler(request):
            if request.url.path == "/profiles":
                return httpx.Response(200, content=b"<html>")
            return vb.handler(request)

        tts = VoiceboxTTSBackend(
            SpeechOutputSettings(), transport=httpx.MockTransport(handler)
        )
        with pytest.raises(SpeechResponseError):
            tts.synthesize("Hi")

    @pytest.mark.parametrize(
        "health",
        [{"status": "unhealthy"}, {"status": "healthy"}, ["healthy"]],
    )
    def test_health(self, vb, health):
        vb.health = health
        expected = isinstance(health, dict) and health["status"] == "healthy"
        assert backend(vb).health() is expected

    def test_health_with_invalid_json(self):
        tts = VoiceboxTTSBackend(
            SpeechOutputSettings(),
            transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x")),
        )
        assert tts.health() is False

    def test_redirects_are_not_followed(self, vb):
        vb.generate = lambda r: httpx.Response(
            307, headers={"location": "http://example.com/generate/stream"}
        )
        with pytest.raises(SpeechResponseError):
            backend(vb).synthesize("Hi")
        assert all(r.url.host == "127.0.0.1" for r in vb.requests)
