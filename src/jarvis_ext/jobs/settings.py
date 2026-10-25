"""``[jarvis.jobs]`` settings for the job store.

::

    [jarvis.jobs]
    database_path = ""   # default: <OPENJARVIS_HOME>/jarvis/jobs.db
    max_attempts = 2     # 1 or 2 (DECISIONS.md D23)

The database holds job payloads (for video jobs, edit plans with workspace
paths), so it is kept with OpenJarvis' own state rather than in a media or
output folder, and is created readable only by you.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from jarvis_ext.config import ExtensionConfigError, load_extension_section
from jarvis_ext.jobs.models import MAX_ATTEMPTS

CONFIG_SECTION = "jobs"
DEFAULT_SUBDIR = "jarvis"
DEFAULT_FILENAME = "jobs.db"


def default_database_path() -> Path:
    from openjarvis.core.paths import get_data_dir

    return get_data_dir() / DEFAULT_SUBDIR / DEFAULT_FILENAME


@dataclass(frozen=True)
class JobSettings:
    """Validated ``[jarvis.jobs]`` settings."""

    database_path: Optional[Path] = None  # None: default_database_path()
    max_attempts: int = MAX_ATTEMPTS

    @classmethod
    def from_mapping(cls, section: Mapping[str, Any]) -> "JobSettings":
        unknown = sorted(set(section) - {"database_path", "max_attempts"})
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
        return cls(database_path=database_path, max_attempts=attempts)

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "JobSettings":
        return cls.from_mapping(load_extension_section(CONFIG_SECTION, config_path))

    @property
    def uses_default_path(self) -> bool:
        return self.database_path is None

    def database_file(self) -> Path:
        return self.database_path or default_database_path()


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
