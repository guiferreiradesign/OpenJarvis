"""Read and draft-only Gmail tools for the dashboard orchestrator."""

from __future__ import annotations

import base64
import email.message
import json
import hashlib
import re
from collections.abc import Callable
from typing import Any

import httpx

from openjarvis.connectors.gmail import (
    GmailConnector,
    _decode_body,
    _extract_header,
    _gmail_api_create_draft,
    _gmail_api_get_thread,
    _gmail_api_get_message,
    _gmail_api_list_messages,
)
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

_GMAIL_TOOLS = frozenset(
    {"gmail_search_emails", "gmail_get_thread", "gmail_create_draft", "gmail_request_send_approval"}
)
_GMAIL_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_EMAIL = re.compile(r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")


def _valid_recipient(value: str) -> bool:
    return (
        len(value) <= 254
        and value.count("@") == 1
        and not any(char in value for char in ",;\r\n")
        and bool(_EMAIL.fullmatch(value.strip()))
    )


class DashboardSessionMissing(Exception):
    """No dashboard chat session is bound to this tool call (not an auth error)."""


_NO_SESSION_MESSAGE = (
    "Gmail send approval is available only from the dashboard chat session. "
    "Gmail credentials are fine; this request carried no dashboard session."
)


def _approval_owner() -> str:
    # Bind approvals to the random browser chat-session capability, which is
    # sent with this request but never added to model messages or tool results.
    from openjarvis.server.gmail_approval import current_dashboard_session
    session = current_dashboard_session.get()
    if not session:
        raise DashboardSessionMissing(_NO_SESSION_MESSAGE)
    return hashlib.sha256(("openjarvis-local-install:" + session).encode()).hexdigest()


def _account_binding(connector: GmailConnector) -> str:
    from openjarvis.connectors.oauth import load_tokens
    tokens = load_tokens(connector._credentials_path) or {}
    # The refresh token is a stable, opaque account surrogate. Hash it only;
    # never return or log OAuth material. Fall back to the connector store path
    # for older grants that do not contain an offline refresh token.
    identity = tokens.get("refresh_token") or connector._credentials_path
    return hashlib.sha256(identity.encode()).hexdigest()


def _connector() -> GmailConnector:
    connector = GmailConnector()
    if not connector.is_connected():
        raise RuntimeError(
            "Gmail is disconnected. Connect it in Dashboard Data Sources."
        )
    return connector


def _api_error(exc: Exception) -> str:
    """Translate Gmail failures without returning response bodies or secrets."""
    if isinstance(exc, DashboardSessionMissing):
        return _NO_SESSION_MESSAGE
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code if response is not None else 0
        if status == 401:
            return (
                "Gmail authentication expired. Reconnect Gmail in Dashboard "
                "Data Sources."
            )
        if status == 403:
            return (
                "Gmail denied this operation. Reconnect and approve the "
                "requested Gmail permissions."
            )
        if status == 404:
            return "The requested Gmail message or thread was not found."
        if status == 429:
            return "Gmail rate limit reached. Wait briefly and try again."
        if status >= 500:
            return "Gmail is temporarily unavailable. Try again later."
        return f"Gmail API rejected the request (HTTP {status})."
    if isinstance(exc, (httpx.TimeoutException, httpx.RequestError)):
        return "Could not reach Gmail. Check the connection and try again."
    if isinstance(exc, RuntimeError):
        if "disconnected" in str(exc).lower():
            return "Gmail is disconnected. Connect it in Dashboard Data Sources."
        return (
            "Gmail authentication refresh failed. Reconnect Gmail in Dashboard "
            "Data Sources."
        )
    return "Gmail operation failed. Reconnect Gmail or try again later."


def _run(name: str, operation: Callable[[], str]) -> ToolResult:
    try:
        return ToolResult(tool_name=name, content=operation(), success=True)
    except Exception as exc:  # API and credential failures become clear results.
        return ToolResult(tool_name=name, content=_api_error(exc), success=False)


def _message_metadata(message: dict[str, Any]) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers", [])
    return {
        "message_id": str(message.get("id", "")),
        "thread_id": str(message.get("threadId", "")),
        "sender": _extract_header(headers, "From"),
        "subject": _extract_header(headers, "Subject"),
        "date": _extract_header(headers, "Date"),
        "snippet": str(message.get("snippet", "")),
    }


@ToolRegistry.register("gmail_search_emails")
class GmailSearchEmailsTool(BaseTool):
    tool_id = "gmail_search_emails"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_id,
            description=(
                "Search the user's Gmail and return message metadata using Gmail "
                "search syntax. Do not infer a recipient for a future draft."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Gmail search query, such as from:alice@example.com "
                            "subject:report"
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (1 to 50)",
                        "default": 10,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            category="communication",
        )

    def execute(self, **params: Any) -> ToolResult:
        query = params.get("query")
        limit = params.get("max_results", 10)
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            return ToolResult(
                self.tool_id,
                "query must be a non-empty string of at most 500 characters.",
                False,
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            return ToolResult(
                self.tool_id, "max_results must be an integer from 1 to 50.", False
            )

        def search() -> str:
            connector = _connector()
            result = connector._call_with_refresh(
                _gmail_api_list_messages, page_token=None, query=query.strip()
            )
            items = result.get("messages", [])[:limit]
            messages = []
            for item in items:
                msg_id = item.get("id")
                if not isinstance(msg_id, str) or not _GMAIL_ID.fullmatch(msg_id):
                    continue
                message = connector._call_with_refresh(
                    _gmail_api_get_message, msg_id
                )
                messages.append(_message_metadata(message))
            return json.dumps({"messages": messages}, ensure_ascii=False)

        return _run(self.tool_id, search)


@ToolRegistry.register("gmail_get_thread")
class GmailGetThreadTool(BaseTool):
    tool_id = "gmail_get_thread"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_id,
            description=(
                "Read the messages and headers in a Gmail thread by its thread ID."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "description": (
                            "Gmail thread ID returned by gmail_search_emails"
                        ),
                    }
                },
                "required": ["thread_id"],
                "additionalProperties": False,
            },
            category="communication",
        )

    def execute(self, **params: Any) -> ToolResult:
        thread_id = params.get("thread_id")
        if not isinstance(thread_id, str) or not _GMAIL_ID.fullmatch(thread_id):
            return ToolResult(
                self.tool_id,
                "thread_id must be a valid Gmail thread identifier.",
                False,
            )

        def get_thread() -> str:
            connector = _connector()
            raw = connector._call_with_refresh(_gmail_api_get_thread, thread_id)
            messages = []
            for message in raw.get("messages", []):
                headers = message.get("payload", {}).get("headers", [])
                messages.append(
                    {
                        "message_id": str(message.get("id", "")),
                        "thread_id": str(message.get("threadId", thread_id)),
                        "from": _extract_header(headers, "From"),
                        "to": _extract_header(headers, "To"),
                        "cc": _extract_header(headers, "Cc"),
                        "date": _extract_header(headers, "Date"),
                        "subject": _extract_header(headers, "Subject"),
                        "snippet": str(message.get("snippet", "")),
                        "body": _decode_body(message.get("payload", {}))[:40000],
                    }
                )
            return json.dumps(
                {"thread_id": thread_id, "messages": messages}, ensure_ascii=False
            )

        return _run(self.tool_id, get_thread)


@ToolRegistry.register("gmail_create_draft")
class GmailCreateDraftTool(BaseTool):
    tool_id = "gmail_create_draft"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_id,
            description=(
                "Create and save a Gmail draft; this does not send it. Require a "
                "recipient explicitly provided by the user. "
                "If the recipient is missing or ambiguous, ask the user before "
                "calling this tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": (
                            "One explicitly confirmed recipient email address"
                        ),
                    },
                    "subject": {"type": "string", "description": "Draft subject"},
                    "body": {"type": "string", "description": "Draft message body"},
                },
                "required": ["to", "subject", "body"],
                "additionalProperties": False,
            },
            category="communication",
        )

    def execute(self, **params: Any) -> ToolResult:
        recipient = params.get("to")
        subject = params.get("subject")
        body = params.get("body")
        if (
            not isinstance(recipient, str)
            or "\r" in recipient
            or "\n" in recipient
            or not _valid_recipient(recipient)
        ):
            return ToolResult(
                self.tool_id,
                "to must be one valid, explicitly confirmed email address.",
                False,
            )
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or len(subject) > 998
            or "\r" in subject
            or "\n" in subject
        ):
            return ToolResult(
                self.tool_id,
                "subject must be non-empty, at most 998 characters, and contain "
                "no line breaks.",
                False,
            )
        if not isinstance(body, str) or not body.strip() or len(body) > 100000:
            return ToolResult(
                self.tool_id,
                "body must be non-empty and at most 100000 characters.",
                False,
            )

        def create_draft() -> str:
            message = email.message.EmailMessage()
            message["To"] = recipient.strip()
            message["Subject"] = subject.strip()
            message.set_content(body)
            raw_message = (
                base64.urlsafe_b64encode(message.as_bytes())
                .decode("ascii")
                .rstrip("=")
            )
            connector = _connector()
            created = connector._call_with_refresh(_gmail_api_create_draft, raw_message)
            draft_id = created.get("id", "")
            return json.dumps(
                {
                    "created": True,
                    "sent": False,
                    "draft_id": draft_id,
                    "message": "Draft created in Gmail; not sent.",
                },
                ensure_ascii=False,
            )

        return _run(self.tool_id, create_draft)


@ToolRegistry.register("gmail_request_send_approval")
class GmailRequestSendApprovalTool(BaseTool):
    tool_id = "gmail_request_send_approval"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.tool_id,
            description=("Prepare an email for review in the dashboard. This only "
                         "creates a pending approval and never sends. Tell the "
                         "user it is awaiting confirmation; never claim it was sent. "
                         "Require a recipient explicitly provided by the user."),
            parameters={"type": "object", "properties": {
                "to": {"type": "string"}, "subject": {"type": "string"},
                "body": {"type": "string"}}, "required": ["to", "subject", "body"],
                "additionalProperties": False}, category="communication",
        )

    def execute(self, **params: Any) -> ToolResult:
        recipient, subject, body = params.get("to"), params.get("subject"), params.get("body")
        if not isinstance(recipient, str) or not _valid_recipient(recipient):
            return ToolResult(self.tool_id, "to must be one valid email address.", False)
        if (not isinstance(subject, str) or not subject.strip() or len(subject) > 998
                or "\r" in subject or "\n" in subject):
            return ToolResult(self.tool_id, "subject must be non-empty, at most 998 characters, and contain no line breaks.", False)
        if not isinstance(body, str) or not body.strip() or len(body) > 100000:
            return ToolResult(self.tool_id, "body must be non-empty and at most 100000 characters.", False)
        try:
            connector = _connector()
            from openjarvis.server.gmail_approval import store
            approval = store.create(
                {"to": recipient.strip(), "subject": subject.strip(), "body": body},
                _approval_owner(), _account_binding(connector),
            )
            approval.pop("dashboard_token", None)
            approval["status"] = "pending"
            approval["sent"] = False
            approval["message"] = "Awaiting explicit dashboard confirmation. Not sent."
            return ToolResult(
                self.tool_id,
                json.dumps(approval, ensure_ascii=False),
                True,
                metadata={"gmail_approval_id": approval["approval_id"]},
            )
        except Exception as exc:
            return ToolResult(self.tool_id, _api_error(exc), False)


def dashboard_gmail_tools(agent_key: str) -> list[BaseTool]:
    """Return the fixed Gmail tool allowlist for the dashboard orchestrator."""
    if agent_key != "orchestrator":
        return []
    # Keep the agent capability bounded even if another Gmail tool is added
    # to the global ToolRegistry in the future.
    classes = {
        GmailSearchEmailsTool.tool_id: GmailSearchEmailsTool,
        GmailGetThreadTool.tool_id: GmailGetThreadTool,
        GmailCreateDraftTool.tool_id: GmailCreateDraftTool,
        GmailRequestSendApprovalTool.tool_id: GmailRequestSendApprovalTool,
    }
    return [classes[name]() for name in sorted(_GMAIL_TOOLS)]


__all__ = ["dashboard_gmail_tools"]
