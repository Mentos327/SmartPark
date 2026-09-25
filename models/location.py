"""
Parking location model for smart.park.

A location is a physical parking site (mall, hospital, bus stage, etc.)
managed by one admin and containing a set of slots.

Each location stores its own M-Pesa credentials so payments go to the
correct till number.
"""
from __future__ import annotations

from database.db import Database

# All 47 Kenyan counties — used for validation and dropdown
KENYA_COUNTIES = [
    "Baringo", "Bomet", "Bungoma", "Busia", "Elgeyo-Marakwet",
    "Embu", "Garissa", "Homa Bay", "Isiolo", "Kajiado",
    "Kakamega", "Kericho", "Kiambu", "Kilifi", "Kirinyaga",
    "Kisii", "Kisumu", "Kitui", "Kwale", "Laikipia",
    "Lamu", "Machakos", "Makueni", "Mandera", "Marsabit",
    "Meru", "Migori", "Mombasa", "Murang'a", "Nairobi",
    "Nakuru", "Nandi", "Narok", "Nyamira", "Nyandarua",
    "Nyeri", "Samburu", "Siaya", "Taita-Taveta", "Tana River",
    "Tharaka-Nithi", "Trans Nzoia", "Turkana", "Uasin Gishu",
    "Vihiga", "Wajir", "West Pokot",
]

# Supported location types (used for pricing and UI hints)
LOCATION_TYPES = [
    ("mall", "Shopping Mall / Centre"),
    ("hotel", "Hotel / Lodge"),
    ("school", "School / University"),
    ("bus_stage", "Bus Stage / Matatu Stand"),
    ("hospital", "Hospital / Clinic"),
    ("church", "Church / Mosque"),
    ("public", "Public Parking"),
    ("private", "Private / Commercial"),
]


def create_location(name, address, county, location_type, total_slots, hourly_rate,
                     admin_id=None, phone=None, description=None):
    """Create a new parking location. Returns the new row's id."""
    new_id = Database.execute(
        """
        INSERT INTO locations (name, address, county, type, total_slots,
                                hourly_rate, is_active, admin_id, phone, description,
                                mpesa_consumer_key, mpesa_consumer_secret,
                                mpesa_shortcode, mpesa_passkey, mpesa_account_type)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, '', '', '', '', 'paybill')
        """,
        (
            name, address, county, location_type, int(total_slots), float(hourly_rate),
            str(admin_id) if admin_id else None, phone or None, description or None,
        ),
    )
    return new_id


def get_all_locations(active_only=True):
    """Return all locations, optionally only active ones."""
    if active_only:
        return Database.fetch_all(
            "SELECT * FROM locations WHERE is_active = 1 ORDER BY name ASC"
        )
    return Database.fetch_all("SELECT * FROM locations ORDER BY name ASC")


def get_location_by_id(location_id):
    """Return a single location row by id, or None."""
    try:
        loc_id = int(location_id)
    except (TypeError, ValueError):
        return None
    return Database.fetch_one("SELECT * FROM locations WHERE id = ?", (loc_id,))

# Whitelist of database columns that are allowed to be updated.
# Only fields contained in this set can be modified through
# update_location().
_ALLOWED_LOCATION_FIELDS = {
    "name", "address", "county", "type", "total_slots", "hourly_rate",
    "is_active", "admin_id", "phone", "description", "camera_url",
    "reservations_paused", "mpesa_consumer_key",
    "mpesa_consumer_secret", "mpesa_shortcode", "mpesa_passkey",
    "mpesa_account_type",
}


def update_location(location_id, updates: dict):
    """
    Update one or more fields on a location.
    Pass a dict of field: value pairs. Only known columns are applied —
    the whitelist prevents untrusted keys from being interpolated into SQL.
    """
    # Keep only the update fields that are present in the approved whitelist.
    fields = [k for k in updates if k in _ALLOWED_LOCATION_FIELDS]
    # If there are no valid fields, stop without changing the database.
    if not fields:
        return
    # Build the SQL SET clause using only approved field names.
    # The actual field values remain parameterized using ? placeholders.
    set_clause = ", ".join(f"{field} = ?" for field in fields)
    # Collect the values corresponding to the approved fields.
    values = [updates[field] for field in fields]
    # Add the location ID as the final parameter for WHERE id = ?.
    values.append(int(location_id))
    # Execute the dynamically constructed UPDATE statement.
    # Convert the list of values to a tuple for the database method.
    Database.execute(f"UPDATE locations SET {set_clause} WHERE id = ?", tuple(values))


def location_has_history(location_id) -> bool:
    """
    True if this location has ever had a parking session or a reservation.

    Used to decide whether a location can be hard-deleted outright, or
    whether it should be suspended instead so historical sessions,
    reservations and audit-log entries that reference it aren't orphaned.
    """
    # Convert the location ID to a string for the database queries.
    loc_id = str(location_id)
    # Count all parking sessions associated with this location
    session_count = Database.count(
        "SELECT COUNT(*) FROM sessions WHERE location_id = ?", (loc_id,)
    )
    # If at least one session exists, the location has historical data.
    if session_count:
        return True

    # If there are no sessions, check whether the location
    # has any historical reservations.
    reservation_count = Database.count(
        "SELECT COUNT(*) FROM reservations WHERE location_id = ?", (loc_id,)
    )
    # Return True if reservations exist, otherwise False.
    return bool(reservation_count)


def delete_location(location_id) -> bool:
    """
    Permanently delete a location that has no session or reservation
    history. Also removes its slots and any registered cameras, since
    those exist only in relation to the location.

    Returns False (and deletes nothing) if the location has history —
    callers should suspend it instead via update_location(is_active=0)
    so existing records keep a valid location to reference.
    """
    loc_id = int(location_id)
    # Do not permanently delete a location that has historical
    # sessions or reservations.
    if location_has_history(loc_id):
        return False
    # Delete all parking slots belonging to the location.
    Database.execute("DELETE FROM slots WHERE location_id = ?", (str(loc_id),))
    # Delete all registered cameras belonging to the location.
    Database.execute("DELETE FROM location_cameras WHERE location_id = ?", (str(loc_id),))
    # Finally delete the location itself.
    Database.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    # Indicate that the location was successfully deleted.
    return True
