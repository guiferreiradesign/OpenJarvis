"""Free-model-only policy for OpenRouter requests.

OpenRouter bills per model, and a name that merely looks free (a provider
prefix, a ``:free`` suffix someone typed, ``openrouter/auto``, ``:nitro`` or
``:floor`` variants) proves nothing. With the policy active, a request is sent
only for a model ID listed *exactly* in ``approved_free_models``. Anything
else is refused locally, before any HTTP request, with a clear error. There is
no automatic model fallback: a rate-limited or unavailable free model is
reported, never replaced by a different model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, FrozenSet, Mapping

from openjarvis.engine._base import EngineConnectionError

DEFAULT_APPROVED_FREE_MODELS = ("openrouter/cohere/north-mini-code:free",)

# Request fields that make OpenRouter itself pick or fall back to other models.
_ROUTING_KEYS = frozenset({"models", "route", "provider", "plugins"})


class OpenRouterModelNotAllowed(EngineConnectionError):
    """The requested OpenRouter model is not on the approved free list."""


@dataclass(frozen=True)
class OpenRouterPolicy:
    free_only: bool = True
    approved_free_models: FrozenSet[str] = field(
        default_factory=lambda: frozenset(DEFAULT_APPROVED_FREE_MODELS)
    )

    @classmethod
    def from_config(cls, config: Any) -> "OpenRouterPolicy":
        section = getattr(getattr(config, "engine", None), "openrouter", None)
        if section is None:
            return cls()
        return cls(
            free_only=bool(section.free_only),
            approved_free_models=frozenset(section.approved_free_models),
        )

    def check(self, model: str, extra: Mapping[str, Any] | None = None) -> None:
        """Raise unless *model* may be requested (any provider prefix)."""
        if not self.free_only:
            return
        if model not in self.approved_free_models:
            approved = ", ".join(sorted(self.approved_free_models)) or "none"
            raise OpenRouterModelNotAllowed(
                f"Model {model!r} is not an approved free model, so "
                "no request was sent. JARVIS does not switch models "
                "automatically. Approved free models "
                f"([engine.openrouter] approved_free_models): {approved}"
            )
        body = (extra or {}).get("extra_body")
        routed = _ROUTING_KEYS & set(body) if isinstance(body, Mapping) else set()
        routed |= _ROUTING_KEYS & set(extra or {})
        if routed:
            raise OpenRouterModelNotAllowed(
                "Free-model-only mode refuses OpenRouter routing/fallback "
                f"fields ({', '.join(sorted(routed))}); no request was sent"
            )


__all__ = [
    "DEFAULT_APPROVED_FREE_MODELS",
    "OpenRouterModelNotAllowed",
    "OpenRouterPolicy",
]
