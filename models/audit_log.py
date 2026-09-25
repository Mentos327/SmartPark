"""
Audit log for smart.park.

Records every significant action (check-in, check-out, slot change, etc.)
with the acting user, role, location, and a details string.

Used by the super-admin logs view and helps detect anomalies.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from database.db import Database


def log_action(user_id, role, action, details=None, location_id=None):
    """
    Save one action to the audit trail.

    user_id     : the id of the user who performed the action
    role        : their role at the time ("guard", "admin", etc.)
    action      : a short action code, e.g. "check_in", "rate_change"
    details     : a dict with any extra context (plate number, old rate, etc.)
    location_id : which parking location this action happened at (if applicable)
    """
    # Record the current UTC time and convert it into a consistent
    # ISO-formatted string for storage in the audit log.
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    # Use the Database helper to insert the audit record. The helper
    # automatically manages the write transaction and connection cleanup.
    Database.execute(
        """
        INSERT INTO audit_logs (user_id, role, action, details, location_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            str(user_id), role, action,
            json.dumps(details or {}),
            # Convert optional action details into a JSON string so that
            # structured information can be stored in the database.
            str(location_id) if location_id else None,
            timestamp,
        ),
    )
