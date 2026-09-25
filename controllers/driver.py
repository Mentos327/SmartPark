"""
Driver-facing routes (URL prefix: /driver).

Covers the driver dashboard, location detail, slot map, reservations,
parking history, and profile. All routes require @driver_required.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from auth.decorators import driver_required
from auth.session_manager import login_user, flash
from core.http import Request, Response
from core.router import Blueprint
from core.templating import to_eat
from database.db import Database
from models.location import get_all_locations, get_location_by_id
from models.slot import get_slots_by_location, get_slot_by_id, count_slots_by_status
from models.reservation import (
    create_reservation, get_reservations_by_driver, get_reservation_by_id, LOCK_MINUTES,
    cancel_driver_reservation, check_slot_overlap, STATUS_PENDING_APPROVAL,
)
from models.user import update_user_profile, update_password, check_password, User, get_user_by_id
from services.email import send_reservation_confirmation, send_reservation_needs_approval

driver_bp = Blueprint("driver")

MIN_RESERVATION_MINUTES = 30  # shortest booking allowed, matches the minimum charge

_render = None
_router = None


def configure(render_func, router):
    global _render, _router
    _render = render_func
    _router = router


def render(request: Request, template_name: str, **context) -> Response:
    html = _render(template_name, request, context)
    return Response.html(html)


@driver_bp.route("/dashboard")
@driver_required
def dashboard(request: Request) -> Response:
    locations = get_all_locations()

    sessions = Database.fetch_all(
        "SELECT * FROM sessions WHERE driver_id = ? ORDER BY entry_time DESC LIMIT 10",
        (request.current_user.id,),
    )

    active_session = None
    for s in sessions:
        if s["status"] == "active":
            active_session = s
            break

    return render(
        request, "driver/dashboard.html",
        locations=locations, sessions=sessions, active_session=active_session,
    )


@driver_bp.route("/location/<location_id>")
@driver_required
def location_detail(request: Request) -> Response:
    location_id = request.param("location_id")
    location = get_location_by_id(location_id)
    if not location:
        flash(request, "Location not found.", "danger")
        return Response.redirect(_router.url_for("driver.dashboard"))

    return render(
        request, "driver/location_detail.html",
        location=location,
        slots=get_slots_by_location(location_id),
        slot_summary=count_slots_by_status(location_id),
    )


@driver_bp.route("/location/<location_id>/camera")
@driver_required
def location_camera(request: Request) -> Response:
    location_id = request.param("location_id")
    location = get_location_by_id(location_id)
    if not location:
        flash(request, "Location not found.", "danger")
        return Response.redirect(_router.url_for("driver.dashboard"))

    return render(
        request, "driver/location_camera.html",
        location=location,
        slots=get_slots_by_location(location_id),
        slot_summary=count_slots_by_status(location_id),
    )


def _driver_has_overlap(driver_id: str, new_from: datetime, new_until: datetime) -> bool:
    # Prevents a driver from double-booking themselves across different locations
    match = Database.fetch_one(
        """
        SELECT id FROM reservations
        WHERE driver_id = ? AND status IN ('pending', 'active')
          AND reserved_from < ? AND reserved_until > ?
        LIMIT 1
        """,
        (str(driver_id), new_until.isoformat(), new_from.isoformat()),
    )
    return match is not None


@driver_bp.route("/reserve", methods=["GET", "POST"])
@driver_required
def reserve(request: Request) -> Response:
    location_id = request.args.get("location_id") or request.form.get("location_id")
    selected_location = get_location_by_id(location_id) if location_id else None
    preselect_slot_id = request.args.get("slot_id") or None

    # Show all slots, not just currently-available ones.
    # A slot marked "reserved" right now may be free during the driver's chosen time window —
    # the overlap check handles the real validation. Filtering on current DB status hides
    # valid options from the driver.
    bookable_slots = [
        s for s in (get_slots_by_location(location_id) if location_id else [])
        if s["status"] in ("available", "reserved")
    ]

    if request.method == "POST":
        slot_id = request.form.get("slot_id")
        from_str = request.form.get("reserved_from")
        until_str = request.form.get("reserved_until")

        if not slot_id or not from_str or not until_str:
            flash(request, "All fields are required.", "danger")
        else:
            if selected_location and selected_location["reservations_paused"]:
                flash(
                    request,
                    "New reservations are temporarily paused for this location due to high demand. "
                    "You are welcome to walk in — spaces may be available.",
                    "warning",
                )
            else:
                try:
                    reserved_from = datetime.strptime(from_str, "%Y-%m-%dT%H:%M")
                    reserved_until = datetime.strptime(until_str, "%Y-%m-%dT%H:%M")

                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    duration_minutes = (reserved_until - reserved_from).total_seconds() / 60

                    if reserved_from <= now:
                        flash(request, "Start time must be in the future.", "danger")
                    elif reserved_from >= reserved_until:
                        flash(request, "End time must be after start time.", "danger")
                    elif duration_minutes < MIN_RESERVATION_MINUTES:
                        flash(
                            request,
                            f"Minimum reservation length is {MIN_RESERVATION_MINUTES} minutes.",
                            "danger",
                        )
                    elif duration_minutes > 24 * 60:
                        flash(request, "A reservation cannot be longer than 24 hours.", "danger")
                    elif _driver_has_overlap(request.current_user.id, reserved_from, reserved_until):
                        flash(
                            request,
                            "You already have a reservation that overlaps this time. "
                            "Check My Reservations before booking again.",
                            "danger",
                        )
                    elif check_slot_overlap(slot_id, reserved_from, reserved_until):
                        flash(
                            request,
                            "That slot is already booked during this time. "
                            "Choose a different slot or time.",
                            "danger",
                        )
                    else:
                        new_id = create_reservation(
                            driver_id=request.current_user.id,
                            location_id=location_id,
                            slot_id=slot_id,
                            reserved_from=reserved_from,
                            reserved_until=reserved_until,
                        )

                        slot_doc = get_slot_by_id(slot_id)
                        new_reservation = get_reservation_by_id(new_id)
                        email_data = {
                            "location_name": selected_location["name"],
                            "slot_number": slot_doc["slot_number"] if slot_doc else "N/A",
                            "reserved_from": reserved_from.strftime("%d %b %Y, %H:%M"),
                            "reserved_until": reserved_until.strftime("%d %b %Y, %H:%M"),
                        }

                        # create_reservation silently flags the booking as
                        # pending_approval instead of pending when the driver
                        # has 3+ no-shows in the last 30 days — the slot is
                        # NOT locked until a guard/admin approves it, so we
                        # must not tell the driver it's confirmed.
                        if new_reservation and new_reservation["status"] == STATUS_PENDING_APPROVAL:
                            send_reservation_needs_approval(
                                driver_email=request.current_user.email, reservation_data=email_data,
                            )
                            flash(
                                request,
                                "Reservation received, but not yet confirmed. Due to recent no-shows, "
                                "it needs approval from a guard or admin before the slot is held for you. "
                                "Check My Reservations for updates.",
                                "warning",
                            )
                        else:
                            send_reservation_confirmation(
                                driver_email=request.current_user.email, reservation_data=email_data,
                            )
                            flash(request, "Reservation confirmed! A confirmation email has been sent.", "success")

                        return Response.redirect(_router.url_for("driver.my_reservations"))

                except ValueError:
                    flash(request, "Invalid date format.", "danger")

    return render(
        request, "driver/reserve.html",
        locations=get_all_locations(),
        slots=bookable_slots,
        selected_location=selected_location,
        preselect_slot_id=preselect_slot_id,
    )


@driver_bp.route("/reservations")
@driver_required
def my_reservations(request: Request) -> Response:
    reservations = get_reservations_by_driver(request.current_user.id)

    for r in reservations:
        slot = get_slot_by_id(r["slot_id"]) if r["slot_id"] else None
        r["slot_number"] = slot["slot_number"] if slot else None

    # Convert all reservation datetimes to Nairobi time (EAT = UTC+3) naive
    # so template comparisons work correctly. The database stores naive UTC
    # ISO strings; to_eat() parses + shifts them +3 hrs so displayed times
    # match what the driver booked.
    for r in reservations:
        reserved_from = datetime.fromisoformat(r["reserved_from"]) if r["reserved_from"] else None
        reserved_until = datetime.fromisoformat(r["reserved_until"]) if r["reserved_until"] else None
        r["reserved_from"] = to_eat(reserved_from)
        r["reserved_until"] = to_eat(reserved_until)
        if r["reserved_from"]:
            r["cancel_cutoff"] = r["reserved_from"] - timedelta(minutes=LOCK_MINUTES)
        else:
            r["cancel_cutoff"] = None

    return render(request, "driver/reservations.html", reservations=reservations)


@driver_bp.route("/reservations/<reservation_id>/cancel", methods=["POST"])
@driver_required
def cancel_reservation(request: Request) -> Response:
    reservation_id = request.param("reservation_id")
    result = cancel_driver_reservation(reservation_id, request.current_user.id)
    if result is True:
        flash(request, "Reservation cancelled. The slot has been freed.", "success")
    elif result == "too_late":
        flash(
            request,
            f"Cannot cancel — reservation starts in less than {LOCK_MINUTES} minutes "
            "and the slot is already being held for you. Please speak to the guard on arrival.",
            "warning",
        )
    else:
        flash(request, "Cannot cancel — reservation not found, already started, or does not belong to you.", "danger")
    return Response.redirect(_router.url_for("driver.my_reservations"))


@driver_bp.route("/history")
@driver_required
def history(request: Request) -> Response:
    sessions = Database.fetch_all(
        "SELECT * FROM sessions WHERE driver_id = ? ORDER BY entry_time DESC LIMIT 100",
        (request.current_user.id,),
    )

    # The duration_macro.html partial does direct datetime arithmetic
    # (exit_time - entry_time), so these fields need to be real datetime
    # objects, not the ISO strings our sqlite3 layer stores them as.
    for s in sessions:
        if s.get("entry_time") and isinstance(s["entry_time"], str):
            s["entry_time"] = datetime.fromisoformat(s["entry_time"])
        if s.get("exit_time") and isinstance(s["exit_time"], str):
            s["exit_time"] = datetime.fromisoformat(s["exit_time"])

    return render(request, "driver/history.html", sessions=sessions)


@driver_bp.route("/profile", methods=["GET", "POST"])
@driver_required
def profile(request: Request) -> Response:
    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_profile":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()

            if not name or not email:
                flash(request, "Name and email are required.", "danger")
            elif update_user_profile(request.current_user.id, name, email):
                user_data = get_user_by_id(request.current_user.id)
                login_user(request, User(user_data))
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
                flash(request, "New passwords do not match.", "danger")
            else:
                update_password(request.current_user.id, new_pw)
                flash(request, "Password changed successfully.", "success")

        return Response.redirect(_router.url_for("driver.profile"))

    return render(request, "driver/profile.html", driver=request.current_user)
