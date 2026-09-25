"""
Parking slot model for smart.park.

Status values:
  available — slot is free
  occupied  — vehicle is currently parked
  reserved  — slot locked for an upcoming reservation

Use the STATUS_* constants instead of bare strings to catch typos.
"""
from __future__ import annotations

from database.db import Database

# Use these constants instead of bare strings throughout the codebase
# so a typo like "availble" becomes a NameError rather than a silent bug.
STATUS_AVAILABLE = "available"
STATUS_OCCUPIED = "occupied"
STATUS_RESERVED = "reserved"
STATUS_DISABLED = "disabled"


def create_slot(location_id, slot_number, slot_type="standard", level="G"):
    """
    Add a new parking slot to a location.

    slot_number : the label shown on the physical bay, e.g. "A1", "G3"
    slot_type   : "standard", "disabled", "vip", etc.
    level       : which floor/level this slot is on, e.g. "G", "1", "B1"

    Returns the new slot's id, or None if that slot number already exists
    at this location (prevents accidental duplicates).
    """
    # Check whether this slot number already exists at the selected location.
    # This prevents duplicate physical parking bays.
    existing = Database.fetch_one(
        "SELECT id FROM slots WHERE location_id = ? AND slot_number = ?",
        (str(location_id), slot_number),
    )
    # Stop creation if a slot with the same number already exists.
    if existing:
        return None  # slot A1 already exists at this location — don't create another

    # Find the current highest display order for this location.
    last = Database.fetch_one(
        "SELECT sort_order FROM slots WHERE location_id = ? "
        "ORDER BY sort_order DESC LIMIT 1",
        (str(location_id),),
    )
    # Place the new slot after the existing slots.
    # Start at 0 when no previous slot exists.
    next_order = (last["sort_order"] + 1) if last and last["sort_order"] is not None else 0
    # Insert the new slot and make it available initially.
    new_id = Database.execute(
        """
        INSERT INTO slots (location_id, slot_number, level, type, status, sort_order)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(location_id), slot_number, level, slot_type, STATUS_AVAILABLE, next_order),
    )
    # Return the database ID of the newly created slot.
    return new_id


def get_slots_by_location(location_id):
    """
    Return all slots for a location, ordered by level then display order.
    This is the main query behind the slot map on the guard and driver pages.
    """
    # Retrieve all slots belonging to the selected location.
    # Sort them first by level and then by their configured display order.
    return Database.fetch_all(
        "SELECT * FROM slots WHERE location_id = ? ORDER BY level ASC, sort_order ASC",
        (str(location_id),),
    )


def get_available_slots(location_id):
    """Return only the available slots — used when assigning a slot at check-in."""
    # Retrieve only slots that can currently be assigned to vehicles.
    return Database.fetch_all(
        "SELECT * FROM slots WHERE location_id = ? AND status = ?",
        (str(location_id), STATUS_AVAILABLE),
    )


def get_slot_by_id(slot_id):
    """Fetch a single slot by its primary key. Returns None if not found."""
    try:
        sid = int(slot_id)
    except (TypeError, ValueError):
        return None
    # Retrieve the slot with the specified primary key
    return Database.fetch_one("SELECT * FROM slots WHERE id = ?", (sid,))


def update_slot_status(slot_id, new_status):
    """
    Change a slot's status (e.g. from 'available' to 'occupied' at check-in).
    Swallows errors with a log message so a status update failure doesn't
    crash the caller's transaction — stale status is better than a 500 error.
    """
    try:
        # Update the status of the specified parking slot.
        Database.execute(
            "UPDATE slots SET status = ? WHERE id = ?", (new_status, int(slot_id))
        )
    except Exception as exc:
        # Log the error instead of allowing the status update failure
        # to crash the operation that called this function.
        # Print to server logs so ops can spot stuck slots in the slot map.
        print(f"[update_slot_status] ERROR updating slot {slot_id} -> {new_status}: {exc}")


def claim_slot(slot_id, new_status=STATUS_OCCUPIED):
    """
    Try to take a slot at check-in.

    The status check and the update happen in the same SQL statement,
    so if two guards check in a vehicle to the same slot at the same
    time, only one UPDATE actually matches a row and wins. The other
    one changes 0 rows and we know someone beat them to it.

    Returns True if we got the slot, False if it was already taken.
    """
    # Change the slot status only when the slot is currently available
    # or reserved. The database performs the check and update together.
    rows_changed = Database.execute(
        """
        UPDATE slots
        SET status = ?
        WHERE id = ? AND status IN (?, ?)
        """,
        (new_status, int(slot_id), STATUS_AVAILABLE, STATUS_RESERVED),
    )
    # Change the slot status only when the slot is currently available
    # or reserved. The database performs the check and update together.
    return rows_changed == 1


def update_slot_order(slot_id, sort_order):
    """
    Update the display order of a slot (used by the drag-to-reorder UI).
    Returns True on success, False on failure.
    """
    try:
        # Save the new display position for the selected slot.
        Database.execute(
            "UPDATE slots SET sort_order = ? WHERE id = ?",
            (int(sort_order), int(slot_id)),
        )
        # Report that the operation completed successfully.
        return True
    except Exception as exc:
        # Log the error and report that the update failed.

        print(f"[update_slot_order] ERROR updating slot {slot_id} -> {sort_order}: {exc}")
        return False


def count_slots_by_status(location_id):
    """
    Return a summary dict of how many slots have each status.
    Used by the admin dashboard's stat cards and the guard's slot map header.
    Example return value: {"available": 8, "occupied": 4, "reserved": 2, "disabled": 1}
    """
    # Create the summary with zero counts as the initial values.
    summary = {
        STATUS_AVAILABLE: 0,
        STATUS_OCCUPIED: 0,
        STATUS_RESERVED: 0,
        STATUS_DISABLED: 0,
    }
    # Ask the database to count slots grouped by their current status.
    rows = Database.fetch_all(
        "SELECT status, COUNT(*) AS cnt FROM slots WHERE location_id = ? GROUP BY status",
        (str(location_id),),
    )
    # Copy the database counts into the summary dictionary.
    for row in rows:
        if row["status"] in summary:
            summary[row["status"]] = row["cnt"]
    # Return the final slot-status summary for the dashboard or slot map.
    return summary
