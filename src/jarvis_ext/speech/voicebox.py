"""Speech output through the local Voicebox service.

``VoiceboxTTSBackend`` implements the upstream ``TTSBackend`` interface and is
registered as ``"voicebox"`` in ``TTSRegistry``.  It only talks to an
already-running Voicebox on this machine; it never starts, stops, configures
or installs Voicebox, and it never downloads models through it.

Requests used (Voicebox API 0.5.0, checked against its ``/openapi.json``):

* ``GET /health`` – liveness.
* ``GET /profiles`` – voice profiles, to resolve the configured profile.
* ``POST /generate/stream`` with a ``GenerationRequest``
  (``profile_id``, ``text``, ``language``, ``engine``, ``personality``)
  – returns the WAV bytes directly and, per Voicebox, does not save them to
  its history.

``personality`` is always sent as ``false``: with ``true`` Voicebox rewrites
the text with its own language model before speaking, and JARVIS must stay
the only thing that decides what is said.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from jarvis_ext.speech.audio import read_wav_info
from jarvis_ext.speech.errors import (
    SpeechError,
    SpeechInputError,
    SpeechResponseError,
    SpeechTimeoutError,
    SpeechUnavailableError,
)
from jarvis_ext.speech.settings import SpeechOutputSettings
from openjarvis.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

BACKEND_ID = "voicebox"  # == jarvis_ext.speech.TTS_BACKEND_ID
HEALTH_TIMEOUT_SECONDS = 2.0
PROFILE_CACHE_SECONDS = 60.0
MAX_AUDIO_BYTES = 200 * 1024 * 1024
_NOT_RUNNING = (
    "Voicebox is not running on this machine; start the Voicebox app "
    "(see jarvis/speech.md)"
)


class VoiceboxTTSBackend(TTSBackend):
    """Text-to-speech through a local Voicebox service (default Kokoro/George)."""

    backend_id = BACKEND_ID

    def __init__(
        self,
        settings: Optional[SpeechOutputSettings] = None,
        *,
        config_path: Optional[Path] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._settings = settings or SpeechOutputSettings.load(config_path)
        self._transport = transport
        self._lock = threading.Lock()
        self._profiles: Optional[List[Dict[str, Any]]] = None
        self._profiles_at = 0.0

    @property
    def settings(self) -> SpeechOutputSettings:
        return self._settings

    # -- TTSBackend ------------------------------------------------------

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str = "",
        speed: float = 1.0,
        output_format: str = "wav",
    ) -> TTSResult:
        """Speak *text* with the configured (or given) Voicebox profile.

        Always returns WAV, whatever *output_format* asks for; the result's
        ``format`` says so.  Voicebox has no speed control, so *speed* is
        ignored.
        """
        text = (text or "").strip()
        if not text:
            raise SpeechInputError("Nothing to say: text is empty")
        if len(text) > self._settings.max_text_chars:
            raise SpeechInputError(
                f"Text is longer than {self._settings.max_text_chars} characters"
            )
        if speed != 1.0:
            logger.debug("Voicebox has no speed setting; ignoring speed=%s", speed)
        if output_format and output_format.lower() != "wav":
            logger.debug("Voicebox returns WAV; ignoring format=%s", output_format)

        profile = self.resolve_profile(voice_id)
        body = {
            "profile_id": profile["id"],
            "text": text,
            "language": self._settings.language,
            "engine": self._settings.engine,
            "personality": False,
        }
        started = time.perf_counter()
        response = self._request(
            "POST",
            "/generate/stream",
            json=body,
            timeout=self._settings.timeout_seconds,
        )
        elapsed = time.perf_counter() - started
        audio = response.content
        if len(audio) > MAX_AUDIO_BYTES:
            raise SpeechResponseError("Voicebox returned an unexpectedly large file")
        if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise SpeechResponseError("Voicebox did not return WAV audio")
        try:
            info = read_wav_info(audio)
        except SpeechInputError as exc:
            raise SpeechResponseError("Voicebox returned unreadable WAV audio") from exc
        if info.frames == 0:
            raise SpeechResponseError("Voicebox returned empty audio")
        return TTSResult(
            audio=audio,
            format="wav",
            duration_seconds=info.duration_seconds,
            voice_id=profile["id"],
            sample_rate=info.sample_rate,
            metadata={
                "backend": BACKEND_ID,
                "profile": profile.get("name", ""),
                "engine": self._settings.engine,
                "generation_seconds": round(elapsed, 4),
            },
        )

    def available_voices(self) -> List[str]:
        """Voicebox profile names (empty when the service is not running)."""
        try:
            return [p.get("name") or p["id"] for p in self._load_profiles()]
        except SpeechError:
            return []

    def health(self) -> bool:
        """True if Voicebox answers ``/health`` as healthy.  Never raises."""
        try:
            response = self._request("GET", "/health", timeout=HEALTH_TIMEOUT_SECONDS)
            data = response.json()
        except (SpeechError, ValueError):
            return False
        return isinstance(data, dict) and data.get("status") == "healthy"

    # -- profiles --------------------------------------------------------

    def resolve_profile(self, voice_id: str = "") -> Dict[str, Any]:
        """Find the profile for *voice_id*, or for the configured preference.

        *voice_id* (or ``profile``) may be a profile id, a profile name, or a
        preset voice such as ``bm_george``.  With neither, the first profile
        whose preset engine and voice match ``engine`` / ``voice`` is used.
        """
        profiles = self._load_profiles()
        wanted = (voice_id or "").strip() or self._settings.profile
        if wanted:
            for key in ("id", "name", "preset_voice_id"):
                for profile in profiles:
                    if profile.get(key) == wanted:
                        return profile
            folded = wanted.casefold()
            for profile in profiles:
                if str(profile.get("name", "")).casefold() == folded:
                    return profile
            raise SpeechUnavailableError(f"Voicebox has no voice profile '{wanted}'")
        for profile in profiles:
            if (
                profile.get("preset_engine") == self._settings.engine
                and profile.get("preset_voice_id") == self._settings.voice
            ):
                return profile
        raise SpeechUnavailableError(
            f"Voicebox has no profile using {self._settings.engine} voice "
            f"'{self._settings.voice}'; create one in Voicebox or set "
            "[jarvis.speech_output] profile"
        )

    def _load_profiles(self) -> List[Dict[str, Any]]:
        with self._lock:
            fresh = time.monotonic() - self._profiles_at < PROFILE_CACHE_SECONDS
            if self._profiles is not None and fresh:
                return self._profiles
        response = self._request(
            "GET", "/profiles", timeout=self._settings.connect_timeout_seconds + 5.0
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SpeechResponseError("Voicebox sent an invalid profile list") from exc
        if not isinstance(data, list):
            raise SpeechResponseError("Voicebox sent an invalid profile list")
        profiles = [
            p for p in data if isinstance(p, dict) and isinstance(p.get("id"), str)
        ]
        with self._lock:
            self._profiles = profiles
            self._profiles_at = time.monotonic()
        return profiles

    # -- HTTP ------------------------------------------------------------

    def _request(
        self, method: str, path: str, *, timeout: float, **kwargs: Any
    ) -> httpx.Response:
        url = self._settings.base_url + path
        limits = httpx.Timeout(timeout, connect=self._settings.connect_timeout_seconds)
        try:
            # trust_env=False: never route local traffic through a proxy from
            # the environment.
            with httpx.Client(
                timeout=limits,
                transport=self._transport,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise SpeechUnavailableError(_NOT_RUNNING) from exc
            raise SpeechTimeoutError(
                f"Voicebox did not answer within {timeout:g} s"
            ) from exc
        except httpx.TransportError as exc:
            raise SpeechUnavailableError(_NOT_RUNNING) from exc
        if response.status_code != 200:
            raise SpeechResponseError(
                f"Voicebox {path} failed with HTTP {response.status_code}"
                f"{_detail(response)}"
            )
        return response


def _detail(response: httpx.Response) -> str:
    """A short, single-line server message for errors, if there is one."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return ""
    if not isinstance(detail, str):
        return ""
    detail = " ".join(detail.split())[:200]
    return f": {detail}" if detail else ""


__all__ = ["BACKEND_ID", "VoiceboxTTSBackend"]
