"""Voice-originated turns run under a restricted, fail-closed tool policy."""

from __future__ import annotations

import asyncio
import contextvars
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openjarvis.agents._stubs import AgentResult
from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.security.capabilities import CapabilityPolicy
from openjarvis.security.input_mode import (
    INPUT_MODE_HEADER,
    current_input_mode,
    input_mode_scope,
    is_voice_turn,
    mode_from_header,
    voice_denial_reason,
)
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec

VOICE_HEADERS = {INPUT_MODE_HEADER: "voice"}


def _tool(name, caps=(), module="openjarvis.tools.fake"):
    class _T(BaseTool):
        @property
        def spec(self):
            return ToolSpec(
                name=name, description=name, required_capabilities=list(caps)
            )

        def execute(self, **params):
            return ToolResult(tool_name=name, content="ran")

    _T.__module__ = module
    return _T()


def _run(executor, name):
    return executor.execute(ToolCall(id="c", name=name, arguments="{}"))


# Tool names mirror the real inventory in DEFAULT_TOOL_CAPABILITIES.
HIGH_RISK = [
    "shell_exec",  # code:execute
    "code_interpreter",  # code:execute
    "file_write",  # file:write
    "apply_patch",  # file:write
    "channel_send",  # channel:send (outbound messages)
    "schedule_task",  # schedule:create
    "agent_spawn",  # system:admin
    "execute_pending_actions",  # system:admin + channel:send
    "memory_store",  # memory:write
    "http_request",  # network:fetch but side-effecting
    "browser_click",
    "gmail_request_send_approval",
    "gmail_create_draft",
    "gmail_search_emails",
]
LOW_RISK = ["file_read", "memory_search", "web_search", "get_weather"]


class TestHeaderMapping:
    def test_absent_and_typed_are_normal(self):
        assert mode_from_header(None) == ""
        assert mode_from_header("") == ""
        assert mode_from_header("typed") == ""

    def test_voice_and_unknown_values_restrict(self):
        assert mode_from_header("voice") == "voice"
        assert mode_from_header(" VOICE ") == "voice"
        # An unrecognised value can only ever make the turn *more* restricted.
        assert mode_from_header("admin") == "voice"
        assert mode_from_header("trusted") == "voice"


class TestExecutorEnforcement:
    def test_voice_denies_high_risk_tools_without_any_policy_configured(self):
        executor = ToolExecutor([_tool(n, module="openjarvis.tools.fake") for n in HIGH_RISK])
        with input_mode_scope("voice"):
            for name in HIGH_RISK:
                result = _run(executor, name)
                assert not result.success, name
                assert result.content != "ran"

    def test_voice_denies_even_with_a_fully_permissive_policy(self):
        executor = ToolExecutor(
            [_tool("shell_exec"), _tool("file_write")],
            capability_policy=CapabilityPolicy(),  # open by default
            agent_id="a",
        )
        with input_mode_scope("voice"):
            assert not _run(executor, "shell_exec").success
            assert not _run(executor, "file_write").success

    def test_typed_turns_keep_existing_behaviour(self):
        executor = ToolExecutor([_tool(n) for n in HIGH_RISK[:4]])
        assert not is_voice_turn()
        for name in HIGH_RISK[:4]:
            assert _run(executor, name).content == "ran"

    def test_typed_turns_still_obey_the_configured_policy(self):
        denied = CapabilityPolicy(default_deny=True)
        executor = ToolExecutor([_tool("file_read")], capability_policy=denied, agent_id="a")
        assert not _run(executor, "file_read").success

    def test_voice_keeps_read_only_low_risk_tools(self):
        from openjarvis.tools.calculator import CalculatorTool
        from openjarvis.tools.think import ThinkTool

        # Reviewed-safe built-ins are recognised by real provenance, not by name.
        assert voice_denial_reason(CalculatorTool()) is None
        assert voice_denial_reason(ThinkTool()) is None
        tools = [
            _tool("file_read"),
            _tool("memory_search"),
            _tool("web_search"),
            _tool("get_weather"),
        ]
        executor = ToolExecutor(tools)
        with input_mode_scope("voice"):
            for name in LOW_RISK:
                assert _run(executor, name).content == "ran", name

    def test_voice_still_honours_a_stricter_configured_policy(self):
        policy = CapabilityPolicy()
        policy.deny("a", "file:read")
        executor = ToolExecutor([_tool("file_read")], capability_policy=policy, agent_id="a")
        with input_mode_scope("voice"):
            assert not _run(executor, "file_read").success

    def test_unknown_tool_with_no_declared_capabilities_fails_closed(self):
        tool = _tool("mystery_plugin", module="thirdparty.plugin")
        assert voice_denial_reason(tool) is not None
        with input_mode_scope("voice"):
            assert not _run(ToolExecutor([tool]), "mystery_plugin").success

    def test_spoofed_reviewed_safe_name_is_denied(self):
        spoof = _tool("calculator", module="thirdparty.plugin")
        with input_mode_scope("voice"):
            assert not _run(ToolExecutor([spoof]), "calculator").success

    def test_third_party_tool_declaring_dangerous_capability_is_denied(self):
        tool = _tool("custom_runner", caps=["code:execute"], module="thirdparty.plugin")
        with input_mode_scope("voice"):
            assert not _run(ToolExecutor([tool]), "custom_runner").success

    def test_mcp_tools_are_denied(self):
        tool = _tool("remote", caps=["tool:invoke"], module="openjarvis.tools.mcp_adapter")
        with input_mode_scope("voice"):
            assert not _run(ToolExecutor([tool]), "remote").success

    def test_policy_failure_denies_instead_of_allowing(self):
        import openjarvis.security.input_mode as im

        original = im.voice_denial_reason

        def boom(_tool):
            raise RuntimeError("cannot evaluate")

        im.voice_denial_reason = boom
        try:
            with input_mode_scope("voice"):
                result = _run(ToolExecutor([_tool("file_read")]), "file_read")
        finally:
            im.voice_denial_reason = original
        assert not result.success
        assert "could not be established" in result.content

    def test_denial_publishes_capability_denied_event(self):
        from openjarvis.core.events import EventBus, EventType

        bus = EventBus()
        seen = []
        bus.subscribe(EventType.CAPABILITY_DENIED, lambda e: seen.append(e.data))
        with input_mode_scope("voice"):
            _run(ToolExecutor([_tool("shell_exec")], bus=bus), "shell_exec")
        assert seen and seen[0]["tool"] == "shell_exec"


class TestGmailAndConfirmation:
    def test_voice_cannot_request_create_or_read_gmail(self):
        for name in ("gmail_request_send_approval", "gmail_create_draft", "gmail_get_thread"):
            assert voice_denial_reason(_tool(name)) is not None

    def test_confirmation_endpoints_reject_voice_requests_before_any_lookup(self):
        from openjarvis.server.gmail_approval import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        session = "s" * 40
        headers = {
            **VOICE_HEADERS,
            "X-OpenJarvis-Chat-Session": session,
            "X-OpenJarvis-Approval": "token",
        }
        for method, path in (
            ("get", "/v1/gmail/approvals/abc"),
            ("post", "/v1/gmail/approvals/abc/confirm"),
            ("post", "/v1/gmail/approvals/abc/cancel"),
        ):
            resp = getattr(client, method)(path, headers=headers)
            assert resp.status_code == 403, path
            assert "Voice-originated" in resp.json()["detail"]

    def test_spoken_approval_text_is_just_text_not_an_approval(self):
        # "yes, approve it" arrives as ordinary chat content; no tool or route
        # treats chat text as a confirmation, and the voice policy blocks the
        # tools that could create an approval.
        executor = ToolExecutor([_tool("gmail_request_send_approval"), _tool("execute_pending_actions")])
        with input_mode_scope("voice"):
            assert not _run(executor, "gmail_request_send_approval").success
            assert not _run(executor, "execute_pending_actions").success


class TestContextScoping:
    def test_scope_restores_on_exit_and_on_exception(self):
        assert current_input_mode.get() == ""
        try:
            with input_mode_scope("voice"):
                assert is_voice_turn()
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert not is_voice_turn()

    def test_new_threads_do_not_inherit_a_mode_set_elsewhere(self):
        seen = []
        with input_mode_scope("voice"):
            t = threading.Thread(target=lambda: seen.append(is_voice_turn()))
            t.start()
            t.join()
        assert seen == [False]

    def test_copied_context_carries_the_mode_into_worker_threads(self):
        seen = []
        with input_mode_scope("voice"):
            ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: seen.append(ctx.run(is_voice_turn)))
        t.start()
        t.join()
        assert seen == [True]

    def test_concurrent_requests_do_not_leak_modes(self):
        async def request(mode, delay):
            with input_mode_scope(mode):
                await asyncio.sleep(delay)
                return is_voice_turn()

        async def main():
            return await asyncio.gather(request("voice", 0.02), request("", 0.0), request("voice", 0.01), request("", 0.03))

        assert asyncio.run(main()) == [True, False, True, False]
        assert not is_voice_turn()

    def test_orchestrator_parallel_tools_keep_the_voice_mode(self):
        """The orchestrator runs approved tool calls in a thread pool."""
        import inspect

        from openjarvis.agents import orchestrator

        source = inspect.getsource(orchestrator.OrchestratorAgent)
        assert "contextvars.copy_context().run, _exec_tool" in source


class _RecordingAgent:
    """Stands in for the server agent: runs one dangerous and one safe tool."""

    _model = "test-model"

    def __init__(self):
        self._tools = [object()]
        self.executor = ToolExecutor([_tool("shell_exec"), _tool("file_read")])
        self.seen = []

    def run(self, input_text, context=None):
        self.seen.append(
            (
                is_voice_turn(),
                _run(self.executor, "shell_exec").success,
                _run(self.executor, "file_read").success,
            )
        )
        return AgentResult(content="done")


def _app(agent):
    from openjarvis.server.routes import router

    app = FastAPI()
    app.include_router(router)
    app.state.engine = object()
    app.state.agent = agent
    return app


def _chat(client, headers=None, stream=False):
    return client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": stream},
        headers=headers or {},
    )


class TestChatRoute:
    def test_non_streaming_voice_request_restricts_tools_and_resets(self):
        agent = _RecordingAgent()
        client = TestClient(_app(agent))
        assert _chat(client, VOICE_HEADERS).status_code == 200
        assert agent.seen == [(True, False, True)]
        assert not is_voice_turn()

    def test_typed_request_is_unchanged(self):
        agent = _RecordingAgent()
        client = TestClient(_app(agent))
        assert _chat(client).status_code == 200
        assert agent.seen == [(False, True, True)]

    def test_streaming_voice_request_restricts_tools(self):
        agent = _RecordingAgent()
        client = TestClient(_app(agent))
        resp = _chat(client, VOICE_HEADERS, stream=True)
        assert resp.status_code == 200
        assert agent.seen == [(True, False, True)]

    def test_voice_mode_does_not_leak_into_the_next_typed_request(self):
        agent = _RecordingAgent()
        client = TestClient(_app(agent))
        _chat(client, VOICE_HEADERS)
        _chat(client)
        _chat(client, VOICE_HEADERS, stream=True)
        _chat(client, stream=True)
        assert agent.seen == [(True, False, True), (False, True, True), (True, False, True), (False, True, True)]

    def test_voice_mode_resets_when_the_agent_raises(self):
        class Boom(_RecordingAgent):
            def run(self, input_text, context=None):
                raise RuntimeError("model failed")

        client = TestClient(_app(Boom()), raise_server_exceptions=False)
        assert _chat(client, VOICE_HEADERS).status_code >= 500
        assert not is_voice_turn()


class TestServerAdvertisesPolicy:
    def test_info_reports_the_voice_input_policy_so_the_ui_can_fail_closed(self):
        client = TestClient(_app(_RecordingAgent()))
        assert client.get("/v1/info").json()["voice_input_policy"] == 1
