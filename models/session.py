"""
Parking session model for smart.park.

A session represents one vehicle's stay in the lot:
  - Created at check-in   (status = active)
  - Completed at check-out (fee and payment recorded, status = completed)

Each session gets a unique receipt number (RCP-YYYYMMDD-XXXXXXXX) used on
the printed receipt and in M-Pesa payment callbacks.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from database.db import Database

# Status constants — use these instead of bare strings to catch typos at import time.
SESSION_ACTIVE = "active"
SESSION_COMPLETED = "completed"


def _generate_receipt_number():
    """
    Create a unique receipt id in the format: RCP-YYYYMMDD-XXXXXXXX

    The 8-character hex suffix (4 random bytes) gives about 4 billion
    combinations per day — practically impossible to collide even during
    a busy event with dozens of concurrent check-ins.
    The database also has a UNIQUE constraint on this column as a backstop.
    """
    # Get today's date using UTC and format it as YYYYMMDD.
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Generate 4 random bytes and represent them as 8 uppercase
    # hexadecimal characters for the receipt's unique suffix.
    suffix = secrets.token_hex(4).upper()  # e.g. "3FA2C91B"
    # Combine the prefix, date and random suffix into the receipt number
    return f"RCP-{today}-{suffix}"  # e.g. "RCP-20260622-3FA2C91B"


def create_session(plate_number, location_id, slot_id=None,
                    guard_id=None, driver_id=None, reservation_id=None,
                    entry_time=None):
    """
    Open a new session when a vehicle enters the car park.

    plate_number   : the vehicle's registration (stored in UPPERCASE, no spaces)
    location_id    : which car park this session belongs to
    slot_id        : the specific bay assigned (optional — some lots don't track bays)
    guard_id       : the guard who did the check-in (for the audit trail)
    driver_id      : the registered driver account, if the plate is known (optional)
    reservation_id : linked reservation, if the driver pre-booked (optional)
    entry_time     : override the entry timestamp (useful for backdating in tests)

    Returns the new session's id.

    Raises sqlite3.IntegrityError if a UNIQUE constraint is violated
    (caught by parking.py).
    """
    # Use the supplied entry time or automatically use the current UTC time.
    actual_entry_time = entry_time if entry_time else datetime.now(timezone.utc)
    # Remove timezone information to match the database's naive UTC format.
    if getattr(actual_entry_time, "tzinfo", None) is not None:
        actual_entry_time = actual_entry_time.replace(tzinfo=None)
    # Insert the vehicle's parking session into the database.
    # Optional IDs are stored as None when they are not provided.
    new_id = Database.execute(
        """
        INSERT INTO sessions (plate_number, location_id, slot_id, guard_id,
                               driver_id, reservation_id, entry_time, exit_time,
                               fee_kes, payment_method, is_paid, status,
                               receipt_number, mpesa_code, payer_phone,
                               receipt_email_sent)
        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?, NULL, NULL, 0)
        """,
        (
            # Normalize the registration before storing it.
            plate_number.upper().strip(),
            str(location_id),
            str(slot_id) if slot_id else None,
            str(guard_id) if guard_id else None,
            str(driver_id) if driver_id else None,
            str(reservation_id) if reservation_id else None,
            actual_entry_time.isoformat(),
            SESSION_ACTIVE,
            _generate_receipt_number(),
        ),
    )
    return new_id


def complete_session(session_id, fee_kes, payment_method, exit_time=None):
    """
    Close a session when the vehicle exits and payment is taken.

    fee_kes        : the amount charged (in Kenyan Shillings)
    payment_method : "cash" or "mpesa"
    exit_time      : defaults to now if not provided

    The WHERE clause only matches a session that's still "active", so if
    two checkout requests (or two M-Pesa callback retries) race for the
    same session, only the first UPDATE actually changes a row.

    Returns True if this call completed the session, False if it was
    already completed (caller should treat that as "nothing to do").
    """
    # Use the supplied exit time or the current UTC time.
    et = exit_time if exit_time else datetime.now(timezone.utc)
    # Remove timezone information before storing the timestamp.
    if getattr(et, "tzinfo", None) is not None:
        et = et.replace(tzinfo=None)

    # Complete the session only if it is still active.
    # This prevents duplicate checkout operations from completing
    # the same session more than once.
    rows_changed = Database.execute(
        """
        UPDATE sessions
        SET exit_time = ?, fee_kes = ?, payment_method = ?, is_paid = 1, status = ?
        WHERE id = ? AND status = ?
        """,
        (et.isoformat(), float(fee_kes), payment_method, SESSION_COMPLETED,
         int(session_id), SESSION_ACTIVE),
    )
    # One changed row means checkout successfully completed the session.
    return rows_changed == 1


def get_session_by_id(session_id):
    """Fetch one session by its primary key. Returns None if not found."""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return None
    # Retrieve the session from the database
    return Database.fetch_one("SELECT * FROM sessions WHERE id = ?", (sid,))


def get_sessions_by_location(location_id, limit=50, paid_only=False):
    """
    Return recent sessions at a location, newest first.
    paid_only=True filters down to paid sessions only (for revenue reports).
    """
    # Start with a query that retrieves sessions for the selected locatio
    sql = "SELECT * FROM sessions WHERE location_id = ?"
    # Store SQL parameter values separately from the SQL statement.
    params: list = [str(location_id)]
    # Add a payment filter when a paid-only report is requested.
    if paid_only:
        sql += " AND is_paid = 1"
    # Sort newest sessions first and apply the result limit.
    sql += " ORDER BY entry_time DESC LIMIT ?"
    params.append(limit)
    # Execute the completed query using parameterized values.
    return Database.fetch_all(sql, tuple(params))


def get_active_sessions_by_location(location_id):
    """Return all currently parked vehicles at a location (status = active)."""
    # Retrieve vehicles that are currently parked at this location
    return Database.fetch_all(
        "SELECT * FROM sessions WHERE location_id = ? AND status = ?",
        (str(location_id), SESSION_ACTIVE),
    )


def get_active_session_by_plate(plate_number):
    """
    Find the currently active session for a given plate number.
    Used at check-out: the guard types the plate and we fetch the open session.
    Returns None if the plate isn't currently parked here.
    """
    # Normalize the plate and search only for an active session.
    return Database.fetch_one(
        "SELECT * FROM sessions WHERE plate_number = ? AND status = ?",
        (plate_number.upper().strip(), SESSION_ACTIVE),
    )


def get_active_session_by_slot(slot_id):
    """
    Find which vehicle (if any) is currently parked in a specific bay.
    Used by the slot map to show the plate on hover.
    """
    try:
        # Normalize the plate and search only for an active session.
        return Database.fetch_one(
            "SELECT * FROM sessions WHERE slot_id = ? AND status = ?",
            (str(slot_id), SESSION_ACTIVE),
        )
    except Exception:
        # Return None if the lookup fails.
        return None


def get_all_sessions_by_plate(plate_number, limit=100):
    """
    Return the full parking history for a plate number, newest first.
    Used on the guard's vehicle search page and the driver's history tab.
    """
    # Search for all sessions belonging to the normalized plate number.
    return Database.fetch_all(
        "SELECT * FROM sessions WHERE plate_number = ? "
        "ORDER BY entry_time DESC LIMIT ?",
        (plate_number.upper().strip(), limit),
    )
