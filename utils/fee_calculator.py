"""
Parking fee calculation for smart.park.

All fees are in Kenya Shillings (KES).

Functions:
  calculate_fee(entry, exit, hourly_rate)             — fee for a completed stay
  calculate_current_fee(entry_time, hourly_rate)      — running fee for an active session
  calculate_reservation_fee(reserved_from, actual_entry, actual_exit, rate) — fee for a pre-booked window
"""
import math
from datetime import datetime, timezone, timedelta
# East Africa Time is three hours ahead of UTC
EAT_OFFSET = timedelta(hours=3)


def strip_timezone(dt):
    """Remove timezone info from a datetime object."""
    # If no datetime was supplied, return None instead of attempting
    # to access timezone information from a missing value.
    if dt is None:
        return None
    # Remove timezone information when it exists so datetime values
    # can be compared consistently during fee calculations.
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def build_duration_display(total_minutes, minimum_applied=False):
    """Return a human-readable duration string, e.g. '1h 15m' or '25m (minimum)'."""
    # Convert the total minutes into complete hours and remaining minutes.
    hours = total_minutes // 60
    mins  = total_minutes % 60
    # Include hours in the display when the duration contains a full hour.
    if hours > 0:
        display = f"{hours}h {mins}m"
    else:
        # Otherwise, display only the number of minutes.
        display = f"{mins}m"
    # Indicate when the 30-minute minimum billing rule was applied.
    if minimum_applied:
        display += " (minimum)"
    return display


def calculate_fee(entry_time, exit_time, hourly_rate_kes):
    """
    Calculate parking fee from entry to exit time.
    Minimum charge: 30 minutes.
    Rounds up to the nearest KES 10 for clean cash/M-Pesa payments.

    Returns a dict with:
        duration_minutes, duration_display, fee_kes,
        per_minute_rate, hourly_rate_kes
    """
    # Remove timezone information from both timestamps so they can
    # be compared and subtracted consistently
    entry_time = strip_timezone(entry_time)
    exit_time  = strip_timezone(exit_time)

    # Calculate the elapsed parking time in whole minutes.
    total_minutes = int((exit_time - entry_time).total_seconds() / 60)
    # Prevent an invalid negative parking duration from affecting the fee.
    if total_minutes < 0:
        total_minutes = 0

    # Record whether the actual duration is below the 30-minute minimum.
    minimum_applied  = total_minutes < 30
    # Use at least 30 minutes when determining the amount to charge.
    billable_minutes = max(total_minutes, 30)

    # Convert the hourly parking rate into a per-minute rate.
    per_minute_rate = hourly_rate_kes / 60
    raw_fee         = billable_minutes * per_minute_rate
    # Round the fee upward to the nearest KES 10 for a clean payment amount.
    fee_kes         = math.ceil(raw_fee / 10) * 10  # round up to nearest KES 10

    # Return the calculated duration, fee and rate information together.
    return {
        "duration_minutes": total_minutes,
        "duration_display": build_duration_display(total_minutes, minimum_applied),
        "fee_kes":          fee_kes,
        "per_minute_rate":  round(per_minute_rate, 2),
        "hourly_rate_kes":  hourly_rate_kes,
    }


def calculate_current_fee(entry_time, hourly_rate_kes):
    """Live fee for a running session (called during checkout preview)."""
    # Use the current UTC time as the temporary exit time and reuse
    # the main fee calculation logic for the running parking session.
    return calculate_fee(entry_time, datetime.now(timezone.utc), hourly_rate_kes)


def calculate_reservation_fee(reserved_from, actual_entry, actual_exit, hourly_rate_kes):
    """
    Fee for a driver who made a reservation.
      - Arrived early  → billing starts at reserved_from
      - Arrived on time/late → billing starts at actual_entry
      - Billing always ends at actual_exit
    """
    # If the actual entry or exit time is missing, there is no
    # completed parking session to calculate a charge for.
    if actual_entry is None or actual_exit is None:
        return {
            "duration_minutes": 0,
            "duration_display": "0m",
            "fee_kes":          0,
            "per_minute_rate":  round(hourly_rate_kes / 60, 2),
            "hourly_rate_kes":  hourly_rate_kes,
            "billing_note":     "No session recorded.",
        }

    # Remove timezone information before comparing the reservation
    # start time with the actual vehicle entry time.
    entry_clean    = strip_timezone(actual_entry)
    res_from_clean = strip_timezone(reserved_from)

    # If the driver arrived before the reservation start time,
    # billing begins at the scheduled reservation time instead.
    if res_from_clean and entry_clean < res_from_clean:
        billing_start = res_from_clean
        # Convert the reservation time to a readable EAT clock time
        # for inclusion in the billing explanation.
        eat_time = (res_from_clean + EAT_OFFSET).strftime('%H:%M')
        billing_note = f"Arrived early. Billing starts from reservation time ({eat_time} EAT)."
    else:
        # For on-time or late arrivals, billing begins when the
        # vehicle actually entered the parking facility.
        billing_start = entry_clean
        billing_note  = "Charged for actual time parked."

    # Reuse the main fee calculation so reservation sessions receive
    # the same minimum-charge and KES 10 rounding rules.
    fee_info = calculate_fee(billing_start, actual_exit, hourly_rate_kes)
    # Add an explanation describing how the billing start time was chosen.
    fee_info["billing_note"] = billing_note
    # Return the complete reservation fee information.
    return fee_info

