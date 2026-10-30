"""Per-turn input mode (typed vs. voice) and the restricted voice tool policy.

A voice-originated chat turn runs under a *narrower* tool policy than a typed
one, because a spoken request can come from a false wake-word activation or a
mis-transcription. The mode is carried in a ``ContextVar`` for the lifetime of
one request and is enforced inside ``ToolExecutor.execute`` — never in a prompt
or in the browser. The mode can only reduce what a turn may do; it has no value
that grants anything extra.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, List, Optional

from openjarvis.security.capabilities import Capability, canonical_tool_capabilities

INPUT_MODE_HEADER = "X-OpenJarvis-Input-Mode"
VOICE = "voice"

# "" means a normal typed turn. Anything else is a restricted mode.
current_input_mode: ContextVar[str] = ContextVar("openjarvis_input_mode", default="")


def mode_from_header(value: Optional[str]) -> str:
    """Map the request header to a mode, failing toward *more* restriction.

    Absent or ``typed`` keeps today's behaviour. ``voice`` — and any other
    non-empty value we do not recognise — selects the restricted policy.
    """
    text = (value or "").strip().lower()
    if text in ("", "typed", "text"):
        return ""
    return VOICE


def is_voice_turn() -> bool:
    return bool(current_input_mode.get())


@contextmanager
def input_mode_scope(mode: str) -> Iterator[None]:
    """Bind *mode* for the duration of the block and always restore it."""
    token = current_input_mode.set(mode)
    try:
        yield
    finally:
        current_input_mode.reset(token)


# Read-only, low-risk capabilities a voice turn may use.
VOICE_ALLOWED_CAPABILITIES = frozenset(
    {
        Capability.FILE_READ.value,
        Capability.MEMORY_READ.value,
        Capability.NETWORK_FETCH.value,
    }
)

# Network tools that read nothing-but-can-still-act or expose private mail.
# Denied by name in addition to the capability allow-list.
VOICE_DENIED_TOOLS = frozenset(
    {
        "http_request",
        "llm",
        "image_generate",
        "text_to_speech",
        "gmail_create_draft",
        "gmail_request_send_approval",
        "gmail_search_emails",
        "gmail_get_thread",
    }
)
VOICE_DENIED_PREFIXES = ("browser_", "gmail_")

# Reviewed-safe built-ins that declare no capabilities.
_VOICE_SAFE_NO_CAPABILITY = frozenset({"calculator", "think"})


def _cap_value(cap: Any) -> str:
    return cap.value if hasattr(cap, "value") else str(cap)


def voice_denial_reason(tool: Any) -> Optional[str]:
    """Why a voice turn may not run *tool*, or ``None`` if it may.

    Fails closed: a tool whose requirements cannot be established, or that
    declares none and is not a reviewed-safe built-in, is denied.
    """
    name = tool.spec.name
    if name in VOICE_DENIED_TOOLS or name.startswith(VOICE_DENIED_PREFIXES):
        return f"Tool '{name}' is not available to voice-originated requests."

    required: List[str] = [_cap_value(c) for c in tool.spec.required_capabilities]
    for cap in canonical_tool_capabilities(tool):
        value = _cap_value(cap)
        if value not in required:
            required.append(value)

    if not required:
        module = type(tool).__module__
        if name in _VOICE_SAFE_NO_CAPABILITY and module.startswith("openjarvis.tools."):
            return None
        return (
            f"Tool '{name}' declares no capabilities, so it cannot be verified"
            " as safe for voice-originated requests."
        )

    for value in required:
        if value not in VOICE_ALLOWED_CAPABILITIES:
            return (
                f"Capability '{value}' is not allowed for voice-originated"
                f" requests (tool '{name}')."
            )
    return None


__all__ = [
    "INPUT_MODE_HEADER",
    "VOICE",
    "VOICE_ALLOWED_CAPABILITIES",
    "VOICE_DENIED_TOOLS",
    "current_input_mode",
    "input_mode_scope",
    "is_voice_turn",
    "mode_from_header",
    "voice_denial_reason",
]
