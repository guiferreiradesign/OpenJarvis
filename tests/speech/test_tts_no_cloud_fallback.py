"""A down local TTS service must never silently reach a cloud provider."""

from __future__ import annotations

import pytest

from openjarvis.speech._tts_discovery import get_tts_backend

CLOUD = ("openai_tts", "cartesia", "elevenlabs")


class _Backend:
    def __init__(self, backend_id: str, healthy: bool) -> None:
        self.backend_id = backend_id
        self._healthy = healthy

    def health(self) -> bool:
        return self._healthy


@pytest.fixture
def registry(monkeypatch):
    """Local backends are down; every cloud backend reports healthy (key set)."""
    import openjarvis.speech  # noqa: F401
    from openjarvis.core.registry import TTSRegistry

    built: list[str] = []

    def get(key):
        def make():
            built.append(key)
            return _Backend(key, healthy=key in CLOUD)

        return make

    monkeypatch.setattr(TTSRegistry, "contains", lambda key: True)
    monkeypatch.setattr(TTSRegistry, "get", get)
    return built


def test_local_down_does_not_fall_back_to_cloud(registry):
    assert get_tts_backend("voicebox") is None
    assert not set(registry) & set(CLOUD)


def test_explicit_cloud_flag_allows_fallback(registry):
    assert get_tts_backend("voicebox", allow_cloud=True).backend_id == "openai_tts"


@pytest.mark.parametrize("preferred", CLOUD)
def test_configured_cloud_backend_is_explicit_opt_in(registry, preferred):
    assert get_tts_backend(preferred).backend_id == preferred


def test_server_route_reports_unavailable_not_cloud(registry, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.core.config import JarvisConfig
    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.include_router(speech_router)
    config = JarvisConfig()
    config.speech.tts_backend = "voicebox"
    app.state.config = config
    client = TestClient(app)

    response = client.post("/v1/speech/synthesize", json={"text": "hello"})
    assert response.status_code == 501
    assert not set(registry) & set(CLOUD)

    config.speech.allow_cloud_tts_fallback = True
    app.state.tts_resolution_task = None
    assert client.get("/v1/speech/tts/health").json()["backend"] == "openai_tts"


# -- CLI voice chat -------------------------------------------------------------


def _voice_session(**speech):
    from dataclasses import dataclass

    from openjarvis.cli._voice_chat import VoiceSession

    @dataclass
    class _Speech:
        tts_backend: str = "voicebox"
        voice_id: str = ""
        voice_speed: float = 1.0
        allow_cloud_tts_fallback: bool = False

    @dataclass
    class _Config:
        speech: _Speech

    return VoiceSession(config=_Config(speech=_Speech(**speech)))


def test_cli_voice_chat_local_down_gets_no_backend(registry):
    assert _voice_session().get_tts_backend() is None
    assert not set(registry) & set(CLOUD)


def test_cli_voice_chat_cloud_only_when_enabled(registry):
    session = _voice_session(allow_cloud_tts_fallback=True)
    assert session.get_tts_backend().backend_id == "openai_tts"


def test_cli_voice_chat_configured_cloud_backend_is_allowed(registry):
    session = _voice_session(tts_backend="cartesia")
    assert session.get_tts_backend().backend_id == "cartesia"


def test_default_config_disables_cloud_fallback():
    from openjarvis.core.config import JarvisConfig

    assert JarvisConfig().speech.allow_cloud_tts_fallback is False
