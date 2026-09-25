"""
Vehicle check-in and check-out logic.

Centralises the multi-step parking flow so it can be called from both
the guard controller and the API controller without duplicating logic.

check_in_vehicle(...)  — record entry, assign slot, link reservation
check_out_vehicle(...) — compute fee, mark slot available, close session
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from models.session import create_session, complete_session, get_session_by_id
from models.slot import (update_slot_status, claim_slot, get_slot_by_id,
                          STATUS_AVAILABLE)
from models.location import get_location_by_id
from models.reservation import get_reservation_by_id, update_reservation_status, STATUS_COMPLETED
from models.audit_log import log_action
from utils.fee_calculator import calculate_current_fee, calculate_reservation_fee
from utils.plate_validator import normalise_plate, is_valid_plate
from utils.mpesa import mpesa_credentials_configured
from config import Config


def check_in_vehicle(plate_number, location_id, slot_id=None,
                      guard_id=None, driver_id=None, reservation_id=None):
    """
    Record a vehicle entering the parking lot.

    Args:
        plate_number:   vehicle plate (will be normalised)
        location_id:    parking location id
        slot_id:        slot to assign (None for walk-in with no slot)
        guard_id:       guard performing the check-in
        driver_id:      driver account id if the driver is registered
        reservation_id: booking id if the driver pre-booked a slot

    Returns:
        {"success": True, "session_id": "...", "warning": None or message}
        {"success": False, "error": "..."}
    """
    plate_number = normalise_plate(plate_number)
    # Normalize the vehicle plate before using it.
    # normalise_plate() standardizes the value so that the same vehicle
    # is stored and searched using a consistent plate format.
    if not plate_number:
        return {"success": False, "error": "Plate number is required."}
    # Stop the check-in if no usable plate number was provided.
    # "not plate_number" is true when the normalized value is empty
    # or otherwise evaluates to false.

    warning = None
    # Start with no warning.
    # A warning is different from an error: an unusual plate format
    # does not automatically stop the check-in.
    if not is_valid_plate(plate_number):
        warning = "Plate format unusual — please verify before continuing."
    # Check whether the normalized plate follows the application's
    # expected vehicle plate format.
    # If the format is unusual, the system warns the guard so the plate
    # can be verified before continuing.

    if not driver_id and reservation_id:
        # If a driver ID was not supplied but a reservation ID was supplied,
        # use the reservation to identify the driver associated with that booking.
        # This allows the newly created parking session to remain linked
        # to the correct driver's account.
        reservation = get_reservation_by_id(reservation_id)
        # Retrieve the reservation from the database using its ID.
        if reservation:
            driver_id = reservation["driver_id"]
        # Only obtain the driver ID if the reservation was successfully found.

    if slot_id:
        # Only perform slot processing when a slot ID was provided.
        # A slot is optional because the function can also support a vehicle
        # entering without an assigned slot
        slot = get_slot_by_id(slot_id)
        # Retrieve the selected slot from the database so the system
        # can confirm that the slot actually exists.
        if not slot:
            return {"success": False, "error": "Slot not found."}
        # Stop the check-in if the supplied slot ID does not correspond
        # to an existing parking slot.
        if not claim_slot(slot_id):
            return {"success": False, "error": "Slot is not available."}
        # Attempt to claim the slot.
        # claim_slot() checks that the slot is in an allowed state and
        # changes it to occupied as part of the database update.
        # Because the availability check and status change are performed
        # together, two guards cannot both successfully claim the same
        # slot when they try to check vehicles in at the same time.

    try:
        # Creating the parking session writes the vehicle's entry information
        # to the database.
        # If session creation fails after a slot was claimed, the code below
        # releases that slot so it does not incorrectly remain occupied.
        session_id = create_session(
            plate_number=plate_number,
            location_id=location_id,
            slot_id=slot_id,
            guard_id=guard_id,
            driver_id=driver_id,
            reservation_id=reservation_id,
        )
    except sqlite3.IntegrityError:
        # Handle a SQLite integrity error separately.
        # In this function, this is used for the situation where the database
        # rejects the session because the vehicle is already checked in.
        if slot_id:
            update_slot_status(slot_id, STATUS_AVAILABLE)
        # If a slot had already been claimed before the database error,
        # release it because the new parking session was not created.
        return {
            "success": False,
            "error": f"{plate_number} is already checked in. Check it out first.",
        }
    # Return a clear failure response telling the caller that the
    # vehicle already has an active check-in.
    except Exception as e:
        # Handle any other unexpected error that occurs while creating
        # the parking session.
        if slot_id:
            update_slot_status(slot_id, STATUS_AVAILABLE)
        # If a slot was claimed before the error occurred, release it.
        # This prevents a failed check-in from leaving the slot marked
        # as occupied.
        return {"success": False, "error": f"Check-in failed: {str(e)}"}
        # Return the error as part of the function's failure response.
        # str(e) converts the exception object into readable text.

    if reservation_id:
        update_reservation_status(reservation_id, "active")
    # If this vehicle entered using a reservation, change the reservation
    # status to "active" because the reserved parking session has now begun.

    log_action(
        # Record the check-in in the audit log.
        user_id=guard_id or "system",
        # Use the guard's ID when available.
        # If no guard ID was supplied, record the actor as "system".
        role="guard",
        # Identify the role associated with this operation.
        action="check_in",
        details={
            # Store important information about this check-in.
            "plate": plate_number,
            # Record the normalized vehicle plate.
            "slot_id": str(slot_id) if slot_id else None,
            # Convert the slot ID to text when a slot exists.
            # If there was no slot, store None.
            "reservation_id": str(reservation_id) if reservation_id else None,
            # Convert the reservation ID to text when a reservation exists.
            # If there was no reservation, store None.
        },
        location_id=location_id,
    )

    return {
        "success": True,
        "session_id": str(session_id),
        "warning": warning,
    }


def check_out_vehicle(session_id, payment_method, guard_id=None, override_fee=None, force_confirm=False):
    """
    Record a vehicle leaving and complete the session.
    Calculates the fee, frees the slot, and marks the session as paid.

    Args:
        session_id:     parking session id
        payment_method: 'cash' or 'mpesa'
        guard_id:       guard processing the checkout
        override_fee:   optional amount (KES) the guard manually entered,
                         used instead of the auto-calculated fee. The
                         calculated fee is still logged alongside it so
                         there is a record of any adjustment.
        force_confirm:  guard/admin explicitly confirming an M-Pesa payment
                         was received, bypassing the wait for Safaricom's
                         callback (e.g. callback URL unreachable, sandbox
                         testing). Only affects the mpesa-pending branch
                         below; has no effect on cash. Always recorded in
                         the audit log as a manual override.

    Returns:
        {"success": True, "fee": amount, "plate": plate_number}
        {"success": False, "error": "message"}
    """
    # Retrieve the parking session from the database using its session ID.
    session = get_session_by_id(session_id)
    if not session:
        return {"success": False, "error": "Session not found."}
    # If no session is found, checkout cannot continue because the
    # system has no parking record to complete.
    if session["status"] == "completed":
        return {"success": False, "error": "Session already completed."}
    # Prevent a parking session that has already been completed
    # from being checked out again.

    location = get_location_by_id(session["location_id"])
    # Retrieve the parking location associated with the session.


    mpesa_manually_confirmed = False
    # Start by assuming that no manual M-Pesa confirmation has occurred.
    # This value will only become True if force_confirm is used
    # in the M-Pesa confirmation section below.
    if (
        # Apply the normal M-Pesa confirmation requirement only when:
            # 1. The selected payment method is M-Pesa.
            # 2. The application configuration requires payment confirmation.
            # 3. The parking location has real M-Pesa/Daraja credentials.
            # These conditions distinguish a real M-Pesa payment process
            # from a simulated payment process.
        payment_method == "mpesa"
        and Config.MPESA_REQUIRE_PAYMENT_CONFIRMATION
        and mpesa_credentials_configured(location)
    ):
        if not force_confirm:
            # If force_confirm is not enabled, the guard cannot complete
            # the M-Pesa checkout manually at this point.
            # The function returns a "pending" response and waits for the
            # Safaricom callback to confirm that the payment was actually # processed.
            return {
                "success": False,
                "pending": True,
                "error": (
                    "Waiting for M-Pesa confirmation from Safaricom. "
                    "This page will move to the receipt automatically once "
                    "the customer completes payment on their phone."
                ),
            }
        mpesa_manually_confirmed = True
        # force_confirm means the guard/admin has explicitly chosen
        # to manually confirm the M-Pesa payment instead of waiting
        # for the normal Safaricom callback
        # force_confirm means the guard/admin has explicitly chosen
        # to manually confirm the M-Pesa payment instead of waiting
        # for the normal Safaricom callback

    hourly_rate = location["hourly_rate"] if location else 30
    # Obtain the hourly parking rate configured for this location.
    # If the location record is unavailable, use KES 30 as the
    # fallback rate defined by this code.

    if session["reservation_id"]:
        # Check whether the parking session is linked to a reservation.
        reservation = get_reservation_by_id(session["reservation_id"])
        # Retrieve the reservation associated with this parking session.
        reserved_from = (
            datetime.fromisoformat(reservation["reserved_from"])
            if reservation and reservation["reserved_from"] else None
        )
        entry_time = datetime.fromisoformat(session["entry_time"])
        # Convert the vehicle's recorded entry time from stored text
        # into a Python datetime object.
        fee_info = calculate_reservation_fee(
            reserved_from=reserved_from,
            actual_entry=entry_time,
            actual_exit=datetime.now(timezone.utc),
            hourly_rate_kes=hourly_rate,
        )
    else:
        entry_time = datetime.fromisoformat(session["entry_time"])
        # First convert the stored entry time into a datetime object.
        fee_info = calculate_current_fee(entry_time, hourly_rate)
        # Calculate the current parking fee using the vehicle's entry time
        # and the hourly rate configured for the parking location.

    calculated_fee_kes = fee_info["fee_kes"]
    # Extract the fee produced by the automatic fee calculator.

    # A guard-entered amount (e.g. a discount, flat fee, or manager
    # override) takes precedence over the auto-calculated fee. Reject
    # anything nonsensical rather than silently falling back.
    if override_fee is not None:
        try:
            override_fee = round(float(override_fee), 2)
        # Try to convert the manually entered amount into a number
        # and round it to two decimal places.
        except (TypeError, ValueError):
            return {"success": False, "error": "Invalid amount entered."}
        # If the supplied value cannot be converted into a valid number,
        # stop the checkout and return an error.
        if override_fee < 0:
            return {"success": False, "error": "Amount cannot be negative."}
        # Prevent the system from accepting a negative parking fee.
        fee_kes = override_fee
        # The validated manual amount becomes the final fee recorded
        # for the parking session.
    else:
        # No manual override was supplied, so use the fee calculated
        # automatically by the parking fee engine.
        fee_kes = calculated_fee_kes

    completed = complete_session(
        session_id=session_id,
        fee_kes=fee_kes,
        payment_method=payment_method,
    )
    if not completed:
        # Someone else (another guard tab, or an M-Pesa callback) completed
        # this session between our status check above and this UPDATE.
        return {"success": False, "error": "Session already completed."}

    if session["slot_id"]:
        update_slot_status(session["slot_id"], STATUS_AVAILABLE)
    # Only after the session has been successfully completed,
    # release the vehicle's assigned parking slot.
    # Changing the slot to AVAILABLE allows it to be assigned
    # to another vehicle.

    if session["reservation_id"]:
        update_reservation_status(session["reservation_id"], STATUS_COMPLETED)
    # If the parking session was created from a reservation,
    # mark that reservation as completed because its parking
    # session has now finished.

    # payment_method can only be "mpesa" here if confirmation was turned
    # off or manually overridden, so mark clearly whether Safaricom itself
    # verified it.
    log_action(
        user_id=guard_id or "system",
        # Record the guard who performed the checkout.
        # If guard_id is not available, record the actor as "system".
        role="guard",
        action="check_out",
        details={
            # Store the important details of the checkout.
            "session_id": str(session_id),
            # Store the ID of the parking session that was completed.
            "plate": session["plate_number"],
            "fee_kes": fee_kes,
            "calculated_fee_kes": calculated_fee_kes,
            "fee_overridden": fee_kes != calculated_fee_kes,
            "payment_method": payment_method,
            "mpesa_confirmed_by_safaricom": False if payment_method == "mpesa" else None,
            # This checkout path does not receive Safaricom's callback
            # as its confirmation source.
            # For M-Pesa, False indicates that this particular completion
            # was not marked as Safaricom-confirmed by this function
            # For non-M-Pesa payments, None is used because Safaricom
            # confirmation does not apply.
            "mpesa_manually_confirmed": mpesa_manually_confirmed,
            # Record whether force_confirm was used to manually confirm
            # an M-Pesa payment.
        },
        location_id=session["location_id"],
        # Record the parking location where the checkout occurred.
    )

    return {
        "success": True,
        "fee": fee_kes,
        "plate": session["plate_number"],
    }
