"""``[jarvis.speech_input]`` and ``[jarvis.speech_output]`` settings.

::

    [jarvis.speech_input]                    # MLX Whisper (Apple Silicon only)
    model = "mlx-community/whisper-base-mlx" # HF repo id or a local model folder
    language = "en"                          # "" lets Whisper detect it
    initial_prompt = "JARVIS, Voicebox, Kokoro, George, Phase 4.1 roadmap"
    max_audio_seconds = 120                  # longer recordings are refused

    [jarvis.speech_output]                   # local Voicebox service
    base_url = "http://127.0.0.1:17493"      # loopback only
    profile = ""            # profile id or name; "" = the engine/voice below
    engine = "kokoro"
    voice = "bm_george"
    language = "en"
    timeout_seconds = 60                     # whole generation request
    connect_timeout_seconds = 2
    max_text_chars = 4000

Both sections are optional; the defaults are the configuration chosen in
Phase 4.1 (see jarvis/speech.md).  As elsewhere in ``jarvis_ext``, unknown
keys and out-of-range values are errors rather than silently ignored.
"""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from jarvis_ext.config import ExtensionConfigError, load_extension_section

INPUT_SECTION = "speech_input"
OUTPUT_SECTION = "speech_output"

DEFAULT_STT_MODEL = "mlx-community/whisper-base-mlx"
DEFAULT_STT_LANGUAGE = "en"
DEFAULT_INITIAL_PROMPT = "JARVIS, Voicebox, Kokoro, George, Phase 4.1 roadmap"

DEFAULT_VOICEBOX_URL = "http://127.0.0.1:17493"
DEFAULT_TTS_ENGINE = "kokoro"
DEFAULT_TTS_VOICE = "bm_george"
DEFAULT_TTS_LANGUAGE = "en"

# Whisper prompts are limited to about 224 tokens; this keeps well inside it.
MAX_PROMPT_CHARS = 600
# Voicebox's own GenerationRequest patterns (from its OpenAPI schema).
VOICEBOX_ENGINES = (
    "qwen",
    "qwen_custom_voice",
    "luxtts",
    "chatterbox",
    "chatterbox_turbo",
    "tada",
    "kokoro",
)
_VOICEBOX_LANGUAGE = re.compile(
    r"^(zh|en|ja|ko|de|fr|ru|pt|es|it|he|ar|da|el|fi|hi|ms|nl|no|pl|sv|sw|tr)$"
)
_WHISPER_LANGUAGE = re.compile(r"^[a-z]{2,3}$")
_LOOPBACK_NAMES = ("localhost",)


def _check_keys(section: Mapping[str, Any], name: str, known: set) -> None:
    unknown = sorted(set(section) - known)
    if unknown:
        raise ExtensionConfigError(
            f"Unknown [jarvis.{name}] keys: {', '.join(unknown)}"
        )


def _string(section: Mapping[str, Any], name: str, key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise ExtensionConfigError(f"{name}.{key} must be a string")
    return value.strip()


def _number(
    section: Mapping[str, Any],
    name: str,
    key: str,
    bounds: Tuple[float, float, float],
) -> float:
    default, low, high = bounds
    raw = section.get(key, default)
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(raw)
        or not low <= raw <= high
    ):
        raise ExtensionConfigError(
            f"{name}.{key} must be a number between {low:g} and {high:g}"
        )
    return float(raw)


@dataclass(frozen=True)
class SpeechInputSettings:
    """Validated ``[jarvis.speech_input]`` settings."""

    model: str = DEFAULT_STT_MODEL
    language: Optional[str] = DEFAULT_STT_LANGUAGE  # None: auto-detect
    initial_prompt: Optional[str] = DEFAULT_INITIAL_PROMPT  # None: no prompt
    max_audio_seconds: float = 120.0

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "SpeechInputSettings":
        name = INPUT_SECTION
        _check_keys(
            section, name, {"model", "language", "initial_prompt", "max_audio_seconds"}
        )
        model = _string(section, name, "model", DEFAULT_STT_MODEL)
        if not model:
            raise ExtensionConfigError(f"{name}.model must not be empty")
        if model.startswith("~"):
            model = str(Path(model).expanduser())
        language = _string(section, name, "language", DEFAULT_STT_LANGUAGE).lower()
        if language and not _WHISPER_LANGUAGE.match(language):
            raise ExtensionConfigError(
                f"{name}.language must be a language code such as 'en', or ''"
            )
        prompt = _string(section, name, "initial_prompt", DEFAULT_INITIAL_PROMPT)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ExtensionConfigError(
                f"{name}.initial_prompt must be at most {MAX_PROMPT_CHARS} characters"
            )
        return cls(
            model=model,
            language=language or None,
            initial_prompt=prompt or None,
            max_audio_seconds=_number(
                section, name, "max_audio_seconds", (120.0, 1.0, 1800.0)
            ),
        )

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "SpeechInputSettings":
        return cls.from_mapping(load_extension_section(INPUT_SECTION, config_path))


@dataclass(frozen=True)
class SpeechOutputSettings:
    """Validated ``[jarvis.speech_output]`` settings."""

    base_url: str = DEFAULT_VOICEBOX_URL
    profile: str = ""  # "": first profile using `engine` + `voice`
    engine: str = DEFAULT_TTS_ENGINE
    voice: str = DEFAULT_TTS_VOICE
    language: str = DEFAULT_TTS_LANGUAGE
    timeout_seconds: float = 60.0
    connect_timeout_seconds: float = 2.0
    max_text_chars: int = 4000

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "SpeechOutputSettings":
        name = OUTPUT_SECTION
        _check_keys(
            section,
            name,
            {
                "base_url",
                "profile",
                "engine",
                "voice",
                "language",
                "timeout_seconds",
                "connect_timeout_seconds",
                "max_text_chars",
            },
        )
        base_url = validate_local_url(
            _string(section, name, "base_url", DEFAULT_VOICEBOX_URL), name
        )
        engine = _string(section, name, "engine", DEFAULT_TTS_ENGINE)
        if engine not in VOICEBOX_ENGINES:
            raise ExtensionConfigError(
                f"{name}.engine must be one of: {', '.join(VOICEBOX_ENGINES)}"
            )
        language = _string(section, name, "language", DEFAULT_TTS_LANGUAGE)
        if not _VOICEBOX_LANGUAGE.match(language):
            raise ExtensionConfigError(
                f"{name}.language is not a language Voicebox supports"
            )
        max_chars = section.get("max_text_chars", 4000)
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 1 <= max_chars <= 50000
        ):
            raise ExtensionConfigError(
                f"{name}.max_text_chars must be an integer between 1 and 50000"
            )
        return cls(
            base_url=base_url,
            profile=_string(section, name, "profile", ""),
            engine=engine,
            voice=_string(section, name, "voice", DEFAULT_TTS_VOICE),
            language=language,
            timeout_seconds=_number(
                section, name, "timeout_seconds", (60.0, 1.0, 600.0)
            ),
            connect_timeout_seconds=_number(
                section, name, "connect_timeout_seconds", (2.0, 0.1, 30.0)
            ),
            max_text_chars=max_chars,
        )

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "SpeechOutputSettings":
        return cls.from_mapping(load_extension_section(OUTPUT_SECTION, config_path))


def validate_local_url(raw: str, section: str = OUTPUT_SECTION) -> str:
    """Return *raw* without a trailing slash if it is a plain loopback HTTP URL.

    The speech service is local by design: only ``http`` to ``localhost`` or a
    loopback address, with no credentials, path, query or fragment.
    """
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise ExtensionConfigError(f"{section}.base_url is not a valid URL") from exc
    host = (parts.hostname or "").lower()
    if parts.scheme != "http" or not host:
        raise ExtensionConfigError(f"{section}.base_url must be an http:// URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ExtensionConfigError(
            f"{section}.base_url must not contain credentials, a query or a fragment"
        )
    if parts.path not in ("", "/"):
        raise ExtensionConfigError(f"{section}.base_url must not contain a path")
    if host not in _LOOPBACK_NAMES:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ExtensionConfigError(
                f"{section}.base_url must point at this machine "
                "(localhost or a loopback address)"
            )
    if port is None:
        raise ExtensionConfigError(f"{section}.base_url must include a port")
    return raw.rstrip("/")


__all__ = [
    "DEFAULT_INITIAL_PROMPT",
    "DEFAULT_STT_LANGUAGE",
    "DEFAULT_STT_MODEL",
    "DEFAULT_TTS_ENGINE",
    "DEFAULT_TTS_VOICE",
    "DEFAULT_VOICEBOX_URL",
    "INPUT_SECTION",
    "OUTPUT_SECTION",
    "SpeechInputSettings",
    "SpeechOutputSettings",
    "validate_local_url",
]
