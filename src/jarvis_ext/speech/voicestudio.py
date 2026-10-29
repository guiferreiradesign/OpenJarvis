"""Speech output through the local VoiceStudio service.

``VoiceStudioTTSBackend`` implements the upstream ``TTSBackend`` interface and
is registered as ``"voicestudio"`` in ``TTSRegistry``.  It only talks to an
already-running VoiceStudio on this machine (loopback URLs only); it never
starts, configures or installs VoiceStudio and never downloads models.

JARVIS's voice is Kokoro 82M through MLX-Audio, voice ``bm_george``.  Every
request names the model, voice, language and format explicitly, so the result
never depends on which engine VoiceStudio has active::

    POST /v1/audio/speech
    {"model": "mlx-audio", "input": ..., "voice": "bm_george",
     "language": "British English", "response_format": "wav", "speed": 1.0}

Other requests: ``GET /health`` (liveness) and ``GET /v1/audio/voices``.

There is no cloud fallback here or in this module's callers' hands: when
VoiceStudio is unreachable the backend raises ``SpeechUnavailableError`` and
nothing else is tried.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, List, Optional

import httpx

from jarvis_ext.speech.audio import read_wav_info
from jarvis_ext.speech.errors import (
    SpeechError,
    SpeechInputError,
    SpeechResponseError,
    SpeechTimeoutError,
    SpeechUnavailableError,
)
from jarvis_ext.speech.settings import VoiceStudioSettings
from openjarvis.speech.tts import TTSBackend, TTSResult

logger = logging.getLogger(__name__)

BACKEND_ID = "voicestudio"  # == jarvis_ext.speech.TTS_BACKEND_ID
HEALTH_TIMEOUT_SECONDS = 2.0
MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 5.0
_NOT_RUNNING = (
    "VoiceStudio is not running on this machine; start the VoiceStudio app "
    "(see jarvis/voicestudio-plan.md)"
)

# Kokoro reads "JARVIS" / "J.A.R.V.I.S." letter by letter.  Spoken text uses
# the word "Jarvis" instead.  Matches JARVIS, Jarvis, jarvis, J.A.R.V.I.S.,
# J.A.R.V.I.S and J-A-R-V-I-S as a whole word (so "JARVIS's" works, "Jarvises"
# does not).
_JARVIS = re.compile(
    r"(?<![\w.])J([.\-]?)A\1R\1V\1I\1S(\.)?(?!\w)", re.IGNORECASE
)
_SPOKEN_JARVIS = "Jarvis"


def normalize_for_speech(text: str) -> str:
    """Return *text* with JARVIS's name written the way it should be spoken.

    ``JARVIS`` and ``J.A.R.V.I.S.`` become the word ``Jarvis`` so the voice
    says the whole name instead of spelling it.  Only the spoken copy is
    changed; displayed text is untouched.
    """

    def replace(match: "re.Match[str]") -> str:
        # The dot after the last letter of "J.A.R.V.I.S." belongs to the
        # acronym; keep it as a full stop only when a sentence really ends.
        dotted_end = bool(match.group(1)) and match.group(2)
        if match.group(2) and not dotted_end:
            return _SPOKEN_JARVIS + "."
        if dotted_end:
            rest = text[match.end() :]
            ends = not rest.strip() or re.match(r"\s+[A-Z]", rest) is not None
            return _SPOKEN_JARVIS + ("." if ends else "")
        return _SPOKEN_JARVIS

    return _JARVIS.sub(replace, text)


class VoiceStudioTTSBackend(TTSBackend):
    """Text-to-speech through a local VoiceStudio (Kokoro / ``bm_george``)."""

    backend_id = BACKEND_ID

    def __init__(
        self,
        settings: Optional[VoiceStudioSettings] = None,
        *,
        config_path: Optional[Path] = None,
        transport: Optional[httpx.BaseTransport] = None,
        sleep: Any = time.sleep,
    ) -> None:
        self._settings = settings or VoiceStudioSettings.load(config_path)
        self._transport = transport
        self._sleep = sleep

    @property
    def settings(self) -> VoiceStudioSettings:
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
        """Speak *text* with the configured (or given) voice.

        Always returns WAV, whatever *output_format* asks for; the result's
        ``format`` says so.  The sample rate is read from the WAV header.  The
        first request after VoiceStudio starts may load the model, so the
        request timeout is generous (``timeout_seconds``).
        """
        spoken = normalize_for_speech((text or "").strip())
        if not spoken:
            raise SpeechInputError("Nothing to say: text is empty")
        if len(spoken) > self._settings.max_text_chars:
            raise SpeechInputError(
                f"Text is longer than {self._settings.max_text_chars} characters"
            )
        if output_format and output_format.lower() != "wav":
            logger.debug("VoiceStudio is asked for WAV; ignoring %s", output_format)
        voice = (voice_id or "").strip() or self._settings.voice
        # speed 1.0 is the "caller said nothing" value; use the configured one.
        effective_speed = speed if speed != 1.0 else self._settings.speed
        body = {
            "model": self._settings.model,
            "input": spoken,
            "voice": voice,
            "language": self._settings.language,
            "response_format": "wav",
            "speed": effective_speed,
        }
        started = time.perf_counter()
        response = self._post_speech(body)
        elapsed = time.perf_counter() - started
        audio = response.content
        if len(audio) > MAX_AUDIO_BYTES:
            raise SpeechResponseError("VoiceStudio returned an unexpectedly large file")
        if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            raise SpeechResponseError("VoiceStudio did not return WAV audio")
        try:
            info = read_wav_info(audio)
        except SpeechInputError as exc:
            raise SpeechResponseError(
                "VoiceStudio returned unreadable WAV audio"
            ) from exc
        if info.frames == 0 or info.sample_rate <= 0:
            raise SpeechResponseError("VoiceStudio returned empty audio")
        return TTSResult(
            audio=audio,
            format="wav",
            duration_seconds=info.duration_seconds,
            voice_id=voice,
            sample_rate=info.sample_rate,
            metadata={
                "backend": BACKEND_ID,
                "model": self._settings.model,
                "language": self._settings.language,
                "channels": info.channels,
                "generation_seconds": round(elapsed, 4),
            },
        )

    def available_voices(self) -> List[str]:
        """Voice ids VoiceStudio lists (empty when it is not running)."""
        try:
            data = self._request(
                "GET", "/v1/audio/voices", timeout=HEALTH_TIMEOUT_SECONDS + 5.0
            ).json()
            return [
                str(v["id"])
                for v in data.get("voices", [])
                if isinstance(v, dict) and v.get("id")
            ]
        except (SpeechError, ValueError, AttributeError):
            return []

    def health(self) -> bool:
        """True if ``/health`` answers 200.  Never raises, never loads a model."""
        try:
            self._request("GET", "/health", timeout=HEALTH_TIMEOUT_SECONDS)
        except SpeechError:
            return False
        return True

    # -- HTTP ------------------------------------------------------------

    def _post_speech(self, body: dict) -> httpx.Response:
        """POST the request; retry once when VoiceStudio says it is busy."""
        try:
            return self._request(
                "POST",
                "/v1/audio/speech",
                json=body,
                timeout=self._settings.timeout_seconds,
            )
        except _Busy as busy:
            self._sleep(busy.retry_after)
            try:
                return self._request(
                    "POST",
                    "/v1/audio/speech",
                    json=body,
                    timeout=self._settings.timeout_seconds,
                )
            except _Busy as again:
                raise SpeechUnavailableError(str(again)) from again

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
                f"VoiceStudio did not answer within {timeout:g} s"
            ) from exc
        except httpx.TransportError as exc:
            raise SpeechUnavailableError(_NOT_RUNNING) from exc
        if response.status_code == 200:
            return response
        message = _error_message(response)
        status = response.status_code
        if status in (429, 503):
            raise _Busy(
                f"VoiceStudio is busy or still loading its model (HTTP {status})"
                f"{message}",
                _retry_after(response),
            )
        if status == 403:
            raise SpeechUnavailableError(
                f"VoiceStudio refused the request (HTTP 403){message}"
            )
        raise SpeechResponseError(
            f"VoiceStudio {path} failed with HTTP {status}{message}"
        )


class _Busy(SpeechUnavailableError):
    """HTTP 429/503 from VoiceStudio; retried once, then reported as unavailable."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(response: httpx.Response) -> float:
    try:
        value = float(response.headers.get("retry-after", "1"))
    except ValueError:
        value = 1.0
    return min(max(value, 0.0), MAX_RETRY_AFTER_SECONDS)


def _error_message(response: httpx.Response) -> str:
    """A short single-line server message for errors, if there is one."""
    try:
        data = response.json()
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return ""
    message = " ".join(message.split())[:200]
    return f": {message}" if message else ""


__all__ = ["BACKEND_ID", "VoiceStudioTTSBackend", "normalize_for_speech"]
