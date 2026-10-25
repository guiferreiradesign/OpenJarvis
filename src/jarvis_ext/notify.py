"""Local notifications for JARVIS: the log, and optionally macOS.

This module only *shows* a message; it knows nothing about jobs, video or
any skill (``jarvis_ext.jobs.notifications`` turns a finished job into a
:class:`Notification`).  Two backends, no dependency, no network (D21):

* ``log`` (default): one INFO line on the ``jarvis_ext.notify`` logger.
* ``macos``: a Notification Center banner through ``/usr/bin/osascript``,
  plus the same log line once it was shown.

**osascript safety.**  The AppleScript source is a fixed constant.  The
title and message are passed as *arguments* to its ``run`` handler (after
``--``), so no notification text is ever parsed as AppleScript.  The
command is an argument list run without a shell, with a minimal
environment, no stdin and a timeout.  Its output is never logged.

**Text safety.**  :class:`Notification` cleans its own fields: control
characters are removed, whitespace collapsed, anything that looks like a
filesystem path replaced by ``[path]``, and the length bounded.  Callers
should still build messages from safe values only (identifiers, not error
messages or payloads).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

BACKENDS = ("log", "macos")
DEFAULT_BACKEND = "log"

MAX_TITLE_CHARS = 80
MAX_MESSAGE_CHARS = 240
DEFAULT_OSASCRIPT = "/usr/bin/osascript"
OSASCRIPT_TIMEOUT_SECONDS = 10.0

# Fixed source; dynamic values only ever arrive as ``argv`` items.
APPLESCRIPT = (
    "on run argv",
    "display notification (item 2 of argv) with title (item 1 of argv)",
    "end run",
)

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
# "/abs/path", "~/x", "C:\x", "./x" and "../x" (up to the next whitespace).
_PATH_RE = re.compile(r"(?:(?<![\w.])(?:~|\.{1,2})?/|\b[A-Za-z]:\\)\S*")
_SPACE_RE = re.compile(r"\s+")


class NotificationError(Exception):
    """A notification could not be shown.

    *code* is a short identifier safe to log and store; *retryable* says
    whether trying again later might work (a timeout) or not (not macOS).
    """

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def clean_text(text: Any, limit: int) -> str:
    """One line of at most *limit* characters, with paths and control codes removed."""
    text = _CONTROL_RE.sub(" ", str(text))
    text = _PATH_RE.sub("[path]", text)
    text = _SPACE_RE.sub(" ", text).strip().lstrip("-").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


@dataclass(frozen=True)
class Notification:
    """A title and a one-line message, cleaned on construction."""

    title: str
    message: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "title", clean_text(self.title, MAX_TITLE_CHARS) or "JARVIS"
        )
        object.__setattr__(self, "message", clean_text(self.message, MAX_MESSAGE_CHARS))


class Notifier(Protocol):
    name: str

    def send(self, notification: Notification) -> None:
        """Show *notification*; raise :class:`NotificationError` on failure."""


class LogNotifier:
    """Writes each notification to the log.  Never fails."""

    name = "log"

    def send(self, notification: Notification) -> None:
        logger.info("Notification: %s — %s", notification.title, notification.message)


class MacOSNotifier:
    """Shows a Notification Center banner with ``osascript``.

    *osascript*, *runner* and *platform* are test seams; the defaults
    (resolved when the notifier is created) run the real ``osascript`` on
    the real platform.
    """

    name = "macos"

    def __init__(
        self,
        *,
        osascript: Optional[str] = None,
        runner: Optional[Callable[..., "subprocess.CompletedProcess[bytes]"]] = None,
        timeout: float = OSASCRIPT_TIMEOUT_SECONDS,
        platform: Optional[str] = None,
    ) -> None:
        self.osascript = osascript or DEFAULT_OSASCRIPT
        self._runner = runner or _default_runner
        self._timeout = timeout
        self._platform = platform or sys.platform

    def check(self) -> None:
        """Raise :class:`NotificationError` unless notifications can be shown here."""
        if self._platform != "darwin":
            raise NotificationError(
                "unsupported_platform",
                "macOS notifications are only available on macOS",
                retryable=False,
            )
        if not os.path.isfile(self.osascript) or not os.access(self.osascript, os.X_OK):
            raise NotificationError(
                "osascript_missing",
                "macOS notifications need osascript, which was not found",
                retryable=False,
            )

    def command(self, notification: Notification) -> list:
        """The exact argument list: fixed script, then ``--``, then the text."""
        script = []
        for line in APPLESCRIPT:
            script += ["-e", line]
        return [
            self.osascript,
            *script,
            "--",
            notification.title,
            notification.message,
        ]

    def send(self, notification: Notification) -> None:
        self.check()
        try:
            proc = self._runner(
                self.command(notification),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
                env=_minimal_env(),
            )
        except subprocess.TimeoutExpired:
            raise NotificationError(
                "timeout", "osascript did not respond", retryable=True
            ) from None
        except FileNotFoundError:
            raise NotificationError(
                "osascript_missing", "osascript was not found", retryable=False
            ) from None
        except OSError as exc:
            raise NotificationError(
                "osascript_unavailable",
                f"osascript could not be started ({type(exc).__name__})",
                retryable=True,
            ) from None
        if proc.returncode != 0:
            raise NotificationError(
                "osascript_failed",
                f"osascript exited with status {proc.returncode}",
                retryable=True,
            )
        logger.info(
            "Notification (macOS): %s — %s", notification.title, notification.message
        )


def make_notifier(backend: str = DEFAULT_BACKEND, **options: Any) -> Notifier:
    """The notifier for a configured backend name.

    ``macos`` is checked at once, so a worker started on another system (or
    without ``osascript``) fails clearly instead of silently not notifying.
    """
    if backend == "log":
        return LogNotifier()
    if backend == "macos":
        notifier = MacOSNotifier(**options)
        notifier.check()
        return notifier
    raise ValueError(f"Unknown notification backend {backend!r}")


# The process runner used by default; tests replace it so automated runs can
# never show a real notification.
_default_runner = subprocess.run


def _minimal_env() -> dict:
    env = {"PATH": "/usr/bin:/bin"}
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


__all__ = [
    "APPLESCRIPT",
    "BACKENDS",
    "DEFAULT_BACKEND",
    "LogNotifier",
    "MacOSNotifier",
    "Notification",
    "NotificationError",
    "Notifier",
    "clean_text",
    "make_notifier",
]
