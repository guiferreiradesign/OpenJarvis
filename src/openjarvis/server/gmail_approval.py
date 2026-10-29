"""Server-side, single-use approval records for Gmail sends."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import hmac
import re
import os
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import APIRouter, HTTPException, Request

from openjarvis.core.paths import get_config_dir

_lock = threading.RLock()
current_dashboard_session: ContextVar[str] = ContextVar("gmail_approval_dashboard_session", default="")


class GmailApprovalStore:
    def __init__(self, path: str = "") -> None:
        self.path = path or str(get_config_dir() / "gmail_send_approvals.db")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS gmail_send_approvals (
                id TEXT PRIMARY KEY, message TEXT NOT NULL, owner TEXT NOT NULL,
                account TEXT NOT NULL, dashboard_token TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, result TEXT
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(gmail_send_approvals)")}
            if "dashboard_token" not in columns:
                db.execute("ALTER TABLE gmail_send_approvals ADD COLUMN dashboard_token TEXT NOT NULL DEFAULT ''")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # SQLite's parent is the private OpenJarvis config directory. Some
            # platforms do not implement POSIX file modes.
            pass

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level="IMMEDIATE")
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(self, message: dict[str, str], owner: str, account: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc).timestamp()
        expires = now + 15 * 60
        approval_id = secrets.token_urlsafe(32)
        dashboard_token = secrets.token_urlsafe(32)
        with _lock, self._connect() as db:
            db.execute(
                "INSERT INTO gmail_send_approvals "
                "(id, message, owner, account, dashboard_token, status, created, expires, result) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, NULL)",
                (approval_id, json.dumps(message), owner, account, dashboard_token, now, expires),
            )
        return {"approval_id": approval_id, **message,
                "dashboard_token": dashboard_token,
                "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    def dashboard_token(self, approval_id: str, owner: str, account: str) -> str | None:
        with _lock, self._connect() as db:
            row = db.execute("SELECT dashboard_token FROM gmail_send_approvals WHERE id=? AND owner=? AND account=?",
                             (approval_id, owner, account)).fetchone()
            return row[0] if row else None

    def check_dashboard_token(self, approval_id: str, owner: str, account: str, presented: str) -> bool:
        expected = self.dashboard_token(approval_id, owner, account)
        return bool(expected and presented and hmac.compare_digest(expected, presented))

    def get(self, approval_id: str, owner: str, account: str) -> dict[str, Any] | None:
        with _lock, self._connect() as db:
            row = db.execute("SELECT * FROM gmail_send_approvals WHERE id=? AND owner=? AND account=?",
                             (approval_id, owner, account)).fetchone()
            if row is None:
                return None
            if row["status"] == "pending" and row["expires"] <= datetime.now(timezone.utc).timestamp():
                db.execute("UPDATE gmail_send_approvals SET status='expired' WHERE id=? AND status='pending'", (approval_id,))
                return None
            return {"id": row["id"], "message": json.loads(row["message"]), "status": row["status"], "result": json.loads(row["result"]) if row["result"] else None}

    def claim(self, approval_id: str, owner: str, account: str) -> dict[str, str] | None:
        with _lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT message, status, expires FROM gmail_send_approvals WHERE id=? AND owner=? AND account=?",
                             (approval_id, owner, account)).fetchone()
            if row is None or row["status"] != "pending":
                return None
            if row["expires"] <= datetime.now(timezone.utc).timestamp():
                db.execute("UPDATE gmail_send_approvals SET status='expired' WHERE id=?", (approval_id,))
                return None
            cur = db.execute("UPDATE gmail_send_approvals SET status='sending' WHERE id=? AND status='pending'", (approval_id,))
            if cur.rowcount != 1:
                return None
            return json.loads(row["message"])

    def finish(self, approval_id: str, result: dict[str, Any] | None, *, sent: bool) -> None:
        with _lock, self._connect() as db:
            db.execute("UPDATE gmail_send_approvals SET status=?, result=? WHERE id=? AND status='sending'",
                       ("sent" if sent else "failed", json.dumps(result) if result else None, approval_id))

    def cancel(self, approval_id: str, owner: str, account: str) -> bool:
        with _lock, self._connect() as db:
            cur = db.execute("UPDATE gmail_send_approvals SET status='cancelled' WHERE id=? AND owner=? AND account=? AND status='pending'",
                             (approval_id, owner, account))
            return cur.rowcount == 1


store = GmailApprovalStore()
router = APIRouter()


def _request_binding(request: Request) -> tuple[str, str]:
    import hashlib
    from openjarvis.connectors.gmail import GmailConnector
    from openjarvis.tools.gmail_tools import _account_binding
    session = request.headers.get("X-OpenJarvis-Chat-Session", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", session):
        raise HTTPException(status_code=403, detail="Dashboard chat session is missing or invalid.")
    owner = hashlib.sha256(("openjarvis-local-install:" + session).encode()).hexdigest()
    account = _account_binding(GmailConnector())
    return owner, account


@router.get("/v1/gmail/approvals/{approval_id}")
async def get_gmail_approval(approval_id: str, request: Request):
    owner, account = _request_binding(request)
    if not store.check_dashboard_token(approval_id, owner, account, request.headers.get("X-OpenJarvis-Approval", "")):
        raise HTTPException(status_code=403, detail="Dashboard approval token is missing or invalid.")
    item = store.get(approval_id, owner, account)
    if item is None:
        raise HTTPException(status_code=404, detail="Approval is missing, expired, or unavailable.")
    return item


@router.post("/v1/gmail/approvals/{approval_id}/cancel")
async def cancel_gmail_approval(approval_id: str, request: Request):
    owner, account = _request_binding(request)
    if not store.check_dashboard_token(approval_id, owner, account, request.headers.get("X-OpenJarvis-Approval", "")):
        raise HTTPException(status_code=403, detail="Dashboard approval token is missing or invalid.")
    if not store.cancel(approval_id, owner, account):
        raise HTTPException(status_code=409, detail="Approval is no longer pending.")
    return {"status": "cancelled"}


@router.post("/v1/gmail/approvals/{approval_id}/confirm")
async def confirm_gmail_approval(approval_id: str, request: Request):
    import base64
    import email.message
    from openjarvis.connectors.gmail import GmailConnector, _gmail_api_send_message
    owner, account = _request_binding(request)
    if not store.check_dashboard_token(approval_id, owner, account, request.headers.get("X-OpenJarvis-Approval", "")):
        raise HTTPException(status_code=403, detail="Dashboard approval token is missing or invalid.")
    message_data = store.claim(approval_id, owner, account)
    if message_data is None:
        raise HTTPException(status_code=409, detail="Approval is missing, expired, cancelled, or already used.")
    msg = email.message.EmailMessage()
    msg["To"] = message_data["to"]
    msg["Subject"] = message_data["subject"]
    msg.set_content(message_data["body"])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    connector = GmailConnector()
    try:
        result = connector._call_with_refresh(_gmail_api_send_message, raw)
        sent = {"status": "sent", "message_id": result.get("id"), "thread_id": result.get("threadId")}
        store.finish(approval_id, sent, sent=True)
        return sent
    except Exception as exc:
        store.finish(approval_id, None, sent=False)
        from openjarvis.tools.gmail_tools import _api_error
        raise HTTPException(status_code=502, detail=_api_error(exc)) from None


__all__ = ["GmailApprovalStore", "store", "router", "current_dashboard_session"]
