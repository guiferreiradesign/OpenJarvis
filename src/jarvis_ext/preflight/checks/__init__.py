"""Built-in preflight checks.

To add a check, write a function with ``@check(...)`` (see
``jarvis_ext.preflight.core``) in the module for its group, or add a new
module here and list it in :data:`MODULES`.  Order inside a group is the order
of registration.
"""

from __future__ import annotations

import importlib

MODULES = (
    "jarvis_ext.preflight.checks.app",
    "jarvis_ext.preflight.checks.media",
    "jarvis_ext.preflight.checks.jobs",
    "jarvis_ext.preflight.checks.brain",
    "jarvis_ext.preflight.checks.future",
)

_loaded = False


def load_builtin_checks() -> None:
    """Import every check module once (importing registers the checks)."""
    global _loaded
    if _loaded:
        return
    for name in MODULES:
        importlib.import_module(name)
    _loaded = True
