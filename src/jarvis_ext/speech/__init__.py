"""JARVIS speech providers: speech input and speech output, kept apart.

* ``mlx_whisper.MlxWhisperBackend`` – speech-to-text (``SpeechBackend``),
  registered as ``"mlx-whisper"``.
* ``voicebox.VoiceboxTTSBackend`` – text-to-speech (``TTSBackend``),
  registered as ``"voicebox"``.

Both implement the upstream OpenJarvis interfaces in
``openjarvis.speech._stubs`` and ``openjarvis.speech.tts``, so agents and
callers depend on those interfaces, never on MLX or Voicebox.  Neither
provider is a tool and neither reasons: JARVIS (the agent) decides what to
say and what to do with a transcript.

Importing this package imports neither MLX, upstream ``openjarvis.speech``
nor any model: the registries hold small factories that import a provider
only when one is created, so text-only use pays nothing for speech.
Settings: ``[jarvis.speech_input]`` and ``[jarvis.speech_output]``
(``settings.py``).  Details: jarvis/speech.md.
"""

from __future__ import annotations

from typing import Any

from openjarvis.core.registry import SpeechRegistry, TTSRegistry

STT_BACKEND_ID = "mlx-whisper"
TTS_BACKEND_ID = "voicebox"


def create_mlx_whisper(**kwargs: Any) -> Any:
    """Create an :class:`MlxWhisperBackend` (registry factory)."""
    from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend

    return MlxWhisperBackend(**kwargs)


def create_voicebox(**kwargs: Any) -> Any:
    """Create a :class:`VoiceboxTTSBackend` (registry factory)."""
    from jarvis_ext.speech.voicebox import VoiceboxTTSBackend

    return VoiceboxTTSBackend(**kwargs)


def ensure_registered() -> None:
    """Register the speech providers unless the registries already hold them."""
    if not SpeechRegistry.contains(STT_BACKEND_ID):
        SpeechRegistry.register_value(STT_BACKEND_ID, create_mlx_whisper)
    if not TTSRegistry.contains(TTS_BACKEND_ID):
        TTSRegistry.register_value(TTS_BACKEND_ID, create_voicebox)


__all__ = [
    "STT_BACKEND_ID",
    "TTS_BACKEND_ID",
    "create_mlx_whisper",
    "create_voicebox",
    "ensure_registered",
]
