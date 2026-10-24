"""Read ``[jarvis.<name>]`` sections from the OpenJarvis ``config.toml``.

The core loader (``openjarvis.core.config.load_config``) ignores top-level
sections it does not know, so JARVIS settings can live in the same file without
touching core config dataclasses.  This module re-reads that file and returns
only the requested extension section.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

SECTION_ROOT = "jarvis"


class ExtensionConfigError(ValueError):
    """Raised when an extension section exists but cannot be used."""


def resolve_config_path(path: Optional[Path] = None) -> Path:
    """Return the config file OpenJarvis itself would load.

    Same precedence as ``load_config``: explicit *path*, then
    ``$OPENJARVIS_CONFIG``, then ``<OPENJARVIS_HOME>/config.toml``.
    """
    if path is not None:
        return Path(path).expanduser().resolve()
    custom = os.environ.get("OPENJARVIS_CONFIG")
    if custom:
        return Path(custom).expanduser().resolve()
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "config.toml"


def load_extension_section(name: str, path: Optional[Path] = None) -> Dict[str, Any]:
    """Return the ``[jarvis.<name>]`` table, or ``{}`` when it is absent.

    A missing config file is not an error (the extension is simply
    unconfigured).  An unreadable or malformed file, or a section that is not a
    table, raises :class:`ExtensionConfigError` so a broken config can never be
    mistaken for "no restriction".
    """
    config_path = resolve_config_path(path)
    if not config_path.exists():
        return {}
    try:
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExtensionConfigError(
            f"Cannot read JARVIS config from {config_path}: {exc}"
        ) from exc

    root = data.get(SECTION_ROOT, {})
    if not isinstance(root, dict):
        raise ExtensionConfigError(f"[{SECTION_ROOT}] must be a table")
    section = root.get(name, {})
    if not isinstance(section, dict):
        raise ExtensionConfigError(f"[{SECTION_ROOT}.{name}] must be a table")
    return section


__all__ = [
    "ExtensionConfigError",
    "load_extension_section",
    "resolve_config_path",
]
