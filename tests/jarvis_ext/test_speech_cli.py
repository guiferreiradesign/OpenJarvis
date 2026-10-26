"""Tests for the ``python -m jarvis_ext speech-*`` commands (all mocked)."""

from __future__ import annotations

import json

import pytest

from jarvis_ext.__main__ import main
from jarvis_ext.speech import mlx_whisper as mw
from jarvis_ext.speech.errors import SpeechUnavailableError
from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend
from jarvis_ext.speech.voicebox import VoiceboxTTSBackend
from openjarvis.speech.tts import TTSResult


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("", encoding="utf-8")
    return path


def test_check_reports_both_providers_without_loading(config, monkeypatch, capsys):
    monkeypatch.setattr(mw, "_module_present", lambda name: False)
    monkeypatch.setattr(VoiceboxTTSBackend, "health", lambda self: False)
    assert main(["speech-check", "--config", str(config)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["speech_input"]["loaded"] is False
    assert report["speech_input"]["error"]
    assert report["speech_output"] == {
        "backend": "voicebox",
        "base_url": "http://127.0.0.1:17493",
        "running": False,
    }


def test_transcribe_reports_cold_and_warm_runs(config, tmp_path, monkeypatch, capsys):
    from tests.jarvis_ext.test_speech_mlx_whisper import FakeMlx, speech_wav

    fake = FakeMlx()
    original = MlxWhisperBackend.__init__

    def init(self, *args, **kwargs):
        original(
            self, *args, module_loader=fake.load, platform_check=lambda: True, **kwargs
        )

    monkeypatch.setattr(mw, "_module_present", lambda name: True)
    monkeypatch.setattr(MlxWhisperBackend, "__init__", init)
    wav = tmp_path / "in.wav"
    wav.write_bytes(speech_wav())
    argv = ["speech-transcribe", str(wav), "--repeat", "3", "--config", str(config)]
    assert main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["text"] == "Hello JARVIS, this is Voicebox speaking."
    assert len(out["warm_inference_seconds"]) == 3
    assert out["cold_load_seconds"] >= 0
    assert out["stats"]["load_count"] == 1
    assert len(fake.model_loads) == 1


def test_say_writes_a_new_file_only(config, tmp_path, monkeypatch, capsys):
    calls = []

    def synthesize(self, text, **kwargs):
        calls.append(text)
        return TTSResult(audio=b"RIFFdata", format="wav", sample_rate=24000)

    monkeypatch.setattr(VoiceboxTTSBackend, "synthesize", synthesize)
    out = tmp_path / "say.wav"
    assert main(["speech-say", "Hi", "--out", str(out), "--config", str(config)]) == 0
    assert out.read_bytes() == b"RIFFdata"
    assert json.loads(capsys.readouterr().out)["file"] == "say.wav"
    assert main(["speech-say", "Hi", "--out", str(out), "--config", str(config)]) == 1
    assert calls == ["Hi"]  # the second call stopped before synthesising


def test_say_reports_provider_errors(config, tmp_path, monkeypatch, capsys):
    def synthesize(self, text, **kwargs):
        raise SpeechUnavailableError("Voicebox is not running on this machine")

    monkeypatch.setattr(VoiceboxTTSBackend, "synthesize", synthesize)
    argv = ["speech-say", "Hi", "--out", str(tmp_path / "x.wav")]
    assert main([*argv, "--config", str(config)]) == 1
    assert "not running" in capsys.readouterr().err
    assert not (tmp_path / "x.wav").exists()
