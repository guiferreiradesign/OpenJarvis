"""JARVIS extensions — personal capabilities layered on top of OpenJarvis.

Everything in this package is custom to the JARVIS fork and deliberately kept
outside ``src/openjarvis`` so upstream OpenJarvis updates merge cleanly.  The
only coupling to the core is one guarded import at the end of
``openjarvis/tools/__init__.py`` that triggers registration below.

Extensions plug in through the standard OpenJarvis registries (currently
``ToolRegistry``, and ``SpeechRegistry``/``TTSRegistry`` for the speech
providers) and read their settings from ``[jarvis.*]`` sections of the
normal OpenJarvis ``config.toml``.  See ``jarvis/README.md``.
"""

from __future__ import annotations


def ensure_registered() -> None:
    """Register every JARVIS extension tool and speech provider (idempotent).

    Module import registers tools once per process; tests clear registries
    between cases, so they call this to restore the entries.
    """
    from jarvis_ext.speech import ensure_registered as _ensure_speech
    from jarvis_ext.tools import ensure_registered as _ensure_tools

    _ensure_tools()
    _ensure_speech()


ensure_registered()

__all__ = ["ensure_registered"]
