"""The browser push-to-talk request path, end to end, with the real local backend.

A real ``MlxWhisperBackend`` (its MLX module replaced by a fake, so no model is
needed) sits behind the real ``/v1/speech/transcribe`` route.  WebM is
converted by the real FFmpeg.  Nothing may reach a cloud provider.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import wave

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from jarvis_ext.speech import mlx_whisper as mw  # noqa: E402
from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend  # noqa: E402
from openjarvis.server.api_routes import speech_router  # noqa: E402
from tests.jarvis_ext.test_speech_mlx_whisper import FakeMlx  # noqa: E402


def _wav(seconds: float = 0.5, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x10\x00" * int(rate * seconds))
    return buf.getvalue()


@pytest.fixture
def local(monkeypatch):
    fake = FakeMlx()
    monkeypatch.setattr(mw, "_module_present", lambda name: True)
    backend = MlxWhisperBackend(
        module_loader=fake.load, platform_check=lambda: True
    )
    app = FastAPI()
    app.state.speech_backend = backend
    app.include_router(speech_router)
    return TestClient(app), fake, backend


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any socket opened by the request path is a failure."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("network access during local transcription")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_wav_upload_is_transcribed_by_the_local_backend(local):
    client, fake, backend = local
    response = client.post(
        "/v1/speech/transcribe", files={"file": ("rec.wav", _wav(), "audio/wav")}
    )
    assert response.status_code == 200
    assert response.json()["text"] == "Hello JARVIS, this is Voicebox speaking."
    assert len(fake.calls) == 1  # the fake MLX model ran, in this process
    assert backend.is_loaded


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_browser_webm_is_converted_then_transcribed_locally(local, tmp_path):
    client, fake, _ = local
    source = tmp_path / "tone.webm"
    made = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
         "sine=frequency=300:duration=1:sample_rate=48000", "-c:a", "libopus",
         str(source)],
        capture_output=True,
        timeout=30,
    )
    if made.returncode != 0:
        pytest.skip("this ffmpeg has no libopus encoder")
    response = client.post(
        "/v1/speech/transcribe",
        files={"file": ("recording.webm", source.read_bytes(), "audio/webm")},
    )
    assert response.status_code == 200
    assert len(fake.calls) == 1
    assert 0.8 < response.json()["duration_seconds"] < 1.3


@pytest.mark.parametrize(
    ("payload", "name", "status"),
    [
        (b"", "r.webm", 400),
        (b"ID3\x04 mp3", "r.mp3", 415),
        (b"RIFF\x00\x00\x00\x00WAVEjunk", "r.wav", 400),
    ],
)
def test_bad_uploads_are_clear_errors_and_never_reach_the_model(
    local, payload, name, status
):
    client, fake, _ = local
    response = client.post("/v1/speech/transcribe", files={"file": (name, payload)})
    assert response.status_code == status
    assert isinstance(response.json()["detail"], str)
    assert fake.calls == []


def test_unavailable_local_backend_is_501_not_a_cloud_call(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-not-real")
    monkeypatch.setattr(mw, "_module_present", lambda name: False)
    from openjarvis.core.config import JarvisConfig
    from openjarvis.speech._discovery import get_speech_backend

    app = FastAPI()
    app.state.speech_backend = get_speech_backend(JarvisConfig())
    app.include_router(speech_router)
    client = TestClient(app)
    assert client.get("/v1/speech/health").json()["available"] is False
    response = client.post(
        "/v1/speech/transcribe", files={"file": ("r.wav", _wav(), "audio/wav")}
    )
    assert response.status_code == 501
    assert "never used" in response.json()["detail"]
