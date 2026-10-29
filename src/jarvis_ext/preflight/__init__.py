"""JARVIS preflight: verify the running application and its essential parts.

Run with ``python -m jarvis_ext preflight`` (see jarvis/preflight.md).
"""

from jarvis_ext.preflight.core import (
    FAIL,
    GROUPS,
    PASS,
    WARN,
    Blocked,
    Context,
    Outcome,
    Report,
    check,
    failed,
    not_run,
    passed,
    run_preflight,
    warned,
)

__all__ = [
    "Blocked",
    "Context",
    "FAIL",
    "GROUPS",
    "Outcome",
    "PASS",
    "Report",
    "WARN",
    "check",
    "failed",
    "not_run",
    "passed",
    "run_preflight",
    "warned",
]
