"""
In-app notification builder.

build_notifications(user) returns a dict of notification counts (keyed by
type, e.g. "conflict_count") injected into every template render so the
notification bell in the nav bar stays current.
"""
from __future__ import annotations

from database.db import Database


def build_notifications(current_user) -> dict:
    notifications: dict = {}
    # Create an empty dictionary to hold the notifications that will
    # eventually be returned to the application's interface.
    if not current_user or not getattr(current_user, "is_authenticated", False):
        # "not current_user" checks whether a user object was supplied.
        # getattr() safely attempts to read the "is_authenticated" attribute.
        # If the attribute does not exist, getattr() returns False.
        return notifications

    try:
        #runs during # page rendering. A notification database error should not prevent
    # the entire page from being displayed.

        if current_user.role == "guard" and current_user.location_id:
            conflict_count = Database.count(
                "SELECT COUNT(*) FROM reservations WHERE location_id = ? AND status = ?",
                (str(current_user.location_id), "conflict"),
            )
            # COUNT(*) tells the database to count matching reservation
            # records instead of returning the actual reservation rows.
            if conflict_count:
                notifications["conflict_count"] = conflict_count
                # Only add a conflict notification when at least one
                # conflicting reservation exists.

            approval_count = Database.count(
                "SELECT COUNT(*) FROM reservations WHERE location_id = ? AND status = ?",
                (str(current_user.location_id), "pending_approval"),
            )
            # Count reservations at this guard's location that are
            # waiting for approval.
            if approval_count:
                notifications["approval_count"] = approval_count
            # Only add the approval notification when there is at least
            # one reservation waiting for approval.
    except Exception as exc:
        # Runs on every page render, so a DB hiccup here should never break
        # the page — but it should show up in the logs, not vanish.
        print(f"[build_notifications] failed for user {current_user.id}: {exc}")

    return notifications
