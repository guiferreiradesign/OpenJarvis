"""Mocked tests for the dashboard's draft-only Gmail agent tools."""

from __future__ import annotations

import base64
import email
import json
from unittest.mock import patch

import httpx
import pytest

from openjarvis.agents.orchestrator import OrchestratorAgent
from openjarvis.core.registry import ToolRegistry
from openjarvis.tools import gmail_tools
from openjarvis.tools.gmail_tools import (
    GmailCreateDraftTool,
    GmailGetThreadTool,
    GmailSearchEmailsTool,
    GmailRequestSendApprovalTool,
    dashboard_gmail_tools,
)


@pytest.fixture(autouse=True)
def _register_tools_after_registry_reset():
    for name, tool_cls in (
        ("gmail_search_emails", GmailSearchEmailsTool),
        ("gmail_get_thread", GmailGetThreadTool),
        ("gmail_create_draft", GmailCreateDraftTool),
        ("gmail_request_send_approval", GmailRequestSendApprovalTool),
    ):
        if not ToolRegistry.contains(name):
            ToolRegistry.register_value(name, tool_cls)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://gmail.googleapis.com")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("mock error", request=request, response=response)


def test_dashboard_orchestrator_gets_only_allowlisted_gmail_tools():
    assert set(gmail_tools._GMAIL_TOOLS) == {
        "gmail_search_emails",
        "gmail_get_thread",
        "gmail_create_draft",
        "gmail_request_send_approval",
    }
    assert set(ToolRegistry.keys()).issuperset(gmail_tools._GMAIL_TOOLS)
    assert not any("gmail_send" in name for name in ToolRegistry.keys())
    assert {tool.spec.name for tool in dashboard_gmail_tools("orchestrator")} == set(
        gmail_tools._GMAIL_TOOLS
    )
    assert dashboard_gmail_tools("simple") == []


def test_search_returns_message_metadata_from_mocked_gmail():
    api_result = {
        "messages": [
            {
                "id": "m-1",
                "threadId": "t-1",
                "snippet": "Review the Q3 plan",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "Alice <alice@example.com>"},
                        {"name": "Subject", "value": "Q3 plan"},
                        {"name": "Date", "value": "Mon, 01 Jan 2024 10:00:00 +0000"},
                    ]
                },
            }
        ]
    }
    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector,
            "_call_with_refresh",
            side_effect=[{"messages": [{"id": "m-1"}]}, api_result["messages"][0]],
        ) as call,
    ):
        result = GmailSearchEmailsTool().execute(query="subject:Q3", max_results=3)

    assert result.success
    message = json.loads(result.content)["messages"][0]
    assert message == {
        "message_id": "m-1",
        "thread_id": "t-1",
        "sender": "Alice <alice@example.com>",
        "subject": "Q3 plan",
        "date": "Mon, 01 Jan 2024 10:00:00 +0000",
        "snippet": "Review the Q3 plan",
    }
    assert call.call_count == 2


def test_get_thread_returns_headers_and_body():
    thread = {
        "messages": [
            {
                "id": "m-1",
                "threadId": "t-1",
                "snippet": "Hello",
                "payload": {
                    "mimeType": "text/plain",
                    "headers": [
                        {"name": "From", "value": "alice@example.com"},
                        {"name": "To", "value": "me@example.com"},
                        {"name": "Subject", "value": "Hello"},
                    ],
                    "body": {"data": "SGVsbG8="},
                },
            }
        ]
    }
    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector, "_call_with_refresh", return_value=thread
        ),
    ):
        result = GmailGetThreadTool().execute(thread_id="t-1")

    assert result.success
    message = json.loads(result.content)["messages"][0]
    assert message["from"] == "alice@example.com"
    assert message["to"] == "me@example.com"
    assert message["subject"] == "Hello"
    assert message["body"].strip() == "Hello"


def test_draft_uses_gmail_drafts_create_and_never_sends():
    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector,
            "_call_with_refresh",
            return_value={"id": "draft-1"},
        ) as call,
    ):
        result = GmailCreateDraftTool().execute(
            to="alice@example.com", subject="Hello", body="A draft only"
        )

    assert result.success
    payload = json.loads(result.content)
    assert payload == {
        "created": True,
        "sent": False,
        "draft_id": "draft-1",
        "message": "Draft created in Gmail; not sent.",
    }
    api_fn, raw = call.call_args.args
    assert api_fn.__name__ == "_gmail_api_create_draft"
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    parsed = email.message_from_bytes(decoded)
    assert parsed["To"] == "alice@example.com"
    assert parsed["Subject"] == "Hello"
    assert parsed.get_payload(decode=True).decode().strip() == "A draft only"


def test_disconnected_credentials_and_expired_credentials_are_clear_and_secret_free():
    secret = "test-access-token-must-not-escape"
    with patch.object(gmail_tools.GmailConnector, "is_connected", return_value=False):
        disconnected = GmailSearchEmailsTool().execute(query="from:alice@example.com")
    assert not disconnected.success
    assert "disconnected" in disconnected.content.lower()
    assert secret not in disconnected.content

    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector,
            "_call_with_refresh",
            side_effect=RuntimeError(
                "Gmail token refresh failed — re-run `jarvis connect gmail`"
            ),
        ),
    ):
        expired = GmailSearchEmailsTool().execute(query="from:alice@example.com")
    assert not expired.success
    assert "refresh" in expired.content.lower()
    assert secret not in expired.content


def test_invalid_arguments_and_api_failures_are_safe_tool_results():
    tool = GmailCreateDraftTool()
    for args in (
        {"to": "", "subject": "s", "body": "b"},
        {"to": "bad address", "subject": "s", "body": "b"},
        {"to": "a@example.com,b@example.com", "subject": "s", "body": "b"},
        {"to": "a@example.com\nBcc:x@y.com", "subject": "s", "body": "b"},
        {"to": "a@example.com", "subject": "s\nBcc: x@y.com", "body": "b"},
        {"to": "a@example.com", "subject": "s", "body": ""},
    ):
        assert not tool.execute(**args).success

    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector,
            "_call_with_refresh",
            side_effect=_http_error(403),
        ),
    ):
        denied = GmailSearchEmailsTool().execute(query="from:alice@example.com")
    assert not denied.success
    assert "permission" in denied.content.lower()

    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector,
            "_call_with_refresh",
            side_effect=_http_error(429),
        ),
    ):
        limited = GmailSearchEmailsTool().execute(query="from:alice@example.com")
    assert not limited.success
    assert "rate limit" in limited.content.lower()

    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector,
            "_call_with_refresh",
            side_effect=_http_error(404),
        ),
    ):
        missing = GmailGetThreadTool().execute(thread_id="thread-1")
    assert not missing.success
    assert "not found" in missing.content.lower()


def test_agent_receives_gmail_result_before_final_answer():
    observed = []
    engine = type("MockEngine", (), {})()
    engine.engine_id = "mock"

    def generate(messages, **kwargs):
        observed.append(messages)
        if len(observed) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-search",
                        "name": "gmail_search_emails",
                        "arguments": '{"query":"from:alice@example.com"}',
                    }
                ],
                "finish_reason": "tool_calls",
                "usage": {},
                "model": "mock",
            }
        return {
            "content": "I found Alice's email.",
            "finish_reason": "stop",
            "usage": {},
            "model": "mock",
        }

    engine.generate = generate
    tool = GmailSearchEmailsTool()
    with (
        patch.object(gmail_tools.GmailConnector, "is_connected", return_value=True),
        patch.object(
            gmail_tools.GmailConnector,
            "_call_with_refresh",
            side_effect=[
                {"messages": [{"id": "m-1"}]},
                {
                    "id": "m-1",
                    "threadId": "t-1",
                    "snippet": "Hi",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "alice@example.com"},
                            {"name": "Subject", "value": "Hello"},
                            {"name": "Date", "value": "today"},
                        ]
                    },
                },
            ],
        ),
    ):
        agent = OrchestratorAgent(engine, "mock", tools=[tool], max_turns=3)
        result = agent.run("Find Alice's email")

    assert result.content == "I found Alice's email."
    assert len(result.tool_results) == 1
    assert result.tool_results[0].success
    result_message = json.loads(result.tool_results[0].content)["messages"][0]
    assert result_message["message_id"] == "m-1"
    tool_messages = [message for message in observed[1] if message.role.value == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0].content)["messages"][0]["subject"] == "Hello"
