"""Tests for ``[jarvis.speech_input]`` / ``[jarvis.speech_output]`` settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_ext.config import ExtensionConfigError
from jarvis_ext.speech.settings import (
    DEFAULT_INITIAL_PROMPT,
    SpeechInputSettings,
    SpeechOutputSettings,
    validate_local_url,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestSpeechInputSettings:
    def test_defaults_are_the_phase_4_1_choice(self):
        settings = SpeechInputSettings()
        assert settings.model == "mlx-community/whisper-base-mlx"
        assert settings.language == "en"
        assert settings.initial_prompt == DEFAULT_INITIAL_PROMPT
        assert DEFAULT_INITIAL_PROMPT == (
            "JARVIS, Voicebox, Kokoro, George, Phase 4.1 roadmap"
        )

    def test_missing_config_file_gives_defaults(self, tmp_path):
        assert SpeechInputSettings.load(tmp_path / "none.toml") == SpeechInputSettings()

    def test_empty_section_gives_defaults(self):
        assert SpeechInputSettings.from_mapping({}) == SpeechInputSettings()

    def test_reads_config_file(self, tmp_path):
        path = _write(
            tmp_path,
            "[jarvis.speech_input]\n"
            'model = "mlx-community/whisper-small-mlx"\n'
            'language = "PT"\n'
            'initial_prompt = "Lisboa, JARVIS"\n'
            "max_audio_seconds = 30\n",
        )
        settings = SpeechInputSettings.load(path)
        assert settings.model == "mlx-community/whisper-small-mlx"
        assert settings.language == "pt"
        assert settings.initial_prompt == "Lisboa, JARVIS"
        assert settings.max_audio_seconds == 30.0

    def test_empty_language_and_prompt_mean_auto_and_none(self):
        settings = SpeechInputSettings.from_mapping(
            {"language": "", "initial_prompt": "  "}
        )
        assert settings.language is None
        assert settings.initial_prompt is None

    def test_local_model_folder_is_expanded(self):
        settings = SpeechInputSettings.from_mapping({"model": "~/models/whisper"})
        assert settings.model == str(Path("~/models/whisper").expanduser())

    @pytest.mark.parametrize(
        "section",
        [
            {"unknown": 1},
            {"model": ""},
            {"model": 3},
            {"language": "english"},
            {"initial_prompt": "x" * 601},
            {"max_audio_seconds": 0},
            {"max_audio_seconds": True},
            {"max_audio_seconds": float("nan")},
        ],
    )
    def test_invalid_values_are_errors(self, section):
        with pytest.raises(ExtensionConfigError):
            SpeechInputSettings.from_mapping(section)


class TestSpeechOutputSettings:
    def test_defaults_prefer_kokoro_george_on_localhost(self):
        settings = SpeechOutputSettings()
        assert settings.base_url == "http://127.0.0.1:17493"
        assert settings.engine == "kokoro"
        assert settings.voice == "bm_george"
        assert settings.profile == ""
        assert settings.language == "en"

    def test_reads_config_file(self, tmp_path):
        path = _write(
            tmp_path,
            "[jarvis.speech_output]\n"
            'base_url = "http://localhost:18000/"\n'
            'profile = "JARVIS"\n'
            "timeout_seconds = 20\n"
            "max_text_chars = 500\n",
        )
        settings = SpeechOutputSettings.load(path)
        assert settings.base_url == "http://localhost:18000"
        assert settings.profile == "JARVIS"
        assert settings.timeout_seconds == 20.0
        assert settings.max_text_chars == 500

    @pytest.mark.parametrize(
        "section",
        [
            {"unknown": 1},
            {"engine": "elevenlabs"},
            {"language": "xx"},
            {"timeout_seconds": 0},
            {"connect_timeout_seconds": 100},
            {"max_text_chars": 0},
            {"max_text_chars": 2.5},
            {"base_url": "http://192.168.1.10:17493"},
        ],
    )
    def test_invalid_values_are_errors(self, section):
        with pytest.raises(ExtensionConfigError):
            SpeechOutputSettings.from_mapping(section)


class TestValidateLocalUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:17493",
            "http://127.0.0.2:17493",
            "http://localhost:17493/",
            "http://[::1]:17493",
        ],
    )
    def test_loopback_urls_are_accepted(self, url):
        assert validate_local_url(url) == url.rstrip("/")

    @pytest.mark.parametrize(
        "url",
        [
            "https://127.0.0.1:17493",  # no TLS on a loopback service
            "http://0.0.0.0:17493",  # "all interfaces", not loopback
            "http://example.com:17493",
            "http://localhost.example.com:17493",
            "http://10.0.0.1:17493",
            "http://user:pw@127.0.0.1:17493",
            "http://127.0.0.1:17493/api",
            "http://127.0.0.1:17493?x=1",
            "http://127.0.0.1",  # port required
            "http://127.0.0.1:99999",
            "127.0.0.1:17493",
            "",
        ],
    )
    def test_other_urls_are_refused(self, url):
        with pytest.raises(ExtensionConfigError):
            validate_local_url(url)
