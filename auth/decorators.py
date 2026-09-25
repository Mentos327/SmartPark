"""
Role-based access control decorators for smart.park.

Usage:
    @guard_required
    def checkin(request): ...

Each decorator checks authentication first, then the required role.
If either check fails the user is redirected (not given a 403 error).

Available decorators:
  @login_required       — any authenticated user
  @driver_required      — drivers only
  @guard_required       — guards only
  @admin_required       — admins and super-admins
  @super_admin_required — super-admins only
  @staff_required       — guards, admins, and super-admins (not drivers)
  @roles_required(*r)   — custom role list (OR logic)
"""
from __future__ import annotations

from functools import wraps
from typing import Callable

from auth.session_manager import flash
from core.http import Request, Response

# Filled in by main.py once the Router/render helper exist (avoids a
# circular import between this module and core/router.py at import time).
# Global placeholders for routing and template rendering.
# These will be assigned actual implementations during app initialization.

_router = None
_render = None
# Start with empty placeholders for router and render function


def set_router(router) -> None:
    # Save the given router object into the global variable _router.
    # The router is used to generate URLs (like redirecting to login).
    global _router
    _router = router


def set_render(render_fn) -> None:
    # Save the given render function into the global variable _render.
    # The render function is used to turn templates into HTML pages.
    global _render
    _render = render_fn


def login_required(f: Callable) -> Callable:
    """Require any authenticated user, regardless of role."""

    @wraps(f)
    def decorated(request: Request, *args, **kwargs):
        if not request.current_user or not request.current_user.is_authenticated:
            # If the user is not logged in, show a warning and redirect to login page.
            flash(request, "Please log in first.", "warning")
            return Response.redirect(_router.url_for("auth.login"))
        # If the user is logged in, allow them to run the protected function.
        return f(request, *args, **kwargs)

    return decorated


def roles_required(*roles: str) -> Callable:
    """
    Factory that returns a decorator enforcing authentication and role
    membership.

    Parameters
    ----------
    *roles:
        One or more role strings the user must possess (e.g. "guard",
        "admin"). The check is OR-based — the user only needs *one*
        of the listed roles.
    """

    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated(request: Request, *args, **kwargs):
            # Step 1 — Authentication: reject anonymous visitors immediately.
            if not request.current_user or not request.current_user.is_authenticated:
                flash(request, "Please log in first.", "warning")
                return Response.redirect(_router.url_for("auth.login"))

            # Step 2 — Authorisation: ensure the user holds a permitted role.
            # The user is already known to be logged in at this point, so an
            # authenticated-but-unauthorised request gets a real 403
            # response rather than a redirect
            if request.current_user.role not in roles:
                if _render is not None:
                    html = _render("errors/403.html", request, {})
                    return Response.html(html, status=403)
                return Response.html("403 Forbidden", status=403)

            return f(request, *args, **kwargs)

        return decorated

    # This decorator checks if a user is logged in and has the right role before allowing access to a protected function. It redirects unauthenticated users to login and shows a 403 error for unauthorised ones.

    return decorator


# These thin wrappers let routes use a single, self-documenting decorator
# instead of roles_required("guard") etc., keeping controller files readable.

def super_admin_required(f: Callable) -> Callable:
    """Restrict access to super-admin users only."""
    return roles_required("super_admin")(f)
# This decorator ensures that only users with the 'super_admin' role can access the function it protects.


def admin_required(f: Callable) -> Callable:
    """
    Restrict access to admin and super-admin users.

    Super-admins are included so they can inspect any admin view without
    needing a separate account.
    """
    return roles_required("admin", "super_admin")(f)


def guard_required(f: Callable) -> Callable:
    """Restrict access to parking-guard users only."""
    return roles_required("guard")(f)


def driver_required(f: Callable) -> Callable:
    """Restrict access to driver (regular customer) users only."""
    return roles_required("driver")(f)


def staff_required(f: Callable) -> Callable:
    """
    Restrict access to any internal staff member.

    Covers guards, location admins, and super-admins — anyone who is NOT
    a regular driver.
    """
    return roles_required("guard", "admin", "super_admin")(f)
