"""The VoiceStudio probe, against a fake loopback server (no real service)."""

from __future__ import annotations

import io
import json
import math
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis_ext.speech.voicestudio_probe import analyse_wav, probe


def _wav(rate=24000, seconds=0.5, amplitude=8000, silent=False) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * seconds)
        vals = [0 if silent else int(amplitude * math.sin(i / 20)) for i in range(n)]
        w.writeframes(struct.pack(f"<{n}h", *vals))
    return buf.getvalue()


@pytest.fixture
def server():
    state = {"speech_status": 200, "audio": _wav(), "seen_headers": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D401
            pass

        def _send(self, status, body, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["seen_headers"].append(dict(self.headers))
            if self.path.startswith("/.well-known"):
                doc = {"protocol": "voicestudio.speech.v1", "service_version": "t",
                       "endpoints": {"capabilities": {}, "batch_transcription": {}}}
                self._send(200, json.dumps(doc).encode())
            elif self.path == "/health":
                self._send(200, b"{}")
            elif self.path == "/v1/audio/voices":
                self._send(200, json.dumps({"voices": [{"name": "alloy"}]}).encode())
            else:
                self._send(404, b"{}")

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if state["speech_status"] == 200:
                self._send(200, state["audio"], "audio/wav")
            else:
                self._send(state["speech_status"], b'{"error":{"code":"x"}}')

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", state
    httpd.shutdown()


def test_analyse_wav_reports_format_and_non_silence():
    info = analyse_wav(_wav(rate=22050, seconds=1.0))
    assert info["valid"] and info["sample_rate"] == 22050
    assert info["channels"] == 1 and info["bits"] == 16
    assert info["duration_s"] == 1.0 and info["non_silent"] is True


def test_silent_and_garbage_audio_are_not_accepted():
    assert analyse_wav(_wav(silent=True))["non_silent"] is False
    assert analyse_wav(b"not a wav")["valid"] is False
    assert analyse_wav(b"")["valid"] is False


def test_probe_reports_a_healthy_service(server):
    url, state = server
    report = probe(url)
    assert report["reachable"] and report["discovery"]["protocol"] == "voicestudio.speech.v1"
    assert report["discovery"]["advertises_tts"] is False
    assert report["health"]["status"] == 200
    assert report["voices"]["count"] == 1
    assert report["synthesis"]["cold"]["wav"]["valid"]
    assert report["synthesis"]["warm"]["status"] == 200
    assert all("Origin" not in h for h in state["seen_headers"])


def test_probe_reports_service_errors_without_raising(server):
    url, state = server
    state["speech_status"] = 503
    report = probe(url)
    assert report["synthesis"]["cold"]["status"] == 503
    assert "warm" not in report["synthesis"]


def test_probe_reports_unreachable_service():
    assert probe("http://127.0.0.1:1")["reachable"] is False


def test_probe_refuses_non_loopback_urls():
    with pytest.raises(ValueError):
        probe("http://example.com:3900")
