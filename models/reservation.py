"""
Reservation model for smart.park.

Reservation status values:
  pending          — driver has booked a slot (the slot itself gets locked,
                     i.e. marked "reserved", 50 min before start time)
  active           — driver has checked in
  completed        — driver has checked out and paid
  expired          — driver did not show up (slot freed automatically)
  cancelled        — driver or admin cancelled before start
  conflict         — reservation window started but the slot is occupied
                     by someone else
  pending_approval — driver has 3+ no-shows; requires guard/admin approval

No-show protection: drivers who miss 3+ reservations in 30 days have
future reservations held for approval before the slot is locked.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from database.db import Database
from models.slot import (update_slot_status, get_slot_by_id,
                          STATUS_AVAILABLE, STATUS_RESERVED, STATUS_OCCUPIED)
from models.location import get_location_by_id
from services.email import send_reservation_reminder

STATUS_PENDING = "pending"
STATUS_PENDING_APPROVAL = "pending_approval"  # repeat no-show flag
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_CONFLICT = "conflict"

# How many minutes before start the slot is locked for the reserved driver.
LOCK_MINUTES = 50

# No-show abuse threshold: 3 no-shows in 30 days triggers manual review.
NOSHOW_THRESHOLD_COUNT = 3
NOSHOW_THRESHOLD_DAYS = 30


def _naive(dt):
    """Strip tzinfo if present, since columns store naive UTC datetimes."""
    if dt is not None and getattr(dt, "tzinfo", None) is not None:
        return dt.replace(tzinfo=None)
    return dt


def _iso(dt):
    """ISO-format a datetime for storage, passing None through untouched."""
    dt = _naive(dt)
    return dt.isoformat() if dt is not None else None


def create_reservation(driver_id, location_id, slot_id, reserved_from, reserved_until):
    """
    Create a new reservation.
    Checks if the driver has too many recent no-shows and flags accordingly.
    """
    # Calculate the datetime representing the beginning of the
    # no-show checking period.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=NOSHOW_THRESHOLD_DAYS)
    # Count the driver's expired reservations created within
    # the defined no-show period.
    recent_noshows = Database.count(
        """
        SELECT COUNT(*) FROM reservations
        WHERE driver_id = ? AND status = ? AND created_at >= ?
        """,
        (str(driver_id), STATUS_EXPIRED, cutoff.isoformat()),
    )
    # If the driver has reached the no-show threshold,
    # require additional approval.
    # Otherwise, place the reservation in the normal pending state.
    initial_status = (
        STATUS_PENDING_APPROVAL
        if recent_noshows >= NOSHOW_THRESHOLD_COUNT
        else STATUS_PENDING
    )
    # Record the current UTC time as the reservation creation time.
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    # Insert the new reservation into the reservations table.
    new_id = Database.execute(
        """
        INSERT INTO reservations (driver_id, location_id, slot_id, reserved_from,
                                   reserved_until, created_at, reminder_sent,
                                   status, noshow_flagged)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            str(driver_id), str(location_id), str(slot_id),
            _iso(reserved_from), _iso(reserved_until), created_at,
            initial_status, 1 if initial_status == STATUS_PENDING_APPROVAL else 0,
        ),# Mark the reservation as no-show flagged when it
            # requires approval because of repeated no-shows.
    )
    return new_id


def check_slot_overlap(slot_id, new_from, new_until):
    """Return True if a pending/active/conflict reservation overlaps the requested window.
    STATUS_CONFLICT is included so a new booking cannot be placed on a slot that is
    already in a conflicted state.
    """
    match = Database.fetch_one(
        """
        SELECT id FROM reservations
        WHERE slot_id = ?
          AND status IN (?, ?, ?, ?)
          AND reserved_from < ?
          AND reserved_until > ?
        LIMIT 1
        """,
        (
            str(slot_id), STATUS_PENDING, STATUS_PENDING_APPROVAL, STATUS_ACTIVE, STATUS_CONFLICT,
            _iso(new_until), _iso(new_from),
        ),
        # Consider all reservation states that should prevent
        # another booking from using the same time period.
        # Compare the existing reservation's times against
        # the requested reservation window.
    )
    # True means an overlapping reservation was found.
    # False means no matching reservation exists.
    return match is not None


def get_reservation_by_id(reservation_id):
    try:
        rid = int(reservation_id)
    except (TypeError, ValueError):
        return None
    return Database.fetch_one("SELECT * FROM reservations WHERE id = ?", (rid,))


def get_pending_reservation_by_id(reservation_id):
    try:
        rid = int(reservation_id)
    except (TypeError, ValueError):
        return None

    # Retrieve the reservation only if its status is pending
    # or pending approval.
    return Database.fetch_one(
        "SELECT * FROM reservations WHERE id = ? AND status IN (?, ?)",
        (rid, STATUS_PENDING, STATUS_PENDING_APPROVAL),
    )


def get_reservations_by_driver(driver_id, limit=100):
    # Retrieve reservations belonging to a specific driver.
    # The default maximum number of records is 100.
    return Database.fetch_all(
        "SELECT * FROM reservations WHERE driver_id = ? "
        "ORDER BY reserved_from DESC LIMIT ?",
        (str(driver_id), limit),
    )


def get_active_reservations_by_location(location_id):
    # Retrieve reservations at a location that are still
    # relevant to current parking operations.
    return Database.fetch_all(
        """
        SELECT * FROM reservations
        WHERE location_id = ? AND status IN (?, ?, ?, ?)
        """,
        (str(location_id), STATUS_PENDING, STATUS_PENDING_APPROVAL, STATUS_ACTIVE, STATUS_CONFLICT),
    )


def get_upcoming_reservation_for_slot(slot_id):
    """Used at check-in to warn the guard about an upcoming booking."""
    # Obtain the current UTC time in the same format used by
    # the reservation datetime columns.
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    # Find the earliest future reservation for the specified slot.
    return Database.fetch_one(
        """
        SELECT * FROM reservations
        WHERE slot_id = ? AND status IN (?, ?) AND reserved_from > ?
        ORDER BY reserved_from ASC LIMIT 1
        """,
        (str(slot_id), STATUS_PENDING, STATUS_PENDING_APPROVAL, now),
    )


def get_pending_approval_reservations(location_id):
    # Retrieve reservations that require manual approval,
    # particularly because of repeated no-shows.
    """Reservations flagged for guard/admin approval (repeat no-shows)."""
    return Database.fetch_all(
        "SELECT * FROM reservations WHERE location_id = ? AND status = ?",
        (str(location_id), STATUS_PENDING_APPROVAL),
    )


def get_pending_approval_reservation_by_id(reservation_id):
    # Retrieve a reservation only when it is currently awaiting approval.
    # This prevents an already-approved reservation from being
    # approved again through a stale or duplicate request.
    """Fetch a single reservation only if it is currently pending_approval —
    used by the guard's approve route so a stale/duplicate click on an
    already-approved reservation fails cleanly instead of re-approving."""
    try:
        rid = int(reservation_id)
    except (TypeError, ValueError):
        return None
    # Retrieve the reservation only if it is still pending approval.
    return Database.fetch_one(
        "SELECT * FROM reservations WHERE id = ? AND status = ?",
        (rid, STATUS_PENDING_APPROVAL),
    )


def update_reservation_status(reservation_id, new_status):
    # Change the current status of a reservation.
    Database.execute(
        "UPDATE reservations SET status = ? WHERE id = ?",
        (new_status, int(reservation_id)),
    )


def approve_reservation(reservation_id):
    """
    Admin/guard manually approves a flagged (pending_approval) reservation,
    moving it back into the normal pending flow.

    Returns True if a pending_approval reservation was approved, False if
    the id was invalid, not found, or not currently pending_approval (e.g.
    it was already approved by another tab, or has since expired/cancelled).
    """
    try:
        rid = int(reservation_id)
    except (TypeError, ValueError):
        return False
    # Retrieve the requested reservation from the database
    res = Database.fetch_one("SELECT * FROM reservations WHERE id = ?", (rid,))
    # Reject the request if the reservation does not exist or is not awaiting approval.
    if not res or res["status"] != STATUS_PENDING_APPROVAL:
        return False
    # Move the reservation back to the normal pending state
    # and clear its no-show warning flag.
    Database.execute(
        "UPDATE reservations SET status = ?, noshow_flagged = 0 WHERE id = ?",
        (STATUS_PENDING, rid),
    )
    # Report that the approval was successful.
    return True


def create_event_reservation(guard_id, location_id, slot_ids,
                              event_label, reserved_from, reserved_until):
    """Reserve multiple slots for an event (school day, function, etc.)."""
    # Count how many slots are successfully reserved
    count = 0
    # Record the time the event reservations were created.
    created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    # Process each requested parking slot
    for slot_id in slot_ids:
        # Skip the slot if its reservation period overlaps another reservation.

        if check_slot_overlap(slot_id, reserved_from, reserved_until):
            continue
            # Create an event reservation without assigning it to a driver.
        Database.execute(
            """
            INSERT INTO reservations (driver_id, guard_id, location_id, slot_id,
                                       event_label, reserved_from, reserved_until,
                                       created_at, reminder_sent, status)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                str(guard_id), str(location_id), str(slot_id), event_label,
                _iso(reserved_from), _iso(reserved_until), created_at, STATUS_PENDING,
            ),
        )
        # Increase the number of successfully reserved slots.
        count += 1
    # Return the total number of slots successfully reserved.
    return count


def get_event_reservations_by_location(location_id):
    # Retrieve active event reservations for a specific parking location.
    return Database.fetch_all(
        """
        SELECT * FROM reservations
        WHERE location_id = ? AND driver_id IS NULL AND status IN (?, ?, ?)
        """,
        (str(location_id), STATUS_PENDING, STATUS_ACTIVE, STATUS_CONFLICT),
    )


def _free_slot_if_still_reserved(slot_id):
    """After a reservation is cancelled, release its slot — but only if
    nothing else has since claimed it (e.g. a walk-in checked into it)."""
    # Retrieve the current slot information.
    slot = get_slot_by_id(slot_id)
    # Only release the slot if it still has the RESERVED status.
    if slot and slot["status"] == STATUS_RESERVED:
        update_slot_status(slot_id, STATUS_AVAILABLE)


def cancel_driver_reservation(reservation_id, driver_id):
    """
    Driver cancels their own reservation.
    Returns True, False, or 'too_late' (inside the lock window).
    """
    try:
        rid = int(reservation_id)
    except (TypeError, ValueError):
        return False
    # Find a reservation that belongs to this driver and can still be cancelled.
    res = Database.fetch_one(
        "SELECT * FROM reservations WHERE id = ? AND driver_id = ? AND status IN (?, ?)",
        (rid, str(driver_id), STATUS_PENDING, STATUS_PENDING_APPROVAL),
    )
    # Stop if the reservation does not exist or cannot be cancelled.
    if not res:
        return False
    # Get the current UTC time using the same naive format as the database.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Convert the stored reservation start time into a Python datetime.
    reserved_from = _naive(datetime.fromisoformat(res["reserved_from"])) if res["reserved_from"] else None
    # Prevent cancellation once the reservation is inside the lock window.
    if reserved_from and (reserved_from - now).total_seconds() / 60 <= LOCK_MINUTES:
        return "too_late"
    # Mark the reservation as cancelled.
    Database.execute(
        "UPDATE reservations SET status = ? WHERE id = ?", (STATUS_CANCELLED, rid)
    )
    # Release the slot if it is still reserved.
    if res["slot_id"]:
        _free_slot_if_still_reserved(res["slot_id"])
    return True


def cancel_event_reservation(reservation_id):
    # Validate the reservation ID.
    try:
        rid = int(reservation_id)
    except (TypeError, ValueError):
        return False
    # Retrieve the reservation
    res = Database.fetch_one("SELECT * FROM reservations WHERE id = ?", (rid,))
    # Only allow cancellation of event reservations.
    if not res or res["driver_id"] is not None:
        return False
    # Mark the event reservation as cancelled.
    Database.execute(
        "UPDATE reservations SET status = ? WHERE id = ?", (STATUS_CANCELLED, rid)
    )
    # Release the associated slot if it is still reserved.
    if res["slot_id"]:
        _free_slot_if_still_reserved(res["slot_id"])
    return True


def resolve_reservation_conflict(reservation_id):
    try:
        rid = int(reservation_id)
    except (TypeError, ValueError):
        return False
    # Retrieve the reservation
    res = Database.fetch_one("SELECT * FROM reservations WHERE id = ?", (rid,))
    # Only a reservation currently marked as conflicted can be resolved.
    if not res or res["status"] != STATUS_CONFLICT:
        return False
    # Return the reservation to the normal pending state.
    Database.execute(
        "UPDATE reservations SET status = ? WHERE id = ?", (STATUS_PENDING, rid)
    )
    # Mark the reservation's slot as reserved again.
    if res["slot_id"]:
        update_slot_status(res["slot_id"], STATUS_RESERVED)
    return True


def manage_reservations(location_id):
    """
    Called every 60 seconds (throttled in sync_reservations).
    Keeps reservations in sync with reality:
      - Expire bookings whose end time has passed
      - Lock slots within LOCK_MINUTES of start
      - Flag conflicts when a reserved slot is occupied by someone else
      - Send reminder emails 2 hours before start (once per reservation)
    """
    # Get the current UTC time in the same format used by the database.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Calculate the time window used to lock upcoming reserved slots.
    lock_cutoff = now + timedelta(minutes=LOCK_MINUTES)
    # Calculate the two-hour window used for reminder emails.
    remind_cutoff = now + timedelta(hours=2)

    # 1. Expire pending/flagged/conflicted reservations past their end time
    expired = Database.fetch_all(
        """
        SELECT * FROM reservations
        WHERE location_id = ? AND status IN (?, ?, ?) AND reserved_until < ?
        """,
        (str(location_id), STATUS_PENDING, STATUS_PENDING_APPROVAL, STATUS_CONFLICT, now.isoformat()),
    )
    for res in expired:
        # Mark the reservation as expired.
        Database.execute(
            "UPDATE reservations SET status = ? WHERE id = ?", (STATUS_EXPIRED, res["id"])
        )

        if res["slot_id"]:
            # Retrieve the slot associated with the expired reservation.
            slot = get_slot_by_id(res["slot_id"])
            # Only consider slots currently reserved or occupied.
            if slot and slot["status"] in (STATUS_RESERVED, STATUS_OCCUPIED):
                # Check whether the slot currently has an active parking session.
                active = Database.fetch_one(
                    "SELECT id FROM sessions WHERE slot_id = ? AND status = 'active'",
                    (str(res["slot_id"]),),
                )
                # Do not release a slot that currently has an active vehicle session.
                if not active:
                    update_slot_status(res["slot_id"], STATUS_AVAILABLE)

    # 2. LOCK SLOTS CLOSE TO THEIR RESERVATION START TIME

    # Find pending reservations starting within the configured lock window.
    soon = Database.fetch_all(
        """
        SELECT * FROM reservations
        WHERE location_id = ? AND status = ? AND reserved_from > ? AND reserved_from <= ?
        """,
        (str(location_id), STATUS_PENDING, now.isoformat(), lock_cutoff.isoformat()),
    )
    for res in soon:
        if res["slot_id"]:
            slot = get_slot_by_id(res["slot_id"])
            # Change an available slot to reserved before the booking begins.
            if slot and slot["status"] == STATUS_AVAILABLE:
                update_slot_status(res["slot_id"], STATUS_RESERVED)

    # 3. Flag conflicts: reservation window started but slot is occupied by someone else

    # Find pending reservations whose start time has arrived.
    overdue = Database.fetch_all(
        """
        SELECT * FROM reservations
        WHERE location_id = ? AND status = ? AND reserved_from <= ?
        """,
        (str(location_id), STATUS_PENDING, now.isoformat()),
    )
    for res in overdue:
        # Skip reservations that do not have an assigned slot.
        if not res["slot_id"]:
            continue
        # Retrieve the current slot state.
        slot = get_slot_by_id(res["slot_id"])
        # Check whether another vehicle currently occupies the reserved slot.
        if slot and slot["status"] == STATUS_OCCUPIED:
            occupying = Database.fetch_one(
                "SELECT * FROM sessions WHERE slot_id = ? AND status = 'active'",
                (str(res["slot_id"]),),
            )
            # Determine whether the occupying vehicle belongs to the
            # same driver who made the reservation.
            same_driver = (
                occupying is not None
                and res["driver_id"]
                and occupying["driver_id"] == res["driver_id"]
            )
            # If another driver occupies the slot, mark the reservation as conflicted.
            if not same_driver:
                Database.execute(
                    "UPDATE reservations SET status = ? WHERE id = ?",
                    (STATUS_CONFLICT, res["id"]),
                )
        # If the slot is still available when the reservation starts,
        # reserve it for the driver.
        elif slot and slot["status"] == STATUS_AVAILABLE:
            update_slot_status(res["slot_id"], STATUS_RESERVED)

    # 4. Send email reminders 2 hours before start (once per reservation)

    # Find driver reservations starting within the next two hours
    # that have not yet received a reminder.
    needs_reminder = Database.fetch_all(
        """
        SELECT * FROM reservations
        WHERE location_id = ? AND status = ? AND driver_id IS NOT NULL
          AND (reminder_sent IS NULL OR reminder_sent != 1)
          AND reserved_from > ? AND reserved_from <= ?
        """,
        (str(location_id), STATUS_PENDING, now.isoformat(), remind_cutoff.isoformat()),
    )
    for res in needs_reminder:

        try:
            # Retrieve the driver associated with the reservation.
            driver = Database.fetch_one(
                "SELECT * FROM users WHERE id = ?", (int(res["driver_id"]),)
            )
        except (TypeError, ValueError):
            # Continue safely if the driver ID is invalid.
            driver = None
        # Retrieve the location and reserved slot details.
        location = get_location_by_id(res["location_id"])
        slot = get_slot_by_id(res["slot_id"]) if res["slot_id"] else None

        # Send the reminder only when the driver and email address exist
        if driver and driver["email"]:
            # Convert stored ISO datetime strings back into datetime objects.
            res_from = datetime.fromisoformat(res["reserved_from"])
            res_until = datetime.fromisoformat(res["reserved_until"])
            # Convert UTC time to East Africa Time.
            from_eat = res_from + timedelta(hours=3)
            until_eat = res_until + timedelta(hours=3)
            # Send the reservation reminder email.
            send_reservation_reminder(
                driver_email=driver["email"],
                reservation_data={
                    "location_name": location["name"] if location else "Unknown",
                    "slot_number": slot["slot_number"] if slot else "N/A",
                    "reserved_from": from_eat.strftime("%d %b %Y, %H:%M") + " EAT",
                    "reserved_until": until_eat.strftime("%d %b %Y, %H:%M") + " EAT",
                },
            )
        # Mark the reminder as sent so it is not sent again.
        Database.execute(
            "UPDATE reservations SET reminder_sent = 1 WHERE id = ?", (res["id"],)
        )
