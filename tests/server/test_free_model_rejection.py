"""An unapproved model is refused cleanly (HTTP 400), before any provider call.

Regression: ``POST /v1/chat/completions`` with ``model: no-such-model`` returned
a bare ``500 Internal Server Error`` because ``OpenRouterModelNotAllowed``
escaped the route. Every client below is a recording fake; nothing is sent.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.core.config import JarvisConfig
from openjarvis.engine._discovery import apply_openrouter_policy
from openjarvis.engine.cloud import CloudEngine
from openjarvis.server.app import create_app

APPROVED = "openrouter/cohere/north-mini-code:free"
BODY = {"messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
def served(monkeypatch):
    for name in list(__import__("os").environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name)
    calls: list[dict] = []

    class _Client:
        def __init__(self) -> None:
            def create(**kwargs):
                calls.append(kwargs)
                raise RuntimeError("provider must not be reached in this test")

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    engine = CloudEngine()
    for attr in [a for a in vars(engine) if a.endswith("_client")]:
        setattr(engine, attr, _Client())
    config = JarvisConfig()
    apply_openrouter_policy(engine, config)
    agent = OrchestratorAgent(engine, APPROVED, tools=[])
    app = create_app(engine, APPROVED, agent=agent, config=config, engine_name="cloud")
    return TestClient(app, raise_server_exceptions=False), calls


@pytest.mark.parametrize(
    "model", ["no-such-model", "gpt-4o", "openrouter/openai/gpt-4o", "openrouter/auto"]
)
def test_unapproved_model_is_a_clean_400_and_nothing_is_sent(served, model):
    client, calls = served
    response = client.post("/v1/chat/completions", json={"model": model, **BODY})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "model_not_approved"
    assert error["type"] == "invalid_request_error"
    assert "not an approved free model" in error["message"]
    assert "no request was sent" in error["message"]
    assert APPROVED in error["message"]
    assert calls == []


def test_direct_engine_path_is_also_a_clean_400(served):
    client, calls = served
    client.app.state.agent = None  # no server agent: _handle_direct path
    response = client.post(
        "/v1/chat/completions", json={"model": "no-such-model", **BODY}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_approved"
    assert calls == []


def test_streaming_reports_the_refusal_readably_and_sends_nothing(served):
    client, calls = served
    response = client.post(
        "/v1/chat/completions",
        json={"model": "no-such-model", "stream": True, **BODY},
    )
    assert "not an approved free model" in response.text
    assert "no request was sent" in response.text
    assert calls == []


def test_approved_model_still_reaches_only_the_provider(served):
    client, calls = served
    response = client.post("/v1/chat/completions", json={"model": APPROVED, **BODY})
    assert response.status_code != 400  # fake provider fails later, not the policy
    assert len(calls) >= 1
    assert all(c["model"] == "cohere/north-mini-code:free" for c in calls)


# -- the engine chain `jarvis serve` really builds (MultiEngine) ----------------


class _LocalEngine:
    """Stands in for a discovered local engine (e.g. Ollama)."""

    engine_id = "ollama"
    is_cloud = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_models(self):
        return ["qwen3.5:2b"]

    def health(self):
        return True

    def generate(self, messages, *, model, **kwargs):
        self.calls.append(model)
        return {"content": "local", "usage": {}, "model": model, "finish_reason": "stop"}

    def close(self):
        pass


@pytest.fixture
def served_multi(monkeypatch):
    from openjarvis.engine.multi import MultiEngine

    for name in list(__import__("os").environ):
        if name.endswith("_API_KEY"):
            monkeypatch.delenv(name)
    calls: list[dict] = []

    class _Client:
        def __init__(self) -> None:
            def create(**kwargs):
                calls.append(kwargs)
                raise RuntimeError("provider must not be reached in this test")

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    cloud = CloudEngine()
    for attr in [a for a in vars(cloud) if a.endswith("_client")]:
        setattr(cloud, attr, _Client())
    config = JarvisConfig()
    apply_openrouter_policy(cloud, config)
    local = _LocalEngine()
    engine = MultiEngine([("cloud", cloud), ("ollama", local)])
    agent = OrchestratorAgent(engine, APPROVED, tools=[])
    app = create_app(engine, APPROVED, agent=agent, config=config, engine_name="multi")
    return TestClient(app, raise_server_exceptions=False), calls, local


def test_unknown_model_through_multiengine_is_a_clean_404_not_a_500(served_multi):
    """The exact failing request: model 'no-such-model' against `jarvis serve`."""
    client, calls, local = served_multi
    response = client.post(
        "/v1/chat/completions", json={"model": "no-such-model", **BODY}
    )
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found"
    assert "no-such-model" in error["message"]
    assert calls == [] and local.calls == []


def test_not_found_message_is_concise_and_lists_no_model_names(served_multi):
    client, _, local = served_multi
    local.list_models = lambda: [f"local-model-{i}" for i in range(200)]
    engine = client.app.state.engine
    while not hasattr(engine, "_refresh_map"):  # unwrap guardrails/instrumentation
        engine = getattr(engine, "_inner", None) or engine._engine
    engine._refresh_map()
    response = client.post("/v1/chat/completions", json={"model": "nope", **BODY})
    message = response.json()["error"]["message"]
    assert response.status_code == 404
    assert len(message) < 200
    assert "local-model-" not in message and "qwen" not in message


@pytest.mark.parametrize(
    "model", ["gpt-4o", "claude-sonnet-4-6", "openrouter/openai/gpt-4o", "openrouter/auto"]
)
def test_cloud_looking_models_through_multiengine_hit_the_policy(served_multi, model):
    client, calls, local = served_multi
    response = client.post("/v1/chat/completions", json={"model": model, **BODY})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_approved"
    assert calls == [] and local.calls == []


def test_multiengine_unknown_model_streaming_is_readable(served_multi):
    client, calls, local = served_multi
    response = client.post(
        "/v1/chat/completions",
        json={"model": "no-such-model", "stream": True, **BODY},
    )
    assert "not found in any engine" in response.text
    assert calls == [] and local.calls == []


def test_multiengine_local_model_never_reaches_the_cloud_provider(served_multi):
    client, calls, local = served_multi
    client.app.state.agent = None
    response = client.post("/v1/chat/completions", json={"model": "qwen3.5:2b", **BODY})
    assert response.status_code == 200
    assert local.calls == ["qwen3.5:2b"] and calls == []


def test_multiengine_approved_model_reaches_only_openrouter(served_multi):
    client, calls, local = served_multi
    client.post("/v1/chat/completions", json={"model": APPROVED, **BODY})
    assert calls and all(c["model"] == "cohere/north-mini-code:free" for c in calls)
    assert local.calls == []
