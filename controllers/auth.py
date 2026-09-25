"""
Authentication routes.

Routes (no URL prefix):
  GET  /                          — public landing page
  GET  /locations/<location_id>   — public location detail
  GET/POST /login                 — login form
  GET/POST /register              — driver self-registration
  GET  /logout                    — sign out
  GET  /dashboard                 — role-based dashboard redirect
  GET/POST /forgot-password       — step 1 of password reset
  GET/POST /reset-password/<tok>  — step 2 of password reset
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from auth.decorators import login_required
from auth.session_manager import login_user, logout_user, flash
from core.http import Request, Response
from core.router import Blueprint
from models.location import get_all_locations, get_location_by_id
from models.slot import count_slots_by_status, get_slots_by_location
from models.user import (
    create_user,
    check_password,
    User,
    set_reset_token,
    get_user_by_reset_token,
    update_password,
    get_user_by_email,
)
from services.email import send_password_reset_email

auth_bp = Blueprint("auth")

# Set by main.py after the app's singletons (router, template env) exist,
# avoiding a circular import between this module and core/templating.py.
_render = None
_router = None


def configure(render_func, router):
    global _render, _router
    _render = render_func
    _router = router


def render(request: Request, template_name: str, **context) -> Response:
    html = _render(template_name, request, context)
    return Response.html(html)


@auth_bp.route("/")
def index(request: Request) -> Response:
    """
    Public landing page.
    Shows all active parking locations so anyone can browse availability
    without needing an account. Authenticated users go straight to their dashboard.
    """
    if request.current_user and request.current_user.is_authenticated:
        return Response.redirect(_router.url_for("auth.dashboard_redirect"))

    locations = get_all_locations()

    # Attach live slot counts to each location card so visitors can see availability
    for loc in locations:
        loc["slot_summary"] = count_slots_by_status(str(loc["id"]))

    return render(request, "index.html", locations=locations)


@auth_bp.route("/locations/<location_id>")
def public_location(request: Request) -> Response:
    """
    Public location detail — shows slot layout for one parking location.
    No login needed. Reserving a slot will prompt them to log in or register.
    """
    location_id = request.param("location_id")
    location = get_location_by_id(location_id)
    if not location:
        flash(request, "Location not found.", "danger")
        return Response.redirect(_router.url_for("auth.index"))

    slots = get_slots_by_location(location_id)
    slot_summary = count_slots_by_status(location_id)

    return render(
        request, "public_location.html",
        location=location, slots=slots, slot_summary=slot_summary,
    )


@auth_bp.route("/login", methods=["GET", "POST"])
def login(request: Request) -> Response:
    """
    Display the login form (GET) or process credentials (POST).

    On success the user is redirected to their role-specific dashboard.
    On failure a flash message is shown and the form is re-displayed.
    """
    if request.current_user and request.current_user.is_authenticated:
        return Response.redirect(_router.url_for("auth.dashboard_redirect"))

    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        user_data = check_password(email, password)

        if user_data:
            user = User(user_data)
            login_user(request, user)
            flash(request, f"Welcome back, {user.name}!", "success")
            return Response.redirect(_router.url_for("auth.dashboard_redirect"))
        else:
            flash(request, "Incorrect email or password. Please try again.", "danger")

    return render(request, "auth/login.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register(request: Request) -> Response:
    """
    Driver self-registration.

    Only drivers can self-register; other roles are created by admins.
    Validates the form, creates the account, then redirects to login.
    """
    if request.current_user and request.current_user.is_authenticated:
        return Response.redirect(_router.url_for("auth.dashboard_redirect"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not name or not email or not password:
            flash(request, "All fields are required.", "danger")
        elif password != confirm:
            flash(request, "Passwords do not match.", "danger")
        elif len(password) < 6:
            flash(request, "Password must be at least 6 characters.", "danger")
        else:
            user_id = create_user(name, email, password, role="driver")
            if user_id:
                flash(request, "Account created! Please log in.", "success")
                return Response.redirect(_router.url_for("auth.index"))
            else:
                flash(request, "That email is already registered.", "danger")

    return render(request, "auth/register.html")


@auth_bp.route("/logout")
@login_required
def logout(request: Request) -> Response:
    """Sign out the current user and redirect to the home page."""
    logout_user(request)
    flash(request, "You have been logged out.", "info")
    return Response.redirect(_router.url_for("auth.index"))


@auth_bp.route("/dashboard")
@login_required
def dashboard_redirect(request: Request) -> Response:
    """
    Role-based dashboard redirect — called after every successful login.

    Each role has its own controller dashboard so we redirect based on
    ``request.current_user.role`` rather than hard-coding URLs here.
    """
    role = request.current_user.role

    role_to_endpoint = {
        "super_admin": "super_admin.dashboard",
        "admin": "admin.dashboard",
        "guard": "guard.dashboard",
        "driver": "driver.dashboard",
    }

    endpoint = role_to_endpoint.get(role)
    if endpoint:
        return Response.redirect(_router.url_for(endpoint))

    flash(request, "Unknown role. Please contact support.", "danger")
    return Response.redirect(_router.url_for("auth.logout"))


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password(request: Request) -> Response:
    """
    Password-reset step 1: accept an email address and send a reset link.

    Security notes
    --------------
    * We always display the same success message whether or not the email
      exists, to avoid leaking which addresses are registered (user enumeration).
    * If a non-expired token already exists we do NOT overwrite it — this
      prevents an attacker from invalidating a legitimate reset link by
      repeatedly requesting new ones.
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()

        user = get_user_by_email(email)
        if user:
            now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
            existing_token = user["reset_token"]
            existing_expires_raw = user["reset_expires_at"]
            existing_expires = (
                datetime.fromisoformat(existing_expires_raw) if existing_expires_raw else None
            )
            if existing_expires and existing_expires.tzinfo is not None:
                existing_expires = existing_expires.replace(tzinfo=None)

            token_still_valid = bool(
                existing_token and existing_expires and now_naive < existing_expires
            )

            if not token_still_valid:
                token = secrets.token_urlsafe(32)
                expires_at = now_naive + timedelta(hours=1)
                set_reset_token(email, token, expires_at)
                reset_url = _router.url_for("auth.reset_password", token=token)
                send_password_reset_email(email, reset_url)

        flash(
            request,
            "If that email is registered, a reset link has been sent. "
            "Check your inbox (and spam folder).",
            "info",
        )
        return Response.redirect(_router.url_for("auth.index"))

    return render(request, "auth/forgot_password.html")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(request: Request) -> Response:
    """
    Password-reset step 2: validate the token and set a new password.

    The token is looked up in the database; if it is missing or expired
    the user is sent back to the forgot-password form.
    """
    token = request.param("token")
    user = get_user_by_reset_token(token)

    if not user:
        flash(request, "This reset link is invalid or has expired. Please request a new one.", "danger")
        return Response.redirect(_router.url_for("auth.forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if len(new_password) < 6:
            flash(request, "Password must be at least 6 characters.", "danger")
        elif new_password != confirm:
            flash(request, "Passwords do not match.", "danger")
        else:
            update_password(str(user["id"]), new_password)
            flash(request, "Password updated successfully. Please log in.", "success")
            return Response.redirect(_router.url_for("auth.index"))

    return render(request, "auth/reset_password.html", token=token)
