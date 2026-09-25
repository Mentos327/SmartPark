"""
Location camera model for smart.park.

One location can have more than one camera (entrance, exit, overview
etc). Each row here is one camera at one location.
"""
from __future__ import annotations

from datetime import datetime, timezone

from database.db import Database


def count_cameras_by_location(location_id):
    """Return how many cameras a location currently has."""
    # Execute a database query that counts all camera records
    # belonging to the specified location.
    row = Database.fetch_one(
        # COUNT(*) counts the matching camera records.
        # AS n gives the returned count the name "n".
        # The WHERE clause ensures that only cameras belonging
        # to the specified location are counted.
        "SELECT COUNT(*) AS n FROM location_cameras WHERE location_id = ?",
        # Provide the location ID as the parameter for the
        # placeholder (?) in the SQL query.
        (str(location_id),),
    )
    return row["n"] if row else 0


def create_camera(location_id, label, camera_url):
    """Add a new camera to a location. Returns the new camera's id.

    No limit on how many cameras a location can have — add as many as
    you need (entrance, exit, overview, extra angles, etc).
    """
    # Find the camera with the highest existing sort order
    # for this particular location.
    last = Database.fetch_one(
        #LIMIT 1 retrieves only that record
        "SELECT sort_order FROM location_cameras WHERE location_id = ? "
        "ORDER BY sort_order DESC LIMIT 1",
        # Supply the location ID for the ? placeholder.
        (str(location_id),),
    )
    # If a previous camera exists and has a valid sort order,
    # place the new camera after it by increasing the order by 1.
    # If no previous camera exists, start the order at 0.
    next_order = (last["sort_order"] + 1) if last and last["sort_order"] is not None else 0

    # Insert the new camera into the location_cameras table.
    # Return the value produced by the database operation,
    # which represents the newly created camera's ID.
    return Database.execute(
        """
        INSERT INTO location_cameras (location_id, label, camera_url, sort_order, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(location_id),
            label.strip() or "Camera",
            camera_url.strip(),
            next_order,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def get_cameras_by_location(location_id):
    """Return every camera at a location, in display order."""
    return Database.fetch_all(
        # Select all camera information for the specified location.
        # Sort cameras by sort_order first and ID second.
        "SELECT * FROM location_cameras WHERE location_id = ? ORDER BY sort_order ASC, id ASC",
        # Supply the location ID for the ? placeholder.
        (str(location_id),),
    )


def get_camera_by_id(camera_id):
    """Fetch a single camera by its primary key. Returns None if not found."""
    try:
        # Convert the supplied camera ID into an integer
        # before using it in the database query.
        cid = int(camera_id)
    except (TypeError, ValueError):
        # If the camera ID cannot be converted into an integer,
        # stop the function and indicate that no camera was found.
        return None
    # Retrieve the camera whose primary key matches the supplied ID.
    return Database.fetch_one("SELECT * FROM location_cameras WHERE id = ?", (cid,))


def delete_camera(camera_id):
    """Remove a camera. Returns True if a row was deleted."""
    try:
        cid = int(camera_id)
    except (TypeError, ValueError):
        return False
    # Delete the camera whose database ID matches cid.
    # The database operation returns the number of affected rows.
    rows = Database.execute("DELETE FROM location_cameras WHERE id = ?", (cid,))
    # Return True if at least one camera record was deleted.
    # Otherwise return False.
    return rows > 0
