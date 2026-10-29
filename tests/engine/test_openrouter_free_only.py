"""OpenRouter is free-model-only: nothing but approved IDs is ever requested.

No test here contacts a provider: the OpenAI-compatible client is a fake that
records calls, so "no request was sent" is asserted directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message, Role
from openjarvis.engine._base import EngineConnectionError
from openjarvis.engine._discovery import _make_engine, apply_openrouter_policy
from openjarvis.engine.cloud import CloudEngine
from openjarvis.engine.openrouter_policy import (
    OpenRouterModelNotAllowed,
    OpenRouterPolicy,
)

FREE = "openrouter/cohere/north-mini-code:free"
OTHER_FREE = "openrouter/meta-llama/llama-3.3-70b-instruct:free"
PAID = "openrouter/openai/gpt-4o"


class _Client:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_with = fail_with
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with:
            raise self.fail_with
        message = SimpleNamespace(content="ok", tool_calls=None)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        if kwargs.get("stream"):
            delta = SimpleNamespace(content="ok")
            return [SimpleNamespace(choices=[SimpleNamespace(delta=delta)])]
        return SimpleNamespace(choices=[choice], usage=usage, model="m")


def _engine(client: _Client, policy: OpenRouterPolicy | None = None) -> CloudEngine:
    with mock.patch.dict("os.environ", {}, clear=True):
        engine = CloudEngine()
    engine._openrouter_client = client
    engine.openrouter_policy = policy
    return engine


MSG = [Message(role=Role.USER, content="hi")]


def _generate(engine, model, **kw):
    return engine.generate(MSG, model=model, **kw)


def test_defaults_approve_only_the_current_model():
    cfg = JarvisConfig().engine.openrouter
    assert cfg.free_only is True
    assert cfg.approved_free_models == [FREE]


def test_config_built_cloud_engine_carries_the_policy():
    EngineRegistry.register_value("cloud", CloudEngine)
    policy = _make_engine("cloud", JarvisConfig()).openrouter_policy
    assert policy.free_only and policy.approved_free_models == {FREE}


def test_apply_policy_is_a_noop_for_other_engines():
    engine = SimpleNamespace()
    apply_openrouter_policy(engine, JarvisConfig())
    assert not hasattr(engine, "openrouter_policy")


def test_approved_free_model_is_requested():
    client = _Client()
    engine = _engine(client, OpenRouterPolicy())
    assert _generate(engine, FREE)["content"] == "ok"
    assert client.calls[0]["model"] == "cohere/north-mini-code:free"


@pytest.mark.parametrize(
    "model",
    [
        PAID,
        OTHER_FREE,  # looks free, but not approved
        "openrouter/auto",
        FREE + ":nitro",
        FREE.replace(":free", ""),  # the paid sibling of the free model
        "openrouter/cohere/north-mini-code",
    ],
)
def test_unapproved_models_are_refused_before_any_request(model):
    client = _Client()
    engine = _engine(client, OpenRouterPolicy())
    with pytest.raises(OpenRouterModelNotAllowed) as err:
        _generate(engine, model)
    assert isinstance(err.value, EngineConnectionError)
    assert "no request was sent" in str(err.value)
    assert FREE in str(err.value)
    assert client.calls == []


def test_extra_approved_ids_are_allowed_only_when_listed_exactly():
    client = _Client()
    policy = OpenRouterPolicy(approved_free_models=frozenset({OTHER_FREE}))
    engine = _engine(client, policy)
    _generate(engine, OTHER_FREE)
    with pytest.raises(OpenRouterModelNotAllowed):
        _generate(engine, FREE)
    assert len(client.calls) == 1


def test_rate_limit_does_not_switch_model():
    client = _Client(fail_with=RuntimeError("429 rate limit exceeded"))
    policy = OpenRouterPolicy(approved_free_models=frozenset({FREE, OTHER_FREE}))
    engine = _engine(client, policy)
    with pytest.raises(RuntimeError, match="429"):
        _generate(engine, FREE)
    assert [c["model"] for c in client.calls] == ["cohere/north-mini-code:free"]


@pytest.mark.parametrize("field", ["models", "route", "provider", "plugins"])
def test_openrouter_side_routing_fields_are_refused(field):
    client = _Client()
    engine = _engine(client, OpenRouterPolicy())
    with pytest.raises(OpenRouterModelNotAllowed):
        _generate(engine, FREE, **{field: ["openai/gpt-4o"]})
    with pytest.raises(OpenRouterModelNotAllowed):
        _generate(engine, FREE, extra_body={"models": ["openai/gpt-4o"]})
    assert client.calls == []


@pytest.mark.asyncio
async def test_streaming_paths_enforce_the_policy():
    client = _Client()
    engine = _engine(client, OpenRouterPolicy())
    with pytest.raises(OpenRouterModelNotAllowed):
        async for _ in engine.stream(MSG, model=PAID):
            pass
    with pytest.raises(OpenRouterModelNotAllowed):
        async for _ in engine.stream_full(MSG, model=PAID):
            pass
    assert client.calls == []
    chunks = [t async for t in engine.stream(MSG, model=FREE)]
    assert chunks == ["ok"]


def test_policy_can_be_disabled_explicitly():
    client = _Client()
    engine = _engine(client, OpenRouterPolicy(free_only=False))
    _generate(engine, PAID)
    assert client.calls[0]["model"] == "openai/gpt-4o"


def test_refusal_is_not_an_unusable_engine_for_other_work():
    """Only the OpenRouter request is refused; the engine object stays usable."""
    client = _Client()
    engine = _engine(client, OpenRouterPolicy())
    with pytest.raises(OpenRouterModelNotAllowed):
        _generate(engine, PAID)
    assert _generate(engine, FREE)["content"] == "ok"


# -- every provider prefix, not only openrouter/ --------------------------------

ALL_CLIENTS = (
    "_openai_client",
    "_anthropic_client",
    "_google_client",
    "_openrouter_client",
    "_atlascloud_client",
    "_minimax_client",
    "_deepseek_client",
    "_codex_client",
)

NON_OPENROUTER_MODELS = [
    "gpt-4o",
    "gpt-5-mini",
    "o3-mini",
    "claude-sonnet-4-6",
    "gemini-2.5-pro",
    "MiniMax-M3",
    "deepseek-chat",
    "atlascloud/some-model",
    "codex-mini",
    "totally-unknown-model",
    "",
    "cohere/north-mini-code:free",  # the approved name without its prefix
]


def _fully_armed_engine(policy: OpenRouterPolicy | None = OpenRouterPolicy()):
    """An engine whose every provider client is a recording fake."""
    clients = {name: _Client() for name in ALL_CLIENTS}
    with mock.patch.dict("os.environ", {}, clear=True):
        engine = CloudEngine()
    for name, client in clients.items():
        setattr(engine, name, client)
    engine.openrouter_policy = policy
    return engine, clients


def _all_calls(clients):
    return {name: c.calls for name, c in clients.items() if c.calls}


@pytest.mark.parametrize("model", NON_OPENROUTER_MODELS)
def test_unapproved_names_never_fall_through_to_another_provider(model):
    engine, clients = _fully_armed_engine()
    with pytest.raises(OpenRouterModelNotAllowed) as err:
        _generate(engine, model)
    assert "no request was sent" in str(err.value)
    assert _all_calls(clients) == {}
    assert engine.can_serve(model) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("model", NON_OPENROUTER_MODELS)
async def test_streaming_entry_points_reject_before_any_client_call(model):
    engine, clients = _fully_armed_engine()
    with pytest.raises(OpenRouterModelNotAllowed):
        async for _ in engine.stream(MSG, model=model):
            pass
    with pytest.raises(OpenRouterModelNotAllowed):
        async for _ in engine.stream_full(MSG, model=model):
            pass
    assert _all_calls(clients) == {}


def test_only_the_approved_model_reaches_any_client():
    engine, clients = _fully_armed_engine()
    _generate(engine, FREE)
    assert list(_all_calls(clients)) == ["_openrouter_client"]
    assert engine.can_serve(FREE) is True


def test_list_models_advertises_only_approved_models():
    engine, _ = _fully_armed_engine()
    assert engine.list_models() == [FREE]


def test_list_models_is_unrestricted_without_a_policy():
    engine, _ = _fully_armed_engine(policy=None)
    assert "gpt-4o" in engine.list_models()


def test_policy_loaded_from_config_applies_to_non_openrouter_names():
    EngineRegistry.register_value("cloud", CloudEngine)
    engine = _make_engine("cloud", JarvisConfig())
    clients = {name: _Client() for name in ALL_CLIENTS}
    for name, client in clients.items():
        setattr(engine, name, client)
    with pytest.raises(OpenRouterModelNotAllowed):
        _generate(engine, "gpt-4o")
    assert _all_calls(clients) == {}
