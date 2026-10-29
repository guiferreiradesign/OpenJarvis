"""Gmail approvals never send until the dedicated confirmation endpoint."""
from __future__ import annotations

import asyncio
import base64
import email
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import httpx
from fastapi import HTTPException
from starlette.requests import Request

from openjarvis.server.gmail_approval import GmailApprovalStore
from openjarvis.server.gmail_approval import current_dashboard_session
from openjarvis.tools.gmail_tools import GmailRequestSendApprovalTool

SESSION = "test-session-0123456789-abcdef-0123456789"


def test_approval_tool_creates_pending_request_without_sending(tmp_path):
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    with (
        patch("openjarvis.server.gmail_approval.store", store),
        patch("openjarvis.tools.gmail_tools._connector") as connector,
        patch("openjarvis.connectors.gmail._gmail_api_send_message") as send,
    ):
        connector.return_value._credentials_path = "/mock/gmail.json"
        token = current_dashboard_session.set(SESSION)
        try:
            result = GmailRequestSendApprovalTool().execute(
                to="person@example.com", subject="Exact subject", body="Exact body"
            )
        finally:
            current_dashboard_session.reset(token)
    assert result.success
    payload = json.loads(result.content)
    assert payload["status"] == "pending" and payload["sent"] is False
    assert "dashboard_token" not in payload
    assert result.metadata["gmail_approval_id"] == payload["approval_id"]
    record = store.get(payload["approval_id"], payload_owner(), payload_account())
    assert record["message"] == {
        "to": "person@example.com", "subject": "Exact subject", "body": "Exact body"
    }
    send.assert_not_called()


def payload_owner():
    import hashlib
    return hashlib.sha256(("openjarvis-local-install:" + SESSION).encode()).hexdigest()


def payload_account():
    import hashlib
    return hashlib.sha256(b"/mock/gmail.json").hexdigest()


def test_approval_is_single_use_and_cancel_expiry_are_terminal(tmp_path):
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    item = store.create({"to": "a@example.com", "subject": "s", "body": "b"}, "u", "a")
    assert store.claim(item["approval_id"], "u", "a") == {"to": "a@example.com", "subject": "s", "body": "b"}
    assert store.claim(item["approval_id"], "u", "a") is None
    cancelled = store.create({"to": "a@example.com", "subject": "s", "body": "b"}, "u", "a")
    assert store.cancel(cancelled["approval_id"], "u", "a")
    assert store.claim(cancelled["approval_id"], "u", "a") is None
    assert store.claim(item["approval_id"], "other", "a") is None
    expired = store.create({"to": "a@example.com", "subject": "s", "body": "b"}, "u", "a")
    with store._connect() as db:
        db.execute("UPDATE gmail_send_approvals SET expires=0 WHERE id=?", (expired["approval_id"],))
    assert store.claim(expired["approval_id"], "u", "a") is None
    assert store.claim("missing", "u", "a") is None


def test_confirmation_sends_server_stored_exact_message(tmp_path):
    from openjarvis.server import gmail_approval as module
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    item = store.create({"to": "person@example.com", "subject": "Trusted", "body": "Approved body"}, payload_owner(), payload_account())
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(api_key="")), "headers": [(b"x-openjarvis-approval", store.dashboard_token(item["approval_id"], payload_owner(), payload_account()).encode()), (b"x-openjarvis-chat-session", SESSION.encode())], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "scheme": "http", "method": "POST", "path": "/", "query_string": b""})
    fake_connector = SimpleNamespace(_credentials_path="/mock/gmail.json")
    fake_connector._call_with_refresh = lambda fn, raw: fn("mock-token", raw)
    with (
        patch.object(module, "store", store),
        patch("openjarvis.connectors.gmail.GmailConnector", return_value=fake_connector),
        patch("openjarvis.connectors.gmail._gmail_api_send_message", return_value={"id": "gmail-123", "threadId": "thread-1"}) as send,
    ):
        result = asyncio.run(module.confirm_gmail_approval(item["approval_id"], request))
    assert result["message_id"] == "gmail-123"
    raw = send.call_args.args[1]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    sent = email.message_from_bytes(decoded)
    assert sent["To"] == "person@example.com"
    assert sent["Subject"] == "Trusted"
    assert sent.get_payload(decode=True).decode().strip() == "Approved body"
    assert store.get(item["approval_id"], payload_owner(), payload_account())["status"] == "sent"


def test_confirmation_requires_dashboard_capability_token(tmp_path):
    from openjarvis.server import gmail_approval as module
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    item = store.create({"to": "person@example.com", "subject": "s", "body": "b"}, payload_owner(), payload_account())
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(api_key="")), "headers": [(b"x-openjarvis-chat-session", SESSION.encode())], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "scheme": "http", "method": "POST", "path": "/", "query_string": b""})
    with patch.object(module, "store", store), pytest.raises(HTTPException) as exc:
        asyncio.run(module.confirm_gmail_approval(item["approval_id"], request))
    assert exc.value.status_code == 403


def test_gmail_api_failure_is_reported_and_approval_cannot_retry(tmp_path):
    from openjarvis.server import gmail_approval as module
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    item = store.create({"to": "person@example.com", "subject": "s", "body": "b"}, payload_owner(), payload_account())
    token = store.dashboard_token(item["approval_id"], payload_owner(), payload_account())
    request = Request({"type": "http", "app": SimpleNamespace(state=SimpleNamespace(api_key="")), "headers": [(b"x-openjarvis-approval", token.encode()), (b"x-openjarvis-chat-session", SESSION.encode())], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "scheme": "http", "method": "POST", "path": "/", "query_string": b""})
    fake = SimpleNamespace(_credentials_path="/mock/gmail.json", _call_with_refresh=lambda *_: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    with patch.object(module, "store", store), patch("openjarvis.connectors.gmail.GmailConnector", return_value=fake), pytest.raises(HTTPException) as exc:
        asyncio.run(module.confirm_gmail_approval(item["approval_id"], request))
    assert exc.value.status_code == 502
    assert "reach gmail" in exc.value.detail.lower()
    assert store.get(item["approval_id"], payload_owner(), payload_account())["status"] == "failed"


# ---------------------------------------------------------------------------
# Regression: approval requests run on ToolExecutor's pooled worker threads.
# Those threads did not inherit the dashboard-session ContextVar, so a valid
# dashboard request was reported as a bogus "Gmail authentication" failure.
# ---------------------------------------------------------------------------

def _run_via_executor(store, arguments=None):
    """Execute the approval tool the way the agent does: through ToolExecutor."""
    from openjarvis.core.types import ToolCall
    from openjarvis.tools._stubs import ToolExecutor

    executor = ToolExecutor([GmailRequestSendApprovalTool()])
    call = ToolCall(
        id="call-1",
        name="gmail_request_send_approval",
        arguments=json.dumps(
            arguments
            or {"to": "me@example.com", "subject": "Test", "body": "Hello"}
        ),
    )
    with (
        patch("openjarvis.server.gmail_approval.store", store),
        patch("openjarvis.tools.gmail_tools._connector") as connector,
        patch("openjarvis.connectors.gmail._gmail_api_send_message") as send,
    ):
        connector.return_value._credentials_path = "/mock/gmail.json"
        result = executor.execute(call)
    return result, send


def test_executor_worker_thread_receives_dashboard_session(tmp_path):
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    token = current_dashboard_session.set(SESSION)
    try:
        result, send = _run_via_executor(store)
    finally:
        current_dashboard_session.reset(token)
    assert result.success, result.content
    payload = json.loads(result.content)
    assert payload["status"] == "pending" and payload["sent"] is False
    assert "dashboard_token" not in payload
    assert result.metadata["gmail_approval_id"] == payload["approval_id"]
    record = store.get(payload["approval_id"], payload_owner(), payload_account())
    assert record["status"] == "pending"
    assert record["message"] == {
        "to": "me@example.com", "subject": "Test", "body": "Hello"
    }
    send.assert_not_called()  # creating an approval never sends


def test_missing_dashboard_session_is_not_reported_as_auth_failure(tmp_path):
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    result, send = _run_via_executor(store)  # no session set
    assert not result.success
    text = result.content.lower()
    assert "dashboard chat session" in text
    assert "reconnect" not in text and "authentication" not in text
    send.assert_not_called()
    with store._connect() as db:  # nothing was stored
        assert db.execute("SELECT COUNT(*) FROM gmail_send_approvals").fetchone()[0] == 0


def test_approval_tool_reports_real_auth_and_disconnect_failures(tmp_path):
    store = GmailApprovalStore(str(tmp_path / "approvals.db"))
    token = current_dashboard_session.set(SESSION)
    try:
        with (
            patch("openjarvis.server.gmail_approval.store", store),
            patch(
                "openjarvis.tools.gmail_tools._connector",
                side_effect=RuntimeError("Gmail is disconnected."),
            ),
        ):
            disconnected = GmailRequestSendApprovalTool().execute(
                to="me@example.com", subject="s", body="b"
            )
    finally:
        current_dashboard_session.reset(token)
    assert not disconnected.success
    assert "disconnected" in disconnected.content.lower()
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM gmail_send_approvals").fetchone()[0] == 0


def test_tool_runner_propagates_contextvars_to_worker_threads():
    from contextvars import ContextVar
    from openjarvis.core.types import ToolResult
    from openjarvis.tools._stubs import _TOOL_RUNNER

    probe: ContextVar[str] = ContextVar("probe", default="unset")
    token = probe.set("caller-value")
    try:
        future = _TOOL_RUNNER.submit(lambda: ToolResult("t", probe.get(), True))
    finally:
        probe.reset(token)
    assert future.result(timeout=5).content == "caller-value"
