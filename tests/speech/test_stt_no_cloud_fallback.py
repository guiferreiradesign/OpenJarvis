"""A missing local speech-to-text backend must never send audio to a cloud provider.

Cloud backends report healthy as soon as their API key exists, so auto
discovery must not try them.  Only an explicit ``speech.backend`` does.
"""

from __future__ import annotations

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.speech._discovery import (
    CLOUD_SPEECH_BACKENDS,
    DISCOVERY_ORDER,
    get_speech_backend,
)

CLOUD = ("openai", "deepgram")


class _Backend:
    def __init__(self, backend_id: str, healthy: bool) -> None:
        self.backend_id = backend_id
        self._healthy = healthy
        self.transcribed = 0

    def health(self) -> bool:
        return self._healthy

    def transcribe(self, audio, **kwargs):  # pragma: no cover - must not run
        self.transcribed += 1
        raise AssertionError("transcribe must not be called by discovery")


@pytest.fixture
def world(monkeypatch):
    """Cloud keys set; the local backend is down.  Records what gets built."""
    import openjarvis.speech  # noqa: F401
    from openjarvis.core.registry import SpeechRegistry

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-not-real")
    state = {"built": [], "local_healthy": False, "instances": []}

    def get(key):
        def make(*args, **kwargs):
            state["built"].append(key)
            backend = _Backend(
                key, healthy=state["local_healthy"] if key not in CLOUD else True
            )
            state["instances"].append(backend)
            return backend

        return make

    monkeypatch.setattr(SpeechRegistry, "contains", lambda key: True)
    monkeypatch.setattr(SpeechRegistry, "get", get)
    return state


def _config(backend: str) -> JarvisConfig:
    config = JarvisConfig()
    config.speech.backend = backend
    return config


def test_cloud_backends_are_listed():
    assert CLOUD_SPEECH_BACKENDS == set(CLOUD)
    assert DISCOVERY_ORDER[0] == "faster-whisper"


def test_auto_with_keys_and_local_down_returns_none_and_builds_no_cloud(world):
    assert get_speech_backend(_config("auto")) is None
    assert world["built"] == ["faster-whisper"]
    assert not set(world["built"]) & set(CLOUD)
    assert all(b.transcribed == 0 for b in world["instances"])


def test_auto_with_keys_still_prefers_the_healthy_local_backend(world):
    world["local_healthy"] = True
    backend = get_speech_backend(_config("auto"))
    assert backend is not None and backend.backend_id == "faster-whisper"
    assert world["built"] == ["faster-whisper"]


def test_auto_without_keys_behaves_the_same(world, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("DEEPGRAM_API_KEY")
    assert get_speech_backend(_config("auto")) is None
    assert world["built"] == ["faster-whisper"]


@pytest.mark.parametrize("provider", CLOUD)
def test_explicitly_configured_cloud_provider_is_still_honoured(world, provider):
    backend = get_speech_backend(_config(provider))
    assert backend is not None and backend.backend_id == provider
    assert world["built"] == [provider]


def test_explicit_local_backend_down_does_not_fall_back(world):
    assert get_speech_backend(_config("faster-whisper")) is None
    assert world["built"] == ["faster-whisper"]


def test_real_cloud_constructors_need_their_key_and_are_not_reached_in_auto(
    monkeypatch,
):
    """Without the registry fake: auto never constructs the real cloud classes."""
    import openjarvis.speech  # noqa: F401
    import openjarvis.speech._discovery as discovery

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-not-real")
    created: list[str] = []
    real = discovery._create_backend

    def spy(key, config):
        created.append(key)
        if key in CLOUD:
            raise AssertionError(f"cloud backend {key} was constructed")
        return None  # local backend unavailable

    monkeypatch.setattr(discovery, "_create_backend", spy)
    assert real is not spy
    assert discovery.get_speech_backend(_config("auto")) is None
    assert created == ["faster-whisper"]


def test_server_route_reports_unavailable_not_cloud(world):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.state.speech_backend = get_speech_backend(_config("auto"))
    app.include_router(speech_router)
    client = TestClient(app)

    health = client.get("/v1/speech/health").json()
    assert health["available"] is False
    assert "local" in health["reason"]

    response = client.post(
        "/v1/speech/transcribe", files={"file": ("a.wav", b"RIFF", "audio/wav")}
    )
    assert response.status_code == 501
    assert "never used" in response.json()["detail"]
    assert world["built"] == ["faster-whisper"]


# -- Step 5: the default recogniser is the local MLX Whisper -------------------


def test_default_config_selects_the_local_recogniser():
    assert JarvisConfig().speech.backend == "mlx-whisper"


def test_default_config_with_cloud_keys_and_no_local_install_is_unavailable(
    monkeypatch,
):
    import jarvis_ext.speech.mlx_whisper as mw

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-not-real")
    monkeypatch.setattr(mw, "_module_present", lambda name: False)
    assert get_speech_backend(JarvisConfig()) is None


def test_default_config_uses_mlx_whisper_when_installed(monkeypatch):
    import jarvis_ext.speech.mlx_whisper as mw
    from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend

    monkeypatch.setattr(mw, "_module_present", lambda name: True)
    monkeypatch.setattr(mw, "platform_supported", lambda: True)
    backend = get_speech_backend(JarvisConfig())
    assert isinstance(backend, MlxWhisperBackend)
    assert not backend.is_loaded  # discovery never loads the model
