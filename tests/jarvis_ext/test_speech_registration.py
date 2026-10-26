"""The speech providers plug into the upstream registries without changing
tools, permissions or text-only behaviour."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import jarvis_ext
from jarvis_ext.speech import STT_BACKEND_ID, TTS_BACKEND_ID
from jarvis_ext.speech.mlx_whisper import MlxWhisperBackend
from jarvis_ext.speech.voicebox import VoiceboxTTSBackend
from jarvis_ext.tools.media_jobs import QUEUE_CAPABILITIES, STATUS_CAPABILITIES
from openjarvis.core.registry import SpeechRegistry, ToolRegistry, TTSRegistry

JARVIS_TOOLS = {
    "media_workspace_inspect",
    "media_render",
    "media_validate",
    "media_job_submit",
    "media_job_status",
    "media_job_revise",
}


class TestRegistration:
    def test_providers_are_registered(self):
        jarvis_ext.ensure_registered()
        jarvis_ext.ensure_registered()  # idempotent
        assert SpeechRegistry.contains(STT_BACKEND_ID)
        assert TTSRegistry.contains(TTS_BACKEND_ID)
        assert isinstance(SpeechRegistry.get(STT_BACKEND_ID)(), MlxWhisperBackend)
        assert isinstance(TTSRegistry.get(TTS_BACKEND_ID)(), VoiceboxTTSBackend)

    def test_no_new_tools_and_unchanged_permissions(self):
        jarvis_ext.ensure_registered()
        names = {name for name, _ in ToolRegistry.items()}
        assert names == JARVIS_TOOLS
        specs = {name: ToolRegistry.get(name)().spec for name in JARVIS_TOOLS}
        assert specs["media_job_submit"].required_capabilities == QUEUE_CAPABILITIES
        assert specs["media_job_revise"].required_capabilities == QUEUE_CAPABILITIES
        assert specs["media_job_status"].required_capabilities == STATUS_CAPABILITIES
        assert specs["media_workspace_inspect"].required_capabilities == ["file:read"]
        assert specs["media_render"].required_capabilities == [
            "file:read",
            "file:write",
        ]
        # Unconfigured: confirmation stays at its default (off).
        assert specs["media_render"].requires_confirmation is False
        assert specs["media_job_submit"].requires_confirmation is False

    def test_upstream_discovery_can_select_the_providers(self, monkeypatch):
        from jarvis_ext.speech import mlx_whisper as mw
        from openjarvis.speech._discovery import get_speech_backend
        from openjarvis.speech._tts_discovery import get_tts_backend

        jarvis_ext.ensure_registered()
        monkeypatch.setattr(mw, "platform_supported", lambda: True)
        monkeypatch.setattr(mw, "_module_present", lambda name: True)
        monkeypatch.setattr(MlxWhisperBackend, "__init__", _default_init(mw))
        config = SimpleNamespace(speech=SimpleNamespace(backend="mlx-whisper"))
        stt = get_speech_backend(config)
        assert isinstance(stt, MlxWhisperBackend)
        assert not stt.is_loaded  # discovery never loads the model

        monkeypatch.setattr(VoiceboxTTSBackend, "health", lambda self: True)
        assert isinstance(get_tts_backend("voicebox"), VoiceboxTTSBackend)

    def test_auto_discovery_order_is_unchanged(self):
        from openjarvis.speech._discovery import DISCOVERY_ORDER
        from openjarvis.speech._tts_discovery import TTS_BACKEND_ORDER

        assert STT_BACKEND_ID not in DISCOVERY_ORDER
        assert TTS_BACKEND_ID not in TTS_BACKEND_ORDER

    def test_unhealthy_providers_are_skipped(self, monkeypatch):
        from jarvis_ext.speech import mlx_whisper as mw
        from openjarvis.speech._discovery import get_speech_backend

        jarvis_ext.ensure_registered()
        monkeypatch.setattr(mw, "_module_present", lambda name: False)
        config = SimpleNamespace(speech=SimpleNamespace(backend="mlx-whisper"))
        assert get_speech_backend(config) is None


def test_importing_the_extension_loads_no_speech_code():
    """Text-only use must not pay for speech: no MLX, no upstream speech."""
    code = (
        "import sys, openjarvis.tools, jarvis_ext\n"
        "from openjarvis.core.registry import SpeechRegistry\n"
        "assert SpeechRegistry.contains('mlx-whisper')\n"
        "bad = [m for m in ('mlx', 'mlx_whisper', 'openjarvis.speech',"
        " 'jarvis_ext.speech.mlx_whisper', 'jarvis_ext.speech.voicebox')"
        " if m in sys.modules]\n"
        "print(bad)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def _default_init(mw):
    original = MlxWhisperBackend.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("platform_check", lambda: True)
        kwargs.setdefault("module_loader", _no_imports)
        original(self, *args, **kwargs)

    return init


def _no_imports(name):
    raise AssertionError(f"discovery imported {name}")
