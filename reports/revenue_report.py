"""
Revenue report queries for smart.park.

Called by the admin and super-admin report views.
Returns aggregated revenue data for one location: totals, a 7-day daily
revenue chart, per-guard activity, recent email failures, and recent
rate-change history.
"""
from __future__ import annotations

import json
from datetime import timedelta, datetime as _dt

from database.db import Database
from models.session import get_sessions_by_location


def _parse_session_datetimes(session):
    """
    Convert entry_time/exit_time from stored ISO strings to datetime objects.
    Templates that do datetime arithmetic (e.g. exit_time - entry_time) need
    real datetime objects, not strings.
    """
    # Convert the stored entry time from an ISO string into a real
    # datetime object when the value exists and is currently text.
    if session.get("entry_time") and isinstance(session["entry_time"], str):
        session["entry_time"] = _dt.fromisoformat(session["entry_time"])
    if session.get("exit_time") and isinstance(session["exit_time"], str):
        session["exit_time"] = _dt.fromisoformat(session["exit_time"])
    return session
    # Return the session after its datetime values have been prepared.


def build_admin_report(location_id) -> dict:
    # Retrieve up to 200 parking sessions belonging to this location.
    sessions = get_sessions_by_location(location_id, limit=200)

    # Parse entry_time/exit_time into real datetime objects up front, once,
    # for every session in this report — both the daily-revenue chart below
    # and the sessions table handed to the template need real datetimes.
    for s in sessions:
        _parse_session_datetimes(s)

    # Keep only sessions that have been paid and have a recorded fee.
    paid_sessions = [s for s in sessions if s["is_paid"] and s["fee_kes"]]

    # Calculate the total revenue and number of sessions/vehicles.
    total_revenue = sum(s["fee_kes"] for s in paid_sessions)
    total_vehicles = len(sessions)

    # Revenue by payment method
    cash_revenue = sum(s["fee_kes"] for s in paid_sessions if s["payment_method"] == "cash")
    mpesa_revenue = sum(s["fee_kes"] for s in paid_sessions if s["payment_method"] == "mpesa")

    # Daily revenue for the last 7 days (EAT = UTC+3)
    # Create an empty dictionary that will store revenue grouped by date.
    # The date will be the key and the total revenue for that date will
    # be the corresponding value.
    daily: dict[str, float] = {}
    # Process every paid session so its fee can be added to the correct day.
    for s in paid_sessions:
        # Retrieve the vehicle's entry datetime from the session.
        entry_dt = s["entry_time"]
        # Only calculate daily revenue when an entry time is available.
        if entry_dt:
            # Convert the entry time to East Africa Time by adding three
            # hours to UTC, then extract only the calendar date.
            eat_date = (entry_dt + timedelta(hours=3)).date()
            # Convert the date into a short text label such as "07 Sep".
            # This label is later used on the administrator's revenue chart.
            key = eat_date.strftime("%d %b")
            # Add this session's fee to the revenue already recorded for
            # that date. If the date has not appeared before, daily.get()
            # returns 0 so the first fee can be added safely.
            daily[key] = daily.get(key, 0) + s["fee_kes"]

    # Sessions come back newest-first, so daily dict keys are newest-date-first.
    # We want the most recent 7 days on the chart, displayed oldest to newest (left to right).
    all_day_keys = list(daily.keys())
    # Select the first seven dates because the report displays the
    # most recent seven available revenue days.
    recent_keys = all_day_keys[:7]
    # Reverse the selected dates so the chart displays them chronologically,
    # from the oldest selected date on the left to the newest on the right.
    daily_labels = list(reversed(recent_keys))
    # Retrieve the revenue amount belonging to each date label.
    # The order matches daily_labels so the chart can pair each date
    # with its corresponding revenue value.
    daily_revenue = [daily[k] for k in daily_labels]

    # Cash vs M-Pesa count
    cash_count = sum(1 for s in paid_sessions if s["payment_method"] == "cash")
    mpesa_count = sum(1 for s in paid_sessions if s["payment_method"] == "mpesa")

    # Per-guard activity: count check-ins and check-outs per guard at this location.
    guard_rows = Database.fetch_all(
        """
        SELECT user_id, action, COUNT(*) AS cnt
        FROM audit_logs
        WHERE location_id = ? AND action IN ('check_in', 'check_out') AND role = 'guard'
        GROUP BY user_id, action
        """,
        (str(location_id),),
    )

    # Create an empty dictionary to organize activity records by guard ID.
    # Each guard will eventually contain their check-in count,
    # check-out count and displayed name.
    guard_activity: dict[str, dict] = {}
    for row in guard_rows:
        gid = row["user_id"]
        # If this guard has not yet been added to the dictionary,
        # create an initial record with zero counts.
        if gid not in guard_activity:
            guard_activity[gid] = {"check_in": 0, "check_out": 0, "name": gid}
            # Store the count returned by SQL under the appropriate action.
            # The action will be either "check_in" or "check_out".
        guard_activity[gid][row["action"]] = row["cnt"]

    # Replace guard ids with real names
    for gid in list(guard_activity):
        try:
            user = Database.fetch_one("SELECT name FROM users WHERE id = ?", (int(gid),))
            if user:
                # If a matching user was found, replace the temporary guard ID
                # with the user's actual name. If the name itself is empty,
                # keep the guard ID as a fallback.
                guard_activity[gid]["name"] = user["name"] or gid
        except (TypeError, ValueError):
            # If the guard ID cannot be converted to an integer or has an
            # invalid type, ignore that record rather than allowing one bad
            # guard record to stop the entire administrator report.
            pass

    # Convert the guard dictionary into a list containing the individual guard activity records.
    guard_activity_list = list(guard_activity.values())
    # Sort the guards alphabetically according to their displayed names
    # so the administrator sees the activity in an organized order
    guard_activity_list.sort(key=lambda g: g["name"])

    # Show recent email failures so the admin knows which emails were not delivered
    email_failures = Database.fetch_all(
        'SELECT * FROM email_failures ORDER BY failed_at DESC LIMIT 10'
    )

    # Show the last 10 rate changes from the audit log so admin can see pricing history
    rate_history = Database.fetch_all(
        "SELECT * FROM audit_logs WHERE location_id = ? AND action = 'price_change' "
        "ORDER BY timestamp DESC LIMIT 10",
        (str(location_id),),
    )
    # Process each price-change record so its stored JSON details can
    # be converted back into normal Python data for the dashboard.
    for entry in rate_history:
        # Only attempt JSON conversion when the details field contains # an actual value.
        if entry.get("details") is not None:

            try:
                # The details were stored in the database as JSON text.
                # json.loads() converts that text back into a Python
                # dictionary/object that the application can use.
                entry["details"] = json.loads(entry["details"])
                # If the stored JSON is invalid or has an unsuitable type,
                # replace it with an empty dictionary rather than crashing
                # the entire administrator report.
            except (TypeError, ValueError):
                entry["details"] = {}

    return {
        "rate_history": rate_history,
        "email_failures": email_failures,
        "guard_activity": guard_activity_list,
        "sessions": sessions[:50],  # show latest 50 in the table
        "total_revenue": total_revenue,
        "total_vehicles": total_vehicles,
        "cash_revenue": cash_revenue,
        "mpesa_revenue": mpesa_revenue,
        "cash_count": cash_count,
        "mpesa_count": mpesa_count,
        "daily_labels": daily_labels,
        "daily_revenue": daily_revenue,
    }
