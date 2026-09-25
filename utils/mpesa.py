"""
Shared M-Pesa credential helpers.

Kept separate from controllers/api.py so services/parking.py
can check "does this location actually have real Daraja credentials?" without
importing a controller module (and without duplicating the field list).
"""
from __future__ import annotations


def get_mpesa_credentials(location) -> dict:
    # Collect the M-Pesa/Daraja credentials configured for this
    # parking location and return them in a consistent dictionary format.
    return {
        # Retrieve each required credential, using an empty string
        # when the location does not have a value configured.
        "consumer_key": location["mpesa_consumer_key"] or "",
        "consumer_secret": location["mpesa_consumer_secret"] or "",
        "shortcode": location["mpesa_shortcode"] or "",
        "passkey": location["mpesa_passkey"] or "",
        # "paybill" -> CustomerPayBillOnline, "till" -> CustomerBuyGoodsOnline.
        "account_type": (location["mpesa_account_type"] or "paybill").lower(),
    }


def mpesa_credentials_configured(location) -> bool:
    """
    True only if this location has real Daraja API credentials on file.

    When this is False, any "push" sent from the checkout page is a
    simulated one (see api.mpesa_push) — there is no real STK
    prompt on the customer's phone and therefore no Safaricom callback
    will ever arrive to confirm it.
    """
    # If no location data is available, real M-Pesa credentials
    # cannot be checked, so report that they are not configured.
    if not location:
        return False
    # Retrieve the M-Pesa credentials associated with this location.
    creds = get_mpesa_credentials(location)
    # Confirm that all four credentials required for the real
    # Daraja integration contain usable values.
    return all([
        creds["consumer_key"], creds["consumer_secret"],
        creds["shortcode"], creds["passkey"],
    ])
