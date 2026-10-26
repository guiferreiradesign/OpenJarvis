"""Errors raised by the JARVIS speech providers.

All of them subclass :class:`RuntimeError`, which is what the upstream
OpenJarvis speech backends raise, so existing callers that catch
``RuntimeError`` or ``Exception`` keep working.  Messages are short and never
include absolute paths, audio content or full server responses.
"""

from __future__ import annotations


class SpeechError(RuntimeError):
    """Base class for speech-provider failures."""


class SpeechUnavailableError(SpeechError):
    """The provider cannot run here: missing package, unsupported platform,
    model that cannot be loaded, or a local service that is not running."""


class SpeechTimeoutError(SpeechError):
    """The provider did not answer in time."""


class SpeechResponseError(SpeechError):
    """The provider answered, but not with something usable."""


class SpeechInputError(SpeechError, ValueError):
    """The request itself is invalid (empty text, unreadable audio ...)."""


__all__ = [
    "SpeechError",
    "SpeechInputError",
    "SpeechResponseError",
    "SpeechTimeoutError",
    "SpeechUnavailableError",
]
