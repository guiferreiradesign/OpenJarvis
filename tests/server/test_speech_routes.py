"""Tests for speech API endpoints."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from openjarvis.speech._stubs import TranscriptionResult  # noqa: E402


def _wav(seconds: float = 0.1, rate: int = 16000) -> bytes:
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x01\x00" * int(rate * seconds))
    return buf.getvalue()


WAV = _wav()


@pytest.fixture
def mock_speech_backend():
    backend = MagicMock()
    backend.backend_id = "mock"
    backend.health.return_value = True
    backend.transcribe.return_value = TranscriptionResult(
        text="Hello world",
        language="en",
        confidence=0.95,
        duration_seconds=1.5,
        segments=[],
    )
    return backend


@pytest.fixture
def app_with_speech(mock_speech_backend):
    from fastapi import FastAPI

    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.state.speech_backend = mock_speech_backend
    app.include_router(speech_router)
    return app


@pytest.fixture
def client(app_with_speech):
    return TestClient(app_with_speech)


def test_transcribe_endpoint(client, mock_speech_backend):
    response = client.post(
        "/v1/speech/transcribe",
        files={"file": ("test.wav", WAV, "audio/wav")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["text"] == "Hello world"
    assert data["language"] == "en"
    assert data["confidence"] == 0.95
    assert data["duration_seconds"] == 1.5


def test_transcribe_endpoint_offloads_backend_work(client, mock_speech_backend):
    expected = TranscriptionResult(
        text="Offloaded",
        language="en",
        confidence=0.9,
        duration_seconds=1.0,
        segments=[],
    )

    real_to_thread = asyncio.to_thread
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        if func is mock_speech_backend.transcribe:
            return expected
        return await real_to_thread(func, *args, **kwargs)  # audio preparation

    with patch(
        "openjarvis.server.api_routes.asyncio.to_thread",
        side_effect=fake_to_thread,
    ):
        response = client.post(
            "/v1/speech/transcribe",
            files={"file": ("test.wav", WAV, "audio/wav")},
        )

    assert response.status_code == 200
    func, args, kwargs = calls[-1]
    assert func is mock_speech_backend.transcribe
    assert args == (WAV,)  # valid WAV passes through unchanged
    assert kwargs == {"format": "wav", "language": None}
    assert response.json()["text"] == "Offloaded"


def test_transcribe_endpoint_surfaces_backend_error(client, mock_speech_backend):
    mock_speech_backend.transcribe.side_effect = RuntimeError("missing cublas64_12.dll")

    response = client.post(
        "/v1/speech/transcribe",
        files={"file": ("test.wav", WAV, "audio/wav")},
    )

    assert response.status_code == 500
    assert "missing cublas64_12.dll" in response.json()["detail"]


def test_transcribe_no_file(client):
    response = client.post("/v1/speech/transcribe")
    assert response.status_code == 400 or response.status_code == 422


def test_health_endpoint(client):
    response = client.get("/v1/speech/health")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] is True
    assert data["backend"] == "mock"


def test_health_endpoint_includes_unavailable_reason(client, mock_speech_backend):
    mock_speech_backend.health.return_value = False
    mock_speech_backend.last_error.return_value = (
        "Install with: uv sync --extra desktop"
    )

    response = client.get("/v1/speech/health")

    assert response.status_code == 200
    data = response.json()
    assert data["available"] is False
    assert data["reason"] == "Install with: uv sync --extra desktop"


def test_health_no_backend():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.state.speech_backend = None
    app.include_router(speech_router)
    client = TestClient(app)

    response = client.get("/v1/speech/health")
    assert response.status_code == 200
    data = response.json()
    assert data["available"] is False


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_tts_backend():
    from openjarvis.speech.tts import TTSResult

    backend = MagicMock()
    backend.backend_id = "mock_tts"
    backend.health.return_value = True
    backend.synthesize.return_value = TTSResult(
        audio=b"RIFFfake-wav-bytes",
        format="wav",
        duration_seconds=1.25,
        voice_id="mock-voice",
        sample_rate=22050,
    )
    return backend


def _tts_app(backend, *, config=None):
    from fastapi import FastAPI

    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.include_router(speech_router)
    # Pre-resolve so the route does not go through backend discovery.
    app.state.tts_backend = backend
    app.state.tts_resolved = True
    app.state.config = config
    return TestClient(app)


def _unresolved_tts_app():
    from fastapi import FastAPI

    from openjarvis.server.api_routes import speech_router

    app = FastAPI()
    app.include_router(speech_router)
    app.state.config = SimpleNamespace(
        speech=SimpleNamespace(
            tts_backend="kokoro", voice_id="bm_george", voice_speed=1.0
        )
    )
    return app


def test_synthesize_returns_wav(mock_tts_backend):
    client = _tts_app(mock_tts_backend)

    response = client.post("/v1/speech/synthesize", json={"text": "Hallo Welt"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-sample-rate"] == "22050"
    assert response.headers["x-voice-id"] == "mock-voice"
    assert response.content == b"RIFFfake-wav-bytes"
    assert mock_tts_backend.synthesize.call_args.kwargs["output_format"] == "wav"


def test_synthesize_honours_request_overrides(mock_tts_backend):
    client = _tts_app(mock_tts_backend)

    client.post(
        "/v1/speech/synthesize",
        json={"text": "Hallo", "voice_id": "de_DE-thorsten-medium", "speed": 1.5},
    )

    kwargs = mock_tts_backend.synthesize.call_args.kwargs
    assert kwargs["voice_id"] == "de_DE-thorsten-medium"
    assert kwargs["speed"] == 1.5


def test_synthesize_rejects_empty_text(mock_tts_backend):
    client = _tts_app(mock_tts_backend)

    response = client.post("/v1/speech/synthesize", json={"text": "   "})

    assert response.status_code == 400
    mock_tts_backend.synthesize.assert_not_called()


def test_synthesize_rejects_oversized_text(mock_tts_backend):
    from openjarvis.server.api_routes import _MAX_TTS_CHARS

    client = _tts_app(mock_tts_backend)

    response = client.post(
        "/v1/speech/synthesize", json={"text": "a" * (_MAX_TTS_CHARS + 1)}
    )

    assert response.status_code == 413
    mock_tts_backend.synthesize.assert_not_called()


def test_synthesize_without_backend_returns_501():
    client = _tts_app(None)

    response = client.post("/v1/speech/synthesize", json={"text": "Hallo"})

    assert response.status_code == 501


def test_synthesize_surfaces_backend_failure(mock_tts_backend):
    mock_tts_backend.synthesize.side_effect = RuntimeError("voice model missing")
    client = _tts_app(mock_tts_backend)

    response = client.post("/v1/speech/synthesize", json={"text": "Hallo"})

    assert response.status_code == 500
    assert response.json()["detail"] == "Speech synthesis failed"


def test_synthesize_offloads_backend_work(mock_tts_backend):
    from openjarvis.speech.tts import TTSResult

    client = _tts_app(mock_tts_backend)
    expected = TTSResult(audio=b"RIFFoffloaded", format="wav", sample_rate=22050)

    with patch(
        "openjarvis.server.api_routes.asyncio.to_thread",
        new_callable=AsyncMock,
        return_value=expected,
    ) as to_thread:
        response = client.post("/v1/speech/synthesize", json={"text": "Hallo"})

    assert response.content == b"RIFFoffloaded"
    to_thread.assert_awaited_once()


def test_tts_health_reports_backend(mock_tts_backend):
    client = _tts_app(mock_tts_backend)

    data = client.get("/v1/speech/tts/health").json()

    assert data["available"] is True
    assert data["backend"] == "mock_tts"


def test_tts_health_without_backend():
    client = _tts_app(None)

    data = client.get("/v1/speech/tts/health").json()

    assert data["available"] is False
    assert "reason" in data


def test_tts_health_retries_after_unavailable(mock_tts_backend):
    app = _unresolved_tts_app()
    client = TestClient(app)

    with patch(
        "openjarvis.speech._tts_discovery.get_tts_backend",
        side_effect=[None, mock_tts_backend],
    ) as discover:
        assert client.get("/v1/speech/tts/health").json()["available"] is False
        assert client.get("/v1/speech/tts/health").json()["available"] is True
        assert client.get("/v1/speech/tts/health").json()["available"] is True

    assert discover.call_count == 2


@pytest.mark.asyncio
async def test_concurrent_tts_health_requests_share_resolution(mock_tts_backend):
    app = _unresolved_tts_app()

    def discover(_preferred):
        time.sleep(0.05)
        return mock_tts_backend

    with patch(
        "openjarvis.speech._tts_discovery.get_tts_backend", side_effect=discover
    ) as mock_discover:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            responses = await asyncio.gather(
                *(client.get("/v1/speech/tts/health") for _ in range(3))
            )

    assert all(response.json()["available"] is True for response in responses)
    mock_discover.assert_called_once_with("kokoro")


@pytest.mark.asyncio
async def test_concurrent_unavailable_probes_share_attempt_then_retry(mock_tts_backend):
    app = _unresolved_tts_app()
    attempts = 0

    def discover(_preferred):
        nonlocal attempts
        attempts += 1
        time.sleep(0.05)
        return None if attempts == 1 else mock_tts_backend

    with patch(
        "openjarvis.speech._tts_discovery.get_tts_backend", side_effect=discover
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            responses = await asyncio.gather(
                *(client.get("/v1/speech/tts/health") for _ in range(3))
            )
            assert all(response.json()["available"] is False for response in responses)
            assert attempts == 1
            assert (await client.get("/v1/speech/tts/health")).json()["available"]

    assert attempts == 2


def test_invalid_synthesis_does_not_load_tts_backend():
    client = TestClient(_unresolved_tts_app())

    with patch("openjarvis.speech._tts_discovery.get_tts_backend") as discover:
        response = client.post("/v1/speech/synthesize", json={"text": "  "})

    assert response.status_code == 400
    discover.assert_not_called()


def test_voice_id_not_reused_across_backends(mock_tts_backend):
    """A configured voice belongs to its backend; a fallback uses its own."""
    from types import SimpleNamespace

    config = SimpleNamespace(
        speech=SimpleNamespace(
            tts_backend="kokoro", voice_id="bm_george", voice_speed=1.0
        )
    )
    client = _tts_app(mock_tts_backend, config=config)

    client.post("/v1/speech/synthesize", json={"text": "Hallo"})

    # Backend is mock_tts, not the configured kokoro, so bm_george must not leak.
    assert mock_tts_backend.synthesize.call_args.kwargs["voice_id"] != "bm_george"


# -- Step 2: browser audio is converted for the local recogniser ---------------


def _post(client, data, name="clip.webm", ctype="audio/webm"):
    return client.post("/v1/speech/transcribe", files={"file": (name, data, ctype)})


def test_browser_webm_is_converted_to_wav_before_transcription(
    client, mock_speech_backend
):
    from jarvis_ext.speech.convert import PreparedAudio

    converted = _wav(seconds=0.2)
    with patch(
        "jarvis_ext.speech.convert.prepare_for_transcription",
        return_value=PreparedAudio(wav=converted, converted=True, source_format="webm"),
    ) as prepare:
        response = _post(client, b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
    assert response.status_code == 200
    prepare.assert_called_once()
    args, kwargs = mock_speech_backend.transcribe.call_args
    assert args == (converted,) and kwargs["format"] == "wav"


def test_wav_is_not_converted(client, mock_speech_backend):
    with patch("jarvis_ext.speech.convert.subprocess.run") as run:
        response = _post(client, WAV, "a.wav", "audio/wav")
    assert response.status_code == 200
    run.assert_not_called()
    assert mock_speech_backend.transcribe.call_args.args == (WAV,)


def test_empty_recording_is_a_400(client, mock_speech_backend):
    response = _post(client, b"")
    assert response.status_code == 400
    assert "empty" in response.json()["detail"]
    mock_speech_backend.transcribe.assert_not_called()


def test_unsupported_format_is_a_415(client, mock_speech_backend):
    response = _post(client, b"ID3\x04 an mp3", "x.wav", "audio/wav")
    assert response.status_code == 415
    mock_speech_backend.transcribe.assert_not_called()


def test_malformed_wav_is_a_400(client, mock_speech_backend):
    response = _post(client, b"RIFF\x00\x00\x00\x00WAVEjunk", "a.wav", "audio/wav")
    assert response.status_code == 400
    mock_speech_backend.transcribe.assert_not_called()


def test_oversized_upload_is_a_413(client, mock_speech_backend):
    big = b"\x1a\x45\xdf\xa3" + b"\x00" * (26 * 1024 * 1024)
    assert _post(client, big).status_code == 413
    mock_speech_backend.transcribe.assert_not_called()


@pytest.mark.parametrize(
    ("error", "status"),
    [
        ("SpeechTimeoutError", 504),
        ("SpeechUnavailableError", 503),
        ("SpeechInputError", 400),
    ],
)
def test_conversion_errors_map_to_http_statuses(
    client, mock_speech_backend, error, status
):
    from jarvis_ext.speech import errors

    with patch(
        "jarvis_ext.speech.convert.prepare_for_transcription",
        side_effect=getattr(errors, error)("conversion problem"),
    ):
        response = _post(client, b"\x1a\x45\xdf\xa3")
    assert response.status_code == status
    assert response.json()["detail"] == "conversion problem"
    mock_speech_backend.transcribe.assert_not_called()


def test_synthesize_uses_configured_george_voice_when_browser_sends_none(
    mock_tts_backend,
):
    """The browser sends only text; the backend config decides the voice."""
    mock_tts_backend.backend_id = "voicestudio"
    config = SimpleNamespace(
        speech=SimpleNamespace(
            tts_backend="voicestudio", voice_id="bm_george", voice_speed=1.0
        )
    )
    client = _tts_app(mock_tts_backend, config=config)

    response = client.post("/v1/speech/synthesize", json={"text": "Good evening."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    kwargs = mock_tts_backend.synthesize.call_args.kwargs
    assert kwargs["voice_id"] == "bm_george"
    assert kwargs["output_format"] == "wav"
