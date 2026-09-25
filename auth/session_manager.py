"""
Session management, login/logout, and flash messages.

Sessions are stored server-side in the http_sessions table. The browser receives
only a signed, opaque session ID cookie (HMAC-SHA256).

Public API:
  load_session_into_request(request)       — call before routing (reads cookie)
  save_session_from_request(request, resp) — call after controller (writes cookie)
  login_user(request, user)                — mark session as belonging to user
  logout_user(request)                     — clear session and delete from DB
  flash(request, message, category)        — queue a one-time message
  get_flashed_messages(request, ...)       — pop and return queued messages
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Config
from core.http import Request, Response
from database.db import Database
from models.user import User

SESSION_COOKIE_NAME = "sp_session"
SESSION_LIFETIME = timedelta(days=14)

_FLASH_KEY = "_flashes"


# --------------------------------------------------------------------------
# Cookie signing — prevents a client from handing us a session_id we never
# issued and reading another user's server-side session data.
# --------------------------------------------------------------------------

def _sign(session_id: str) -> str:
    signature = hmac.new(
        Config.SECRET_KEY.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{session_id}.{signature}"


def _unsign(signed_value: str) -> Optional[str]:
    try:
        session_id, signature = signed_value.rsplit(".", 1)
    except ValueError:
        return None
    expected = hmac.new(
        Config.SECRET_KEY.encode("utf-8"), session_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return session_id


# --------------------------------------------------------------------------
# Server-side session storage (http_sessions table)
# --------------------------------------------------------------------------

def _load_session(session_id: str) -> dict:
    row = Database.fetch_one(
        "SELECT data, expires_at FROM http_sessions WHERE session_id = ?",
        (session_id,),
    )
    if not row:
        return {}
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        Database.execute("DELETE FROM http_sessions WHERE session_id = ?", (session_id,))
        return {}
    try:
        return json.loads(row["data"])
    except (TypeError, ValueError):
        return {}


def _save_session(session_id: str, data: dict) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_at = now + SESSION_LIFETIME
    existing = Database.fetch_one(
        "SELECT session_id FROM http_sessions WHERE session_id = ?", (session_id,)
    )
    if existing:
        Database.execute(
            "UPDATE http_sessions SET data = ?, expires_at = ? WHERE session_id = ?",
            (json.dumps(data), expires_at.isoformat(), session_id),
        )
    else:
        Database.execute(
            "INSERT INTO http_sessions (session_id, data, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, json.dumps(data), now.isoformat(), expires_at.isoformat()),
        )


def _delete_session(session_id: str) -> None:
    Database.execute("DELETE FROM http_sessions WHERE session_id = ?", (session_id,))


# --------------------------------------------------------------------------
# Request-level hooks — called once per request from main.py.
# --------------------------------------------------------------------------

def load_session_into_request(request: Request) -> None:
    """
    Populate request.session and request.current_user from the signed
    session cookie. Call once per request, before routing.
    """
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    session_id = _unsign(raw_cookie) if raw_cookie else None

    if session_id:
        request.session = _load_session(session_id)
        request._session_id = session_id
    else:
        request.session = {}
        request._session_id = None

    user_id = request.session.get("_user_id")
    request.current_user = User.get_by_id(user_id) if user_id else None


def save_session_from_request(request: Request, response: Response) -> None:
    """
    Save any session changes to the DB and set the session cookie on the response.
    Call once per request, after the controller runs.
    """
    session_id = request._session_id
    if session_id is None:
        # Only create a new session row if something was written — avoids
        # empty rows for every anonymous page view.
        if not request.session:
            return
        session_id = secrets.token_urlsafe(32)
        request._session_id = session_id

    _save_session(session_id, request.session)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _sign(session_id),
        max_age=int(SESSION_LIFETIME.total_seconds()),
    )


def login_user(request: Request, user: User) -> None:
    """Mark this request's session as belonging to `user`."""
    request.session["_user_id"] = user.id
    request.current_user = user


def logout_user(request: Request) -> None:
    """Clear the logged-in user from this session and delete the session from DB."""
    request.session.pop("_user_id", None)
    request.current_user = None
    if request._session_id:
        _delete_session(request._session_id)
        request._session_id = None
        request.session = {}


def flash(request: Request, message: str, category: str = "message") -> None:
    """Queue a one-time message to show on the next rendered page."""
    flashes = request.session.setdefault(_FLASH_KEY, [])
    flashes.append([category, message])


def get_flashed_messages(request: Request, with_categories: bool = False,
                          category_filter: Optional[list] = None):
    """
    Pop and return queued flash messages.

    Messages are removed from the session when read, so a page refresh
    never shows the same message twice.
    """
    flashes = request.session.pop(_FLASH_KEY, [])
    if category_filter:
        flashes = [f for f in flashes if f[0] in category_filter]
    if with_categories:
        return [tuple(f) for f in flashes]
    return [f[1] for f in flashes]
