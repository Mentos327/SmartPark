"""
Jinja2 template rendering for smart.park.

build_environment(router) : configure the Jinja2 Environment (call once at startup).
render(env, router, name, request, context) : render a template by filename.

The following variables are injected automatically into every template:
  url_for, current_user, session, request, get_flashed_messages, now, notifications.

All times are converted to EAT (UTC+3) using the eat_time filter and eat_now().
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def format_eat_time(dt):
    """Jinja filter: convert a stored UTC datetime to EAT (UTC+3) for display."""
    if dt is None:
        return "N/A"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    eat = dt + timedelta(hours=3)
    return eat.strftime("%d %b %Y, %H:%M")


def eat_now():
    """Return the current Nairobi time (EAT = UTC+3) as a naive datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)


def to_eat(dt):
    """Convert a naive UTC datetime (or ISO string) to naive EAT. Returns None if dt is None."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return dt
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt + timedelta(hours=3)


class _AnonymousUser:
    """Placeholder for unauthenticated visitors.
    Templates can safely check current_user.is_authenticated at all times."""
    is_authenticated = False
    name = ""
    email = ""
    role = None
    id = None
    location_id = None


ANONYMOUS_USER = _AnonymousUser()


def build_environment(router):
    """
    Build and configure the Jinja2 Environment for the whole app.
    Call once at startup. Pass the Router so url_for() can resolve
    endpoint names to URL paths.
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )

    # ---- filters ----
    env.filters["eat_time"] = format_eat_time
    env.filters["to_eat"] = to_eat

    # url_for, now, notifications, etc. are injected per-render in render() below.

    return env, router


def render(env: Environment, router, template_name: str, request, context: Optional[dict] = None) -> str:
    """
    Render a template by name, automatically injecting:
      url_for, current_user, session, request, get_flashed_messages,
      now, notifications.

    Extra context values can be passed via the context dict.
    """
    from auth.session_manager import get_flashed_messages
    from services.notification import build_notifications

    template = env.get_template(template_name)

    base_context = {
        "url_for": router.url_for,
        "current_user": request.current_user or ANONYMOUS_USER,
        "session": request.session,
        "request": request,
        "get_flashed_messages": lambda **kw: get_flashed_messages(request, **kw),
        "now": eat_now(),
        "notifications": build_notifications(request.current_user),
    }
    if context:
        base_context.update(context)

    return template.render(**base_context)
