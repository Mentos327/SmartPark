"""
Guard-facing routes (URL prefix: /guard).

Covers the guard dashboard, slot map, camera scan, vehicle check-in and
check-out, receipt printing, plate search, profile, and event reservations.
All routes require @guard_required.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from auth.decorators import guard_required
from auth.session_manager import login_user, flash
from core.http import Request, Response
from core.router import Blueprint
from database.db import Database
from models.location import get_location_by_id
from models.slot import (get_slots_by_location, get_slot_by_id, get_available_slots,
                          count_slots_by_status,
                          STATUS_AVAILABLE, STATUS_OCCUPIED, STATUS_RESERVED)
from models.session import (get_active_sessions_by_location, get_active_session_by_plate,
                             get_active_session_by_slot, get_all_sessions_by_plate,
                             get_session_by_id)
from models.reservation import (get_active_reservations_by_location, get_event_reservations_by_location,
                                 get_pending_reservation_by_id, get_reservation_by_id,
                                 get_upcoming_reservation_for_slot, manage_reservations,
                                 create_event_reservation, cancel_event_reservation,
                                 resolve_reservation_conflict, approve_reservation,
                                 get_pending_approval_reservation_by_id, STATUS_CONFLICT)
from models.user import get_driver_by_plate, check_password, update_password, User, get_user_by_id
from models.audit_log import log_action
from services.parking import check_in_vehicle, check_out_vehicle
from services.email import send_receipt_email
from utils.fee_calculator import calculate_current_fee, calculate_fee, strip_timezone

guard_bp = Blueprint("guard")

_render = None
_router = None


def configure(render_func, router):
    global _render, _router
    _render = render_func
    _router = router


def render(request: Request, template_name: str, **context) -> Response:
    html = _render(template_name, request, context)
    return Response.html(html)


def get_guard_location(request: Request):
    return get_location_by_id(request.current_user.location_id)


def sync_reservations(location_id) -> None:
    """
    Run manage_reservations at most once per 60 seconds per location.
    Stored in the database (system_state table) so the throttle works
    across multiple server processes/threads, not just in memory.
    Called from both the dashboard and check-in so reservations stay
    current even when the dashboard tab is not open.
    """
    loc_key = "res_sync_" + str(location_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    tracker = Database.fetch_one("SELECT last_run FROM system_state WHERE key = ?", (loc_key,))
    last_run = datetime.fromisoformat(tracker["last_run"]) if tracker and tracker["last_run"] else None

    if last_run is None or (now - last_run).total_seconds() >= 60:
        manage_reservations(location_id)
        if tracker:
            Database.execute(
                "UPDATE system_state SET last_run = ? WHERE key = ?", (now.isoformat(), loc_key)
            )
        else:
            Database.execute(
                "INSERT INTO system_state (key, last_run) VALUES (?, ?)", (loc_key, now.isoformat())
            )


# --------------------------------------------------------------------------
# Dashboard, slot map, camera, search, profile
# --------------------------------------------------------------------------

@guard_bp.route("/dashboard")
@guard_required
def dashboard(request: Request) -> Response:
    """
    Guard home — active vehicles, reservations, and conflicts.
    Supports multiple guards at the same location:
    - All sessions shown with the guard's name who checked them in
    - 'mine' query param filters to only this guard's check-ins
    """
    location = get_guard_location(request)
    location_id = request.current_user.location_id

    sync_reservations(location_id)

    all_active = get_active_sessions_by_location(location_id)
    slot_summary = count_slots_by_status(location_id)
    all_reservations = get_active_reservations_by_location(location_id)
    event_reservations = get_event_reservations_by_location(location_id)

    # 'mine' filter — show only sessions this guard checked in
    show_mine = request.args.get("mine") == "1"
    if show_mine:
        active_sessions = [s for s in all_active if s["guard_id"] == request.current_user.id]
    else:
        active_sessions = all_active

    guards_at_location = Database.fetch_all(
        "SELECT * FROM users WHERE location_id = ? AND role = 'guard'", (str(location_id),)
    )
    guard_names = {str(g["id"]): (g["name"] or "Unknown") for g in guards_at_location}
    multiple_guards = len(guards_at_location) > 1

    conflicts = []
    reservations = []
    for r in all_reservations:
        if r["status"] == STATUS_CONFLICT:
            conflicts.append(r)
        else:
            reservations.append(r)

    for conflict in conflicts:
        occupying_session = get_active_session_by_slot(conflict["slot_id"])
        conflict["occupying_plate"] = occupying_session["plate_number"] if occupying_session else "Unknown"
        conflict["occupying_session_id"] = str(occupying_session["id"]) if occupying_session else None

    all_slots = get_slots_by_location(location_id)
    slot_numbers = {str(s["id"]): s["slot_number"] for s in all_slots}

    # Attach slot numbers to active sessions (transient display field — not a real column)
    for s in active_sessions:
        s["slot_number"] = slot_numbers.get(str(s["slot_id"] or ""), "")

    # Today's completed sessions for the guard's location
    eat_today = (datetime.now(timezone.utc) + timedelta(hours=3)).date()
    eat_today_start = datetime.combine(eat_today, datetime.min.time()) - timedelta(hours=3)
    eat_today_end = datetime.combine(eat_today, datetime.max.time()) - timedelta(hours=3)
    today_sessions = Database.fetch_all(
        """
        SELECT * FROM sessions
        WHERE location_id = ? AND is_paid = 1 AND exit_time >= ? AND exit_time <= ?
        ORDER BY exit_time DESC LIMIT 20
        """,
        (str(location_id), eat_today_start.isoformat(), eat_today_end.isoformat()),
    )

    # Combine reservations and conflicts for dashboard display
    active_reservations = []
    for r in reservations + conflicts + event_reservations:
        r["slot_number"] = slot_numbers.get(str(r["slot_id"] or ""), "")
        if r["driver_id"]:
            driver_doc = get_user_by_id(r["driver_id"])
            r["driver_name"] = driver_doc["name"] if driver_doc else "Driver"
        else:
            r["driver_name"] = r["event_label"] or "Event"
        active_reservations.append(r)

    return render(
        request, "guard/dashboard.html",
        location=location, active_sessions=active_sessions, all_active_count=len(all_active),
        slot_summary=slot_summary, slots=all_slots, reservations=reservations, conflicts=conflicts,
        active_reservations=active_reservations, event_reservations=event_reservations,
        guard_names=guard_names, multiple_guards=multiple_guards, show_mine=show_mine,
        slot_numbers=slot_numbers, today_sessions=today_sessions,
    )


@guard_bp.route("/slot-map")
@guard_required
def slot_map(request: Request) -> Response:
    location_id = request.current_user.location_id
    return render(
        request, "guard/slot_map.html",
        location=get_guard_location(request),
        slots=get_slots_by_location(location_id),
        slot_summary=count_slots_by_status(location_id),
    )


@guard_bp.route("/camera")
@guard_required
def camera(request: Request) -> Response:
    """Camera scan page — plate scan on the left, live slot map on the right."""
    location = get_guard_location(request)
    location_id = request.current_user.location_id
    return render(
        request, "guard/camera_scan.html",
        location=location,
        slots=get_slots_by_location(location_id) if location else [],
        slot_summary=count_slots_by_status(location_id) if location else {},
    )


@guard_bp.route("/search", methods=["GET", "POST"])
@guard_required
def search(request: Request) -> Response:
    """Search by plate — shows active session and full parking history."""
    location = get_guard_location(request)
    active_session = None
    history = []

    if request.method == "POST":
        plate = request.form.get("plate", "").strip().upper()
        if plate:
            active_session = get_active_session_by_plate(plate)
            history = get_all_sessions_by_plate(plate)
            if not active_session and not history:
                flash(request, f"No sessions found for {plate}.", "warning")

    # The duration_macro.html partial does direct datetime arithmetic
    # (exit_time - entry_time), so these fields need to be real datetime
    # objects, not the ISO strings our sqlite3 layer stores them as.
    for s in history:
        if s.get("entry_time") and isinstance(s["entry_time"], str):
            s["entry_time"] = datetime.fromisoformat(s["entry_time"])
        if s.get("exit_time") and isinstance(s["exit_time"], str):
            s["exit_time"] = datetime.fromisoformat(s["exit_time"])

    return render(
        request, "guard/search.html",
        location=location, active_session=active_session, history=history,
    )


@guard_bp.route("/profile", methods=["GET", "POST"])
@guard_required
def profile(request: Request) -> Response:
    """Guard views and updates their own profile details."""
    guard = get_user_by_id(request.current_user.id)

    if request.method == "POST":
        action = request.form.get("action")

        if action == "update_profile":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()

            if not name or not email:
                flash(request, "Name and email are required.", "danger")
            else:
                existing = Database.fetch_one("SELECT id FROM users WHERE email = ?", (email,))
                if existing and str(existing["id"]) != str(request.current_user.id):
                    flash(request, "That email is already used by another account.", "danger")
                else:
                    Database.execute(
                        "UPDATE users SET name = ?, email = ?, phone = ? WHERE id = ?",
                        (name, email, phone or None, int(request.current_user.id)),
                    )
                    updated = get_user_by_id(request.current_user.id)
                    login_user(request, User(updated))
                    flash(request, "Profile updated.", "success")

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

        return Response.redirect(_router.url_for("guard.profile"))

    return render(request, "guard/profile.html", guard=guard)


# --------------------------------------------------------------------------
# Reservation lookup + manual check-in form
# --------------------------------------------------------------------------

@guard_bp.route("/checkin/reservation/<reservation_id>")
@guard_required
def checkin_reservation(request: Request) -> Response:
    reservation_id = request.param("reservation_id")
    reservation = get_pending_reservation_by_id(reservation_id)

    if not reservation:
        # Give a specific message depending on what happened to the reservation
        any_res = get_reservation_by_id(reservation_id)
        if any_res and any_res["status"] == "expired":
            flash(
                request,
                "This reservation has expired — the driver did not arrive in time. "
                "Check them in manually as a walk-in if they are here now.",
                "warning",
            )
        elif any_res and any_res["status"] in ("cancelled", "completed"):
            flash(request, f"This reservation is already {any_res['status']}. "
                            "Check them in manually if needed.", "warning")
        else:
            flash(request, "Reservation not found or already used.", "danger")
        return Response.redirect(_router.url_for("guard.checkin"))

    # Make sure this reservation belongs to this guard's location
    if str(reservation["location_id"]) != str(request.current_user.location_id):
        flash(
            request,
            "This reservation is for a different parking location. "
            "Ask the driver to check they are at the right place.",
            "danger",
        )
        return Response.redirect(_router.url_for("guard.checkin"))

    slot = get_slot_by_id(reservation["slot_id"])
    if slot and slot["status"] == STATUS_OCCUPIED:
        flash(request, "That slot is currently occupied. Resolve the conflict first.", "danger")
        return Response.redirect(_router.url_for("guard.dashboard"))

    # Warn the guard if the driver is arriving more than 30 minutes early
    reserved_from = reservation["reserved_from"]
    if reserved_from:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        res_start = strip_timezone(datetime.fromisoformat(reserved_from))
        minutes_early = int((res_start - now_naive).total_seconds() / 60)
        if minutes_early > 30:
            flash(
                request,
                f"Note: reservation starts in {minutes_early} minutes. "
                "Driver is arriving early — billing will start at the reserved time.",
                "info",
            )

    return Response.redirect(
        _router.url_for("guard.checkin", reservation_id=reservation_id, slot_id=str(reservation["slot_id"]))
    )


@guard_bp.route("/checkin", methods=["GET", "POST"])
@guard_required
def checkin(request: Request) -> Response:
    """
    Check a vehicle in by plate number and optional slot.
    If reservation_id is present, the session is linked
    to that reservation so checkout charges actual time, not the full window.
    """
    location = get_guard_location(request)
    location_id = request.current_user.location_id
    all_slots = get_slots_by_location(location_id)

    # Pre-fill if the guard arrived here via checkin_reservation
    prefill_reservation_id = request.args.get("reservation_id") or None
    prefill_slot_id = request.args.get("slot_id") or None
    prefill_reservation = None

    if prefill_reservation_id:
        prefill_reservation = get_pending_reservation_by_id(prefill_reservation_id)
        if not prefill_reservation:
            flash(request, "Reservation not found or already used.", "warning")
            prefill_reservation_id = None
            prefill_slot_id = None
        else:
            # Attach slot number and driver name so the template can display them
            res_slot = get_slot_by_id(prefill_reservation["slot_id"])
            prefill_reservation["slot_number"] = res_slot["slot_number"] if res_slot else prefill_slot_id
            driver_doc = get_user_by_id(prefill_reservation["driver_id"]) if prefill_reservation["driver_id"] else None
            prefill_reservation["driver_name"] = driver_doc["name"] if driver_doc else "Driver"

    # Build available slot list for the dropdown
    available_slots = [s for s in all_slots if s["status"] == STATUS_AVAILABLE]

    if prefill_slot_id:
        reserved_slot = None
        for s in all_slots:
            if str(s["id"]) == prefill_slot_id and s["status"] == STATUS_RESERVED:
                reserved_slot = s
                break
        if reserved_slot and reserved_slot not in available_slots:
            reserved_slot["is_drivers_slot"] = True
            available_slots.insert(0, reserved_slot)

    # Attach upcoming reservation time to each slot for the warning label
    for slot in available_slots:
        upcoming = get_upcoming_reservation_for_slot(str(slot["id"]))
        slot["upcoming_reservation_at"] = (
            datetime.fromisoformat(upcoming["reserved_from"]) if upcoming and upcoming["reserved_from"] else None
        )

    if request.method == "POST":
        plate_number = request.form.get("plate_number", "").strip().upper()
        slot_id = request.form.get("slot_id") or None
        reservation_id = request.form.get("reservation_id") or None

        if not plate_number:
            flash(request, "Please enter a plate number.", "danger")
        else:
            # Warn guard if assigning a walk-in to a slot that is reserved soon
            if slot_id and not reservation_id:
                upcoming = get_upcoming_reservation_for_slot(slot_id)
                if upcoming:
                    res_time = datetime.fromisoformat(upcoming["reserved_from"]).strftime("%d %b, %H:%M")
                    flash(request, f"Note: Slot is reserved from {res_time}. "
                                    "Vehicle must leave before then.", "warning")

            # Try to link the session to a registered driver account
            driver_doc = get_driver_by_plate(plate_number)
            linked_driver_id = str(driver_doc["id"]) if driver_doc else None

            result = check_in_vehicle(
                plate_number=plate_number, location_id=location_id, slot_id=slot_id,
                guard_id=request.current_user.id, driver_id=linked_driver_id,
                reservation_id=reservation_id,
            )

            if result["success"]:
                sync_reservations(location_id)
                flash(request, f"{plate_number} checked in.", "success")
                return Response.redirect(_router.url_for("guard.dashboard"))
            else:
                flash(request, result["error"], "danger")

    return render(
        request, "guard/checkin.html",
        location=location, available_slots=available_slots, all_slots=all_slots,
        scanned_plate=request.args.get("plate", ""),
        prefill_reservation=prefill_reservation, prefill_reservation_id=prefill_reservation_id,
        prefill_slot_id=prefill_slot_id,
    )


# --------------------------------------------------------------------------
# Live fee estimate, payment, printable receipt
# --------------------------------------------------------------------------

@guard_bp.route("/checkout/<session_id>", methods=["GET", "POST"])
@guard_required
def checkout(request: Request) -> Response:
    """
    Show live fee estimate (GET) and process payment (POST).
    On POST the session is marked completed; the guard is then redirected
    to the receipt page.
    """
    session_id = request.param("session_id")
    session = get_session_by_id(session_id)
    if not session:
        flash(request, "Session not found.", "danger")
        return Response.redirect(_router.url_for("guard.dashboard"))

    location = get_location_by_id(session["location_id"])
    # Fall back to KES 30/hr if the location record is missing.
    hourly_rate = location["hourly_rate"] if location else 30

    # Live fee estimate based on entry time -> now.
    fee_info = calculate_current_fee(datetime.fromisoformat(session["entry_time"]), hourly_rate)

    if request.method == "POST":
        payment_method = request.form.get("payment_method", "cash")
        force_confirm = request.form.get("force_confirm") == "1"
        override_amount_raw = request.form.get("override_amount", "").strip()
        override_amount = None
        if override_amount_raw:
            try:
                override_amount = float(override_amount_raw)
            except ValueError:
                flash(request, "Please enter a valid amount.", "danger")
                return render(request, "guard/checkout.html", session=session, fee_info=fee_info, location=location)

        result = check_out_vehicle(
            session_id=session_id, payment_method=payment_method, guard_id=request.current_user.id,
            override_fee=override_amount, force_confirm=force_confirm,
        )

        if result["success"]:
            flash(request, f"Payment confirmed. Fee: KES {result['fee']}", "success")
            return Response.redirect(_router.url_for("guard.receipt", session_id=session_id))
        elif result.get("pending"):
            # not a failure, just waiting on the Safaricom callback —
            # the polling JS on checkout.html will redirect once it lands
            flash(request, result["error"], "info")
        else:
            flash(request, result["error"], "danger")

    return render(request, "guard/checkout.html", session=session, fee_info=fee_info, location=location)


@guard_bp.route("/receipt/<session_id>")
@guard_required
def receipt(request: Request) -> Response:
    """
    Printable receipt shown after a successful checkout.

    Recalculates the fee breakdown from the stored entry/exit times so the
    receipt always shows accurate tier/rate/duration data — even if the
    session was completed a while ago.

    Also emails the receipt to the driver once (guarded by receipt_email_sent
    flag to prevent duplicate emails on page refreshes).
    """
    session_id = request.param("session_id")
    session = get_session_by_id(session_id)
    if not session:
        flash(request, "Session not found.", "danger")
        return Response.redirect(_router.url_for("guard.dashboard"))

    # Only completed sessions have a receipt; redirect mid-session checkouts.
    if session["status"] != "completed":
        flash(request, "Session not yet completed.", "warning")
        return Response.redirect(_router.url_for("guard.checkout", session_id=session_id))

    location = get_location_by_id(session["location_id"])
    hourly_rate = location["hourly_rate"] if location else 30

    # Re-derive the fee breakdown from the actual entry/exit times.
    # We cannot rely solely on session["fee_kes"] because the template also
    # needs duration_display — which is not stored on the session row.
    entry_time = datetime.fromisoformat(session["entry_time"]) if session["entry_time"] else None
    exit_time = datetime.fromisoformat(session["exit_time"]) if session["exit_time"] else datetime.now(timezone.utc)

    fee_info = calculate_fee(entry_time, exit_time, hourly_rate)

    # Prefer the stored fee (what was actually charged) over the recalculated
    # one, in case the guard processed payment at a different time.
    fee_kes = session["fee_kes"] or fee_info["fee_kes"]

    if session["driver_id"] and not session["receipt_email_sent"]:
        driver = get_user_by_id(session["driver_id"])
        if driver and driver["email"]:
            try:
                send_receipt_email(
                    driver_email=driver["email"], plate=session["plate_number"],
                    location_name=location["name"] if location else "",
                    entry_time=entry_time, exit_time=exit_time,
                    duration=fee_info["duration_display"], fee_kes=fee_kes,
                    payment_method=session["payment_method"] or "cash",
                )
                # Set the flag so a page refresh does not re-send the email.
                Database.execute(
                    "UPDATE sessions SET receipt_email_sent = 1 WHERE id = ?", (int(session_id),)
                )
            except Exception as exc:
                # Non-fatal: log but don't blow up the receipt page.
                print(f"[receipt] Could not send receipt email: {exc}")

    return render(
        request, "guard/receipt.html",
        session=session, location=location, fee_kes=fee_kes,
        hourly_rate_kes=hourly_rate, duration_display=fee_info["duration_display"],
    )


# --------------------------------------------------------------------------
# Event/VIP reservations and conflict resolution
# --------------------------------------------------------------------------

@guard_bp.route("/event-reserve", methods=["GET", "POST"])
@guard_required
def event_reserve(request: Request) -> Response:
    """Reserve one or more slots for an event — meeting, VIP visit, etc."""
    location = get_guard_location(request)
    location_id = request.current_user.location_id
    available_slots = get_available_slots(location_id)

    if request.method == "POST":
        event_label = request.form.get("event_label", "").strip()
        from_str = request.form.get("reserved_from", "")
        until_str = request.form.get("reserved_until", "")
        slot_ids = request.form.getlist("slot_ids")

        if not event_label:
            flash(request, "Please enter an event name.", "danger")
        elif not slot_ids:
            flash(request, "Please select at least one slot.", "danger")
        elif not from_str or not until_str:
            flash(request, "Start and end time are required.", "danger")
        else:
            try:
                reserved_from = datetime.strptime(from_str, "%Y-%m-%dT%H:%M")
                reserved_until = datetime.strptime(until_str, "%Y-%m-%dT%H:%M")

                now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                if reserved_from <= now_naive:
                    flash(request, "Start time must be in the future.", "danger")
                elif reserved_from >= reserved_until:
                    flash(request, "End time must be after start time.", "danger")
                else:
                    count = create_event_reservation(
                        guard_id=request.current_user.id, location_id=location_id,
                        slot_ids=slot_ids, event_label=event_label,
                        reserved_from=reserved_from, reserved_until=reserved_until,
                    )
                    if count == 0:
                        flash(request, "All selected slots are already booked for that time.", "danger")
                    elif count < len(slot_ids):
                        flash(request, f"{count} slot(s) reserved. {len(slot_ids) - count} already booked.", "warning")
                    else:
                        flash(request, f"{count} slot(s) reserved for '{event_label}'.", "success")
                        return Response.redirect(_router.url_for("guard.dashboard"))

            except ValueError:
                flash(request, "Invalid date format.", "danger")

    return render(
        request, "guard/event_reserve.html",
        location=location, available_slots=available_slots,
    )


@guard_bp.route("/event-reserve/cancel/<reservation_id>", methods=["POST"])
@guard_required
def cancel_event(request: Request) -> Response:
    """Cancel a single event slot reservation and free the slot."""
    reservation_id = request.param("reservation_id")
    if cancel_event_reservation(reservation_id):
        flash(request, "Slot reservation cancelled.", "success")
    else:
        flash(request, "Could not cancel — reservation not found.", "danger")
    return Response.redirect(_router.url_for("guard.dashboard"))


@guard_bp.route("/conflict/resolve/<reservation_id>", methods=["POST"])
@guard_required
def resolve_conflict(request: Request) -> Response:
    """
    Guard marks a conflict as resolved after the blocking vehicle has moved.
    Resets the reservation to pending so the reserved driver can still use the slot.
    """
    reservation_id = request.param("reservation_id")
    if resolve_reservation_conflict(reservation_id):
        flash(request, "Conflict resolved. Slot is ready for the reserved driver.", "success")
    else:
        flash(request, "Could not resolve — reservation not found.", "danger")
    return Response.redirect(_router.url_for("guard.dashboard"))


@guard_bp.route("/reservation/approve/<reservation_id>", methods=["POST"])
@guard_required
def approve_pending_reservation(request: Request) -> Response:
    """
    Approve a reservation that was auto-flagged for manual review after the
    driver racked up 3+ no-shows in 30 days (see models/reservation.py).
    Moves it back to 'pending' so it locks the slot and gets checked in
    normally. Only reservations at this guard's own location can be
    approved, and only while still in the pending_approval state — so a
    double click, or a stale tab from another guard, is a safe no-op.
    """
    reservation_id = request.param("reservation_id")
    reservation = get_pending_approval_reservation_by_id(reservation_id)

    if not reservation:
        flash(request, "That reservation is no longer waiting for approval.", "warning")
        return Response.redirect(_router.url_for("guard.dashboard"))

    if str(reservation["location_id"]) != str(request.current_user.location_id):
        flash(request, "That reservation is for a different parking location.", "danger")
        return Response.redirect(_router.url_for("guard.dashboard"))

    if approve_reservation(reservation_id):
        log_action(
            user_id=request.current_user.id, role="guard", action="approve_reservation",
            details={"reservation_id": str(reservation_id), "driver_id": reservation["driver_id"]},
            location_id=request.current_user.location_id,
        )
        flash(request, "Reservation approved. It will lock the slot as normal before the start time.", "success")
    else:
        flash(request, "Could not approve — reservation not found.", "danger")

    return Response.redirect(_router.url_for("guard.dashboard"))
