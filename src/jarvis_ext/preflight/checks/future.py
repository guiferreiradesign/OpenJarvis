"""Group E: speech providers that exist today, and roadmap features that do not.

Optional speech components that are missing are warnings, not failures.  A
roadmap feature with no code yet is reported as NOT IMPLEMENTED (a warning),
never as a pass.  When a feature's module appears, its placeholder keeps
warning ("no integration test registered") until a real check replaces it, so
new capability cannot silently go unverified.

To add the real test for a feature: write a ``@check(...)`` for it (in this
module or a new one listed in ``checks/__init__.py``) and delete its entry
from :data:`PLANNED`.
"""

from __future__ import annotations

import importlib.util
from typing import List, Tuple

from jarvis_ext.preflight.core import (
    Context,
    Outcome,
    check,
    failed,
    known_secret_values,
    passed,
    warned,
)


@check(
    "future.speech_output",
    "future",
    "Speech output provider (VoiceStudio) reachable",
    budget=5,
)
def speech_output(ctx: Context) -> Outcome:
    from jarvis_ext.config import ExtensionConfigError
    from jarvis_ext.speech.errors import SpeechError
    from jarvis_ext.speech.voicestudio import VoiceStudioTTSBackend

    try:
        backend = VoiceStudioTTSBackend(config_path=ctx.config_path)
    except (ExtensionConfigError, SpeechError) as exc:
        return failed(f"[jarvis.voicestudio] is invalid: {exc}")
    if not backend.health():
        return warned(
            "optional: VoiceStudio is not running at "
            f"{backend.settings.base_url}; JARVIS stays text-only. Nothing was spoken"
        )
    return passed(
        f"VoiceStudio healthy; JARVIS will ask for model {backend.settings.model!r}, "
        f"voice {backend.settings.voice!r}, {backend.settings.language}. "
        "Health only: no audio was generated"
    )


@check(
    "future.speech_input",
    "future",
    "Speech input provider (MLX Whisper) importable",
    budget=5,
)
def speech_input(ctx: Context) -> Outcome:
    from jarvis_ext.config import ExtensionConfigError
    from jarvis_ext.speech.errors import SpeechError
    from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend

    try:
        backend = MlxWhisperBackend(config_path=ctx.config_path)
    except (ExtensionConfigError, SpeechError) as exc:
        return failed(f"[jarvis.speech_input] is invalid: {exc}")
    reason = backend.unavailable_reason()
    if reason:
        return warned(
            f"optional: speech input unavailable ({reason}); text input unaffected"
        )
    return passed(
        "mlx-whisper importable. The model was not loaded and nothing was transcribed"
    )


# Upstream's TTS auto-discovery (openjarvis.speech._tts_discovery) tries the
# configured backend, then these in order.  The three cloud ones count as
# "healthy" as soon as their API key exists, so a stopped local speech
# service plus one of these keys sends reply text to that provider.
CLOUD_TTS_KEYS = (
    ("openai_tts", "OPENAI_API_KEY"),
    ("cartesia", "CARTESIA_API_KEY"),
    ("elevenlabs", "ELEVENLABS_API_KEY"),
)


@check(
    "future.speech_cloud_fallback",
    "future",
    "Speech cannot silently fall back to a cloud voice",
    budget=3,
)
def speech_cloud_fallback(ctx: Context) -> Outcome:
    configured = known_secret_values()
    exposed = [(backend, key) for backend, key in CLOUD_TTS_KEYS if key in configured]
    if not exposed:
        return passed(
            "no cloud TTS key (OpenAI, Cartesia, ElevenLabs) is configured, so "
            "upstream speech auto-discovery has no cloud voice to fall back to"
        )
    names = ", ".join(f"{backend} ({key} is set)" for backend, key in exposed)
    import inspect

    from openjarvis.core.config import load_config
    from openjarvis.speech._tts_discovery import get_tts_backend

    default = inspect.signature(get_tts_backend).parameters.get("allow_cloud")
    if default is None or default.default is not False:
        return failed(
            f"cloud TTS key(s) present ({names}) and speech discovery does not "
            "default to local-only: reply text could be sent to that provider"
        )
    speech = load_config(ctx.config_path).speech if ctx.config_path else None
    if speech is not None and (
        speech.allow_cloud_tts_fallback or speech.tts_backend in dict(CLOUD_TTS_KEYS)
    ):
        return warned(
            f"cloud TTS key(s) present ({names}) and the configuration "
            "explicitly enables a cloud voice (speech.tts_backend or "
            "speech.allow_cloud_tts_fallback). Nothing was sent by this check"
        )
    return passed(
        f"cloud TTS key(s) present ({names}) but cloud speech is not enabled: "
        "discovery is local-only unless speech.allow_cloud_tts_fallback is true"
    )


# (check id, title, module that will hold it, roadmap step)
PLANNED: List[Tuple[str, str, str, int]] = [
    (
        "future.voice_adapter",
        "Voice adapter + VoiceStudio speech output",
        "jarvis_ext.voice",
        1,
    ),
    (
        "future.whisper_loop",
        "Local Whisper speech recognition in the voice loop",
        "jarvis_ext.voice.listen",
        2,
    ),
    (
        "future.wake_word",
        "Hands-free wake word, push-to-talk, interruption",
        "jarvis_ext.voice.wake",
        3,
    ),
    (
        "future.notes",
        "Persistent Markdown notes with immediate indexing",
        "jarvis_ext.notes",
        4,
    ),
    (
        "future.brain_switch",
        "Explicit local/cloud brain switching + provider indicator",
        "jarvis_ext.brain",
        5,
    ),
    (
        "future.privacy_audit",
        "Local-mode privacy verification and network audit",
        "jarvis_ext.privacy",
        6,
    ),
]


def _module_exists(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _placeholder(check_id: str, title: str, module: str, step: int) -> None:
    @check(check_id, "future", title)
    def planned(ctx: Context, _module: str = module, _step: int = step) -> Outcome:
        if _module_exists(_module):
            return Outcome(
                "WARN",
                f"{_module} now exists but no integration check is registered for it "
                "yet. Add one to jarvis_ext/preflight/checks/future.py",
                ran=False,
            )
        return Outcome(
            "WARN",
            f"NOT IMPLEMENTED (roadmap step {_step}); nothing to test yet",
            ran=False,
        )


for _entry in PLANNED:
    _placeholder(*_entry)
