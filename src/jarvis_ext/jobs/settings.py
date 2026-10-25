"""``[jarvis.jobs]`` settings for the job store.

::

    [jarvis.jobs]
    database_path = ""          # default: <OPENJARVIS_HOME>/jarvis/jobs.db
    max_attempts = 2            # 1 or 2 (DECISIONS.md D23)
    retry_delay_seconds = 60    # back-off before a temporary failure is retried
    poll_seconds = 2            # idle worker: how often to look for work
    heartbeat_seconds = 10      # running job: how often the worker reports in
    stale_after_seconds = 120   # no heartbeat for this long: worker presumed dead
    notifications = "log"       # "log", or "macos" (log + Notification Center)

The database holds job payloads (for video jobs, edit plans with workspace
paths), so it is kept with OpenJarvis' own state rather than in a media or
output folder, and is created readable only by you.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from jarvis_ext.config import ExtensionConfigError, load_extension_section
from jarvis_ext.jobs.models import MAX_ATTEMPTS
from jarvis_ext.notify import BACKENDS, DEFAULT_BACKEND

CONFIG_SECTION = "jobs"
DEFAULT_SUBDIR = "jarvis"
DEFAULT_FILENAME = "jobs.db"
LOCK_SUFFIX = ".worker.lock"

# (default, minimum, maximum) for the worker's timing settings.
TIMINGS: Dict[str, Tuple[float, float, float]] = {
    "retry_delay_seconds": (60.0, 0.0, 3600.0),
    "poll_seconds": (2.0, 0.1, 60.0),
    "heartbeat_seconds": (10.0, 0.05, 300.0),
    "stale_after_seconds": (120.0, 1.0, 86400.0),
}


def default_database_path() -> Path:
    from openjarvis.core.paths import get_data_dir

    return get_data_dir() / DEFAULT_SUBDIR / DEFAULT_FILENAME


@dataclass(frozen=True)
class JobSettings:
    """Validated ``[jarvis.jobs]`` settings."""

    database_path: Optional[Path] = None  # None: default_database_path()
    max_attempts: int = MAX_ATTEMPTS
    retry_delay_seconds: float = 60.0
    poll_seconds: float = 2.0
    heartbeat_seconds: float = 10.0
    stale_after_seconds: float = 120.0
    notifications: str = DEFAULT_BACKEND

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "JobSettings":
        known = {"database_path", "max_attempts", "notifications", *TIMINGS}
        unknown = sorted(set(section) - known)
        if unknown:
            raise ExtensionConfigError(
                f"Unknown [jarvis.{CONFIG_SECTION}] keys: {', '.join(unknown)}"
            )
        raw_path = section.get("database_path", "")
        if not isinstance(raw_path, str):
            raise ExtensionConfigError("jobs.database_path must be a string")
        database_path = _validate_path(raw_path) if raw_path.strip() else None

        attempts = section.get("max_attempts", MAX_ATTEMPTS)
        if (
            isinstance(attempts, bool)
            or not isinstance(attempts, int)
            or not 1 <= attempts <= MAX_ATTEMPTS
        ):
            raise ExtensionConfigError(
                f"jobs.max_attempts must be an integer between 1 and {MAX_ATTEMPTS}"
            )
        timings = {}
        for key, (default, low, high) in TIMINGS.items():
            raw = section.get(key, default)
            if (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or not math.isfinite(raw)
                or not low <= raw <= high
            ):
                raise ExtensionConfigError(
                    f"jobs.{key} must be a number between {low:g} and {high:g}"
                )
            timings[key] = float(raw)
        # A live worker must never look dead: allow three missed heartbeats.
        if timings["stale_after_seconds"] < 3 * timings["heartbeat_seconds"]:
            raise ExtensionConfigError(
                "jobs.stale_after_seconds must be at least 3 × heartbeat_seconds"
            )
        notifications = section.get("notifications", DEFAULT_BACKEND)
        if notifications not in BACKENDS:
            raise ExtensionConfigError(
                f"jobs.notifications must be one of: {', '.join(BACKENDS)}"
            )
        return cls(
            database_path=database_path,
            max_attempts=attempts,
            notifications=notifications,
            **timings,
        )

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "JobSettings":
        return cls.from_mapping(load_extension_section(CONFIG_SECTION, config_path))

    @property
    def uses_default_path(self) -> bool:
        return self.database_path is None

    def database_file(self) -> Path:
        return self.database_path or default_database_path()

    def lock_file(self) -> Path:
        """The worker's single-instance lock, next to the database."""
        db = self.database_file()
        return db.with_name(db.name + LOCK_SUFFIX)


def _validate_path(raw: str) -> Path:
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise ExtensionConfigError("jobs.database_path must be an absolute path")
    if not path.name or path.name in (".", ".."):
        raise ExtensionConfigError("jobs.database_path must name a file")
    # Resolve the folder only: the file itself may not exist yet, and a
    # symlinked file is refused when the store opens it.
    return path.parent.resolve() / path.name


__all__ = ["CONFIG_SECTION", "JobSettings", "default_database_path"]
