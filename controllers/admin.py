"""
Admin-facing routes (URL prefix: /admin).

Covers slot management, guard management, pricing/payments, reports,
camera view, and admin profile. All routes require @admin_required.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from auth.decorators import admin_required
from auth.session_manager import login_user, flash
from core.http import Request, Response
from core.router import Blueprint
from database.db import Database
from models.slot import (create_slot, get_slots_by_location,
                          update_slot_status, count_slots_by_status)
from models.location import get_location_by_id, update_location
from models.camera import (create_camera, get_cameras_by_location,
                            get_camera_by_id, delete_camera)
from models.user import (create_user, get_users_at_location,
                          delete_user, update_admin_profile,
                          update_password, check_password, User, get_user_by_id,
                          update_guard_details)
from models.session import get_sessions_by_location
from models.audit_log import log_action
from services.email import send_guard_credentials

admin_bp = Blueprint("admin")

_render = None


def _get_own_guard(request, guard_id):
    """Fetch a guard row scoped to the current admin's location, or None
    if it doesn't exist / isn't a guard / belongs to another location."""
    try:
        return Database.fetch_one(
            "SELECT * FROM users WHERE id = ? AND role = 'guard' AND location_id = ?",
            (int(guard_id), str(request.current_user.location_id)),
        )
    except (TypeError, ValueError):
        return None


_router = None  # Stores the application's router; initially no router is configured.

#initializes the rendering system by storing the rendering function and router
def configure(render_func, router):
    # Configures the module by storing the rendering function and application router.
    global _render, _router
    # Store the rendering function for use when generating HTML pages.
    _render = render_func
    # Store the application's router for use by other functions in this module.
    _router = router


def render(request: Request, template_name: str, **context) -> Response:
    # Generates an HTML page using the specified template and supplied context data.
    #configures the application's rendering system and provides a render() function that takes a template and its data, generates the HTML, and returns it to the user's browser.

    html = _render(template_name, request, context)

    # Return the generated HTML to the browser as an HTTP response.
    return Response.html(html)


def get_admin_location(request: Request):
    """Return the location row for the currently logged-in admin."""
    return get_location_by_id(request.current_user.location_id)
#supports location-based access control

@admin_bp.route("/dashboard") # Defines the URL used to access the administrator dashboard.
@admin_required
def dashboard(request: Request) -> Response:
    # Handles the administrator dashboard request and prepares its data.
    location = get_admin_location(request)
    # Retrieve the parking location assigned to the currently logged-in administrator.
    if not location:
        flash(request, "No location assigned to your account.", "warning")
        return Response.redirect(_router.url_for("auth.logout"))

    slot_summary = count_slots_by_status(request.current_user.location_id)
    # Count parking slots by status for the administrator's assigned location.
    recent_sessions = get_sessions_by_location(request.current_user.location_id, limit=10)
    # Retrieve the 10 most recent parking sessions for the administrator's location.

    eat_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    # Get today's date in East Africa Time (EAT), which is UTC+3.
    eat_today_start = datetime.combine(eat_today, datetime.min.time()) - timedelta(hours=3)
    # Convert the start of today in EAT to the corresponding UTC time for database filtering.
    eat_today_end = datetime.combine(eat_today, datetime.max.time()) - timedelta(hours=3)
    # Convert the end of today in EAT to the corresponding UTC time for database filtering.

    today_paid = Database.fetch_all(
        """
        SELECT * FROM sessions
        WHERE location_id = ? AND is_paid = 1
          AND entry_time >= ? AND entry_time <= ?
        """, # Retrieve all paid sessions for today's EAT date
        (str(request.current_user.location_id), eat_today_start.isoformat(), eat_today_end.isoformat()),
        # Supply the administrator's location ID and the start/end timestamps used by the query.
    )
    today_revenue = sum(s["fee_kes"] or 0 for s in today_paid)
    #Calculate total revenue from all paid parking sessions recorded today.
    slots = get_slots_by_location(request.current_user.location_id)
    # Retrieve all parking slots belonging to the administrator's location
    mpesa_revenue = sum((s["fee_kes"] or 0) for s in today_paid if s["payment_method"] == "mpesa")
    # Calculate today's revenue collected through M-Pesa payments.
    cash_revenue = sum((s["fee_kes"] or 0) for s in today_paid if s["payment_method"] == "cash")
    # Calculate today's revenue collected through cash payments.

    slot_map = {str(s["id"]): s["slot_number"] for s in slots}
    # Create a lookup dictionary that maps each slot ID to its slot number.
    for s in recent_sessions:
        # Add the corresponding parking slot number to each recent session.
        s["slot_number"] = slot_map.get(str(s["slot_id"] or ""), "")
        # Add the matching slot number to the session, or an empty value if no slot is found.

    return render(
        request, "admin/dashboard.html",
        location=location, slot_summary=slot_summary, slots=slots,
        recent_sessions=recent_sessions, today_revenue=today_revenue,
        mpesa_revenue=mpesa_revenue, cash_revenue=cash_revenue,
    )
    # Render the administrator dashboard and provide all required data to the template.

@admin_bp.route("/slots")# Defines the URL for the administrator's parking slots page.
@admin_required
def slots(request: Request) -> Response:
    # Handles the administrator's parking slots page and returns it as a web response.
    location = get_admin_location(request)
    all_slots = get_slots_by_location(request.current_user.location_id)
    return render(request, "admin/slots.html", location=location, slots=all_slots)


@admin_bp.route("/slots/create", methods=["POST"])
@admin_required
def create_slot_view(request: Request) -> Response:
    slot_number = request.form.get("slot_number", "").strip()
    # Get the submitted slot number and remove unnecessary spaces.
    slot_type = request.form.get("slot_type", "standard")
    # Get the submitted slot type, using "standard" as the default.
    level = request.form.get("level", "G").strip() or "G"
    # Get the parking level, defaulting to "G" when no valid level is provided.

    if not slot_number:
        flash(request, "Slot number is required.", "danger")
    else:
        result = create_slot(request.current_user.location_id, slot_number, slot_type, level)
        if result is None:
            flash(request, f"Slot {slot_number} already exists at this location.", "danger")
        else:
            flash(request, f"Slot {slot_number} created.", "success")

    return Response.redirect(_router.url_for("admin.slots"))
    # Redirect back to the administrator's slots page after processing the request.

@admin_bp.route("/slots/<slot_id>/status", methods=["POST"])
@admin_required
def update_slot(request: Request) -> Response:
    slot_id = request.param("slot_id")
    # Retrieve the ID of the parking slot from the URL.
    update_slot_status(slot_id, request.form.get("status"))
    flash(request, "Slot status updated.", "success")
    return Response.redirect(_router.url_for("admin.slots"))


@admin_bp.route("/guards")
@admin_required
def guards(request: Request) -> Response:
    # Handles the request for the administrator's Guards page.
    location = get_admin_location(request)
    all_users = get_users_at_location(request.current_user.location_id)
    all_guards = [u for u in all_users if u["role"] == "guard"]
    # Filters the list of users and keeps only users whose role is "guard".
    # u represents one user at a time from all_users.
    # u["role"] accesses that user's role stored in the database record.
    return render(request, "admin/guards.html", location=location, guards=all_guards)
    # Displays the Guards page and sends the location and filtered guard list
    # to the admin/guards.html template.

@admin_bp.route("/guards/create", methods=["GET", "POST"])# GET displays the form, while POST submits the form data.
@admin_required
def create_guard(request: Request) -> Response:
    # Handles the process of displaying the guard creation form
    location = get_admin_location(request)

    if request.method == "POST":
        # Checks whether the form has been submitted.
        name            = request.form.get("name", "").strip()
        email           = request.form.get("email", "").strip().lower() or None
        password        = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        phone           = request.form.get("phone", "").strip() or None
        national_id     = request.form.get("national_id", "").strip() or None

        if not name:
            flash(request, "Guard name is required.", "danger")
        elif not password:
            flash(request, "A password is required.", "danger")
        elif len(password) < 6:
            flash(request, "Password must be at least 6 characters.", "danger")
        elif password != confirm_password:
            flash(request, "Passwords do not match.", "danger")
        else:
            user_id = create_user(
                name=name, email=email, password=password, role="guard",
                location_id=request.current_user.location_id,
                phone=phone, national_id=national_id,
            )
            if user_id:
                if email:
                    send_guard_credentials(email, name, password, location["name"])
                    flash(request, f"Guard {name} created and credentials emailed to {email}.", "success")
                    # Sends the guard's login credentials to their email address.
                else:
                    flash(request, f"Guard {name} created. Share their password with them directly.", "success")
                return Response.redirect(_router.url_for("admin.guards"))
                # Redirects the administrator back to the Guards page
                # after successfully creating the account.
            else:
                flash(request, "That email address is already in use.", "danger")

    return render(request, "admin/create_guard.html", location=location)

# <guard_id> is a dynamic URL parameter identifying the guard.
@admin_bp.route("/guards/<guard_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_guard(request: Request) -> Response:
    """Edit a guard's name, email, phone, and national ID.
    Only the admin of the same location can edit their guards.
    """
    location = get_admin_location(request)
    guard_id = request.param("guard_id")
    # Retrieves the guard's ID from the URL.
    guard = _get_own_guard(request, guard_id)
    # Retrieves the guard only if the guard exists, has the guard role,
    # and belongs to the administrator's assigned parking location.
    if not guard:
        flash(request, "Guard not found or not at your location.", "danger")
        return Response.redirect(_router.url_for("admin.guards"))

    if request.method == "POST":
        name        = request.form.get("name", "").strip()
        email       = request.form.get("email", "").strip().lower() or None
        phone       = request.form.get("phone", "").strip() or None
        national_id = request.form.get("national_id", "").strip() or None

        if not name:
            flash(request, "Guard name is required.", "danger")
        elif not email:
            flash(request, "Guard email is required.", "danger")
        elif update_guard_details(guard_id, name, email, phone, national_id):
            flash(request, f"{name}'s details have been updated.", "success")
            return Response.redirect(_router.url_for("admin.guards"))
        else:
            flash(request, "That email address is already in use.", "danger")
        # Re-fetch so the form shows what was actually submitted on error.
        guard = Database.fetch_one("SELECT * FROM users WHERE id = ?", (int(guard_id),))

    return render(request, "admin/edit_guard.html", location=location, guard=guard)


@admin_bp.route("/guards/<guard_id>/dismiss", methods=["POST"])
@admin_required
def dismiss_guard(request: Request) -> Response:
    """Permanently delete a guard account.
    Only the admin of the same location can dismiss a guard.
    """
    guard_id = request.param("guard_id")
    guard = _get_own_guard(request, guard_id)
    # Retrieves the guard only if the guard exists, has the "guard" role,
    # and belongs to the currently logged-in administrator's location.

    if not guard:
        flash(request, "Guard not found or not at your location.", "danger")
        return Response.redirect(_router.url_for("admin.guards"))

    guard_name = guard["name"] or "Guard"
    # If the name is empty or missing, "Guard" is used as a fallback.

    delete_user(guard_id)
    flash(request, f"{guard_name} has been dismissed and their account deleted.", "success")
    return Response.redirect(_router.url_for("admin.guards"))


@admin_bp.route("/pricing", methods=["GET", "POST"])
@admin_required
def pricing(request: Request) -> Response:
    # Handles viewing and updating parking pricing and M-Pesa settings.

    location = get_admin_location(request)
    # Retrieves the parking location assigned to the currently logged-in administrator.

    if request.method == "POST":
        action = request.form.get("action")
        # Gets the "action" value from the submitted form.

        if action == "update_settings":
            try:
                # Starts error handling for invalid numerical input.
                new_rate = float(request.form.get("hourly_rate"))
                # Gets the new hourly rate from the form and converts it to a decimal number.
                old_rate = location["hourly_rate"] or 0
                # Gets the previous hourly rate.
                # If no previous rate exists, 0 is used.

                update_location(request.current_user.location_id, {
                    "hourly_rate": new_rate,
                }) # Updates the hourly parking rate for the administrator's location.

                log_action(
                    user_id=request.current_user.id, role="admin", action="price_change",
                    details={"old_rate": old_rate, "new_rate": new_rate},
                    location_id=request.current_user.location_id,
                )
                # Records the price change in the audit log, including
                # who made the change, the old rate, the new rate, and the location.

                if new_rate == 0:
                    flash(
                        request,
                        "Rate saved as KES 0/hr. Vehicles will be checked out for free. "
                        "Update this if that was not intentional.",
                        "warning",
                    ) # Warns the administrator that a zero rate means parking will be free.
                else:
                    flash(request, f"Rate updated to KES {new_rate:.0f}/hr.", "success")
            except (TypeError, ValueError):
                # Handles invalid or missing rate values that cannot be converted to a number.

                flash(request, "Invalid rate. Please enter a number.", "danger")

        elif action == "update_mpesa":
            shortcode = request.form.get("mpesa_shortcode", "").strip()
            passkey = request.form.get("mpesa_passkey", "").strip()
            consumer_key = request.form.get("mpesa_consumer_key", "").strip()
            consumer_secret = request.form.get("mpesa_consumer_secret", "").strip()
            account_type = request.form.get("mpesa_account_type", "paybill").strip().lower()
            if account_type not in ("paybill", "till"):
            # Checks whether the submitted account type is valid.
                account_type = "paybill"
            # Uses PayBill as the default if an invalid account type was submitted.

            if not shortcode:
                # Checks whether a PayBill or Till number was provided.
                flash(request, "PayBill or Till number is required.", "danger")
            else:
                update_location(request.current_user.location_id, {
                    "mpesa_shortcode": shortcode,
                    "mpesa_passkey": passkey or None,
                    "mpesa_consumer_key": consumer_key or None,
                    "mpesa_consumer_secret": consumer_secret or None,
                    "mpesa_account_type": account_type,
                })# Saves the M-Pesa configuration for the administrator's location.
                # Empty optional credentials are stored as None.
                flash(request, "M-Pesa credentials saved.", "success")

        elif action == "clear_mpesa":
            update_location(request.current_user.location_id, {
                "mpesa_shortcode": None,
                "mpesa_passkey": None,
                "mpesa_consumer_key": None,
                "mpesa_consumer_secret": None,
                "mpesa_account_type": "paybill",
            })# Clears all stored M-Pesa credentials and resets the account type to PayBill.
            flash(request, "M-Pesa credentials cleared. Payments will fall back to simulation if MPESA_SIMULATION=true in .env.", "info")

        return Response.redirect(_router.url_for("admin.pricing"))
        # Redirects back to the Pricing page after processing the submitted action.

    return render(request, "admin/pricing.html", location=location)
    # Displays the Pricing page and provides the current location settings to the template.

@admin_bp.route("/reports")
@admin_required
def reports(request: Request) -> Response:
    from reports.revenue_report import build_admin_report
    # Imports the function responsible for generating the administrator's report data.
    location = get_admin_location(request)
    context = build_admin_report(request.current_user.location_id)
    # Generates report data for the administrator's assigned parking location.
    # The location ID ensures that the report is based on the correct parking facility.
    return render(request, "admin/reports.html", location=location, **context)
    #Render the administrator reports page and give it the administrator's location
    #plus all the report data produced by build_admin_report()

@admin_bp.route("/profile", methods=["GET", "POST"])
@admin_required
def profile(request: Request) -> Response:
    """Admin views and updates their own profile details."""
    admin = get_user_by_id(request.current_user.id)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_profile":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            national_id = request.form.get("national_id", "").strip()
            business_permit = request.form.get("business_permit", "").strip()

            if not name or not email:
                flash(request, "Name and email are required.", "danger")
            elif update_admin_profile(request.current_user.id, name, email,
                                       phone, national_id, business_permit):
                updated = get_user_by_id(request.current_user.id)
                # Retrieves the updated administrator record.
                login_user(request, User(updated))
                # Refreshes the current login session with the updated user information.
                flash(request, "Profile updated.", "success")
            else:
                flash(request, "That email is already used by another account.", "danger")

        elif action == "change_password":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")

            if not check_password(request.current_user.email, current_pw):
                flash(request, "Current password is incorrect.", "danger")
            elif len(new_pw) < 6:
                flash(request, "New password must be at least 6 characters.", "danger")
            elif new_pw != confirm_pw:
                flash(request, "Passwords do not match.", "danger")
            else:
                update_password(request.current_user.id, new_pw)
                flash(request, "Password changed successfully.", "success")

        return Response.redirect(_router.url_for("admin.profile"))

    return render(request, "admin/profile.html", admin=admin)
    # Displays the administrator's profile page and provides the administrator's
    # information to the profile template.


@admin_bp.route("/camera")
@admin_required
def camera(request: Request) -> Response:
    # Displays CCTV cameras and parking slot information for the administrator's location.
    location = get_admin_location(request)
    # Retrieves the parking location assigned to the currently logged-in administrator.
    slots = get_slots_by_location(request.current_user.location_id) if location else []
    # Retrieves all parking slots belonging to the administrator's location.
    # If no location is found, an empty list is used.
    slot_summary = count_slots_by_status(request.current_user.location_id) if location else {}
    # Retrieves a summary of parking slots grouped by their status.
    # If no location is found, an empty dictionary is used.
    cameras = get_cameras_by_location(request.current_user.location_id) if location else []
    # Retrieves all CCTV cameras belonging to the administrator's assigned location.
    return render(
        request, "admin/camera.html",
        location=location, slots=slots, slot_summary=slot_summary, cameras=cameras,
    )
    # Renders the camera management page and passes the location, parking slots,
    # slot summary, and CCTV cameras to the template.


@admin_bp.route("/camera/add", methods=["POST"])
@admin_required
def camera_add(request: Request) -> Response:

    """Add another CCTV feed for this admin's location."""
    label = request.form.get("label", "").strip() or "Camera"
    # Gets the camera label from the submitted form.
    # If no label is provided, "Camera" is used as the default label.
    camera_url = request.form.get("camera_url", "").strip()
    # Gets the CCTV camera feed URL from the submitted form

    if not camera_url:
        flash(request, "Camera URL is required.", "danger")
        return Response.redirect(_router.url_for("admin.camera"))
        # Returns the administrator to the camera management page.

    create_camera(request.current_user.location_id, label, camera_url)
    # Creates the camera and associates it with the administrator's parking location.
    log_action(
        user_id=request.current_user.id, role="admin", action="camera_add",
        details={"label": label}, location_id=request.current_user.location_id,
    )
    flash(request, f'"{label}" added.', "success")
    return Response.redirect(_router.url_for("admin.camera"))
    # Redirects the administrator back to the camera management page.

@admin_bp.route("/camera/<camera_id>/delete", methods=["POST"])
@admin_required
def camera_delete(request: Request) -> Response:
    """Remove a CCTV feed. Only the owning location's admin can delete it."""
    camera_id = request.param("camera_id")
    cam = get_camera_by_id(camera_id)

    if not cam or str(cam["location_id"]) != str(request.current_user.location_id):
        # Checks that the camera exists and belongs to the current administrator's location.
        # This prevents an administrator from deleting another location's camera.
        flash(request, "Camera not found.", "danger")
        return Response.redirect(_router.url_for("admin.camera"))

    delete_camera(camera_id)
    log_action(
        user_id=request.current_user.id, role="admin", action="camera_delete",
        details={"label": cam["label"]}, location_id=request.current_user.location_id,
    )
    flash(request, f'"{cam["label"]}" removed.', "info")
    return Response.redirect(_router.url_for("admin.camera"))
    # Redirects back to the camera management page after deletion.