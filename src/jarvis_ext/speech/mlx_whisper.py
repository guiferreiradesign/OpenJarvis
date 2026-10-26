"""Local speech-to-text with MLX Whisper on Apple Silicon.

``MlxWhisperBackend`` implements the upstream ``SpeechBackend`` interface, so
anything that accepts an OpenJarvis speech backend can use it.  It is
registered as ``"mlx-whisper"`` in ``SpeechRegistry``.

Latency rules (Phase 4.1):

* Nothing is imported or downloaded when this module is imported.  The model
  is loaded on :meth:`MlxWhisperBackend.load` or, lazily, on the first
  transcription, and then stays in memory for the life of the process.
  ``mlx_whisper`` keeps it in its process-wide ``ModelHolder``; loading it
  here through the same holder means the first request does not pay for it
  twice and later requests never reload it.
* Audio is decoded in process (``jarvis_ext.speech.audio``); no ``ffmpeg``
  or Python subprocess is started per utterance.
* Inference is serialised with a lock: one MLX model, one request at a time.
* Cold loading and warm inference are timed separately (:meth:`stats`).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import platform
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional

from jarvis_ext.speech.audio import WHISPER_SAMPLE_RATE, decode_wav
from jarvis_ext.speech.errors import (
    SpeechError,
    SpeechInputError,
    SpeechUnavailableError,
)
from jarvis_ext.speech.settings import SpeechInputSettings
from openjarvis.speech._stubs import Segment, SpeechBackend, TranscriptionResult

logger = logging.getLogger(__name__)

BACKEND_ID = "mlx-whisper"  # == jarvis_ext.speech.STT_BACKEND_ID
INSTALL_HINT = "install the speech-mlx extra (see jarvis/speech.md)"


def platform_supported() -> bool:
    """MLX runs only on Apple Silicon Macs."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


@dataclass(frozen=True)
class TranscriptionTiming:
    """How long one transcription took, with model loading kept apart."""

    cold: bool  # True if this request had to load the model first
    load_seconds: float  # 0.0 for warm requests
    inference_seconds: float
    audio_seconds: float


class MlxWhisperBackend(SpeechBackend):
    """Speech-to-text with ``mlx-whisper`` (default ``whisper-base-mlx``)."""

    backend_id = BACKEND_ID

    def __init__(
        self,
        settings: Optional[SpeechInputSettings] = None,
        *,
        config_path: Optional[Path] = None,
        module_loader: Callable[[str], ModuleType] = importlib.import_module,
        platform_check: Callable[[], bool] = platform_supported,
    ) -> None:
        self._settings = settings or SpeechInputSettings.load(config_path)
        self._import = module_loader
        self._platform_check = platform_check
        self._lock = threading.RLock()
        self._transcribe_fn: Optional[Callable[..., Dict[str, Any]]] = None
        self._load_seconds: Optional[float] = None
        self._load_count = 0
        self._transcriptions = 0
        self._last: Optional[TranscriptionTiming] = None

    # -- SpeechBackend ---------------------------------------------------

    def transcribe(
        self,
        audio: bytes,
        *,
        format: str = "wav",
        language: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe a PCM WAV recording and return the raw Whisper text."""
        if (format or "").lower().lstrip(".") != "wav":
            raise SpeechInputError("MLX Whisper accepts WAV audio only")
        samples = decode_wav(audio, max_seconds=self._settings.max_audio_seconds)
        return self.transcribe_waveform(samples, language=language)

    def health(self) -> bool:
        """True if this machine can run the backend.  Never loads the model."""
        return self.unavailable_reason() is None

    def supported_formats(self) -> List[str]:
        return ["wav"]

    # -- JARVIS additions ------------------------------------------------

    @property
    def settings(self) -> SpeechInputSettings:
        return self._settings

    @property
    def is_loaded(self) -> bool:
        return self._transcribe_fn is not None

    def unavailable_reason(self) -> Optional[str]:
        """Why the backend cannot run here, or None if it can."""
        if not self._platform_check():
            return "MLX Whisper needs an Apple Silicon Mac (macOS on arm64)"
        if self._transcribe_fn is None and not _module_present("mlx_whisper"):
            return f"mlx-whisper is not installed; {INSTALL_HINT}"
        return None

    def load(self) -> float:
        """Load the model now (if needed) and return the seconds it took.

        Returns ``0.0`` when the model is already loaded.  A first load may
        download the model (about 145 MB for whisper-base-mlx) into the
        Hugging Face cache.
        """
        with self._lock:
            if self._transcribe_fn is not None:
                return 0.0
            reason = self.unavailable_reason()
            if reason:
                raise SpeechUnavailableError(reason)
            model = self._settings.model
            shown = _model_label(model)
            logger.info(
                "Loading speech model %s (the first use may download it)", shown
            )
            started = time.perf_counter()
            try:
                package = self._import("mlx_whisper")
                transcribe_fn = getattr(package, "transcribe")
                holder_module = self._import("mlx_whisper.transcribe")
                mx = self._import("mlx.core")
                holder = getattr(holder_module, "ModelHolder", None)
                if holder is not None:
                    # transcribe() uses float16 unless fp16=False is passed.
                    holder.get_model(model, mx.float16)
                else:  # older/newer layout: warm up through the public API
                    transcribe_fn(
                        _silence(), path_or_hf_repo=model, language="en", verbose=None
                    )
            except ImportError as exc:
                logger.warning("Cannot import mlx-whisper: %s", exc)
                raise SpeechUnavailableError(
                    f"mlx-whisper is not installed; {INSTALL_HINT}"
                ) from exc
            except Exception as exc:  # download, file or MLX errors
                logger.warning("Loading speech model %s failed", shown, exc_info=True)
                raise SpeechUnavailableError(
                    f"Cannot load speech model {shown}: {type(exc).__name__}"
                ) from exc
            elapsed = time.perf_counter() - started
            self._transcribe_fn = transcribe_fn
            self._load_seconds = elapsed
            self._load_count += 1
            logger.info("Speech model %s loaded in %.2f s", shown, elapsed)
            return elapsed

    def transcribe_waveform(
        self, samples: Any, *, language: Optional[str] = None
    ) -> TranscriptionResult:
        """Transcribe mono float32 samples at 16 kHz (as from ``decode_wav``)."""
        audio_seconds = len(samples) / WHISPER_SAMPLE_RATE
        if audio_seconds <= 0:
            raise SpeechInputError("Audio contains no samples")
        lang = (language or "").strip().lower() or self._settings.language
        with self._lock:
            cold = self._transcribe_fn is None
            load_seconds = self.load() if cold else 0.0
            assert self._transcribe_fn is not None
            started = time.perf_counter()
            try:
                raw = self._transcribe_fn(
                    samples,
                    path_or_hf_repo=self._settings.model,
                    language=lang,
                    initial_prompt=self._settings.initial_prompt,
                    verbose=None,
                )
            except Exception as exc:
                logger.warning("MLX Whisper transcription failed", exc_info=True)
                raise SpeechError(
                    f"Transcription failed: {type(exc).__name__}"
                ) from exc
            inference = time.perf_counter() - started
            self._transcriptions += 1
            self._last = TranscriptionTiming(
                cold=cold,
                load_seconds=load_seconds,
                inference_seconds=inference,
                audio_seconds=audio_seconds,
            )
        logger.debug(
            "Transcribed %.2f s of audio in %.3f s (%s)",
            audio_seconds,
            inference,
            "cold" if cold else "warm",
        )
        return _to_result(raw, lang, audio_seconds)

    @property
    def last_timing(self) -> Optional[TranscriptionTiming]:
        return self._last

    def stats(self) -> Dict[str, Any]:
        """Diagnostics: load state and timings.  No paths, no transcripts."""
        last = self._last
        return {
            "backend": BACKEND_ID,
            "model": _model_label(self._settings.model),
            "language": self._settings.language,
            "initial_prompt": self._settings.initial_prompt,
            "loaded": self.is_loaded,
            "load_count": self._load_count,
            "load_seconds": _round(self._load_seconds),
            "transcriptions": self._transcriptions,
            "last_cold": last.cold if last else None,
            "last_inference_seconds": _round(last.inference_seconds) if last else None,
            "last_audio_seconds": _round(last.audio_seconds) if last else None,
        }


def _to_result(
    raw: Any, requested_language: Optional[str], audio_seconds: float
) -> TranscriptionResult:
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        raise SpeechError("Transcription failed: unexpected result from mlx-whisper")
    segments = []
    for item in raw.get("segments") or []:
        if not isinstance(item, dict):
            continue
        try:
            segments.append(
                Segment(
                    text=str(item.get("text", "")).strip(),
                    start=float(item.get("start", 0.0)),
                    end=float(item.get("end", 0.0)),
                )
            )
        except (TypeError, ValueError):
            continue
    detected = raw.get("language")
    return TranscriptionResult(
        text=raw["text"].strip(),
        language=detected if isinstance(detected, str) else requested_language,
        duration_seconds=audio_seconds,
        segments=segments,
    )


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _model_label(model: str) -> str:
    """A repo id as is; a local model folder by its name only (no paths)."""
    if model.startswith(("/", ".", "~")) or "\\" in model:
        return Path(model).name or "local model"
    return model


def _silence() -> Any:
    import numpy as np

    return np.zeros(WHISPER_SAMPLE_RATE, dtype=np.float32)


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 4)


__all__ = [
    "BACKEND_ID",
    "MlxWhisperBackend",
    "TranscriptionTiming",
    "platform_supported",
]
