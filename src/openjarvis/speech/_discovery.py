"""Auto-discover available speech-to-text backends."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from openjarvis.core.config import JarvisConfig
    from openjarvis.speech._stubs import SpeechBackend

logger = logging.getLogger(__name__)

# Priority order: local first, then cloud
DISCOVERY_ORDER = [
    "faster-whisper",
    "openai",
    "deepgram",
]

# Backends that upload the recording to a third party. A cloud backend counts
# as healthy as soon as its API key exists, so auto-discovery never tries
# them; one is used only when ``speech.backend`` names it explicitly.
CLOUD_SPEECH_BACKENDS = frozenset({"openai", "deepgram"})


def _create_backend(
    key: str,
    config: "JarvisConfig",
) -> Optional["SpeechBackend"]:
    """Try to instantiate a speech backend by registry key."""
    from openjarvis.core.registry import SpeechRegistry

    if not SpeechRegistry.contains(key):
        return None

    try:
        backend_cls = SpeechRegistry.get(key)

        if key == "faster-whisper":
            return backend_cls(
                model_size=config.speech.model,
                device=config.speech.device,
                compute_type=config.speech.compute_type,
            )
        elif key == "openai":
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return None
            return backend_cls(api_key=api_key)
        elif key == "deepgram":
            api_key = os.environ.get("DEEPGRAM_API_KEY", "")
            if not api_key:
                return None
            return backend_cls(api_key=api_key)
        else:
            return backend_cls()
    except Exception:
        return None


def get_speech_backend(config: "JarvisConfig") -> Optional["SpeechBackend"]:
    """Resolve the speech backend from config.

    If ``config.speech.backend`` is ``"auto"``, tries the local backends in
    priority order and returns the first healthy one.  Cloud backends are
    skipped in auto mode, whatever API keys exist, so audio never leaves the
    machine unless ``speech.backend`` is set to ``"openai"`` or
    ``"deepgram"`` on purpose.  Returns ``None`` when nothing local works.
    """
    # Trigger registration of built-in backends
    import openjarvis.speech  # noqa: F401

    # JARVIS's local recogniser ("mlx-whisper") lives in jarvis_ext; make sure it
    # is registered even when the tool registry has not been imported yet.
    try:
        from jarvis_ext.speech import ensure_registered

        ensure_registered()
    except ImportError:
        pass

    backend_key = config.speech.backend

    if backend_key != "auto":
        backend = _create_backend(backend_key, config)
        if backend is None:
            return None
        try:
            return backend if backend.health() else None
        except Exception:
            return None

    # Auto-discovery: try each in priority order
    for key in DISCOVERY_ORDER:
        if key in CLOUD_SPEECH_BACKENDS:
            continue
        backend = _create_backend(key, config)
        if backend is None:
            continue
        try:
            if backend.health():
                return backend
        except Exception:
            continue

    logger.info(
        "No local speech-to-text backend is available; cloud speech is not "
        "used in auto mode (set speech.backend to opt in)"
    )
    return None
